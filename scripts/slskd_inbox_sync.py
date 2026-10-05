#!/usr/bin/env python3
"""Pull finished slskd downloads from the VPS into the music Inbox on hyperion.

Runs on hyperion (it writes to /mnt/SharedStuff, which only exists there).
slskd writes a download into `downloads/` only once it is complete; partial
files live in `incomplete/`, so nothing half-finished is ever copied.

rsync is not installed on the VPS, so this lists the remote tree over ssh and
copies only what is missing or the wrong size. Files are written to a `.part`
name and renamed into place, so a copy that dies mid-transfer never leaves a
truncated file under its real name. Nothing is deleted on either side; the
VPS copy stays as the record of what slskd fetched.
"""
import os
import subprocess
import sys

REMOTE = os.environ.get("SLSKD_VPS_HOST", "vps")
REMOTE_DIR = os.environ.get(
    "SLSKD_REMOTE_DOWNLOADS", "/home/isaboo/soulseek/data/downloads")
INBOX = os.environ.get("SLSKD_INBOX", "/mnt/SharedStuff/Music/Inbox")
SSH_TIMEOUT = int(os.environ.get("SLSKD_SYNC_TIMEOUT", "60"))


def remote_files():
    """Yield (relative_path, size) for every regular file under REMOTE_DIR.

    NUL-separated so names with spaces, tabs, or newlines survive intact.
    """
    cmd = ["ssh", "-o", "BatchMode=yes", REMOTE,
           f"cd {shell_quote(REMOTE_DIR)} && find . -type f -printf '%s\\0%P\\0'"]
    r = subprocess.run(cmd, capture_output=True, timeout=SSH_TIMEOUT, check=True)
    fields = r.stdout.split(b"\0")
    # Output ends with a NUL, so the final split element is empty.
    for i in range(0, len(fields) - 1, 2):
        yield fields[i + 1].decode("utf-8", "surrogateescape"), int(fields[i])


def shell_quote(s):
    return "'" + s.replace("'", "'\\''") + "'"


def fetch(rel, dest):
    """Stream one remote file to dest via a .part file, then rename it."""
    part = dest + ".part"
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    remote_path = f"{REMOTE_DIR}/{rel}"
    with open(part, "wb") as out:
        proc = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", REMOTE, "cat -- " + shell_quote(remote_path)],
            stdout=out, stderr=subprocess.PIPE, timeout=SSH_TIMEOUT * 20)
    if proc.returncode != 0:
        os.remove(part)
        raise RuntimeError(proc.stderr.decode(errors="replace").strip() or "ssh cat failed")
    os.replace(part, dest)


def main():
    os.makedirs(INBOX, exist_ok=True)
    copied = skipped = failed = 0
    try:
        files = list(remote_files())
    except (subprocess.SubprocessError, OSError) as e:
        print(f"sync: cannot list {REMOTE}:{REMOTE_DIR}: {e}", file=sys.stderr)
        return 1

    for rel, size in files:
        dest = os.path.join(INBOX, rel)
        if os.path.isfile(dest) and os.path.getsize(dest) == size:
            skipped += 1
            continue
        try:
            fetch(rel, dest)
            copied += 1
            print(f"sync: copied {rel}")
        except (RuntimeError, subprocess.SubprocessError, OSError) as e:
            failed += 1
            print(f"sync: failed {rel}: {e}", file=sys.stderr)

    print(f"sync: {copied} copied, {skipped} already present, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
