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
import json
import mimetypes
import queue as _queue
import re
import shlex
import shutil
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote

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
