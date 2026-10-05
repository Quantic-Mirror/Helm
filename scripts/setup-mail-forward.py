#!/usr/bin/env python3
"""Set up mail->Helm reporting on hyperion.

Creates ~/.forward so postfix pipes each delivered message into
pipe_to_helm.py, which POSTs the headers to the Helm API on the VPS.

Two things this must get right:

  1. A ~/.forward that consists ONLY of a pipe DOES replace mailbox delivery.
     That was verified, not assumed: with only `|/path/pipe_to_helm.py`, mail
     went to the pipe and never reached the Maildir, so mutt showed nothing and
     the queue stayed empty. To keep BOTH, the forward must list the pipe and
     then the mailbox explicitly:

         |/path/pipe_to_helm.py
         /home/isaboo/Maildir/

     The bare path is delivered by postfix's local(8) agent into the Maildir.
     The pipe is listed first so the Helm report is attempted before the
     mailbox write.

  2. The Helm bearer token has to exist on hyperion; the pipe authenticates to
     the VPS with it. We copy it out of the running container over the tailnet
     rather than asking for it, and it never touches this script's output.

Prints what it did; safe to re-run.
"""
import os
import stat
import subprocess
import sys

HOME = os.path.expanduser("~")
FORWARD = os.path.join(HOME, ".forward")
PIPE = os.path.join(HOME, "repos/Helm/scripts/pipe_to_helm.py")
MAILBOX = os.path.join(HOME, "Maildir")
TOKEN_DIR = os.path.join(HOME, ".config/helm")
TOKEN = os.path.join(TOKEN_DIR, "token")

# Pipe first (report to Helm), then the mailbox (so mutt still gets a copy).
FORWARD_CONTENT = f"""|{PIPE}
{MAILBOX}/
"""


def get_token():
    """Pull the Helm token off the VPS out of the running container."""
    r = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "vps",
         "docker exec helm cat /app/state/helm_token.txt"],
        capture_output=True, text=True, timeout=30)
    tok = r.stdout.strip()
    if r.returncode != 0 or not tok:
        print("could not read the Helm token from the VPS container:", r.stderr.strip()[:200])
        return None
    return tok


def main():
    if not os.path.exists(PIPE):
        print(f"pipe script missing: {PIPE}")
        print("Is the Helm repo checked out at ~/repos/Helm on hyperion?")
        return 1

    os.makedirs(TOKEN_DIR, exist_ok=True)
    os.chmod(TOKEN_DIR, 0o700)
    tok = get_token()
    if not tok:
        print("no token; ~/.forward not written")
        return 1
    with open(TOKEN, "w") as f:
        f.write(tok + "\n")
    os.chmod(TOKEN, 0o600)
    print(f"wrote {TOKEN} (mode 600)")

    # Keep exactly one pipe line plus the mailbox line. Re-running replaces
    # rather than appends, so this cannot accumulate duplicates (which would
    # report every message to Helm N times).
    if os.path.exists(FORWARD):
        existing = open(FORWARD).read()
        kept = [ln for ln in existing.splitlines()
                if ln.strip() and ln.strip() != f"|{PIPE}"
                and ln.strip() != f"{MAILBOX}/"]
        content = "\n".join([f"|{PIPE}", f"{MAILBOX}/"] + kept) + "\n"
        if existing == content:
            print(f"{FORWARD} already correct")
        else:
            with open(FORWARD, "w") as f:
                f.write(content)
            print(f"updated {FORWARD}")
    else:
        with open(FORWARD, "w") as f:
            f.write(FORWARD_CONTENT)
        print(f"wrote {FORWARD}")

    # postfix requires .forward to be readable only by its owner (it refuses
    # group/other-readable forwards as a hijack risk).
    os.chmod(FORWARD, stat.S_IRUSR | stat.S_IWUSR)

    # The script must be executable for postfix to run it.
    os.chmod(PIPE, 0o755)

    print("\n--- verify ---")
    print(f"forward: {open(FORWARD).read().strip()}")
    print(f"pipe exec: {os.access(PIPE, os.X_OK)}")
    print("\nNow send a test mail:")
    print(f"  printf 'Subject: pipe test\\n\\nbody\\n' | sendmail isaboo@hyperion")
    return 0


if __name__ == "__main__":
    sys.exit(main())