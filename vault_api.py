"""
vault_api.py — backend logic for the Helm password vault widget.
Shells out to `pass` rather than reimplementing GPG handling, so it
inherits gpg-agent's cache/timeout/lock behavior automatically.

Place this file next to helm_server.py and `import vault_api` from it.
"""

import subprocess
import os
import re
import datetime
import secrets
import string
from pathlib import Path

try:
    from zxcvbn import zxcvbn
except ImportError:
    zxcvbn = None  # health endpoint will report scoring unavailable

STORE_DIR = Path(os.environ.get("PASSWORD_STORE_DIR", os.path.expanduser("~/.password-store")))


def _run_pass(args, input_text=None):
    """Run a pass(1) command, return (stdout, stderr, returncode)."""
    result = subprocess.run(
        ["pass"] + args,
        input=input_text,
        capture_output=True,
        text=True,
    )
    return result.stdout, result.stderr, result.returncode


def list_entries():
    """Walk the store directly for .gpg files -- faster than parsing `pass ls`."""
    entries = []
    for path in STORE_DIR.rglob("*.gpg"):
        if ".git" in path.parts:
            continue
        entries.append(str(path.relative_to(STORE_DIR).with_suffix("")))
    return sorted(entries)


def parse_entry_body(body):
    """First line = secret. Remaining 'key: value' lines = metadata."""
    lines = body.splitlines()
    secret = lines[0] if lines else ""
    fields = {}
    notes_lines = []
    for line in lines[1:]:
        if ":" in line and re.match(r"^[a-zA-Z_]+:\s", line):
            key, _, value = line.partition(":")
            fields[key.strip().lower()] = value.strip()
        elif line.strip():
            notes_lines.append(line)
    fields["_extra_notes"] = "\n".join(notes_lines)
    return secret, fields


# ---- status ----

def get_status():
    """
    Reports entry count always. Lock state is best-effort: queries gpg-agent
    directly via KEYINFO rather than decrypting anything, so checking status
    never itself triggers a pinentry prompt.
    """
    check = subprocess.run(
        ["gpg-connect-agent", "--no-autostart", "KEYINFO --list", "/bye"],
        capture_output=True, text=True,
    )
    # KEYINFO lines look like: "S KEYINFO <keygrip> D - - - P - - -" where
    # index 6 (0-based, after splitting on whitespace) is the cache flag:
    # "1" = this key's passphrase is currently cached, "-" = not cached.
    # Confirmed against real gpg-connect-agent output on hyperion.
    is_unlocked = False
    for line in check.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 7 and parts[0] == "S" and parts[1] == "KEYINFO":
            if parts[6] == "1":
                is_unlocked = True
                break

    return {"locked": not is_unlocked, "entry_count": len(list_entries())}


def lock_vault():
    """Reload the agent, which flushes all cached passphrases."""
    subprocess.run(["gpg-connect-agent", "reloadagent", "/bye"], capture_output=True)
    return {"locked": True}


# ---- search (never returns secrets) ----

def search_entries(query=""):
    query = query.lower()
    results = []
    for path in list_entries():
        if query and query not in path.lower():
            continue
        results.append({"path": path})
    return results


# ---- single entry decrypt ----

def get_entry(path):
    stdout, stderr, rc = _run_pass(["show", "--", path])
    if rc != 0:
        return None, stderr
    secret, fields = parse_entry_body(stdout)
    return {
        "path": path,
        "secret": secret,
        "username": fields.get("username", fields.get("login", "")),
        "url": fields.get("url", ""),
        "tags": [t.strip() for t in fields.get("tags", "").split(",") if t.strip()],
        "expires": fields.get("expires", ""),
        "notes": fields.get("_extra_notes", ""),
    }, None


# ---- create / update entry ----

def build_entry_body(secret, username="", url="", tags="", expires="", notes=""):
    lines = [secret]
    if username:
        lines.append(f"username: {username}")
    if url:
        lines.append(f"url: {url}")
    if tags:
        lines.append(f"tags: {tags}")
    if expires:
        lines.append(f"expires: {expires}")
    if notes:
        lines.append(notes)
    return "\n".join(lines) + "\n"


def create_or_update_entry(path, secret, username="", url="", tags="", expires="", notes="", force=False):
    body = build_entry_body(secret, username, url, tags, expires, notes)
    args = ["insert", "-m"]
    if force:
        args.append("-f")
    args.append(path)
    _, stderr, rc = _run_pass(args, input_text=body)
    return rc == 0, stderr


# ---- generate (does not write to store) ----

def generate_password(length=20, symbols=True):
    alphabet = string.ascii_letters + string.digits
    if symbols:
        alphabet += "!@#$%^&*()-_=+"
    return "".join(secrets.choice(alphabet) for _ in range(int(length)))


# ---- health scan ----

def run_health_scan():
    entries = list_entries()
    weak = []
    duplicates = {}
    expired = []
    expiring_soon = []
    missing_username = []
    no_tags = []
    scoring_skipped = []

    today = datetime.date.today()

    for path in entries:
        data, err = get_entry(path)
        if data is None:
            continue

        secret = data["secret"]
        tags = data["tags"]

        if secret:
            duplicates.setdefault(secret, []).append(path)

        if not data["username"]:
            missing_username.append(path)
        if not tags:
            no_tags.append(path)

        if data["expires"]:
            try:
                exp_date = datetime.date.fromisoformat(data["expires"])
                if exp_date < today:
                    expired.append(path)
                elif (exp_date - today).days <= 30:
                    expiring_soon.append(path)
            except ValueError:
                pass

        # Never score entries tagged api-key, or anything long enough to trip
        # zxcvbn's own hard length cap -- these aren't login passwords.
        if "api-key" in tags or len(secret) > 72:
            scoring_skipped.append(path)
            continue

        if zxcvbn and secret:
            try:
                result = zxcvbn(secret)
                if result["score"] <= 2:
                    weak.append({"path": path, "score": result["score"]})
            except ValueError:
                scoring_skipped.append(path)

    reused = {secret: paths for secret, paths in duplicates.items() if len(paths) > 1}

    return {
        "entry_count": len(entries),
        "weak": weak,
        "reused_passwords": [{"paths": paths} for paths in reused.values()],
        "expired": expired,
        "expiring_soon": expiring_soon,
        "missing_username": missing_username,
        "no_tags": no_tags,
        "scoring_skipped": scoring_skipped,
    }
