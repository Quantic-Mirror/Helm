#!/usr/bin/env python3
"""Mail the day's cleaning chores: daily must-dos + today's single room focus.

Static weekly schedule (no Helm state involved — this is just a rotation by
weekday), mailed the same way morning_digest.py sends its digest: shell out to
notify.py, which speaks SMTP directly to postfix on hyperion's tailnet IP.

Config (env):
    DIGEST_TO     recipient (default: isaboo@hyperion)
    DIGEST_TZ     zone for "today" (default America/New_York) — matters
                  because this runs on the VPS, not in the user's local zone.
"""
import os
import subprocess
import sys
from datetime import datetime

REPO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
NOTIFY = os.path.join(REPO, "scripts", "notify.py")
RECIPIENT = os.environ.get("DIGEST_TO", "isaboo@hyperion")
TZ_NAME = os.environ.get("DIGEST_TZ", "America/New_York")

# Every day, regardless of the rotation below.
DAILY = [
    "Scoop litter box(es)",
    "Quick sweep of obvious pet hair / tidy living room",
]

# Monday=0 .. Sunday=6 (datetime.weekday()). One room/focus per day so
# nothing stacks — see the two-bathroom / three-bedroom split.
ROTATION = {
    0: ("Kitchen", [
        "Deep clean kitchen — stove, microwave, sink scrub, wipe cabinets",
        "Take out trash/recycling",
        "Wash pet food & water bowls",
    ]),
    1: ("Bathroom 1", [
        "Toilet, sink, shower/tub, mirror, floor",
    ]),
    2: ("Bedrooms", [
        "Dust and vacuum/sweep all 3 bedrooms",
    ]),
    3: ("Bathroom 2", [
        "Toilet, sink, shower/tub, mirror, floor",
    ]),
    4: ("Living room", [
        "Vacuum (pet hair focus), dust surfaces, wipe down furniture",
    ]),
    5: ("Whole-house floors", [
        "Vacuum/mop everywhere",
        "Wash dog bed & blankets",
        "Deep-clean litter box (full litter change + scrub)",
    ]),
    6: ("Laundry + reset", [
        "Change bedsheets (rotate through the 3 bedrooms weekly)",
        "Take out trash/recycling",
        "Quick tidy walkthrough",
    ]),
}


def local_now():
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo(TZ_NAME))
    except Exception:  # noqa: BLE001 -- missing tzdata shouldn't kill the mail
        return datetime.now()


def build_body(now):
    weekday = now.weekday()
    focus_title, focus_items = ROTATION[weekday]
    lines = [f"Chores for {now.strftime('%A, %B %d')}", ""]
    lines.append(f"Today's focus: {focus_title}")
    lines += [f"  - {it}" for it in focus_items]
    lines.append("")
    lines.append("Every day:")
    lines += [f"  - {it}" for it in DAILY]
    return "\n".join(lines) + "\n", focus_title


def main():
    now = local_now()
    body, focus_title = build_body(now)
    subject = f"Chores — {now.strftime('%a %b %d')}: {focus_title}"
    r = subprocess.run(
        [sys.executable, NOTIFY, subject, body, "--from", "helm@vps", "--to", RECIPIENT],
        capture_output=True, text=True, timeout=60,
    )
    if r.returncode != 0:
        print(f"chores-digest: send failed: {r.stderr.strip()[:300]}", file=sys.stderr)
        return 1
    print(r.stdout.strip())
    return 0


if __name__ == "__main__":
    sys.exit(main())
