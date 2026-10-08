#!/usr/bin/env python3
"""Pull mail the VPS queued while hyperion was off, and deliver it locally.

notify.py on the VPS writes undeliverable messages to ~/mail-spool as .eml
files. This runs on hyperion (at login and every few minutes after), fetches
them over ssh, hands each to hyperion's own postfix on localhost, and only
then deletes it from the VPS. Delivery goes through the normal MTA path, so
~/.forward runs (pipe_to_helm.py, slskd-pipe.sh) and the message lands in
~/Maildir exactly like mail sent while hyperion was up.

At-least-once: if this dies between delivering and deleting, that one message
arrives twice. A lock keeps two runs from overlapping.

Config (env): MAIL_SPOOL_HOST (ssh alias, default vps), MAIL_SPOOL_DIR
(default mail-spool, relative to the remote home), MAIL_SPOOL_SMTP (default
127.0.0.1:25).
"""
import email
import email.policy
import email.utils
import fcntl
import os
import smtplib
import subprocess
import sys

HOST = os.environ.get("MAIL_SPOOL_HOST", "vps")
RDIR = os.environ.get("MAIL_SPOOL_DIR", "mail-spool")
SMTP_HOST, _, SMTP_PORT = os.environ.get("MAIL_SPOOL_SMTP", "127.0.0.1:25").partition(":")
SSH = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", HOST]
LOCK = os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "mail-spool-pull.lock")


def remote(cmd, data=None, timeout=60):
    return subprocess.run(SSH + [cmd], input=data, capture_output=True, timeout=timeout)


def main():
    lock = open(LOCK, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return 0  # another run is in progress

    r = remote(f"ls -1 {RDIR} 2>/dev/null | grep '\\.eml$' | sort")
    if r.returncode not in (0, 1):  # 1 = empty spool (grep found nothing)
        print(f"mail-spool-pull: cannot reach {HOST}: {r.stderr.decode().strip()[:200]}", file=sys.stderr)
        return 1
    names = [n for n in r.stdout.decode().split() if n]
    delivered = failed = 0
    for name in names:
        if "/" in name or name.startswith("."):
            continue
        got = remote(f"cat {RDIR}/{name}")
        if got.returncode != 0 or not got.stdout:
            print(f"mail-spool-pull: could not read {name}", file=sys.stderr)
            failed += 1
            continue
        msg = email.message_from_bytes(got.stdout, policy=email.policy.default)
        rcpts = [a for _, a in email.utils.getaddresses(msg.get_all("To", []) + msg.get_all("Cc", [])) if a]
        sender = email.utils.parseaddr(msg.get("From", ""))[1] or "helm@vps"
        try:
            with smtplib.SMTP(SMTP_HOST, int(SMTP_PORT or 25), timeout=30) as s:
                s.sendmail(sender, rcpts or ["isaboo@hyperion"], got.stdout)
        except (smtplib.SMTPException, OSError) as e:
            print(f"mail-spool-pull: local delivery of {name} failed: {e}", file=sys.stderr)
            failed += 1
            continue
        # Delivered; only now remove it from the VPS.
        if remote(f"rm -f {RDIR}/{name}").returncode != 0:
            print(f"mail-spool-pull: delivered {name} but could not delete it (may repeat)", file=sys.stderr)
        delivered += 1
    if names:
        print(f"mail-spool-pull: {delivered} delivered, {failed} failed, {len(names)} queued")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
