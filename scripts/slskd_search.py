#!/usr/bin/env python3
"""slskd search client: run a query, filter the noise, return candidates.

The filtering matters more than it sounds. A bare Soulseek text search is a
substring match, so "beach house" returns Proto House and Italo Disco
compilations alongside Beach House albums — measured on a live search where the
single largest response (353 files) was entirely the wrong genre. Those results
are real and downloadable; they are simply not what was asked for.

So candidates are ranked, not filtered, and the caller decides. Junk is ranked
last rather than dropped, because a hard filter that throws away a real album
is worse than showing one that is obviously wrong.

API notes verified against a live 0.26.0.0 instance:
  POST /api/v0/searches                 -> {id}; accepts per-request
                                          responseLimit / fileLimit /
                                          minimumResponseFileCount /
                                          searchTimeout
  GET  /api/v0/searches/{id}            -> record; note `responses` is [] here
  GET  /api/v0/searches/{id}/responses  -> the ACTUAL per-peer results.
                                          Reading `responses` off the parent
                                          record instead makes a working
                                          search look empty (it reports
                                          responseCount=300 alongside
                                          responses=[]).
  POST /api/v0/transfers/downloads      -> enqueue
  GET  /api/v0/transfers/downloads      -> list

Never prints the API key.
"""
import json
import os
import re
import ssl
import subprocess
import time
import urllib.error
import urllib.request

SLSKD = os.environ.get("SLSKD_URL", "http://100.77.126.57:5030")
CONFIG = os.environ.get("SLSKD_CONFIG", "/home/isaboo/soulseek/data/slskd.yml")

# Audio extensions worth surfacing. Soulseek shares are full of .nfo, .cue,
# .log, .jpg and archive clutter; those are never the thing being searched for.
AUDIO_EXT = {
    "mp3", "flac", "m4a", "aac", "ogg", "opus", "wav", "aiff", "ape", "wv",
    "alac", "mpc", "shn", "tta", "dsf",
}
# Extensions that accompany music but are not music.
JUNK_EXT = {"nfo", "cue", "log", "jpg", "jpeg", "png", "gif", "txt", "m3u",
            "pls", "sfv", "accurip", "accurip", "nzb", "par2", "r00", "sfv"}

# Bitrates above this are lossless mislabelled as MP3 (a 320kbps FLAC is a
# common scene artifact), and below this is a low-bitrate transcode.
SUSPICIOUS_MAX_MP3_KBPS = 320

# Lossless container extensions. Verified against live search responses: slskd's
# SearchFile objects carry NO bitRate field at all, so quality has to be derived
# from the extension plus bitDepth/sampleRate. (An earlier version read
# `bitRate` and scored every candidate 0kbps, which silently flattened the
# quality term to a constant.)
LOSSLESS_EXT = {"flac", "alac", "ape", "wv", "shn", "tta", "aiff", "aif",
                "wav", "dsf", "dff"}
LOSSLESS_HI_RES_SAMPLE_RATES = {88200, 96000, 176400, 192000, 384000}

_STOPWORDS = {
    "the", "a", "an", "of", "and", "feat", "featuring", "ft",
}


def api_key(config=CONFIG):
    """Read the API key from the config. Never returns it to a log."""
    try:
        out = subprocess.run(
            ["grep", "-A5", "^ web:", config],
            capture_output=True, text=True, timeout=20).stdout
    except Exception:
        return None
    for line in out.split("\n"):
        s = line.strip()
        if s.startswith("key:"):
            return s.split(":", 1)[1].strip()
    return None


class SlskdError(RuntimeError):
    pass


class Client:
    def __init__(self, base=SLSKD, key=None, timeout=45):
        self.base = base.rstrip("/")
        self.key = key or api_key()
        self.timeout = timeout
        if not self.key:
            raise SlskdError("no API key found in " + CONFIG)

    def _call(self, path, method="GET", body=None):
        headers = {"X-API-Key": self.key, "Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(
            self.base + path, headers=headers, data=data, method=method)
        ctx = None
        if self.base.startswith("https://"):
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=ctx) as r:
                raw = r.read().decode()
                if not raw:
                    return None
                try:
                    return json.loads(raw)
                except Exception:
                    return raw
        except urllib.error.HTTPError as e:
            raise SlskdError(f"{method} {path} -> HTTP {e.code}: "
                             f"{e.read().decode()[:200]}")
        except Exception as e:
            raise SlskdError(f"{method} {path} -> {e}")

    # ── status ──────────────────────────────────────────────────────────────

    def status(self):
        app = self._call("/api/v0/application") or {}
        priv = (app.get("user") or {}).get("privileges") or {}
        shares = app.get("shares") or {}
        return {
            "version": (app.get("version") or {}).get("current"),
            "server": (app.get("server") or {}).get("address"),
            "privileged": priv.get("isPrivileged"),
            "shares_dirs": shares.get("directories"),
            "shares_files": shares.get("files"),
        }

    # ── search ──────────────────────────────────────────────────────────────

    def search(self, query, response_limit=500, file_limit=10000,
               min_response_files=2, search_timeout_ms=30000,
               poll_seconds=5, max_wait=90):
        """Start a search and wait for it. Returns (search_id, record)."""
        body = {
            "searchText": query,
            "responseLimit": response_limit,
            "fileLimit": file_limit,
            "minimumResponseFileCount": min_response_files,
            "searchTimeout": search_timeout_ms,
        }
        started = self._call("/api/v0/searches", method="POST", body=body)
        sid = None
        if isinstance(started, dict):
            sid = started.get("id")
        if not sid:
            raise SlskdError(f"could not get a search id from {started!r}")

        deadline = time.time() + max_wait
        rec = None
        while time.time() < deadline:
            time.sleep(poll_seconds)
            rec = self._call(f"/api/v0/searches/{sid}") or {}
            state = rec.get("state") or ""
            # Terminal states all carry a comma-suffixed reason
            # ("Completed", "Completed, FileLimitReached", ...).
            head = state.split(",")[0].strip()
            if head in ("Completed", "Cancelled", "Error", "TimedOut"):
                break
        return sid, rec or {}

    def responses(self, search_id):
        """The actual per-peer results. NOT the parent record's `responses`."""
        data = self._call(f"/api/v0/searches/{search_id}/responses")
        return data if isinstance(data, list) else []

    # ── downloads ───────────────────────────────────────────────────────────

    def enqueue(self, username, files):
        """Queue downloads. `files` is a list of SearchFile dicts."""
        return self._call("/api/v0/transfers/downloads", method="POST",
                          body={"username": username, "files": files})

    def downloads(self):
        d = self._call("/api/v0/transfers/downloads")
        return d if isinstance(d, list) else []


# ── filtering / ranking ─────────────────────────────────────────────────────

def _tokens(s):
    return {t for t in re.split(r"[^a-z0-9]+", (s or "").lower()) if t and t not in _STOPWORDS}


# Path components that are structural noise rather than artist/album names.
# "shared\_TAGGED\..." and "@@mfapl\Music (320)\..." both appear constantly.
_NOISE_SEGMENTS = {
    "shared", "_tagged", "music", "downloads", "incomplete", "complete",
    "albums", "album", "artists", "artist", "songs", "song", "tracks", "track",
    "lossless", "flac", "mp3", "cd", "dvd", "disc", "discography",
    # "Music (320)", "Music (lossless)", "MP3 320" — quality-labelled parents.
    "music (320)", "music (lossless)", "music (flac)", "music (ape)",
    "mp3 320", "flac 320", "new music",
}


def _clean_segment(s):
    """Trim decoration from a path segment: leading track numbers, years."""
    s = s.strip()
    # "01 - Myth" -> "Myth"; "01. Dark Spring" -> "Dark Spring"
    s = re.sub(r"^\d{1,3}\s*[-._)\]]\s*", "", s)
    # A trailing or leading year in brackets/parens.
    s = re.sub(r"^[\(\[]\d{4}[\)\]]\s*", "", s)
    s = re.sub(r"\s*[\(\[]\d{4}[\)\]]$", "", s)
    return s.strip()


def _looks_like_filename(seg):
    """True if this segment is really a file name, not a folder."""
    return bool(re.search(r"\.[A-Za-z0-9]{2,4}$", seg.strip()))


def _is_disc_folder(seg):
    """True if this directory names a disc/CD, not an album.

    Multi-disc releases put a sub-folder between the album and its tracks
    ("Up in Flames (2003)/Digital Media 02/01 - Cherrybomb.flac"). Treating that
    as the album splits one release across several groups.
    """
    s = (seg or "").strip().lower()
    patterns = (
        r"^(cd|disc|disk|dvd|part|vol|volume)\s*[-_. ]*\d+$",
        r"^digital\s+media\s*\d+$",
        r"^\d+$",                      # bare "1", "2" as a disc folder
    )
    return any(re.match(p, s) for p in patterns)


def guess_artist_album(path):
    """Best-effort (artist, album) from a Soulseek share path.

    Share layouts are inconsistent, and the observed shapes are:
      music\\Caribou\\Andorra (2007)\\01 - Melody Day.flac
          -> ("Caribou", "Andorra")            depth 4, album is a real folder
      shared\\Caribou - Suddenly\\Caribou - Suddenly - 01 Sister.flac
          -> ("Caribou - Suddenly", "Suddenly") depth 3, album in the FOLDER
      Caribou - Suddenly - 01 Sister.flac
          -> ("Caribou - Suddenly", "Suddenly") flat, album in the FILENAME
      @@mfapl\\Music (320)\\Beach House\\7\\01 Dark Spring.mp3
          -> ("Beach House", "7")            noise stripped, disc folder kept

    Taking a fixed component index split the "Caribou - Suddenly" case into
    one "album" per track, because the filename repeats the album title. So:
    drop noise and track-numbered segments, then treat the last remaining
    directory as the album — and if there is no directory at all, recover the
    album from the filename pattern "Artist - Album - NN Track".
    """
    parts = [p for p in re.split(r"[\\/]+", path or "") if p.strip()]
    if not parts:
        return "", ""

    parts = [p for p in parts if p.lower() not in _NOISE_SEGMENTS
             and not p.lower().startswith("@@")]

    # Drop the filename; we want the directories that precede it.
    dirs = parts[:-1] if len(parts) > 1 else []
    dirs = [_clean_segment(d) for d in dirs]
    dirs = [d for d in dirs if d and not _looks_like_filename(d)]

    filename = parts[-1] if parts else ""
    stem = re.sub(r"\.[A-Za-z0-9]{2,4}$", "", filename).strip()

    # A disc sub-folder ("Digital Media 02", "CD1", "Disc 2") sits between the
    # album and the tracks and is not itself the album. Detect and drop it so
    # "music\Caribou\Up in Flames (2003)\Digital Media 02\track.flac" groups as
    # one album rather than per-disc.
    if len(dirs) >= 2 and _is_disc_folder(dirs[-1]):
        dirs = dirs[:-1]

    if dirs:
        # The deepest directory is the album; the one above it is the artist.
        album = dirs[-1]
        artist = dirs[-2] if len(dirs) >= 2 else ""

        # A single-folder share with an unnumbered track title
        # ("_TAGGED\Beach House\Equal Mind.mp3") has only the artist as a
        # directory; the album is the track title. Without this, every
        # single-file share collapsed into one album bucket. This is checked
        # BEFORE the "Artist - Album" handling below, which needs the folder to
        # still be treated as the album.
        if len(dirs) == 1 and not re.match(r"^\d{1,3}\s*[-._)\]]", stem):
            artist, album = dirs[-1], stem
        elif len(dirs) == 1:
            artist, album = dirs[-1], ""

        # A folder named "Artist - Album" whose tracks repeat the same prefix
        # ("Caribou - Suddenly\Caribou - Suddenly - 01 Sister.flac") is one
        # album, not one album per track. Split the folder once so every track
        # in it lands in the same group.
        if " - " in album:
            head, _, tail = album.partition(" - ")
            if stem.lower().startswith(head.strip().lower()):
                m = re.match(r"^.+?\s+-\s+(?P<alb>.+?)\s+-\s+\d{1,3}\b", stem)
                artist = head.strip()
                album = m.group("alb").strip() if m else (tail.strip() or album)
    else:
        # Flat share: "Artist - Album - 01 Track" or "Artist - Album - 01 - Track".
        m = re.match(r"^(?P<art>.+?)\s+-\s+(?P<alb>.+?)\s+-\s+\d{1,3}\b", stem)
        if m:
            artist, album = m.group("art").strip(), m.group("alb").strip()
        else:
            # No separator to split on: treat the whole stem as the album and
            # leave the artist for the caller's own matching to infer.
            artist, album = "", stem

    # No post-hoc " - " splitting here. The cases above already assign artist
    # and album from the structure of the path; re-splitting them afterwards
    # re-broke correct results (a parsed "Discos - Proto House - NuDisco"
    # album became artist="Discos", album="Proto House - NuDisco").

    return artist, album



def score_response(query, resp):
    """Rank one peer's response. Higher is better; None-able fields.

    The dominant signal is how much of the response actually matches the query
    tokens, because a peer sharing 353 unrelated files will otherwise look like
    the best source purely on volume.
    """
    qtok = _tokens(query)
    files = resp.get("files") or []
    if not files:
        return -1, {}

    audio = []
    for f in files:
        ext = (f.get("extension") or "").lower().lstrip(".")
        if ext in AUDIO_EXT:
            audio.append(f)
    if not audio:
        return -1, {}

    # Relevance: fraction of audio files whose path contains the query tokens.
    hits = 0
    for f in audio:
        ftok = _tokens(f.get("filename"))
        if qtok and qtok.issubset(ftok | _tokens(f.get("filename", "") + " ")):
            hits += 1
        elif qtok & ftok:
            hits += 0.5
    relevance = hits / len(audio) if audio else 0.0

    # Artist agreement across files is a stronger signal than raw token overlap:
    # a real album share has one artist folder repeated, not a grab-bag.
    artists = {}
    for f in audio:
        a, _ = guess_artist_album(f.get("filename"))
        if a:
            artists[a.lower()] = artists.get(a.lower(), 0) + 1
    top_artist, top_n = ("", 0)
    if artists:
        top_artist, top_n = max(artists.items(), key=lambda kv: kv[1])
    consistency = top_n / len(audio) if audio else 0.0
    artist_match = 0.0
    if qtok and _tokens(top_artist):
        artist_match = 1.0 if qtok <= _tokens(top_artist) else (
            0.6 if qtok & _tokens(top_artist) else 0.0)

    # Quality, derived from what slskd actually reports. There is no bitRate
    # field: a SearchFile has extension, bitDepth, sampleRate, size and length
    # (seconds). Derive an approximate kbps from size/length where possible so
    # lossy sources can still be compared against each other.
    lossless_flags = []
    approx_kbps = []
    for f in audio:
        ext = (f.get("extension") or "").lower().lstrip(".")
        is_ll = ext in LOSSLESS_EXT
        sr = f.get("sampleRate")
        bd = f.get("bitDepth")
        if is_ll and isinstance(sr, int) and sr in LOSSLESS_HI_RES_SAMPLE_RATES:
            is_ll = "hires"
        lossless_flags.append(is_ll)
        size = f.get("size") or 0
        secs = f.get("length") or 0
        if size and secs:
            approx_kbps.append(int(size * 8 / secs / 1000))

    n_ll = sum(1 for v in lossless_flags if v)
    lossless_frac = n_ll / len(lossless_flags) if lossless_flags else 0.0
    hires = any(v == "hires" for v in lossless_flags)
    avg_kbps = int(sum(approx_kbps) / len(approx_kbps)) if approx_kbps else 0

    if lossless_frac >= 0.9:
        quality = 1.0 + (0.15 if hires else 0.0)   # lossless, bonus for hi-res
    elif lossless_frac > 0:
        quality = 0.6 + 0.4 * lossless_frac
    elif avg_kbps >= 256:
        quality = 0.7        # solid 320
    elif avg_kbps >= 160:
        quality = 0.4
    elif avg_kbps:
        quality = 0.1        # low-bitrate transcode
    else:
        quality = 0.3        # unknown, don't punish

    # A free slot and a short queue are what make a source actually usable now.
    usable = 1.0 if resp.get("hasFreeUploadSlot") else 0.0
    q = resp.get("queueLength")
    queue_pen = 0.0
    if isinstance(q, int):
        queue_pen = 1.0 if q == 0 else (0.6 if q < 25 else (0.3 if q < 100 else 0.1))

    total = (relevance * 40
             + artist_match * 25
             + consistency * 15
             + quality * 12
             + usable * 5
             + queue_pen * 3)

    info = {
        "username": resp.get("username"),
        "files": len(audio),
        "total_files": len(files),
        "relevance": round(relevance, 3),
        "artist": top_artist,
        "consistency": round(consistency, 3),
        "artist_match": round(artist_match, 3),
        "avg_kbps": avg_kbps,
        "lossless_frac": round(lossless_frac, 3),
        "hires": hires,
        "lossless": lossless_frac >= 0.9,
        "has_free_slot": bool(resp.get("hasFreeUploadSlot")),
        "queue_length": q,
        "locked": resp.get("lockedFileCount") or 0,
        "score": round(total, 2),
    }
    return total, info


def rank(query, responses, limit=12):
    """Rank peers for a query. Returns [(score, info, raw_response), ...] best first."""
    scored = []
    for r in responses:
        s, info = score_response(query, r)
        if s < 0:
            continue
        scored.append((s, info, r))
    scored.sort(key=lambda t: -t[0])
    return scored[:limit]


def summarise(score, info, max_files=6):
    """Human-readable one-source summary for the notification mail."""
    bits = []
    bits.append(f"{info['username']}  score {score}")
    bits.append(f"  artist      : {info['artist'] or '?'}")
    bits.append(f"  files       : {info['files']} audio"
                + (f" (+{info['total_files'] - info['files']} non-audio)"
                   if info["total_files"] > info["files"] else ""))
    bits.append(f"  quality     : "
                + (f"{info['lossless_frac']:.0%} lossless"
                   + (" (hi-res)" if info.get("hires") else "")
                   if info.get("lossless_frac") else f"~{info['avg_kbps']}kbps lossy"))
    bits.append(f"  relevance   : {info['relevance']:.0%}"
                f"   artist match: {info['artist_match']:.0%}")
    bits.append(f"  slot        : {'free' if info['has_free_slot'] else 'busy'}"
                f"   queue: {info['queue_length']}")
    return "\n".join(bits)
