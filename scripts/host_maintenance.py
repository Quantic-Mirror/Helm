#!/usr/bin/env python3
"""Weekly host maintenance: safe cleanup + a mailed report.

Self-contained (stdlib only, no Helm repo dependency) so it can be copied
to any host as a single file — unlike chores_digest.py, this runs on
hyperion/tv/popcorn/the VPS, not just where the Helm repo is cloned.
Embeds its own tiny SMTP sender rather than importing notify.py for that
reason (see Helm's scripts/notify.py for the original, same approach).

Tier 1 (actually performed, safe/reversible or purely reclaiming disk space):
    - pacman cache trim (paccache -rk2)       [needs root — skipped + flagged if not]
    - journal vacuum (--vacuum-time=2weeks)   [needs root — skipped + flagged if not]
    - docker system prune -af                 (no root needed; isaboo is in the docker group)

Tier 2 (report only, never auto-applied):
    - orphaned package list/count
    - available update count
    - failed systemd units (system + user)
    - disk usage, flagged over 85%

Deliberately NOT done: pacman -Syu / apt upgrade, or removing orphans.
Those change what's installed and should stay a deliberate manual step.

Config (env):
    DIGEST_TO    recipient (default: isaboo@hyperion)
    DIGEST_TZ    zone for the date header (default America/New_York)
"""
import os
import shutil
import smtplib
import socket
import subprocess
import sys
from datetime import datetime
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

HYPERION_TAILNET_IP = "100.79.12.117"
SMTP_PORT = 25
RECIPIENT = os.environ.get("DIGEST_TO", "isaboo@hyperion")
TZ_NAME = os.environ.get("DIGEST_TZ", "America/New_York")


def local_now():
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo(TZ_NAME))
    except Exception:  # noqa: BLE001 -- missing tzdata shouldn't kill the mail
        return datetime.now()


def send_mail(subject, body, sender, recipient):
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = recipient
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain="helm.local")
    msg.set_content(body)
    with smtplib.SMTP(HYPERION_TAILNET_IP, SMTP_PORT, timeout=15) as s:
        s.send_message(msg)


def run(cmd, timeout=120):
    """Returns (ok, stdout+stderr stripped). Never raises."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        out = (r.stdout + r.stderr).strip()
        return r.returncode == 0, out
    except (OSError, subprocess.SubprocessError) as e:
        return False, str(e)


def have(cmd):
    return shutil.which(cmd) is not None


def section(lines, title, body_lines):
    lines.append(f"\n{title}:")
    if not body_lines:
        lines.append("  (nothing to report)")
    else:
        lines += [f"  - {b}" for b in body_lines]


def disk_usage_lines():
    ok, out = run(["df", "-h", "--output=target,fstype,pcent,avail"])
    lines = []
    if not ok:
        return ["could not read disk usage"]
    # Skip pseudo filesystems -- not real disk, just noise in a weekly report.
    skip_fstypes = {"tmpfs", "devtmpfs", "efivarfs", "proc", "sysfs", "cgroup2",
                     "overlay", "squashfs", "devpts", "autofs", "mqueue",
                     "securityfs", "pstore", "bpf", "tracefs", "configfs",
                     "debugfs", "hugetlbfs", "fusectl", "binfmt_misc"}
    for ln in out.splitlines()[1:]:
        parts = ln.split()
        if len(parts) < 4:
            continue
        target, fstype, pcent, avail = parts[0], parts[1], parts[2], parts[3]
        if fstype in skip_fstypes:
            continue
        try:
            pct = int(pcent.rstrip("%"))
        except ValueError:
            continue
        flag = "  ⚠ over 85%" if pct >= 85 else ""
        lines.append(f"{target}: {pcent} used, {avail} free{flag}")
    return lines


def failed_units_lines():
    lines = []
    # systemctl --failed prefixes each row with a "●" bullet column, so the
    # unit name is parts[1] (if the bullet is there) not parts[0].
    def unit_name(ln):
        parts = ln.split()
        return parts[1] if parts and parts[0] in ("●", "*") else parts[0]

    ok, out = run(["systemctl", "--failed", "--no-legend"])
    if ok and out:
        lines += [f"[system] {unit_name(ln)}" for ln in out.splitlines() if ln.strip()]
    ok, out = run(["systemctl", "--user", "--failed", "--no-legend"])
    if ok and out:
        lines += [f"[user] {unit_name(ln)}" for ln in out.splitlines() if ln.strip()]
    return lines


def pacman_section(lines):
    if not have("pacman"):
        return
    # Cache size (report regardless of whether trimming succeeds). du exits
    # 1 if it can't read some root-owned subdir (e.g. in-progress downloads)
    # and interleaves those stderr lines with its stdout total in whatever
    # order the pipes flush, so "last line" isn't reliable -- find the line
    # that actually reports the total for cache_dir instead.
    cache_dir = "/var/cache/pacman/pkg"
    _, out = run(["du", "-sh", cache_dir])
    cache_size = "unknown"
    for ln in out.splitlines():
        if ln.endswith(cache_dir):
            cache_size = ln.split()[0]
            break

    # Orphans: read-only, no root needed.
    ok, out = run(["pacman", "-Qtdq"])
    orphans = [ln for ln in out.splitlines() if ln.strip()] if ok else []

    # Updates: use checkupdates (pacman-contrib) if present -- doesn't
    # touch the live pacman db, so no root needed. Skipped otherwise rather
    # than running `pacman -Sy` unattended (that writes the sync db as root
    # and is the first half of an unattended -Syu, which we're avoiding).
    if have("checkupdates"):
        ok, out = run(["checkupdates"], timeout=60)
        updates = [ln for ln in out.splitlines() if ln.strip()] if ok else []
        update_note = f"{len(updates)} available" if ok else "check failed"
    else:
        update_note = "checkupdates not installed (pacman-contrib) — skipped"

    # Tier 1 action: paccache needs root. Try it; if it fails on permissions,
    # report the manual command instead of silently doing nothing.
    trim_note = "pacman-contrib not installed — skipped"
    if have("paccache"):
        ok, out = run(["paccache", "-rk2"])
        if ok:
            trim_note = out.splitlines()[-1] if out else "trimmed"
        else:
            trim_note = "needs root — run manually: sudo paccache -rk2"

    section(lines, "Pacman cache", [f"{cache_size} before trim", trim_note])
    section(lines, "Orphaned packages", [f"{len(orphans)} found"] + orphans[:20])
    section(lines, "Available updates", [update_note])


def journal_section(lines):
    if not have("journalctl"):
        return
    ok, out = run(["journalctl", "--vacuum-time=2weeks"])
    if ok:
        note = out.splitlines()[-1] if out else "vacuumed"
    else:
        note = "needs root — run manually: sudo journalctl --vacuum-time=2weeks"
    section(lines, "Journal", [note])


def docker_section(lines):
    if not have("docker"):
        return
    # The `docker` CLI can be installed without the daemon running (e.g. tv
    # has the client but no dockerd) -- check connectivity before treating a
    # prune failure as noteworthy rather than just "not applicable here".
    ok, _ = run(["docker", "info"], timeout=10)
    if not ok:
        section(lines, "Docker", ["installed but daemon not reachable — skipped"])
        return
    ok, before = run(["docker", "system", "df", "--format",
                       "{{.Type}}: {{.Reclaimable}} reclaimable"])
    ok2, prune_out = run(["docker", "system", "prune", "-af"], timeout=180)
    note = prune_out.splitlines()[-1] if ok2 and prune_out else "prune failed or nothing to do"
    section(lines, "Docker", (before.splitlines() if ok else []) + [f"prune: {note}"])


def main():
    now = local_now()
    host = socket.gethostname()
    lines = [f"Weekly maintenance — {host} — {now.strftime('%A, %B %d')}"]

    pacman_section(lines)
    journal_section(lines)
    docker_section(lines)

    if have("apt"):
        ok, out = run(["apt", "list", "--upgradable"])
        count = max(0, len([ln for ln in out.splitlines() if "/" in ln]) ) if ok else 0
        section(lines, "Available updates (apt, index may be stale)", [f"{count} available"])

    section(lines, "Failed systemd units", failed_units_lines())
    section(lines, "Disk usage", disk_usage_lines())

    body = "\n".join(lines) + "\n"
    subject = f"Weekly maintenance — {host} — {now.strftime('%a %b %d')}"
    try:
        send_mail(subject, body, sender=f"{host}-maintenance@helm.local", recipient=RECIPIENT)
    except (smtplib.SMTPException, OSError) as e:
        print(f"host-maintenance: send failed: {e}", file=sys.stderr)
        return 1
    print(f"sent maintenance report for {host}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
