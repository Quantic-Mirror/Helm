#!/usr/bin/env python3
"""Mail the day's cleaning chores, and add them to the Helm Calendar tab.

Static weekly schedule (no Helm state needed to decide WHAT's due — this is
just a rotation by weekday). Two independent outputs from the same schedule:

  1. Email, the same way morning_digest.py sends its digest: shell out to
     notify.py, which speaks SMTP directly to postfix on hyperion's tailnet IP.
  2. A same-day entry on Helm's existing Calendar tab (state.calendarEvents),
     written through the normal /api/state GET+PUT round trip — the same
     "fetch, mutate, PUT the whole blob back" pattern the frontend itself
     uses (see CLAUDE.md's LWW note) — not a direct marks_state.json edit,
     which the running server would just overwrite on its next write.

A failure in step 2 does not block step 1: you still get the email even if
the calendar push can't reach the API for some reason.

Config (env):
    DIGEST_TO       recipient (default: isaboo@hyperion)
    DIGEST_TZ       zone for "today" (default America/New_York) — matters
                    because this runs on the VPS, not in the user's local zone.
    HELM_API_URL    base URL for the Helm API (default: this VPS's own
                    published address, https://100.77.126.57:8443 — override
                    if SERVER_HOST/HELM_BIND in .env ever changes).
    HELM_STATE_DIR  where helm_token.txt lives (default: REPO/data, matching
                    the compose bind mount).
"""
import json
import os
import ssl
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime

REPO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
NOTIFY = os.path.join(REPO, "scripts", "notify.py")
RECIPIENT = os.environ.get("DIGEST_TO", "isaboo@hyperion")
TZ_NAME = os.environ.get("DIGEST_TZ", "America/New_York")

STATE_DIR = os.environ.get("HELM_STATE_DIR", os.path.join(REPO, "data"))
TOKEN_FILE = os.path.join(STATE_DIR, "helm_token.txt")
# Same host Helm itself is published on (HELM_BIND/SERVER_HOST in .env) — not
# localhost, since the port is bound to that specific interface, not loopback.
HELM_API_URL = os.environ.get("HELM_API_URL", "https://100.77.126.57:8443")

# Every day, regardless of the rotation below.
DAILY = [
    "Scoop litter box(es)",
    "Quick sweep of obvious pet hair / tidy living room",
]

# High-traffic floors need mopping more than once a week, independent of the
# single-room-focus ROTATION below (which only touches kitchen/living room
# once each). Tue/Thu/Sat/Sun = ~every 2 days, one 1-day gap (Sat->Sun).
MOP_DAYS = {1, 3, 5, 6}  # Tuesday, Thursday, Saturday, Sunday
MOP_ITEM = "Mop kitchen & living room floors"

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
    if weekday in MOP_DAYS:
        lines.append("")
        lines.append("Also today:")
        lines.append(f"  - {MOP_ITEM}")
    lines.append("")
    lines.append("Every day:")
    lines += [f"  - {it}" for it in DAILY]
    return "\n".join(lines) + "\n", focus_title


def calendar_items_for(now):
    """What goes on the Calendar tab today: the room-focus title + mop, if
    due. Deliberately excludes the DAILY footer (litter/tidy) — those happen
    every single day regardless, so a permanent calendar entry for them would
    just be clutter, not information."""
    weekday = now.weekday()
    focus_title, _ = ROTATION[weekday]
    items = [f"🧹 {focus_title}"]
    if weekday in MOP_DAYS:
        items.append(f"🧹 {MOP_ITEM}")
    return items


def _helm_token():
    try:
        with open(TOKEN_FILE) as f:
            return f.read().strip()
    except OSError:
        return None  # auth disabled (no helm_token.txt) — same fail-open stance as the server


def _api_request(method, path, body=None):
    token = _helm_token()
    req = urllib.request.Request(HELM_API_URL + path, method=method)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        req.add_header("Content-Type", "application/json")
    # Self-signed cert, same box Helm itself runs on (see HELM_API_URL) — not
    # a different trust boundary than reading marks_state.json directly would
    # be, just reached through the API instead of the file.
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with urllib.request.urlopen(req, data=data, timeout=15, context=ctx) as resp:
        return json.loads(resp.read())


def push_calendar_events(now, items):
    """Add today's chore item(s) to state.calendarEvents, idempotently —
    skips any item whose text is already present for today so a re-run
    (manual retry, Persistent= catch-up after downtime) doesn't duplicate."""
    date_key = now.strftime("%Y-%m-%d")
    current = _api_request("GET", "/api/state")
    state = current.get("state") or {}
    calendar_events = state.setdefault("calendarEvents", {})
    day_list = calendar_events.setdefault(date_key, [])
    existing = {(e.get("text") if isinstance(e, dict) else e) for e in day_list}
    added = 0
    for text in items:
        if text in existing:
            continue
        day_list.append({"text": text, "reminderDays": 0})
        added += 1
    if added:
        _api_request("PUT", "/api/state", {"state": state})
    return added


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

    # Best-effort: the email above is the primary channel and already
    # succeeded, so a calendar-push problem (API down, bad token, network
    # blip) is logged but doesn't turn this run into a failure.
    try:
        added = push_calendar_events(now, calendar_items_for(now))
        print(f"calendar: added {added} event(s) for {now.strftime('%Y-%m-%d')}")
    except (urllib.error.URLError, OSError, ValueError) as e:
        print(f"chores-digest: calendar push failed: {e}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
