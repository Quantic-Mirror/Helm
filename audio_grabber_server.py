#!/usr/bin/env python3
"""
audio_grabber_server.py — small standalone HTTP service that searches
YouTube and downloads audio via yt-dlp, meant to run on hyperion (where
you actually want the files to land) rather than popcorn.

helm_server.py on popcorn proxies /api/audio/* browser requests here rather
than running yt-dlp on popcorn directly -- same pattern as vault_server.py
proxying the pass-backed vault API.

Auth: requires a shared token in audio_token.txt (next to this script) that
must match the same file next to helm_server.py on popcorn. Generate once
and copy to both machines:
    openssl rand -hex 32 > audio_token.txt

Usage:
    python3 audio_grabber_server.py [port]

Default port: 8091
"""

import sys
import os
import concurrent.futures
import json
import mimetypes
import queue as _queue
import random
import re
import shlex
import shutil
import subprocess
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote

import mpv_ipc

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8091
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TOKEN_FILE = os.path.join(SCRIPT_DIR, "audio_token.txt")


def _load_token():
    if not os.path.exists(TOKEN_FILE):
        print(f"WARNING: {TOKEN_FILE} not found. Generate one with:")
        print(f"  openssl rand -hex 32 > {TOKEN_FILE}")
        print("...then copy that same file to popcorn, next to helm_server.py.")
        print("All requests will be rejected until it exists.")
        return None
    with open(TOKEN_FILE) as f:
        return f.read().strip()


AUDIO_TOKEN = _load_token()


def _run(cmd, timeout=10, env=None):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
        return r.stdout.strip(), r.stderr.strip(), r.returncode
    except subprocess.TimeoutExpired:
        return "", "timeout", 1
    except Exception as e:
        return "", str(e), 1


def _find_binary(name, env_var):
    """Resolve an external tool's absolute path. A systemd unit's inherited
    PATH is often much narrower than an interactive shell's (may miss
    ~/.local/bin, /usr/local/bin, etc.), which is why a bare 'yt-dlp' can
    work fine at a terminal but fail with FileNotFoundError when a service
    unit runs it. env_var lets it be pinned explicitly (see TOKEN_FILE/
    VAULT_BACKEND_URL for the same override pattern elsewhere) without
    editing this file."""
    override = os.environ.get(env_var)
    if override:
        return override
    found = shutil.which(name)
    if found:
        return found
    for candidate in (f"/usr/bin/{name}", f"/usr/local/bin/{name}", os.path.expanduser(f"~/.local/bin/{name}")):
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return name  # last resort -- let it fail with a clear PATH-related error


YT_DLP_BIN = _find_binary("yt-dlp", "YT_DLP_BIN")


def _widened_path_env():
    """Newer yt-dlp versions shell out to an external JS runtime (deno, by
    default) on PATH to solve YouTube's signature/nsig challenges -- this is
    what a "[jsc:deno] Solving JS challenges using deno" log line means. If
    that lookup fails, yt-dlp doesn't error out; it just produces a stream
    URL YouTube then serves as a 403 when actually fetched, which is
    indistinguishable from a real block unless you know to look for it.
    Deno's official installer puts it in ~/.deno/bin, which (like
    ~/.local/bin for yt-dlp itself, see YT_DLP_BIN above) is on an
    interactive shell's PATH but not on a systemd unit's. Widen PATH for the
    subprocess rather than trying to locate every tool yt-dlp might shell
    out to individually."""
    env = dict(os.environ)
    existing = env.get("PATH", "").split(os.pathsep)
    for extra in ("/usr/local/bin", os.path.expanduser("~/.local/bin"), os.path.expanduser("~/.deno/bin")):
        if extra not in existing:
            existing.append(extra)
    env["PATH"] = os.pathsep.join(existing)
    return env


AUDIO_ENV = _widened_path_env()

# Escape hatch for YouTube's anti-bot blocks (403s), which are a moving
# target: today's fix (e.g. --extractor-args "youtube:player_client=android")
# may stop working after YouTube's next change, or an outdated yt-dlp build
# reaching for a client YouTube has since started blocking (that was the
# real cause the one time this bit us -- `yt-dlp -U` fixed it, no extra args
# needed). Rather than hardcoding a specific workaround here that will go
# stale, let it be tuned live via env var, e.g.:
#   YTDLP_EXTRA_ARGS='--extractor-args "youtube:player_client=android"'
YTDLP_EXTRA_ARGS = shlex.split(os.environ.get("YTDLP_EXTRA_ARGS", ""))

# A 403 on the video *data/webpage* fetch itself (as opposed to a specific
# stream URL 403, which is the deno/nsig issue AUDIO_ENV addresses above) is
# usually YouTube rate-limiting or blocking the server's IP for making
# anonymous, unauthenticated requests. The durable fix is authenticating
# with cookies exported from a real browser session. If a cookies file
# (Netscape format, e.g. from the "Get cookies.txt LOCALLY" browser
# extension) is dropped next to this script, use it automatically;
# YTDLP_COOKIES_FILE overrides the location. Never commit this file -- it's
# gitignored, same as audio_token.txt/vault_token.txt.
AUDIO_COOKIES_FILE = os.environ.get("YTDLP_COOKIES_FILE") or os.path.join(SCRIPT_DIR, "audio-cookies.txt")
AUDIO_COOKIES_ARGS = ["--cookies", AUDIO_COOKIES_FILE] if os.path.isfile(AUDIO_COOKIES_FILE) else []

# YouTube sometimes gates an entire search *results page* behind a "Confirm
# your age" / "Sign in" interstitial for anonymous (logged-out) requests --
# not because the query itself is unusual, but because YouTube's own ranking
# decided one of the top hits for that specific query needs age verification
# (confirmed by inspecting the raw search response for a query that reliably
# reproduces this: the JSON comes back with a backgroundPromoRenderer titled
# "Confirm your age" instead of any itemSectionRenderer video entries).
# yt-dlp's search extractor doesn't recognize that block, so it just reports
# "0 items" -- no error, nothing to catch, which is why this surfaces here as
# "yt-dlp finished but did not report an output file" rather than a clear
# search-failed message. There's no header/flag workaround for this (unlike
# the SafeSearch PREF cookie, which does nothing here -- verified empirically,
# it still returns 0 items with or without it): the interstitial requires an
# actually-authenticated, age-verified session, which only a real cookies
# file provides. See AUDIO_COOKIES_FILE above for how to set one up.
SEARCH_NO_RESULTS_HINT = (
    "no downloadable result for this search -- if this keeps happening for "
    "songs that clearly exist on YouTube, it's likely the age/sign-in "
    "interstitial described above; adding a real cookies file "
    f"({AUDIO_COOKIES_FILE}) usually fixes it"
)

# Defaults to a folder next to this script, but override with
# AUDIO_DOWNLOAD_DIR to land downloads somewhere else entirely, e.g. a
# shared media mount (see AUDIO_DOWNLOAD_DIR= in audio-grabber.service).
AUDIO_DIR = os.environ.get("AUDIO_DOWNLOAD_DIR") or os.path.join(SCRIPT_DIR, "audio-downloads")
AUDIO_FORMATS = ("mp3", "m4a", "flac", "wav", "opus")
AUDIO_MAX_BATCH = 50  # guard against an accidental huge paste queuing hundreds of jobs
AUDIO_SEARCH_LIMIT = 8

_audio_lock = threading.Lock()
_audio_jobs = {}      # job_id (int) -> job dict
_audio_job_seq = 0
_audio_queue = _queue.Queue()


def search_audio_candidates(query, limit=AUDIO_SEARCH_LIMIT):
    """Return up to `limit` YouTube search hits for `query` without
    downloading anything, so the caller can pick the right one instead of
    trusting yt-dlp's top hit (which is what queue_audio_downloads() does).
    --flat-playlist skips per-video format resolution, so this is fast and
    much less likely to trip YouTube's anti-bot blocks than an actual
    download is."""
    limit = max(1, min(15, limit))
    cmd = [YT_DLP_BIN, f"ytsearch{limit}:{query}", "--flat-playlist", "--dump-json", "--no-warnings"] + AUDIO_COOKIES_ARGS + YTDLP_EXTRA_ARGS
    stdout, stderr, rc = _run(cmd, timeout=30, env=AUDIO_ENV)
    if rc != 0:
        tail = [l for l in (stderr or stdout or "").strip().splitlines() if l.strip()]
        return None, (" / ".join(tail[-3:]) if tail else f"yt-dlp exited with code {rc}")

    results = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        vid = obj.get("id")
        results.append({
            "id": vid,
            "title": obj.get("title") or "(untitled)",
            "uploader": obj.get("uploader") or obj.get("channel") or "",
            "duration": obj.get("duration"),
        })
    return results, None


def _new_audio_job(label):
    global _audio_job_seq
    with _audio_lock:
        _audio_job_seq += 1
        job = {
            "id": _audio_job_seq,
            "label": label,
            "status": "queued",
            "message": "",
            "filename": None,
            "queuedAt": time.time(),
            "startedAt": None,
            "finishedAt": None,
        }
        _audio_jobs[job["id"]] = job
    return job


def _run_audio_download(job_id, target, is_url, audio_format, quality):
    with _audio_lock:
        job = _audio_jobs.get(job_id)
        if not job:
            return
        job["status"] = "running"
        job["startedAt"] = time.time()

    os.makedirs(AUDIO_DIR, exist_ok=True)
    cmd = [YT_DLP_BIN, "--no-playlist"]
    if not is_url:
        # Free-text query (artist+song) -- let yt-dlp search and take its
        # best guess. For a guaranteed-correct match, the caller should
        # search via search_audio_candidates() and pass an exact video URL
        # (is_url=True) instead.
        cmd += ["--default-search", "ytsearch1"]
    cmd += [
        "-f", "bestaudio/best",
        "-x", "--audio-format", audio_format,
        "--audio-quality", quality,
        "-o", os.path.join(AUDIO_DIR, "%(title)s.%(ext)s"),
        # Prints the final on-disk path after extraction/move, so we don't
        # have to diff a directory listing to learn the resulting filename.
        "--print", "after_move:filepath",
    ] + AUDIO_COOKIES_ARGS + YTDLP_EXTRA_ARGS + [target]
    # Search + download + audio extraction for one track comfortably fits in
    # 10 minutes even on a slow connection; if it hangs longer than that,
    # something's wrong and the job should surface as failed rather than
    # blocking the worker thread (and every queued job behind it) forever.
    stdout, stderr, rc = _run(cmd, timeout=600, env=AUDIO_ENV)

    with _audio_lock:
        job["finishedAt"] = time.time()
        if rc == 0:
            filepath = next((l for l in reversed(stdout.splitlines()) if l.strip()), None)
            job["filename"] = os.path.basename(filepath) if filepath else None
            job["status"] = "done" if filepath else "error"
            job["message"] = "" if filepath else (
                f"yt-dlp finished but did not report an output file -- {SEARCH_NO_RESULTS_HINT}"
                if not is_url else
                "yt-dlp finished but did not report an output file (the file may already exist in "
                "AUDIO_DIR from an earlier download of the same title)"
            )
        else:
            # Keep the last few lines, not just one -- a bare "HTTP Error
            # 403: Forbidden" is usually preceded by which format/client
            # yt-dlp was trying, which matters for diagnosing YouTube's
            # anti-bot blocks (see YTDLP_EXTRA_ARGS above).
            tail = [l for l in (stderr or stdout or "").strip().splitlines() if l.strip()]
            job["status"] = "error"
            job["message"] = " / ".join(tail[-3:]) if tail else f"yt-dlp exited with code {rc}"


def _audio_worker():
    while True:
        job_id, target, is_url, audio_format, quality = _audio_queue.get()
        try:
            _run_audio_download(job_id, target, is_url, audio_format, quality)
        except Exception as e:
            with _audio_lock:
                job = _audio_jobs.get(job_id)
                if job:
                    job["status"] = "error"
                    job["message"] = str(e)
                    job["finishedAt"] = time.time()
        finally:
            _audio_queue.task_done()


threading.Thread(target=_audio_worker, daemon=True).start()


def _parse_audio_batch(text):
    """Same 'Artist | Song Title' line format as yt_audio_grabber.py's --file mode."""
    tracks = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "|" not in line:
            continue
        artist, _, song = line.partition("|")
        artist, song = artist.strip()[:200], song.strip()[:200]
        if artist and song:
            tracks.append((artist, song))
    return tracks


def _normalize_format_quality(audio_format, quality):
    if audio_format not in AUDIO_FORMATS:
        audio_format = "mp3"
    quality = quality.strip() if isinstance(quality, str) else ""
    if not quality.isdigit():
        quality = "192"
    return audio_format, quality


def queue_audio_downloads(tracks, audio_format, quality):
    """tracks: (artist, song) pairs downloaded via search auto-pick (yt-dlp's
    top hit). Used by the quick single-track field and by batch mode, where
    picking each match individually would defeat the point of pasting a
    whole list. For a guaranteed-correct match on a single track, search via
    /api/audio/search first and queue the exact video with queue_audio_pick()."""
    audio_format, quality = _normalize_format_quality(audio_format, quality)
    jobs = []
    for artist, song in tracks[:AUDIO_MAX_BATCH]:
        job = _new_audio_job(f"{artist} — {song}")
        _audio_queue.put((job["id"], f"{artist} {song} audio", False, audio_format, quality))
        jobs.append(job)
    return jobs


def queue_audio_pick(video_id, title, audio_format, quality):
    audio_format, quality = _normalize_format_quality(audio_format, quality)
    job = _new_audio_job(title or video_id)
    url = f"https://www.youtube.com/watch?v={video_id}"
    _audio_queue.put((job["id"], url, True, audio_format, quality))
    return job


def get_audio_jobs():
    with _audio_lock:
        jobs = sorted(_audio_jobs.values(), key=lambda j: j["id"], reverse=True)
    return jobs[:100]


def get_audio_files():
    if not os.path.isdir(AUDIO_DIR):
        return []
    files = []
    for name in os.listdir(AUDIO_DIR):
        path = os.path.join(AUDIO_DIR, name)
        if not os.path.isfile(path):
            continue
        try:
            files.append({"filename": name, "size": os.path.getsize(path), "createdAt": os.path.getmtime(path)})
        except OSError:
            continue
    files.sort(key=lambda f: f["createdAt"], reverse=True)
    return files


def delete_audio_file(filename):
    if not filename or "/" in filename or "\\" in filename or filename in (".", ".."):
        return False, "Invalid filename"
    path = os.path.join(AUDIO_DIR, filename)
    if not os.path.isfile(path):
        return False, "File not found"
    try:
        os.remove(path)
        return True, "Deleted"
    except OSError as e:
        return False, str(e)


# ── SOMAFM RADIO PLAYER CONTROL ──────────────────────────────────────────────
# Runs helm_server.py's SomaFM widget "always on" background player: mpv,
# playing the Indie Pop Rocks stream, as a `soma-radio.service` systemd
# --user unit on this host (hyperion -- has real audio output, unlike
# popcorn, per the user). helm_server.py's Services page can't reach this
# directly: gather_services_status()/control_service() there only ever run
# systemctl/docker against the local host helm_server.py itself is on (no
# proxy hop, unlike Vault/Audio), which is popcorn, not hyperion. Rather
# than inventing a new "remote systemd" proxy just for this one unit, reuse
# the proxy tunnel that already exists to this exact host: any /api/audio/*
# path is already forwarded here verbatim by proxy_to_audio() in
# helm_server.py, so these two routes just ride along on that for free.
SOMA_RADIO_UNIT = "soma-radio.service"


def get_soma_radio_status():
    out, err, rc = _run(
        ["systemctl", "--user", "show", SOMA_RADIO_UNIT, "--property=ActiveState,SubState"],
        timeout=10,
    )
    active = "ActiveState=active" in out
    running = "SubState=running" in out
    return {"running": active and running, "raw": out or err}


def control_soma_radio(action):
    if action not in ("start", "stop", "restart"):
        return False, "Invalid action"
    _, err, rc = _run(["systemctl", "--user", action, SOMA_RADIO_UNIT], timeout=15)
    return rc == 0, (err if rc != 0 else f"{action} successful")


# ── MEDIA LIBRARY (recursive scan of /mnt/SharedStuff/Music) ────────────────
# Distinct from AUDIO_DIR above: AUDIO_DIR is only the yt-dlp download
# staging folder (flat, small). The media library is the whole music mount
# -- recursive, potentially thousands of files -- so it needs its own
# on-demand-scan-with-cache model rather than the flat os.listdir()
# get_audio_files() already does above.
MEDIA_LIBRARY_ROOT = os.environ.get("MEDIA_LIBRARY_ROOT") or "/mnt/SharedStuff/Music"
MEDIA_LIBRARY_CACHE_FILE = os.path.join(SCRIPT_DIR, "media_library_cache.json")
MEDIA_AUDIO_EXTS = (".mp3", ".m4a", ".flac", ".wav", ".opus", ".ogg", ".aac")

# ffprobe calls are I/O-bound (subprocess spawn + disk read/parse), not
# CPU-bound, so running several at once -- rather than the one-at-a-time
# loop this started as -- cuts wall-clock time on a large, mostly-uncached
# library substantially without saturating hyperion. 8 is a reasonable
# default for a personal appliance; override via env var if the underlying
# storage (e.g. a slow network mount) or CPU count warrants a different
# number.
MEDIA_SCAN_WORKERS = int(os.environ.get("MEDIA_SCAN_WORKERS", "8"))

# Bump whenever _probe_track()/_path_fallback_tags() change in a way that
# should force previously-cached entries to be re-probed rather than reused
# via the mtime-unchanged fast path in _run_library_scan() -- otherwise a
# logic fix here would silently never apply to files already in the cache
# until something else touches them.
MEDIA_SCAN_FORMAT_VERSION = 3

# Two "Artist - Album" folder-naming conventions observed in this library,
# used by _path_fallback_tags() below: one with a leading "NNNN. " index,
# one without. Both require whitespace on *both* sides of the hyphen (not
# just "-") so a bare mid-word hyphen in a band name (e.g. "AC-DC") can't be
# misread as the artist/album separator.
_NUMBERED_ALBUM_FOLDER_RE = re.compile(r"^\s*\d+\.\s*(.+?)\s+-\s+(.+?)\s*(?:\(\d{4}\))?\s*$")
_ARTIST_ALBUM_FOLDER_RE = re.compile(r"^(.+?)\s+-\s+(.+?)\s*(?:\(\d{4}\))?\s*$")

# ffprobe (not a guarded mutagen import) reads tag metadata: unlike zxcvbn in
# vault_api.py, ffprobe is already a hard dependency of this exact host for
# yt-dlp's audio extraction, so there's nothing to gracefully degrade
# without -- this follows the YT_DLP_BIN/_find_binary() shell-out precedent
# above, not the guarded-optional-import one.
FFPROBE_BIN = _find_binary("ffprobe", "FFPROBE_BIN")

_library_lock = threading.Lock()
_library_cache = None  # {"scannedAt": float|None, "tracks": [...]}, lazily loaded
_library_scan_state = {"scanning": False, "scannedCount": 0, "totalCount": 0, "startedAt": None}


def _load_library_cache():
    global _library_cache
    if _library_cache is not None:
        return _library_cache
    if os.path.exists(MEDIA_LIBRARY_CACHE_FILE):
        try:
            with open(MEDIA_LIBRARY_CACHE_FILE) as f:
                _library_cache = json.load(f)
                return _library_cache
        except (json.JSONDecodeError, OSError):
            pass
    _library_cache = {"scannedAt": None, "tracks": []}
    return _library_cache


def _save_library_cache(cache):
    global _library_cache
    tmp_path = MEDIA_LIBRARY_CACHE_FILE + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(cache, f)
    os.replace(tmp_path, MEDIA_LIBRARY_CACHE_FILE)
    _library_cache = cache


def get_media_library():
    cache = _load_library_cache()
    return {"scannedAt": cache.get("scannedAt"), "tracks": cache.get("tracks", [])}


def get_library_scan_status():
    with _library_lock:
        return dict(_library_scan_state)


def _probe_track(path):
    """Shell out to ffprobe for artist/title/album/duration. Never raises --
    a probe failure (corrupt file, ffprobe missing, no tags at all) just
    means filename-derived, metadata-light display, matching the rest of
    this feature's tolerance for an imperfect real-world music folder."""
    title_fallback = os.path.splitext(os.path.basename(path))[0]
    result = {"artist": "", "title": title_fallback, "album": "", "duration": None}
    stdout, stderr, rc = _run(
        [FFPROBE_BIN, "-v", "quiet", "-print_format", "json", "-show_format", path],
        timeout=15,
    )
    if rc != 0 or not stdout:
        return result
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return result
    fmt = data.get("format", {})
    # Tag casing is inconsistent across files/encoders (Artist vs artist vs
    # ARTIST) -- normalize to lowercase keys before reading.
    tags = {k.lower(): v for k, v in (fmt.get("tags") or {}).items()}
    result["artist"] = tags.get("artist") or ""
    result["title"] = tags.get("title") or title_fallback
    result["album"] = tags.get("album") or ""
    try:
        result["duration"] = float(fmt["duration"]) if fmt.get("duration") else None
    except (TypeError, ValueError):
        result["duration"] = None
    return result


def _path_fallback_tags(relpath):
    """Best-effort artist/album guess from directory structure, used only to
    fill in whatever ffprobe found no tag for -- never overrides real
    embedded metadata. This library mixes a few conventions:
      - Artist/[...intermediate.../]Album/track.mp3 (nested, 2+ directory
        levels) -- trust the real nesting: top-level directory is the
        artist, the file's immediate parent directory is the album.
      - "NNNN. Artist - Album (Year)/track.mp3" or "Artist - Album
        (Year)/track.mp3" (a single folder combining artist and album, with
        or without a numeric index) -- only tried when there's no nested
        album directory to trust instead, so a real Artist/Album structure
        always wins over guessing at a " - " split in a top-level name.
    Files with no directory at all (shouldn't happen given
    MEDIA_LIBRARY_ROOT is itself a directory, but be defensive) get "", ""."""
    dirs = relpath.split(os.sep)[:-1]
    if not dirs:
        return "", ""
    top = dirs[0]
    m = _NUMBERED_ALBUM_FOLDER_RE.match(top)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    if len(dirs) > 1:
        return top, dirs[-1]
    m = _ARTIST_ALBUM_FOLDER_RE.match(top)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return top, ""


def _walk_media_library():
    """Yield (relpath, abspath, mtime) for every audio file under
    MEDIA_LIBRARY_ROOT. relpath is used as the track's stable id -- see
    _run_library_scan()."""
    if not os.path.isdir(MEDIA_LIBRARY_ROOT):
        return
    for dirpath, _dirnames, filenames in os.walk(MEDIA_LIBRARY_ROOT):
        for name in filenames:
            if not name.lower().endswith(MEDIA_AUDIO_EXTS):
                continue
            abspath = os.path.join(dirpath, name)
            relpath = os.path.relpath(abspath, MEDIA_LIBRARY_ROOT)
            try:
                mtime = os.path.getmtime(abspath)
            except OSError:
                continue
            yield relpath, abspath, mtime


def _probe_entry(item):
    index, relpath, abspath, mtime = item
    meta = _probe_track(abspath)
    if not meta["artist"] or not meta["album"]:
        fallback_artist, fallback_album = _path_fallback_tags(relpath)
        meta["artist"] = meta["artist"] or fallback_artist
        meta["album"] = meta["album"] or fallback_album
    return index, {
        "id": relpath,
        "path": relpath,
        "filename": os.path.basename(relpath),
        "artist": meta["artist"],
        "title": meta["title"],
        "album": meta["album"],
        "duration": meta["duration"],
        "mtime": mtime,
    }


def _run_library_scan():
    """Runs in a background thread (see start_library_rescan()). Reuses the
    previous cache entry for any file whose mtime hasn't changed, so
    re-scanning after adding a handful of new tracks to a large library only
    probes the new ones, not the whole tree. The tree walk itself (just stat
    calls) is fast even for tens of thousands of files -- it's ffprobe that's
    expensive, so only the probing step is parallelized across
    MEDIA_SCAN_WORKERS threads."""
    try:
        previous = _load_library_cache()
        if previous.get("formatVersion") == MEDIA_SCAN_FORMAT_VERSION:
            previous_by_path = {t["path"]: t for t in previous.get("tracks", [])}
        else:
            # A _probe_track()/_path_fallback_tags() change means fields on
            # already-cached entries may be stale/incomplete -- treat this
            # as "nothing cached" so every file gets re-probed once, rather
            # than silently keeping old data forever via the mtime-match
            # fast path below.
            previous_by_path = {}

        # Walk up front so scannedCount/totalCount reflect real progress
        # against the actual file count, not "however many discovered so far".
        entries = list(_walk_media_library())
        with _library_lock:
            _library_scan_state["totalCount"] = len(entries)

        tracks = [None] * len(entries)
        to_probe = []
        for i, (relpath, abspath, mtime) in enumerate(entries):
            prev = previous_by_path.get(relpath)
            if prev is not None and prev.get("mtime") == mtime:
                tracks[i] = prev
            else:
                to_probe.append((i, relpath, abspath, mtime))

        count = len(entries) - len(to_probe)
        with _library_lock:
            _library_scan_state["scannedCount"] = count

        if to_probe:
            with concurrent.futures.ThreadPoolExecutor(max_workers=MEDIA_SCAN_WORKERS) as pool:
                futures = [pool.submit(_probe_entry, item) for item in to_probe]
                for future in concurrent.futures.as_completed(futures):
                    i, track = future.result()
                    tracks[i] = track
                    count += 1
                    with _library_lock:
                        _library_scan_state["scannedCount"] = count

        tracks.sort(key=lambda t: (t.get("artist") or "", t.get("album") or "", t.get("path") or ""))
        _save_library_cache({"scannedAt": time.time(), "formatVersion": MEDIA_SCAN_FORMAT_VERSION, "tracks": tracks})
    finally:
        with _library_lock:
            _library_scan_state["scanning"] = False


def start_library_rescan():
    with _library_lock:
        if _library_scan_state["scanning"]:
            return False, "Scan already in progress"
        _library_scan_state["scanning"] = True
        _library_scan_state["scannedCount"] = 0
        _library_scan_state["totalCount"] = 0
        _library_scan_state["startedAt"] = time.time()
    threading.Thread(target=_run_library_scan, daemon=True).start()
    return True, "Scan started"


def _library_track_by_id(track_id):
    for t in get_media_library()["tracks"]:
        if t["id"] == track_id:
            return t
    return None


def _resolve_track_path(track_id):
    t = _library_track_by_id(track_id)
    if not t:
        return None
    return os.path.join(MEDIA_LIBRARY_ROOT, t["path"])


# ── MEDIA PLAYLISTS ───────────────────────────────────────────────────────────
# Server-side so playlists are identical from any device, same reasoning as
# marks_state.json for bookmarks -- atomic tmp+replace writes, same pattern
# as _write_state_to_disk in helm_server.py. trackIds reference the same
# relative-path id scheme the library scan produces above; there's only one
# identifier scheme in this whole feature, nothing to reconcile.
MEDIA_PLAYLISTS_FILE = os.path.join(SCRIPT_DIR, "media_playlists.json")
_playlists_lock = threading.Lock()


def _load_playlists_data():
    if not os.path.exists(MEDIA_PLAYLISTS_FILE):
        return {"playlists": []}
    try:
        with open(MEDIA_PLAYLISTS_FILE) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {"playlists": []}


def _save_playlists_data(data):
    tmp_path = MEDIA_PLAYLISTS_FILE + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(data, f)
    os.replace(tmp_path, MEDIA_PLAYLISTS_FILE)


def _find_playlist(data, playlist_id):
    for pl in data["playlists"]:
        if pl["id"] == playlist_id:
            return pl
    return None


def get_playlists():
    with _playlists_lock:
        return _load_playlists_data()["playlists"]


def create_playlist(name):
    name = (name or "").strip()[:200]
    if not name:
        return None, "Name required"
    with _playlists_lock:
        data = _load_playlists_data()
        now = time.time()
        pl = {"id": uuid.uuid4().hex[:12], "name": name, "trackIds": [], "createdAt": now, "updatedAt": now}
        data["playlists"].append(pl)
        _save_playlists_data(data)
    return pl, None


def rename_playlist(playlist_id, name):
    name = (name or "").strip()[:200]
    if not name:
        return False, "Name required"
    with _playlists_lock:
        data = _load_playlists_data()
        pl = _find_playlist(data, playlist_id)
        if not pl:
            return False, "Playlist not found"
        pl["name"] = name
        pl["updatedAt"] = time.time()
        _save_playlists_data(data)
    return True, "Renamed"


def delete_playlist(playlist_id):
    with _playlists_lock:
        data = _load_playlists_data()
        before = len(data["playlists"])
        data["playlists"] = [p for p in data["playlists"] if p["id"] != playlist_id]
        if len(data["playlists"]) == before:
            return False, "Playlist not found"
        _save_playlists_data(data)
    return True, "Deleted"


def add_track_to_playlist(playlist_id, track_id):
    if not track_id:
        return False, "trackId required"
    with _playlists_lock:
        data = _load_playlists_data()
        pl = _find_playlist(data, playlist_id)
        if not pl:
            return False, "Playlist not found"
        pl["trackIds"].append(track_id)
        pl["updatedAt"] = time.time()
        _save_playlists_data(data)
    return True, "Added"


def remove_track_from_playlist(playlist_id, index):
    with _playlists_lock:
        data = _load_playlists_data()
        pl = _find_playlist(data, playlist_id)
        if not pl:
            return False, "Playlist not found"
        if not isinstance(index, int) or index < 0 or index >= len(pl["trackIds"]):
            return False, "Invalid index"
        pl["trackIds"].pop(index)
        pl["updatedAt"] = time.time()
        _save_playlists_data(data)
    return True, "Removed"


def reorder_playlist(playlist_id, track_ids):
    with _playlists_lock:
        data = _load_playlists_data()
        pl = _find_playlist(data, playlist_id)
        if not pl:
            return False, "Playlist not found"
        if not isinstance(track_ids, list) or sorted(track_ids) != sorted(pl["trackIds"]):
            return False, "trackIds must be a reordering of the existing playlist"
        pl["trackIds"] = track_ids
        pl["updatedAt"] = time.time()
        _save_playlists_data(data)
    return True, "Reordered"


# ── MEDIA PLAYER CONTROL (mpv IPC) ───────────────────────────────────────────
# Thin HTTP wrappers over mpv_ipc.py, driving the always-idle mpv instance
# started by mpv-player.service (see that file). Auto-advance between queued
# tracks is handled by mpv's own internal playlist, not reimplemented here --
# see mpv_ipc.py's docstring.
_player_state = {"shuffle": False}  # single shared playback session -- no per-client state needed


def get_player_status():
    status, err = mpv_ipc.mpv_status()
    soma = get_soma_radio_status()
    if err:
        return {
            "playing": False, "paused": True, "trackId": None, "title": "", "artist": "",
            "position": 0, "duration": 0, "volume": 100, "shuffle": _player_state["shuffle"],
            "queue": [], "queuePos": -1, "somaRadioRunning": soma.get("running", False),
            "error": err,
        }

    playlist, _ = mpv_ipc.get_playlist()
    queue, queue_pos = [], -1
    if isinstance(playlist, list):
        for i, item in enumerate(playlist):
            abspath = item.get("filename", "")
            try:
                relpath = os.path.relpath(abspath, MEDIA_LIBRARY_ROOT)
            except ValueError:
                relpath = abspath
            queue.append(relpath)
            if item.get("current"):
                queue_pos = i

    path = status.get("path")
    track_id = None
    if path:
        try:
            track_id = os.path.relpath(path, MEDIA_LIBRARY_ROOT)
        except ValueError:
            track_id = None
    track = _library_track_by_id(track_id) if track_id else None
    idle = bool(status.get("idle-active"))

    return {
        "playing": bool(path) and not idle,
        "paused": bool(status.get("pause")) if status.get("pause") is not None else True,
        "trackId": track_id,
        "title": (track or {}).get("title") or status.get("media-title") or "",
        "artist": (track or {}).get("artist") or "",
        "position": status.get("time-pos") or 0,
        "duration": status.get("duration") or (track or {}).get("duration") or 0,
        "volume": status.get("volume") if status.get("volume") is not None else 100,
        "shuffle": _player_state["shuffle"],
        "queue": queue,
        "queuePos": queue_pos,
        "somaRadioRunning": soma.get("running", False),
    }


def resolve_play_request(body):
    """Turn a /player/play body ({trackId} or {trackIds} or {playlistId})
    into an ordered list of library track ids."""
    if body.get("trackId"):
        return [body["trackId"]], None
    if body.get("trackIds"):
        ids = body["trackIds"]
        if not isinstance(ids, list) or not ids:
            return None, "trackIds must be a non-empty list"
        return ids, None
    if body.get("playlistId"):
        with _playlists_lock:
            pl = _find_playlist(_load_playlists_data(), body["playlistId"])
        if not pl:
            return None, "Playlist not found"
        if not pl["trackIds"]:
            return None, "Playlist is empty"
        return list(pl["trackIds"]), None
    return None, "Provide trackId, trackIds, or playlistId"


def player_play(track_ids):
    paths = [p for p in (_resolve_track_path(tid) for tid in track_ids) if p and os.path.isfile(p)]
    if not paths:
        return False, "No playable tracks found"
    if _player_state["shuffle"] and len(paths) > 1:
        random.shuffle(paths)
    ok, err = mpv_ipc.mpv_loadfile(paths[0], mode="replace")
    if not ok:
        return False, err
    for p in paths[1:]:
        mpv_ipc.mpv_loadfile(p, mode="append")
    # mpv's `pause` property is sticky across loadfile -- it is NOT reset
    # just because a new file was loaded. If mpv was last left paused (e.g.
    # after a `stop`), loading a new file here would otherwise sit there
    # paused until something else explicitly resumes it. "Play" should mean
    # play, so force pause off.
    mpv_ipc.mpv_set_property("pause", False)
    return True, "Playing"


def player_set_shuffle(enabled):
    """Shuffle is tracked as server-side state and applied at load time
    (player_play above), not via mpv's own `shuffle` command -- avoids
    having to reconcile mpv's internal shuffled order with what the
    frontend displays as "queue". Toggling mid-playback reshuffles only the
    not-yet-played remainder of the current queue; the current track is
    left playing undisturbed."""
    enabled = bool(enabled)
    _player_state["shuffle"] = enabled
    if not enabled:
        return True, "Shuffle off"

    playlist, err = mpv_ipc.get_playlist()
    if err or not isinstance(playlist, list):
        return True, "Shuffle on (nothing queued to reshuffle yet)"
    current_idx = next((i for i, item in enumerate(playlist) if item.get("current")), None)
    if current_idx is None or current_idx >= len(playlist) - 1:
        return True, "Shuffle on"

    remaining = [item.get("filename") for item in playlist[current_idx + 1:]]
    random.shuffle(remaining)
    for _ in remaining:
        mpv_ipc.mpv_command("playlist-remove", current_idx + 1)
    for filename in remaining:
        mpv_ipc.mpv_loadfile(filename, mode="append")
    return True, "Shuffle on, queue reshuffled"


def player_queue_add(track_id):
    path = _resolve_track_path(track_id)
    if not path or not os.path.isfile(path):
        return False, "Track not found"
    return mpv_ipc.mpv_loadfile(path, mode="append-play")


class AudioHandler(BaseHTTPRequestHandler):

    def _check_auth(self):
        if AUDIO_TOKEN is None:
            self.send_json(503, {"error": "Server not configured: missing audio_token.txt"})
            return False
        supplied = self.headers.get("X-Audio-Token", "")
        if supplied != AUDIO_TOKEN:
            self.send_json(401, {"error": "Invalid or missing audio token"})
            return False
        return True

    def send_json(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self):
        # Shared by the media-library/playlists/player routes below, which
        # have enough call sites that repeating the inline try/except here
        # (as the original download/download-pick/radio routes above do)
        # would be more duplication than it's worth.
        length = int(self.headers.get("Content-Length", 0))
        try:
            return json.loads(self.rfile.read(length)) if length else {}
        except Exception:
            return {}

    def do_GET(self):
        if not self._check_auth():
            return
        parsed = urlparse(self.path)

        if parsed.path == "/api/audio/search":
            qs = parse_qs(parsed.query)
            query = (qs.get("q", [""])[0]).strip()[:200]
            if not query:
                self.send_json(400, {"error": "Missing q parameter"})
                return
            results, err = search_audio_candidates(query)
            if err:
                self.send_json(502, {"error": err})
                return
            self.send_json(200, {"results": results})
            return

        if parsed.path == "/api/audio/jobs":
            self.send_json(200, {"jobs": get_audio_jobs()})
            return

        if parsed.path == "/api/audio/files":
            self.send_json(200, {"files": get_audio_files()})
            return

        if parsed.path == "/api/audio/radio/status":
            self.send_json(200, get_soma_radio_status())
            return

        if parsed.path == "/api/audio/library":
            self.send_json(200, get_media_library())
            return

        if parsed.path == "/api/audio/library/scan-status":
            self.send_json(200, get_library_scan_status())
            return

        if parsed.path == "/api/audio/playlists":
            self.send_json(200, {"playlists": get_playlists()})
            return

        if parsed.path == "/api/audio/player/status":
            self.send_json(200, get_player_status())
            return

        if parsed.path.startswith("/api/audio/files/"):
            filename = unquote(parsed.path[len("/api/audio/files/"):])
            # Safety: reject any path traversal attempts (same pattern as
            # helm_server.py's /api/backups/<file>); unlike backups these
            # filenames come from arbitrary video titles so they need
            # unquoting first.
            if "/" in filename or "\\" in filename or filename in ("", ".", ".."):
                self.send_json(400, {"error": "Invalid filename"})
                return
            path = os.path.join(AUDIO_DIR, filename)
            if not os.path.isfile(path):
                self.send_json(404, {"error": "File not found"})
                return
            ctype, _ = mimetypes.guess_type(path)
            with open(path, "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", ctype or "application/octet-stream")
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.end_headers()
            self.wfile.write(data)
            return

        self.send_json(404, {"error": "Not found"})

    def do_POST(self):
        if not self._check_auth():
            return
        parsed = urlparse(self.path)

        if parsed.path == "/api/audio/download":
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length)) if length else {}
            except Exception:
                body = {}
            artist = (body.get("artist") or "").strip()[:200]
            song = (body.get("song") or "").strip()[:200]
            tracks = [(artist, song)] if artist and song else []
            tracks.extend(_parse_audio_batch(body.get("batchText") or ""))
            if not tracks:
                self.send_json(400, {"error": "Provide artist+song and/or batchText"})
                return
            jobs = queue_audio_downloads(tracks, body.get("format", "mp3"), str(body.get("quality", "192")))
            self.send_json(200, {"jobs": jobs})
            return

        if parsed.path == "/api/audio/download-pick":
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length)) if length else {}
            except Exception:
                body = {}
            video_id = (body.get("videoId") or "").strip()
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", video_id or ""):
                self.send_json(400, {"error": "Invalid or missing videoId"})
                return
            title = (body.get("title") or "").strip()[:200]
            job = queue_audio_pick(video_id, title, body.get("format", "mp3"), str(body.get("quality", "192")))
            self.send_json(200, {"jobs": [job]})
            return

        if parsed.path == "/api/audio/files/delete":
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length)) if length else {}
            except Exception:
                body = {}
            ok, msg = delete_audio_file(body.get("filename", ""))
            self.send_json(200 if ok else 400, {"ok": ok, "message": msg})
            return

        if parsed.path == "/api/audio/radio/control":
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length)) if length else {}
            except Exception:
                body = {}
            ok, msg = control_soma_radio(body.get("action", ""))
            self.send_json(200 if ok else 400, {"ok": ok, "message": msg})
            return

        if parsed.path == "/api/audio/library/rescan":
            ok, msg = start_library_rescan()
            self.send_json(200 if ok else 409, {"ok": ok, "message": msg})
            return

        if parsed.path == "/api/audio/playlists/create":
            body = self._read_json_body()
            pl, err = create_playlist(body.get("name", ""))
            self.send_json(200 if pl else 400, {"playlist": pl, "error": err})
            return

        if parsed.path == "/api/audio/playlists/rename":
            body = self._read_json_body()
            ok, msg = rename_playlist(body.get("id", ""), body.get("name", ""))
            self.send_json(200 if ok else 400, {"ok": ok, "message": msg})
            return

        if parsed.path == "/api/audio/playlists/delete":
            body = self._read_json_body()
            ok, msg = delete_playlist(body.get("id", ""))
            self.send_json(200 if ok else 400, {"ok": ok, "message": msg})
            return

        if parsed.path == "/api/audio/playlists/add-track":
            body = self._read_json_body()
            ok, msg = add_track_to_playlist(body.get("id", ""), body.get("trackId", ""))
            self.send_json(200 if ok else 400, {"ok": ok, "message": msg})
            return

        if parsed.path == "/api/audio/playlists/remove-track":
            body = self._read_json_body()
            ok, msg = remove_track_from_playlist(body.get("id", ""), body.get("index"))
            self.send_json(200 if ok else 400, {"ok": ok, "message": msg})
            return

        if parsed.path == "/api/audio/playlists/reorder":
            body = self._read_json_body()
            ok, msg = reorder_playlist(body.get("id", ""), body.get("trackIds"))
            self.send_json(200 if ok else 400, {"ok": ok, "message": msg})
            return

        if parsed.path == "/api/audio/player/play":
            body = self._read_json_body()
            track_ids, err = resolve_play_request(body)
            if err:
                self.send_json(400, {"ok": False, "message": err})
                return
            ok, msg = player_play(track_ids)
            self.send_json(200 if ok else 400, {"ok": ok, "message": msg})
            return

        if parsed.path == "/api/audio/player/pause":
            ok, err = mpv_ipc.mpv_set_property("pause", True)
            self.send_json(200 if ok else 400, {"ok": ok, "message": err or "Paused"})
            return

        if parsed.path == "/api/audio/player/resume":
            ok, err = mpv_ipc.mpv_set_property("pause", False)
            self.send_json(200 if ok else 400, {"ok": ok, "message": err or "Resumed"})
            return

        if parsed.path == "/api/audio/player/seek":
            body = self._read_json_body()
            try:
                position = float(body.get("position"))
            except (TypeError, ValueError):
                self.send_json(400, {"ok": False, "message": "Invalid position"})
                return
            ok, err = mpv_ipc.mpv_command("seek", position, "absolute")
            self.send_json(200 if ok else 400, {"ok": ok, "message": err or "Seeked"})
            return

        if parsed.path == "/api/audio/player/next":
            ok, err = mpv_ipc.mpv_command("playlist-next")
            self.send_json(200 if ok else 400, {"ok": ok, "message": err or "Next"})
            return

        if parsed.path == "/api/audio/player/prev":
            ok, err = mpv_ipc.mpv_command("playlist-prev")
            self.send_json(200 if ok else 400, {"ok": ok, "message": err or "Prev"})
            return

        if parsed.path == "/api/audio/player/volume":
            body = self._read_json_body()
            try:
                volume = max(0, min(100, int(body.get("volume"))))
            except (TypeError, ValueError):
                self.send_json(400, {"ok": False, "message": "Invalid volume"})
                return
            ok, err = mpv_ipc.mpv_set_property("volume", volume)
            self.send_json(200 if ok else 400, {"ok": ok, "message": err or "Volume set"})
            return

        if parsed.path == "/api/audio/player/shuffle":
            body = self._read_json_body()
            ok, msg = player_set_shuffle(body.get("enabled", False))
            self.send_json(200 if ok else 400, {"ok": ok, "message": msg})
            return

        if parsed.path == "/api/audio/player/queue/add":
            body = self._read_json_body()
            ok, err = player_queue_add(body.get("trackId", ""))
            self.send_json(200 if ok else 400, {"ok": ok, "message": err or "Queued"})
            return

        if parsed.path == "/api/audio/player/queue/remove":
            body = self._read_json_body()
            index = body.get("index")
            if not isinstance(index, int) or index < 0:
                self.send_json(400, {"ok": False, "message": "Invalid index"})
                return
            ok, err = mpv_ipc.mpv_command("playlist-remove", index)
            self.send_json(200 if ok else 400, {"ok": ok, "message": err or "Removed"})
            return

        if parsed.path == "/api/audio/player/queue/reorder":
            body = self._read_json_body()
            index, target_index = body.get("index"), body.get("targetIndex")
            if not isinstance(index, int) or not isinstance(target_index, int):
                self.send_json(400, {"ok": False, "message": "Invalid index"})
                return
            ok, err = mpv_ipc.mpv_command("playlist-move", index, target_index)
            self.send_json(200 if ok else 400, {"ok": ok, "message": err or "Reordered"})
            return

        self.send_json(404, {"error": "Not found"})

    def log_message(self, format, *args):
        # Quieter logging -- matches vault_server.py's precedent.
        pass


def main():
    server = ThreadingHTTPServer(("0.0.0.0", PORT), AudioHandler)
    print(f"Audio Grabber server running at http://0.0.0.0:{PORT}")
    print(f"Downloads saved to {AUDIO_DIR} (using {YT_DLP_BIN})")
    print(f"Subprocess PATH: {AUDIO_ENV['PATH']}")
    print(f"Cookies: {'using ' + AUDIO_COOKIES_FILE if AUDIO_COOKIES_ARGS else 'none found at ' + AUDIO_COOKIES_FILE + ' (unauthenticated requests -- more likely to hit 403s)'}")
    if AUDIO_TOKEN is None:
        print("!! No audio_token.txt found -- all requests will be rejected until one exists.")
    print("This should only be reachable from popcorn on your LAN, not the public internet.")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping.")
        server.shutdown()


if __name__ == "__main__":
    main()
