#!/usr/bin/env python3
"""
yt_audio_grabber.py

Searches YouTube for a track (artist + song title) and downloads the audio.
Requires: yt-dlp, ffmpeg. Works on Linux, macOS, and Windows.

Install dependencies (Arch Linux):
    sudo pacman -S ffmpeg
    pip install yt-dlp --break-system-packages
    # or, in a venv: pip install yt-dlp

Install dependencies (Windows):
    # ffmpeg — pick one:
    winget install ffmpeg
    #   or: choco install ffmpeg
    #   or download a build from https://www.gyan.dev/ffmpeg/builds/ and add
    #   its \bin folder to your PATH.

    # yt-dlp (in PowerShell or cmd.exe):
    pip install yt-dlp
    #   or, in a venv:  py -m venv venv  &&  venv\Scripts\activate  &&  pip install yt-dlp

Usage (same on every OS; use "python" instead of "python3" on Windows if
that's how Python is aliased on your system):
    # Single track via CLI args
    python3 yt_audio_grabber.py --artist "Artist Name" --song "Song Title"
    py yt_audio_grabber.py --artist "Artist Name" --song "Song Title"      (Windows)

    # Batch mode from a file (one track per line: "Artist | Song Title")
    python3 yt_audio_grabber.py --file tracks.txt

    # Optional: choose output directory and audio format
    python3 yt_audio_grabber.py --file tracks.txt --outdir ./downloads --format mp3
    py yt_audio_grabber.py --file tracks.txt --outdir .\downloads --format mp3   (Windows)

Notes:
    - This tool only automates a search + download; it does not bypass any
      access controls. Only use it on content you have the rights to download
      (e.g. your own uploads, royalty-free/Creative Commons tracks, or content
      whose license/terms explicitly permit offline downloading).
    - Downloading copyrighted music you don't have rights to may violate
      YouTube's Terms of Service and copyright law in your jurisdiction.
"""

import argparse
import sys
from pathlib import Path

try:
    import yt_dlp
except ImportError:
    sys.exit(
        "Missing dependency 'yt-dlp'. Install it with:\n"
        "    pip install yt-dlp --break-system-packages"
    )


def parse_track_file(path: Path):
    """Yield (artist, song) tuples from a file with 'Artist | Song Title' lines."""
    tracks = []
    with path.open("r", encoding="utf-8") as f:
        for lineno, raw_line in enumerate(f, start=1):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if "|" not in line:
                print(f"[warn] Skipping malformed line {lineno}: {raw_line!r}")
                continue
            artist, _, song = line.partition("|")
            artist, song = artist.strip(), song.strip()
            if not artist or not song:
                print(f"[warn] Skipping incomplete line {lineno}: {raw_line!r}")
                continue
            tracks.append((artist, song))
    return tracks


def build_ydl_opts(outdir: Path, audio_format: str, quality: str):
    outdir.mkdir(parents=True, exist_ok=True)
    return {
        "format": "bestaudio/best",
        "outtmpl": str(outdir / "%(title)s.%(ext)s"),
        "noplaylist": True,
        "quiet": False,
        "no_warnings": False,
        "default_search": "ytsearch1",  # take the single best search hit
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": audio_format,
                "preferredquality": quality,
            }
        ],
    }


def download_track(artist: str, song: str, ydl_opts: dict) -> bool:
    query = f"{artist} {song} audio"
    print(f"\n[info] Searching for: {artist} - {song}")
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([query])
        return True
    except yt_dlp.utils.DownloadError as e:
        print(f"[error] Failed to download '{artist} - {song}': {e}")
        return False


def main():
    parser = argparse.ArgumentParser(
        description="Search YouTube and download audio for a song by artist and title."
    )
    parser.add_argument("--artist", help="Artist name (used with --song)")
    parser.add_argument("--song", help="Song title (used with --artist)")
    parser.add_argument(
        "--file",
        type=Path,
        help="Path to a text file with one 'Artist | Song Title' entry per line",
    )
    parser.add_argument(
        "--outdir",
        type=Path,
        default=Path("./downloads"),
        help="Output directory for downloaded audio (default: ./downloads)",
    )
    parser.add_argument(
        "--format",
        default="mp3",
        choices=["mp3", "m4a", "flac", "wav", "opus"],
        help="Output audio format (default: mp3)",
    )
    parser.add_argument(
        "--quality",
        default="192",
        help="Audio quality/bitrate for lossy formats, e.g. 192, 256, 320 (default: 192)",
    )
    args = parser.parse_args()

    if not args.file and not (args.artist and args.song):
        parser.error("Provide either --file, or both --artist and --song.")

    tracks = []
    if args.artist and args.song:
        tracks.append((args.artist, args.song))
    if args.file:
        if not args.file.exists():
            parser.error(f"File not found: {args.file}")
        tracks.extend(parse_track_file(args.file))

    if not tracks:
        sys.exit("[error] No valid tracks to process.")

    ydl_opts = build_ydl_opts(args.outdir, args.format, args.quality)

    successes, failures = 0, 0
    for artist, song in tracks:
        if download_track(artist, song, ydl_opts):
            successes += 1
        else:
            failures += 1

    print(f"\n[done] {successes} succeeded, {failures} failed. Output dir: {args.outdir.resolve()}")


if __name__ == "__main__":
    main()
