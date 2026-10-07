#!/usr/bin/env python3
"""Mail a morning summary of the last 24 hours of Helm activity.

Runs on the VPS, next to the state it reports on. Sections, each short:

  - New bookmarks (both profiles), from the addedAt timestamps on each bookmark
  - New Music tab favorites (state.musicXplorer, addedAt)
  - Music work: what slskd searched for and what beets imported or left for
    review. Reported by slskd_drain.py and slskd_ingest.py through
    /api/backup-events (see scripts/activity.py)
  - Services that went down or came back (scripts/service_watch.py)
  - Commits that landed on main in the repo this runs from

Reads state directly from the state dir rather than the HTTP API: the digest
is cron-style and should not depend on a token round-trip through the server it
is reporting on.

Config (env):
    HELM_STATE_DIR   state dir (default: REPO/data, which the compose bind mount uses)
    DIGEST_TO        recipient (default: isaboo@hyperion)
    DIGEST_HOURS     window in hours (default 24)
    DIGEST_TZ        zone for displayed times (default America/New_York)
"""
import json
import os
import subprocess
import sys
import time
from datetime import datetime

REPO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
STATE_DIR = os.environ.get("HELM_STATE_DIR", os.path.join(REPO, "data"))
STATE_FILE = os.path.join(STATE_DIR, "marks_state.json")
EVENTS_FILE = os.path.join(STATE_DIR, "backup_events.json")
NOTIFY = os.path.join(REPO, "scripts", "notify.py")
RECIPIENT = os.environ.get("DIGEST_TO", "isaboo@hyperion")
HOURS = float(os.environ.get("DIGEST_HOURS", "24"))
TZ_NAME = os.environ.get("DIGEST_TZ", "America/New_York")

MUSIC_STAGE = "music"
SERVICE_STAGE = "service"


def local_tz():
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(TZ_NAME)
    except Exception:  # noqa: BLE001 -- missing tzdata shouldn't kill the digest
        return None


TZ = local_tz()


def clock(ts):
    dt = datetime.fromtimestamp(ts, TZ) if TZ else datetime.fromtimestamp(ts)
    return dt.strftime("%H:%M")


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


def new_bookmarks(state, cutoff_ms):
    """Bookmarks added since cutoff, from both profiles, de-duplicated by id."""
    seen, out = set(), []
    for profile_key, label in (("profilePersonal", "Personal"), ("profileWork", "Work")):
        sub = state.get(profile_key) or {}
        for bm in sub.get("bookmarks") or []:
            if bm.get("id") in seen:
                continue
            seen.add(bm.get("id"))
            if (bm.get("addedAt") or 0) >= cutoff_ms:
                out.append((label, bm.get("title") or bm.get("url") or "(untitled)", bm.get("url", "")))
    return out


def new_favorites(state, cutoff_ms):
    return [e for e in state.get("musicXplorer") or [] if (e.get("addedAt") or 0) >= cutoff_ms]


def recent_events(events, cutoff_ts):
    return [e for e in events if (e.get("ts") or 0) >= cutoff_ts]


def repo_commits(since_hours):
    """Commits on origin/main in the window. Empty on any git failure, with a note."""
    try:
        subprocess.run(["git", "-C", REPO, "fetch", "-q", "origin"],
                       capture_output=True, text=True, timeout=60)
        r = subprocess.run(
            ["git", "-C", REPO, "log", "origin/main", f"--since={int(since_hours)} hours ago",
             "--format=%h %s"],
            capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as e:
        return None, str(e)
    if r.returncode != 0:
        return None, r.stderr.strip()[:200]
    return [ln for ln in r.stdout.splitlines() if ln.strip()], None


def section(title, lines):
    if not lines:
        return [f"{title}: none."]
    return [f"{title}:"] + [f"  - {ln}" for ln in lines]


def build_body(state, events, cutoff, cutoff_ms, hours):
    evs = recent_events(events, cutoff)
    lines = []

    bms = new_bookmarks(state, cutoff_ms)
    lines += section("New bookmarks",
                     [f"{title} ({label}) {url}".rstrip() for label, title, url in bms])

    favs = new_favorites(state, cutoff_ms)
    lines += section("Music favorites",
                     [f"{e.get('artist', '?')} — {e.get('song', '?')}" for e in favs])

    music = [e for e in evs if e.get("stage") == MUSIC_STAGE]
    searched = [e.get("message") for e in music if e.get("status") == "searched"]
    imported = [e.get("name", "").rsplit(" @", 1)[0] for e in music if e.get("status") == "imported"]
    review = [e.get("name", "").rsplit(" @", 1)[0] for e in music if e.get("status") == "review"]
    pulled = [e.get("message") for e in music if e.get("status") == "pulled"]
    lines += section("Soulseek searches", searched)
    lines += section("Downloaded and pulled from the VPS", pulled)
    lines += section("Imported by beets", imported)
    lines += section("Left for beets review", review)

    svc = []
    for e in sorted((e for e in evs if e.get("stage") == SERVICE_STAGE), key=lambda e: e.get("ts", 0)):
        svc.append(f"{clock(e['ts'])} {e.get('message', e.get('name', ''))}")
    lines += section("Services", svc)

    commits, err = repo_commits(hours)
    if err is not None:
        lines += [f"Repo commits: could not read git ({err})."]
    else:
        lines += section("Commits on main", commits)

    header = f"Helm activity, last {int(hours)} hours (as of {clock(time.time())})."
    return "\n".join([header, ""] + lines) + "\n"


def main():
    hours = HOURS
    now = time.time()
    cutoff = now - hours * 3600
    state = load_json(STATE_FILE, {}).get("state", {})
    events = load_json(EVENTS_FILE, {}).get("events", [])

    body = build_body(state, events, cutoff, int(cutoff * 1000), hours)
    stamp = datetime.fromtimestamp(now, TZ).strftime("%Y-%m-%d") if TZ else time.strftime("%Y-%m-%d")
    r = subprocess.run(
        [sys.executable, NOTIFY, f"Helm morning digest — {stamp}", body,
         "--from", "helm@vps", "--to", RECIPIENT],
        capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        print(f"morning-digest: send failed: {r.stderr.strip()[:300]}", file=sys.stderr)
        return 1
    print(r.stdout.strip())
    return 0


if __name__ == "__main__":
    sys.exit(main())
