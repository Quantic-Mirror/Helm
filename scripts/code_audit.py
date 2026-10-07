#!/usr/bin/env python3
"""Weekly Helm code audit: deterministic pattern/convention checks over the
trailing 7 days of commits, mailed as a punch list.

Report-only, same posture as host_maintenance.py's pacman-upgrade stance:
nothing here is auto-fixed. Every finding pairs a location with the exact
fix, written so it can be handed to a future session (human or agent) and
acted on directly without re-diagnosing.

Runs on hyperion against the local clone (needs git history), unlike
host_maintenance.py/chores_digest.py which are copied standalone to hosts
without this repo -- this one imports nothing special, just shells to git.

Checks (grep/structure-based, not semantic judgment -- see the chat history
for why: catches real incidents like a `git add -A` sweeping an unrelated
untracked directory into a commit, without needing an LLM call):

  1. Hardcoded secrets in added lines
  2. .innerHTML assignment without escHtml() (this codebase's established
     XSS-safe-interpolation convention)
  3. eval/exec, shell=True, pickle.loads
  4. Commits touching 3+ unrelated top-level paths (the client-fixes/ class
     of mistake -- verify-not-auto-flag, since legitimately broad commits
     exist)
  5. The /api/* blanket auth-gate lines still present verbatim in
     helm_server.py (regression check, not a per-route check -- new routes
     under /api/ are already covered by the blanket gate; the real risk is
     someone weakening the gate itself)
  6. New non-stdlib imports added to helm_server.py (stdlib-only by
     documented convention)
  7. New `open(path, 'w')` in added lines without an accompanying .tmp +
     os.replace() in the same diff hunk (atomic-write convention)
  8. New top-level const/let in index.html's script outside the documented
     early-declarations block, in a commit that also touches switchTab() --
     advisory TDZ-risk flag, not a hard fail (see CLAUDE.md's TDZ note)

Config (env):
    DIGEST_TO     recipient (default: isaboo@hyperion)
    DIGEST_TZ     zone for the date header (default America/New_York)
    AUDIT_SINCE   git --since window (default "7 days ago")
"""
import os
import re
import subprocess
import sys
from datetime import datetime

REPO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
NOTIFY = os.path.join(REPO, "scripts", "notify.py")
RECIPIENT = os.environ.get("DIGEST_TO", "isaboo@hyperion")
TZ_NAME = os.environ.get("DIGEST_TZ", "America/New_York")
SINCE = os.environ.get("AUDIT_SINCE", "7 days ago")

HELM_SERVER = os.path.join(REPO, "helm_server.py")
INDEX_HTML = os.path.join(REPO, "index.html")

AUTH_GATE_LINES = [
    # do_GET's blanket /api/* gate (besides health and the backups
    # either/or). If this exact substring disappears, the gate was edited.
    'elif parsed.path.startswith("/api/") and not self._require_auth():',
    # do_POST's blanket gate.
    'if parsed.path.startswith("/api/") and parsed.path != "/api/backup-events":',
]

SECRET_RE = re.compile(
    r"(api[_-]?key|secret|password|token|passwd)\s*[=:]\s*['\"][^'\"]{6,}['\"]",
    re.IGNORECASE,
)
STDLIB = set(sys.stdlib_module_names) if hasattr(sys, "stdlib_module_names") else set()
# Already-vetted exception (see CLAUDE.md's "no pip dependencies" note --
# zxcvbn lives in vault_api.py, guarded by try/except, not helm_server.py).
STDLIB_EXTRA_OK = {"zxcvbn"}


def local_now():
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo(TZ_NAME))
    except Exception:  # noqa: BLE001 -- missing tzdata shouldn't kill the mail
        return datetime.now()


def git(args, timeout=60):
    try:
        r = subprocess.run(["git", "-C", REPO] + args, capture_output=True,
                            text=True, timeout=timeout)
        return r.returncode == 0, r.stdout
    except (OSError, subprocess.SubprocessError) as e:
        return False, str(e)


def commits_in_window():
    ok, out = git(["log", f"--since={SINCE}", "--pretty=format:%H %s"])
    if not ok:
        return []
    return [ln.split(" ", 1) for ln in out.splitlines() if ln.strip()]


def added_lines_by_file(since=SINCE):
    """Yields (filepath, lineno_in_new_file, line_text) for every added
    line across the window, file by file (unified diff, context=0)."""
    ok, out = git(["log", f"--since={since}", "-p", "-U0", "--no-color"])
    if not ok or not out:
        return
    current_file = None
    new_lineno = None
    for raw in out.splitlines():
        if raw.startswith("+++ b/"):
            current_file = raw[6:]
            continue
        m = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)", raw)
        if m:
            new_lineno = int(m.group(1))
            continue
        if raw.startswith("+++") or raw.startswith("---"):
            continue
        if raw.startswith("+"):
            if current_file and new_lineno is not None:
                yield current_file, new_lineno, raw[1:]
            if new_lineno is not None:
                new_lineno += 1
        elif raw.startswith("-"):
            pass  # removed line, doesn't advance new_lineno
        elif not raw.startswith("@@") and new_lineno is not None and current_file:
            new_lineno += 1  # context line, when -U is not 0 (kept for safety)


def check_secrets(findings):
    for path, lineno, line in added_lines_by_file():
        if not SECRET_RE.search(line):
            continue
        # Exclude shell/compose variable-reference syntax (${VAR:?msg} /
        # ${VAR:-default}) -- that's a reference to an externally-supplied
        # value, not a literal secret, and commonly reads as "secret = ..."
        # to the regex (e.g. VIKUNJA_SECRET: "${VIKUNJA_SECRET:?set a secret
        # in .env}").
        if re.search(r"\$\{[A-Za-z0-9_]+:[?-]", line):
            continue
        findings.append({
            "category": "Hardcoded secret",
            "where": f"{path}:{lineno}",
            "detail": line.strip()[:120],
            "fix": "Move the value to a *_token.txt in STATE_DIR or .env "
                   "(gitignored), reference it via env var/file read instead "
                   "of a literal. If already pushed, rotate the secret.",
        })


def check_innerhtml(findings):
    for path, lineno, line in added_lines_by_file():
        if not path.endswith(".html"):
            continue
        if ".innerHTML" not in line or "=" not in line:
            continue
        # Only flag genuine interpolation risk: a template literal with ${...}
        # and no escHtml() call on the line. Plain string/backtick literals
        # with no interpolation (".innerHTML = ''", a static <div> literal)
        # carry no injection risk and were the bulk of the false positives
        # the unrefined version of this check produced.
        if "${" not in line or "escHtml(" in line:
            continue
        findings.append({
            "category": "Possible XSS (.innerHTML template interpolation without escHtml)",
            "where": f"{path}:{lineno}",
            "detail": line.strip()[:120],
            "fix": "Wrap any untrusted string in escHtml() before interpolating "
                   "into innerHTML, or use .textContent if no markup is needed. "
                   "Note: multi-line template literals only get caught on the "
                   "line containing ${...} -- check nearby lines in the same "
                   "template by hand too.",
        })


def check_dangerous_calls(findings):
    patterns = [
        (re.compile(r"\beval\(|\bexec\("), "eval()/exec()"),
        (re.compile(r"shell\s*=\s*True"), "subprocess shell=True"),
        (re.compile(r"pickle\.loads?\("), "pickle.loads()"),
        (re.compile(r"os\.system\("), "os.system()"),
    ]
    for path, lineno, line in added_lines_by_file():
        if not path.endswith(".py"):
            continue
        for pattern, label in patterns:
            if pattern.search(line):
                findings.append({
                    "category": f"Dangerous call: {label}",
                    "where": f"{path}:{lineno}",
                    "detail": line.strip()[:120],
                    "fix": "Use subprocess.run([...]) with a list (no shell=True), "
                           "json for serialization instead of pickle, and avoid "
                           "eval/exec on anything not fully trusted/static.",
                })


def check_scattered_commits(findings):
    for sha, subject in commits_in_window():
        ok, out = git(["show", "--name-only", "--pretty=format:", sha])
        if not ok:
            continue
        files = [f for f in out.splitlines() if f.strip()]
        top_dirs = {f.split("/")[0] if "/" in f else "." for f in files}
        if len(top_dirs) >= 3:
            findings.append({
                "category": "Commit touches 3+ unrelated paths",
                "where": f"{sha[:8]} \"{subject}\"",
                "detail": ", ".join(sorted(top_dirs)),
                "fix": "Verify every file in this commit actually belongs to it "
                       "(git show --stat <sha>) -- this is the 'git add -A swept "
                       "in an untracked directory' mistake pattern. If something "
                       "doesn't belong, git rm --cached it in a follow-up commit.",
            })


def check_auth_gate(findings):
    try:
        with open(HELM_SERVER) as f:
            content = f.read()
    except OSError:
        return
    for gate_line in AUTH_GATE_LINES:
        if gate_line not in content:
            findings.append({
                "category": "CRITICAL: /api/* auth gate missing",
                "where": "helm_server.py",
                "detail": f"expected line not found: {gate_line}",
                "fix": "This is the blanket auth check that protects every /api/* "
                       "route except /api/health and /api/backup-events. If it was "
                       "intentionally refactored, verify the replacement still "
                       "rejects unauthenticated requests on GET/POST/PUT before "
                       "any route dispatch. If not intentional, restore it.",
            })


def check_new_imports(findings):
    import_re = re.compile(r"^\s*(?:import|from)\s+([A-Za-z0-9_]+)")
    scripts_dir = os.path.join(REPO, "scripts")
    for path, lineno, line in added_lines_by_file():
        if path != "helm_server.py":
            continue
        m = import_re.match(line)
        if not m:
            continue
        module = m.group(1)
        if module in STDLIB or module in STDLIB_EXTRA_OK or not STDLIB:
            continue
        # Local first-party modules (scripts/slskd_live.py etc, imported via
        # sys.path.insert) aren't a new dependency -- skip them.
        if os.path.exists(os.path.join(scripts_dir, f"{module}.py")):
            continue
        findings.append({
            "category": "New non-stdlib import in helm_server.py",
            "where": f"{path}:{lineno}",
            "detail": line.strip()[:120],
            "fix": "helm_server.py is stdlib-only by convention (see CLAUDE.md's "
                   "'Prefer lightweight/stdlib' section). Either implement without "
                   "the dependency, or if it's genuinely needed, guard the import "
                   "with try/except ImportError so its absence degrades one "
                   "feature instead of breaking the server (same pattern as "
                   "zxcvbn in vault_api.py).",
        })


def check_atomic_writes(findings):
    open_re = re.compile(r"open\([^)]*['\"]w['\"]")
    ok, out = git(["log", f"--since={SINCE}", "-p", "-U3", "--no-color", "--", "*.py"])
    if not ok or not out:
        return
    current_file = None
    hunk_lines = []

    def flush():
        if current_file and hunk_lines:
            joined = "\n".join(hunk_lines)
            for ln in hunk_lines:
                if ln.startswith("+") and open_re.search(ln) and "tmp" not in joined.lower():
                    findings.append({
                        "category": "File write without atomic tmp+replace",
                        "where": current_file,
                        "detail": ln.strip()[:120],
                        "fix": "Write to <path>.tmp first, then os.replace(tmp, path) "
                               "-- see _write_state_to_disk/_maybe_write_backup in "
                               "helm_server.py for the established pattern. Avoids a "
                               "torn/corrupt file if the process dies mid-write.",
                    })

    for raw in out.splitlines():
        if raw.startswith("+++ b/"):
            flush()
            current_file = raw[6:]
            hunk_lines = []
            continue
        if raw.startswith("@@"):
            flush()
            hunk_lines = []
            continue
        hunk_lines.append(raw)
    flush()


def check_tdz_risk(findings):
    decl_re = re.compile(r"^\+(?:const|let)\s+([A-Za-z_][A-Za-z0-9_]*)")
    for sha, subject in commits_in_window():
        ok, out = git(["show", sha, "--", "index.html"])
        if not ok or "switchTab(" not in out:
            continue
        # Fetch the pre-commit version once per commit to distinguish a truly
        # NEW declaration from a diff artifact of editing one element inside
        # an existing array/object (unified diff shows the whole line as
        # removed+re-added when only one element changes -- e.g. VALID_TABS
        # gaining a new tab name reads as "+const VALID_TABS = [...]" every
        # single time, even though the declaration itself isn't new).
        ok_parent, parent_content = git(["show", f"{sha}^:index.html"])
        for ln in out.splitlines():
            m = decl_re.match(ln.strip())
            if not m:
                continue
            name = m.group(1)
            if ok_parent and re.search(rf"\b(?:const|let)\s+{re.escape(name)}\b", parent_content):
                continue  # already existed -- this is a modification, not new
            findings.append({
                "category": "Possible TDZ risk (advisory)",
                "where": f"{sha[:8]} index.html",
                "detail": ln.strip()[:120],
                "fix": "This commit adds a genuinely NEW top-level const/let AND "
                       "touches switchTab() -- if the new declaration is "
                       "referenced by a render function reachable from the "
                       "startup hash check, move the declaration into the early "
                       "declarations block (search TAGS/COL_COLORS/let state) "
                       "per CLAUDE.md's TDZ bug pattern note. Verify, don't "
                       "assume -- this check can't tell if it's actually reached "
                       "early.",
            })


def main():
    now = local_now()
    findings = []

    check_secrets(findings)
    check_innerhtml(findings)
    check_dangerous_calls(findings)
    check_scattered_commits(findings)
    check_auth_gate(findings)
    check_new_imports(findings)
    check_atomic_writes(findings)
    check_tdz_risk(findings)

    lines = [f"Helm code audit — trailing {SINCE} — {now.strftime('%A, %B %d')}", ""]
    if not findings:
        lines.append("No findings this week.")
    else:
        by_cat = {}
        for f in findings:
            by_cat.setdefault(f["category"], []).append(f)
        for cat, items in by_cat.items():
            lines.append(f"{cat} ({len(items)}):")
            for it in items:
                lines.append(f"  - {it['where']}")
                lines.append(f"    found: {it['detail']}")
                lines.append(f"    fix:   {it['fix']}")
            lines.append("")

    body = "\n".join(lines) + "\n"
    subject = f"Helm code audit — {now.strftime('%a %b %d')} — {len(findings)} finding(s)"
    r = subprocess.run(
        [sys.executable, NOTIFY, subject, body, "--from", "helm-audit@hyperion", "--to", RECIPIENT],
        capture_output=True, text=True, timeout=60,
    )
    if r.returncode != 0:
        print(f"code-audit: send failed: {r.stderr.strip()[:300]}", file=sys.stderr)
        return 1
    print(f"sent code audit: {len(findings)} finding(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
