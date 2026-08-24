#!/usr/bin/env python3
"""
commit_lyrics.py — embeds reviewed lyrics candidates (staged by
fetch_lyrics_batch.py) into their audio files, then removes the staged
file so re-running this script doesn't redo already-committed work.

Each staged file's path mirrors its source audio file's path under
MEDIA_LIBRARY_ROOT, plus a trailing ".txt" (e.g. a staged file at
"Flipturn/Rodeo Clown/Rodeo Clown.mp3.txt" embeds into
"Flipturn/Rodeo Clown/Rodeo Clown.mp3") -- so review before running this is
just: delete a staged .txt to reject it, or edit its contents to fix it.
Nothing under STAGING_DIR at commit time is assumed correct beyond "the
user left it there on purpose."

Usage:
    python3 commit_lyrics.py [--dry-run]

    --dry-run   Show what would be embedded without writing anything.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lyrics_tags import set_lyrics

MEDIA_LIBRARY_ROOT = os.environ.get("MEDIA_LIBRARY_ROOT") or "/mnt/SharedStuff/Music"
STAGING_DIR = os.environ.get("LYRICS_STAGING_DIR") or os.path.expanduser("~/.lyrics-staging")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not os.path.isdir(STAGING_DIR):
        print(f"No staging directory at {STAGING_DIR} -- nothing to commit. Run fetch_lyrics_batch.py first.")
        return

    committed, missing_source, errors = 0, 0, 0

    for dirpath, _dirnames, filenames in os.walk(STAGING_DIR):
        for name in filenames:
            if not name.endswith(".txt"):
                continue
            staged_file = os.path.join(dirpath, name)
            relpath = os.path.relpath(staged_file, STAGING_DIR)[: -len(".txt")]
            audio_path = os.path.join(MEDIA_LIBRARY_ROOT, relpath)

            if not os.path.isfile(audio_path):
                print(f"  MISSING SOURCE  {relpath} (audio file no longer exists -- leaving staged file in place)")
                missing_source += 1
                continue

            with open(staged_file, "r", encoding="utf-8") as f:
                text = f.read().strip()
            if not text:
                print(f"  SKIP (empty)  {relpath}")
                continue

            if args.dry_run:
                print(f"  WOULD EMBED  {relpath} ({len(text)} chars)")
                committed += 1
                continue

            ok, err = set_lyrics(audio_path, text)
            if not ok:
                print(f"  ERROR  {relpath}: {err}")
                errors += 1
                continue

            os.remove(staged_file)
            print(f"  EMBEDDED  {relpath}")
            committed += 1

    # Clean up now-empty directories left behind under STAGING_DIR.
    if not args.dry_run:
        for dirpath, dirnames, filenames in os.walk(STAGING_DIR, topdown=False):
            if dirpath != STAGING_DIR and not dirnames and not filenames:
                os.rmdir(dirpath)

    print()
    verb = "Would embed" if args.dry_run else "Embedded"
    print(f"{verb}: {committed}  |  Missing source file: {missing_source}  |  Errors: {errors}")


if __name__ == "__main__":
    main()
