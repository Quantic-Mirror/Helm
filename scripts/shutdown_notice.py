#!/usr/bin/env python3
"""Mail a notice when hyperion powers off. Reboots are deliberately silent.

Called from the ExecStop of hyperion-shutdown-mail.service (a system unit). The
stop hook runs on every shutdown path, so the reboot check is what separates the
two: while a reboot is underway reboot.target is active, and during a poweroff
or halt it is not.

Limit: this only fires on an orderly shutdown. A hard power cut or a crash
never runs ExecStop, so there is no mail for those.

Not tested against a real shutdown: testing it would mean rebooting or powering
off hyperion, so verify with `systemctl poweroff` once the unit is installed.
"""
import os
import subprocess
import sys
import time

NOTIFY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "notify.py")
RECIPIENT = os.environ.get("SHUTDOWN_TO", "isaboo@hyperion")


def rebooting():
    r = subprocess.run(["systemctl", "is-active", "--quiet", "reboot.target"])
    return r.returncode == 0


def main():
    if rebooting():
        return 0
    stamp = time.strftime("%Y-%m-%d %H:%M %Z")
    r = subprocess.run(
        [sys.executable, NOTIFY, f"hyperion shut down ({stamp})",
         "hyperion is powering off. Mail to it will queue until it is back.",
         "--from", "helm@hyperion", "--to", RECIPIENT],
        capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        # Exit 0 would hide a lost notice; a non-zero exit just means the stop hook logs it.
        print(f"shutdown-notice: send failed: {r.stderr.strip()[:300]}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
