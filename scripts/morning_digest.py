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

The same sections go out three ways: as the mail body, as a plain-text
attachment, and as a single-file HTML infographic attachment. The HTML uses no
external assets, so it opens offline and in mail clients that allow attachments.

Reads state directly from the state dir rather than the HTTP API: the digest
is cron-style and should not depend on a token round-trip through the server it
is reporting on.

Config (env):
    HELM_STATE_DIR   state dir (default: REPO/data, which the compose bind mount uses)
    DIGEST_TO        recipient (default: isaboo@hyperion)
    DIGEST_HOURS     window in hours (default 24)
    DIGEST_TZ        zone for displayed times (default America/New_York)
"""
import html
import json
import os
import subprocess
import sys
import tempfile
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
    """Commits on origin/main in the window. (None, reason) if git can't be read."""
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


def collect(state, events, cutoff, cutoff_ms, hours):
    """Gather the digest as (title, items, note) sections, shared by both renderers.

    items is a list of strings; note, when set, replaces the list with a short
    explanation (used when git can't be read).
    """
    evs = recent_events(events, cutoff)
    sections = []

    bms = new_bookmarks(state, cutoff_ms)
    sections.append(("New bookmarks",
                     [f"{title} ({label}) {url}".rstrip() for label, title, url in bms], None))

    favs = new_favorites(state, cutoff_ms)
    sections.append(("Music favorites",
                     [f"{e.get('artist', '?')} — {e.get('song', '?')}" for e in favs], None))

    music = [e for e in evs if e.get("stage") == MUSIC_STAGE]
    sections.append(("Soulseek searches",
                     [e.get("message") for e in music if e.get("status") == "searched"], None))
    sections.append(("Downloaded and pulled from the VPS",
                     [e.get("message") for e in music if e.get("status") == "pulled"], None))
    sections.append(("Imported by beets",
                     [e.get("name", "").rsplit(" @", 1)[0] for e in music if e.get("status") == "imported"], None))
    sections.append(("Left for beets review",
                     [e.get("name", "").rsplit(" @", 1)[0] for e in music if e.get("status") == "review"], None))

    svc = [f"{clock(e['ts'])} {e.get('message', e.get('name', ''))}"
           for e in sorted((e for e in evs if e.get("stage") == SERVICE_STAGE),
                           key=lambda e: e.get("ts", 0))]
    sections.append(("Services", svc, None))

    commits, err = repo_commits(hours)
    if err is not None:
        sections.append(("Commits on main", [], f"could not read git ({err})"))
    else:
        sections.append(("Commits on main", commits, None))
    return sections


def render_text(header, sections):
    lines = [header, ""]
    for title, items, note in sections:
        if note:
            lines.append(f"{title}: {note}.")
        elif not items:
            lines.append(f"{title}: none.")
        else:
            lines.append(f"{title}:")
            lines += [f"  - {it}" for it in items]
    return "\n".join(lines) + "\n"


# Kept self-contained: no fonts, scripts or images, so the attachment renders
# the same offline and in any mail client's preview.
HTML_STYLE = """
:root { --bg:#f6f4ef; --card:#fff; --ink:#1d1b18; --muted:#6b665d; --accent:#c9622b; --line:#e4dfd4; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#16151a; --card:#211f26; --ink:#ece9e2; --muted:#9b958a; --accent:#f09a5c; --line:#34313b; }
}
* { box-sizing: border-box; }
body { margin:0; padding:24px 16px; background:var(--bg); color:var(--ink);
       font:15px/1.5 -apple-system, "Segoe UI", Helvetica, Arial, sans-serif; }
main { max-width:760px; margin:0 auto; }
h1 { font-size:22px; margin:0 0 4px; }
.sub { color:var(--muted); margin:0 0 20px; }
.tiles { display:grid; grid-template-columns:repeat(auto-fit,minmax(130px,1fr)); gap:10px; margin-bottom:20px; }
.tile { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:12px; }
.tile .n { font-size:28px; font-weight:700; color:var(--accent); line-height:1.1; }
.tile .l { color:var(--muted); font-size:13px; }
.card { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:14px 16px; margin-bottom:12px; }
.card h2 { font-size:15px; margin:0 0 6px; }
.card ul { margin:0; padding-left:18px; }
.card li { margin:2px 0; overflow-wrap:anywhere; }
.none { color:var(--muted); font-style:italic; margin:0; }
.note { color:var(--muted); margin:0; }
"""


def render_html(header, stamp, sections):
    counts = {title: len(items) for title, items, _ in sections}
    tiles = "".join(
        f'<div class="tile"><div class="n">{counts.get(t, 0)}</div><div class="l">{html.escape(t)}</div></div>'
        for t in ("New bookmarks", "Music favorites", "Soulseek searches",
                  "Imported by beets", "Services", "Commits on main"))
    cards = []
    for title, items, note in sections:
        if note:
            body = f'<p class="note">{html.escape(title)}: {html.escape(note)}.</p>'
        elif not items:
            body = '<p class="none">none</p>'
        else:
            body = "<ul>" + "".join(f"<li>{html.escape(str(it))}</li>" for it in items) + "</ul>"
        cards.append(f'<section class="card"><h2>{html.escape(title)}</h2>{body}</section>')
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>Helm digest {html.escape(stamp)}</title>"
        f"<style>{HTML_STYLE}</style></head><body><main>"
        f"<h1>Helm digest</h1><p class=\"sub\">{html.escape(stamp)} · {html.escape(header)}</p>"
        f'<div class="tiles">{tiles}</div>{"".join(cards)}'
        "</main></body></html>\n")


def main():
    now = time.time()
    cutoff = now - HOURS * 3600
    state = load_json(STATE_FILE, {}).get("state", {})
    events = load_json(EVENTS_FILE, {}).get("events", [])

    sections = collect(state, events, cutoff, int(cutoff * 1000), HOURS)
    header = f"Helm activity, last {int(HOURS)} hours (as of {clock(now)})."
    stamp = datetime.fromtimestamp(now, TZ).strftime("%Y-%m-%d") if TZ else time.strftime("%Y-%m-%d")
    body = render_text(header, sections)

    # Attachments are temp files, removed as soon as the send returns. The same
    # text goes in the body and as .txt so it can be grepped without the mail
    # client; the HTML is the infographic.
    with tempfile.TemporaryDirectory() as tmp:
        txt = os.path.join(tmp, f"helm-digest-{stamp}.txt")
        page = os.path.join(tmp, f"helm-digest-{stamp}.html")
        with open(txt, "w", encoding="utf-8") as f:
            f.write(body)
        with open(page, "w", encoding="utf-8") as f:
            f.write(render_html(header, stamp, sections))
        r = subprocess.run(
            [sys.executable, NOTIFY, f"Helm morning digest — {stamp}", body,
             "--from", "helm@vps", "--to", RECIPIENT, "--attach", txt, "--attach", page],
            capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        print(f"morning-digest: send failed: {r.stderr.strip()[:300]}", file=sys.stderr)
        return 1
    print(r.stdout.strip())
    return 0


if __name__ == "__main__":
    sys.exit(main())
