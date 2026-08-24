#!/usr/bin/env python3
"""
fetch_lyrics_batch.py — stage lyrics candidates for review before embedding
them into the music library.

Queries lrclib.net (a free, open, key-less lyrics API -- https://lrclib.net)
for each track in MPD's library that doesn't already have embedded lyrics.
Matches on artist + title *and* duration (via lrclib's /api/search, keeping
only candidates within DURATION_TOLERANCE_SECS of the track's real
duration), not just fuzzy title text -- this is what the ncmpcpp web
scrapers (Tekstowo/plyrics/etc.) don't do, and why they were returning
lyrics for the wrong song on less obvious tracks.

Nothing gets embedded here -- results are staged as plain-text files under
STAGING_DIR, mirroring the library's own folder structure so each staged
file maps unambiguously back to one audio file. Review them (read through,
delete anything wrong, edit anything close-but-off), then run
commit_lyrics.py to embed whatever's still there.

Usage:
    python3 fetch_lyrics_batch.py [--limit N] [--artist "Name"] [--redo]

    --limit N       Stop after staging N candidates (default: 25 -- a
                     batch small enough to actually review, not a
                     library-wide fetch-and-forget).
    --artist NAME   Only consider tracks by this artist (case-insensitive
                     substring match).
    --redo          Re-fetch even for tracks that already have a staged
                     candidate sitting unreviewed from a previous run
                     (default: skip them, since fetching again would just
                     overwrite an edit you may have already made).
"""

import argparse
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lyrics_tags import get_lyrics, UNSUPPORTED_EXTS

MEDIA_LIBRARY_ROOT = os.environ.get("MEDIA_LIBRARY_ROOT") or "/mnt/SharedStuff/Music"
STAGING_DIR = os.environ.get("LYRICS_STAGING_DIR") or os.path.expanduser("~/.lyrics-staging")
MPD_HOST = os.environ.get("MPD_HOST", "127.0.0.1")
MPD_PORT = int(os.environ.get("MPD_PORT", "6600"))

LRCLIB_SEARCH_URL = "https://lrclib.net/api/search"
DURATION_TOLERANCE_SECS = 3
REQUEST_DELAY_SECS = 0.6  # be a polite, unhurried client to a free API


def mpd_command(sock, cmd):
    sock.sendall((cmd + "\n").encode())
    data = b""
    sock.settimeout(10)
    while True:
        chunk = sock.recv(65536)
        if not chunk:
            break
        data += chunk
        if data.endswith(b"OK\n") or b"\nACK" in data:
            break
    return data.decode(errors="replace")


def parse_mpd_tracks(raw):
    """Parses MPD's "key: value" block format (from `search filename ""`)
    into a list of track dicts, one per "file:" line."""
    tracks = []
    current = None
    for line in raw.splitlines():
        if line == "OK" or line.startswith("ACK"):
            break
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        value = value.strip()
        if key == "file":
            if current is not None:
                tracks.append(current)
            current = {"file": value}
        elif current is not None:
            current[key] = value
    if current is not None:
        tracks.append(current)
    return tracks


def get_all_tracks():
    sock = socket.create_connection((MPD_HOST, MPD_PORT), timeout=10)
    sock.recv(200)  # banner
    raw = mpd_command(sock, 'search filename ""')
    sock.close()
    return parse_mpd_tracks(raw)


def search_lrclib(artist, title):
    qs = urllib.parse.urlencode({"artist_name": artist, "track_name": title})
    req = urllib.request.Request(f"{LRCLIB_SEARCH_URL}?{qs}", headers={
        "User-Agent": "Helm-lyrics-fetch/1.0 (personal use, github.com/lrclib/lrclib)",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read()), None
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code}"
    except urllib.error.URLError as e:
        return None, str(e.reason)


def best_match(candidates, known_duration):
    if not known_duration:
        return None
    best, best_diff = None, DURATION_TOLERANCE_SECS + 1
    for c in candidates:
        if c.get("instrumental") or not c.get("plainLyrics"):
            continue
        diff = abs((c.get("duration") or 0) - known_duration)
        if diff <= DURATION_TOLERANCE_SECS and diff < best_diff:
            best, best_diff = c, diff
    return best


def staged_path_for(relpath):
    return os.path.join(STAGING_DIR, relpath + ".txt")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--limit", type=int, default=25)
    parser.add_argument("--artist", default=None)
    parser.add_argument("--redo", action="store_true")
    args = parser.parse_args()

    print(f"Fetching track list from MPD ({MPD_HOST}:{MPD_PORT})...")
    tracks = get_all_tracks()
    print(f"{len(tracks)} tracks in the library.")

    if args.artist:
        needle = args.artist.lower()
        tracks = [t for t in tracks if needle in t.get("Artist", "").lower()]
        print(f"{len(tracks)} match artist filter '{args.artist}'.")

    staged, skipped_has_lyrics, skipped_no_tags, skipped_unsupported, skipped_already_staged, not_found, errors = 0, 0, 0, 0, 0, 0, 0

    for t in tracks:
        if staged >= args.limit:
            break

        relpath = t["file"]
        abspath = os.path.join(MEDIA_LIBRARY_ROOT, relpath)
        title, artist = t.get("Title"), t.get("Artist")
        try:
            duration = float(t.get("duration") or t.get("Time") or 0)
        except ValueError:
            duration = 0

        if relpath.lower().endswith(UNSUPPORTED_EXTS):
            skipped_unsupported += 1
            continue
        if not title or not artist:
            skipped_no_tags += 1
            continue

        staged_file = staged_path_for(relpath)
        if os.path.exists(staged_file) and not args.redo:
            skipped_already_staged += 1
            continue

        if get_lyrics(abspath):
            skipped_has_lyrics += 1
            continue

        candidates, err = search_lrclib(artist, title)
        time.sleep(REQUEST_DELAY_SECS)
        if err:
            print(f"  ERROR  {artist} — {title}: {err}")
            errors += 1
            continue

        match = best_match(candidates, duration)
        if not match:
            print(f"  MISS   {artist} — {title} ({duration:.0f}s) — no duration-matched candidate")
            not_found += 1
            continue

        os.makedirs(os.path.dirname(staged_file), exist_ok=True)
        with open(staged_file, "w", encoding="utf-8") as f:
            f.write(match["plainLyrics"])
        print(f"  STAGED {artist} — {title} (matched \"{match['artistName']}\" / \"{match['albumName']}\", {match['duration']:.0f}s)")
        staged += 1

    print()
    print(f"Staged: {staged}  |  Already had lyrics: {skipped_has_lyrics}  |  Already staged: {skipped_already_staged}")
    print(f"No duration match: {not_found}  |  Missing tags: {skipped_no_tags}  |  Unsupported format: {skipped_unsupported}  |  Errors: {errors}")
    print()
    print(f"Review staged candidates under {STAGING_DIR} -- delete anything wrong,")
    print("edit anything close-but-off, then run commit_lyrics.py.")


if __name__ == "__main__":
    main()
