#!/usr/bin/env python3
"""Pull finished slskd downloads from the VPS into the music Inbox on hyperion.

Runs on hyperion (it writes to /mnt/SharedStuff, which only exists there).
slskd writes a download into `downloads/` only once it is complete; partial
files live in `incomplete/`, so nothing half-finished is ever copied.

Copies with rsync over one shared ssh connection. Nothing is deleted on either
side; the VPS copy stays as the record of what slskd fetched.
"""
import json
import os
import ssl
import subprocess
import sys
import urllib.request

REMOTE = os.environ.get("SLSKD_VPS_HOST", "vps")
REMOTE_DIR = os.environ.get(
    "SLSKD_REMOTE_DOWNLOADS", "/home/isaboo/soulseek/data/downloads")
INBOX = os.environ.get("SLSKD_INBOX", "/mnt/SharedStuff/Music/Inbox")
SSH_TIMEOUT = int(os.environ.get("SLSKD_SYNC_TIMEOUT", "60"))
HELM_URL = os.environ.get("HELM_URL", "https://100.77.126.57:8443").rstrip("/")
TOKEN_FILE = os.environ.get("HELM_TOKEN_FILE", "/home/isaboo/.config/helm/token")

# One multiplexed ssh connection serves the listing and every file copy. Without
# it each file paid a fresh handshake over the tailnet, which dominated the time.
SSH_OPTS = ["-o", "BatchMode=yes",
            "-o", "ControlMaster=auto",
            "-o", "ControlPersist=300",
            "-o", "ControlPath=~/.ssh/slskd-sync-%C"]


def remote_files():
    """Yield (relative_path, size) for every regular file under REMOTE_DIR.

    NUL-separated so names with spaces, tabs, or newlines survive intact.
    """
    cmd = ["ssh", *SSH_OPTS, REMOTE,
           f"cd {shell_quote(REMOTE_DIR)} && find . -type f -printf '%s\\0%P\\0'"]
    r = subprocess.run(cmd, capture_output=True, timeout=SSH_TIMEOUT, check=True)
    fields = r.stdout.split(b"\0")
    # Output ends with a NUL, so the final split element is empty.
    for i in range(0, len(fields) - 1, 2):
        yield fields[i + 1].decode("utf-8", "surrogateescape"), int(fields[i])


def _report_key(rel):
    """The key Helm matches on: the folder and name, as in the VPS downloads/ tree.

    This is the same reduction slskd_live._rel_path makes from a slskd filename.
    """
    return rel.replace("\\", "/")


def shell_quote(s):
    return "'" + s.replace("'", "'\\''") + "'"


def report_synced(rels):
    """Tell Helm which VPS downloads are now in the Inbox, so the Soulseek panel
    can show them as synced. Best effort: a failed report is logged, and the
    next run sends the whole set again."""
    try:
        with open(TOKEN_FILE) as fh:
            token = fh.read().strip()
    except OSError as e:
        print(f"sync: no Helm token, not reporting: {e}", file=sys.stderr)
        return
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # Helm's cert is self-signed on the tailnet
    # Chunked so a large library does not make one huge request.
    for i in range(0, len(rels), 2000):
        body = json.dumps({"paths": rels[i:i + 2000]}).encode()
        req = urllib.request.Request(
            HELM_URL + "/api/slskd/synced", data=body, method="POST",
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer " + token})
        try:
            with urllib.request.urlopen(req, timeout=30, context=ctx):
                pass
        except Exception as e:  # noqa: BLE001
            print(f"sync: could not report synced files: {e}", file=sys.stderr)
            return


def copy_new():
    """Move the VPS downloads/ tree into the Inbox with rsync.

    The VPS copy is a holding area, so rsync removes each source file once it
    has been copied. rsync checks each transferred file's checksum before it
    removes the source, and writes to a temporary name until complete. Nothing
    is removed on the Inbox side. Files already in the Inbox are not copied again,
    so a file deleted from the Inbox stays deleted.
    """
    rsh = "ssh " + " ".join(SSH_OPTS)
    cmd = ["rsync", "-a", "--remove-source-files", "--timeout=120",
           "--out-format=sync: moved %n",
           "-e", rsh, f"{REMOTE}:{REMOTE_DIR}/", INBOX.rstrip("/") + "/"]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=SSH_TIMEOUT * 60)
    for line in r.stdout.splitlines():
        if line.strip():
            print(line)
    if r.returncode != 0:
        print(f"sync: rsync exited {r.returncode}: {r.stderr.strip()[:300]}", file=sys.stderr)
    return r.returncode


def prune_remote_dirs():
    """Remove folders left empty on the VPS once their files have moved."""
    cmd = ["ssh", *SSH_OPTS, REMOTE,
           f"find {shell_quote(REMOTE_DIR)} -mindepth 1 -type d -empty -delete"]
    subprocess.run(cmd, capture_output=True, timeout=SSH_TIMEOUT)


def main():
    os.makedirs(INBOX, exist_ok=True)
    # List before copying: rsync removes the VPS files, so they can't be listed after.
    try:
        files = list(remote_files())
    except (subprocess.SubprocessError, OSError) as e:
        print(f"sync: cannot list {REMOTE}:{REMOTE_DIR}: {e}", file=sys.stderr)
        return 1
    rc = copy_new()

    # Report only what is confirmed in the Inbox with the expected size.
    present = []
    for rel, size in files:
        dest = os.path.join(INBOX, rel)
        if os.path.isfile(dest) and os.path.getsize(dest) == size:
            present.append(_report_key(rel))
    if present:
        report_synced(present)
    prune_remote_dirs()
    print(f"sync: {len(present)} of {len(files)} file(s) confirmed in the Inbox")
    return 1 if rc else 0


if __name__ == "__main__":
    sys.exit(main())
