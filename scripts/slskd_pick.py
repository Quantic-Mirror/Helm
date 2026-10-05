#!/usr/bin/env python3
"""Handle a `slskd-get:` pick: enqueue the chosen source, retrying across peers.

Reads the offers persisted by slskd_drain.py, resolves the source number from
the results mail, and enqueues that peer's files. Whole album by default, since
that is what the mail displays; a single track can be requested explicitly.

Why peers are retried rather than the search re-run: Soulseek peer presence is
not carried in a search response, so by the time you pick, the peer you were
shown may be gone. Measured on a live "beach house" search: 22 consecutive
enqueue attempts returned "User enqueue appears to be offline", while a manual
download from the web UI worked immediately — proving the account and network
were fine and the failures were churn. A stale peer answers instantly with an
error, so trying the next is cheap.

If the chosen source's peer is offline, the fallbacks are deliberate and
reported: a same-artist source from the same search, then any source, and the
mail says which was used. Silently downloading from a different peer than the one
you picked would be worse than telling you.
"""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.environ.get(
    "SLSKD_SCRIPTS", os.path.dirname(os.path.abspath(__file__))))

import slskd_offers as OFF
import slskd_queue as Q
import slskd_search as S

STATE = Q.STATE
NOTIFY = os.environ.get("NOTIFY", os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "notify.py"))
RECIPIENT = os.environ.get("DIGEST_TO", "isaboo@hyperion")

SLSKD = os.environ.get("SLSKD_URL", "http://100.77.126.57:5030")
# slskd's own internal peer timeout is ~5s; give it a little more so a slow but
# live peer is not written off.
PEER_TIMEOUT_NOTE = "slskd times out on an unresponsive peer after ~5s"


def read_token():
    out = subprocess.run(
        ["grep", "-A5", "^ web:", S.CONFIG],
        capture_output=True, text=True, timeout=20).stdout
    for line in out.split("\n"):
        if line.strip().startswith("key:"):
            return line.split(":", 1)[1].strip()
    return None


def enqueue(username, files, key, timeout=45):
    """Queue files from one peer. Returns (ok, message).

    Uses the per-user route, POST /transfers/downloads/{username}, with a
    top-level array of {filename, size}. The size matters: sent as 0, slskd
    accepts the request and then aborts the transfer ("remote size ... does not
    match expected size 0"). The older /transfers/downloads/enqueue route
    answers "User enqueue appears to be offline" for peers that are online and
    downloadable, so it is not used.
    """
    payload = [{"filename": f.get("filename"), "size": f.get("size") or 0} for f in files]
    url = f"{SLSKD}/api/v0/transfers/downloads/{urllib.parse.quote(username, safe='')}"
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"X-API-Key": key, "Content-Type": "application/json"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode()
            data = json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}: {e.read().decode()[:160]}"
    except Exception as e:  # noqa: BLE001
        return False, str(e)[:160]
    # A 200 can still enqueue nothing; the reason is in `failed`.
    if isinstance(data, dict) and not data.get("enqueued") and data.get("failed"):
        return False, json.dumps(data["failed"][0])[:160]
    return True, data


def mail(subject, body):
    if not os.path.exists(NOTIFY):
        print(f"pick: notify.py missing at {NOTIFY}", file=sys.stderr)
        return False
    r = subprocess.run(
        ["python3", NOTIFY, subject, body, "--to", RECIPIENT],
        capture_output=True, text=True, timeout=90)
    if r.returncode != 0:
        print(f"pick: notify failed: {r.stderr.strip()[:200]}", file=sys.stderr)
        return False
    return True


def whole_album_of(offer, album_hint=None):
    """Choose which files to request: one album, defaulting to the largest.

    The results mail shows the biggest albums first, so "the album" most often
    means the first one displayed. Requesting every file from a peer is not the
    default: a peer advertising 353 files is a grab-bag, not an album.
    """
    import re
    groups = {}
    for f in offer.get("files") or []:
        _a, album = S.guess_artist_album(f.get("filename"))
        groups.setdefault(album or "(unknown)", []).append(f)

    if album_hint:
        hl = album_hint.lower()
        for name, files in groups.items():
            if hl in name.lower():
                return name, files
        return None, []

    if not groups:
        return None, list(offer.get("files") or [])
    name, files = max(groups.items(), key=lambda kv: len(kv[1]))
    return name, files


def try_sources(offers, key, wanted_index, query, album_hint=None, log=print):
    """Try the picked source, then fall back. Returns (offer, album, files, notes)."""
    order = []
    if 1 <= wanted_index <= len(offers):
        order.append(offers[wanted_index - 1])
    # Same-artist sources from the same search, then everything else.
    artist = (offers[wanted_index - 1].get("artist") or "").lower() \
        if 1 <= wanted_index <= len(offers) else ""
    for o in offers:
        if o in order:
            continue
        if artist and (o.get("artist") or "").lower() == artist:
            order.append(o)
    for o in offers:
        if o not in order:
            order.append(o)

    notes = []
    # Soulseek peers can require a share ratio — they refuse to upload to users
    # who share nothing. slskd on the VPS has no shared directory (the user's
    # music library lives on hyperion and is not mounted in), so a peer that
    # enforces this answers "User enqueue appears to be offline" even though it
    # is actually present and free. A peer that does NOT enforce it downloads
    # immediately — this was the earlier manual success. The honest failure mode
    # is therefore "peer requires sharing", not "peer is offline".
    for attempt, offer in enumerate(order, 1):
        album, files = whole_album_of(offer, album_hint)
        if not files:
            notes.append(f"{offer['username']}: no audio files in offer")
            continue
        label = f"[{attempt}] {offer['username']}"
        if album_hint and not album:
            notes.append(f"{offer['username']}: no album matching {album_hint!r}")
            continue
        log(f"pick: trying {label} ({len(files)} files, album={album!r})")
        ok, msg = enqueue(offer["username"], files, key)
        if ok:
            if attempt > 1:
                notes.append(
                    f"source [{wanted_index}] was unavailable; used "
                    f"{offer['username']} instead")
            return offer, album, files, notes
        low = str(msg).lower()
        if "offline" in low:
            notes.append(f"{offer['username']}: offline")
        elif "timed out" in low:
            notes.append(f"{offer['username']}: timed out ({PEER_TIMEOUT_NOTE})")
        else:
            notes.append(f"{offer['username']}: {msg}")
    return None, None, None, notes


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Handle a slskd-get: pick")
    ap.add_argument("query")
    ap.add_argument("index", type=int, help="1-based source number from the mail")
    ap.add_argument("--album", help="album name substring, to pick a specific one")
    args = ap.parse_args()

    offers = OFF.get_offers(STATE, query=args.query)
    if not offers:
        available = OFF.queries(STATE)
        hint = (f"  Stored queries: {', '.join(available)}" if available
                else "  Nothing stored — the search may have expired or never ran.")
        mail("Soulseek: nothing to pick",
             f"No stored results for {args.query!r}.\n\n{hint}\n\n"
             f"Re-run the search with:  slskd: {args.query}\n")
        print(f"pick: no offers for {args.query!r}", file=sys.stderr)
        return 1

    key = read_token()
    if not key:
        mail("Soulseek: cannot download",
             f"Could not read the slskd API key, so {args.query!r} was not "
             f"downloaded.\n")
        return 1

    offer, album, files, notes = try_sources(
        offers, key, args.index, args.query, args.album,
        log=lambda m: print(m, file=sys.stderr))

    if not files:
        attempted = len(notes)
        body = [
            f"Could not download {args.query!r} from any source.",
            "",
            f"All {attempted} sources in the results were tried and none "
            "accepted transfers. The most common cause: Soulseek peers can "
            "require a share ratio, and slskd on the VPS shares no directories. "
            "The music library is on hyperion and is not mounted into the "
            "container, so peers that enforce sharing answer "
            '"User enqueue appears to be offline" even when present and free.',
            "",
            "Retry options:",
            "  - reply slskd-get: [N] <query> for a different listed source",
            "  - or re-run the search; some peers enforce sharing and some do not",
            "",
            "Attempted sources:",
        ] + [f"  - {n}" for n in notes]
        body += ["", "Re-run with:  slskd: " + args.query]
        mail(f"Soulseek: no source available for {args.query}", "\n".join(body))
        print("pick: every source failed", file=sys.stderr)
        return 1

    OFF.mark_claimed(STATE, offer)
    mb = sum(f.get("size") or 0 for f in files) / 1e6
    lines = [
        f"Queued from {offer['username']}",
        f"  album   : {album}",
        f"  tracks  : {len(files)}  ({mb:.0f} MB)",
        "",
        "Files land on the VPS. They are not moved to the MPD library "
        "automatically.",
    ]
    if notes:
        lines += ["", "Notes:"] + [f"  - {n}" for n in notes]
    lines += ["", "Track it with:  slskd's Transfers view in the web UI."]
    mail(f"Downloading: {args.query} — {album}", "\n".join(lines))
    print(f"pick: queued {len(files)} files from {offer['username']}",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
