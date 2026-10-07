#!/usr/bin/env python3
"""
Helm local server — serves the Helm dashboard, proxies feed/page fetches,
and holds the canonical app state on disk for multi-device sync.

This replaces:
  1. `python -m http.server` (for serving index.html, manifest.json, etc.)
  2. The public CORS proxy chain Helm falls back to in the browser
  3. Manual Save Config / Load Config file shuttling between devices

Auth:
  If helm_token.txt exists in STATE_DIR, every /api/* route requires
  `Authorization: Bearer <token>` — except GET /api/health (container
  healthcheck), POST /api/backup-events (its own X-Backup-Token), and GET
  /api/backups* (readable with either the bearer token or a valid
  X-Backup-Token, so the backup pipeline can pull snapshots without also
  holding the Helm token). With no such file, auth is disabled (fail-open),
  matching the vault/audio proxies. Cross-origin callers must be listed in
  HELM_ALLOWED_ORIGINS; there is no wildcard CORS.

State sync:
  GET  /api/state          -> returns { state, version, updatedAt }
  PUT  /api/state          -> body: { state, version }
                               last-write-wins: always overwrites and bumps the
                               version counter (never rejects on a stale
                               version). Clients poll every 5s to converge.
  GET  /api/sysstats       -> returns host CPU/memory/disk/uptime stats, for
                               the System Stats widget. Always reflects the
                               machine running this server, not the device
                               viewing the page.

Rolling backups:
  After every successful PUT to /api/state, the server writes a timestamped
  snapshot to helm-backups/ (next to this script) at most once per hour.
  The 10 most recent snapshots are kept; older ones are pruned automatically.
  To restore: copy any helm-backups/helm-backup-YYYYMMDD-HHMMSS.json over
  marks_state.json and restart the server (or PUT it via /api/state directly).

  GET  /api/backups        -> list of available snapshots (newest first)
  GET  /api/backups/<name> -> download a specific snapshot as JSON


HTTPS:
  If cert.pem and key.pem exist next to this script, the server starts in
  HTTPS mode automatically. This is required for the Web Crypto API (used
  by "Save Encrypted") to work when accessing Helm from any device other
  than the host itself — browsers only expose crypto.subtle on secure
  contexts (https://, or http://localhost on the host machine).
  See the accompanying setup notes for how to generate a self-signed cert.

Usage:
    python3 marks_server.py [port]

Default port is 8080 (or 8443 conventionally for HTTPS, but any port works).
"""

import sys
import os
import json
import time
import ssl
import hmac
import ipaddress
import threading
import urllib.request
import urllib.error
from collections import defaultdict
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, quote, urlencode

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# Where mutable state / secrets / certs live. Defaults to SCRIPT_DIR (the
# original layout — state files sit next to the script). In the container the
# code is baked into the image at /app and this is pointed at a bind-mounted
# /app/state so state survives image rebuilds; see docker-compose.yml.
STATE_DIR = os.environ.get("HELM_STATE_DIR", SCRIPT_DIR)
os.makedirs(STATE_DIR, exist_ok=True)

STATE_FILE = os.path.join(STATE_DIR, "marks_state.json")

# ── HELM ACCESS AUTH ─────────────────────────────────────────────────────────
# Every /api/* route (except /api/health, used by the container healthcheck,
# and POST /api/backup-events, which carries its own X-Backup-Token) requires a
# shared bearer token when helm_token.txt exists in STATE_DIR. Same lightweight
# model as vault_token.txt / audio_token.txt. Generate once:
#     openssl rand -hex 32 > $HELM_STATE_DIR/helm_token.txt
# then paste it into each device via the dashboard (Data ▸ Access Token).
# If the file is absent, auth is disabled (fail-open, matching the vault/audio
# proxies) so existing single-host localhost setups are unchanged.
HELM_TOKEN_FILE = os.path.join(STATE_DIR, "helm_token.txt")

# Cross-origin: the dashboard is served same-origin from this server, so no CORS
# header is needed for it. Any other origin that must call /api/* has to be
# listed explicitly here (comma-separated) — a bare "*" would let any website
# the user visits read /api/state and the vault proxy. Empty = same-origin only.
ALLOWED_ORIGINS = [o.strip() for o in os.environ.get("HELM_ALLOWED_ORIGINS", "").split(",") if o.strip()]

# Upper bound on request bodies we buffer whole (PUT /api/state and the small
# POST endpoints). The synced blob is comfortably under this; anything larger is
# almost certainly abuse, and reading it would just be an unauthenticated way to
# exhaust memory.
MAX_BODY_BYTES = 8 * 1024 * 1024


def _helm_token():
    if os.path.exists(HELM_TOKEN_FILE):
        with open(HELM_TOKEN_FILE) as f:
            return f.read().strip()
    return None


# ── VAULT / AUDIO PROXY ──────────────────────────────────────────────────────
# pass + the GPG agent (vault) and yt-dlp + cookies (audio) live on the
# workstation, not here. /api/vault/* and /api/audio/* requests get forwarded
# there rather than handled locally.
#
# The workstation dual-boots and each OS is a distinct tailnet node (hyperion
# when booted to Linux, shrike when booted to Windows/WSL2), so no single
# static address always works. VAULT_HOSTS / AUDIO_HOSTS list every candidate
# host (comma-separated, no scheme/port); the proxy tries them in order,
# remembers whichever answered, and tries that one first next time -- so steady
# state is a single request and only the first call after a boot switch pays a
# failover timeout. When VAULT_HOSTS is unset the legacy single
# VAULT_BACKEND_URL / VAULT_HOST is used, so existing deployments are unchanged.
VAULT_TOKEN_FILE = os.path.join(STATE_DIR, "vault_token.txt")
AUDIO_TOKEN_FILE = os.path.join(STATE_DIR, "audio_token.txt")


def _backend_urls(hosts_env, port, legacy_url_env, legacy_default):
    hosts = os.environ.get(hosts_env, "").strip()
    if hosts:
        return [f"http://{h.strip()}:{port}" for h in hosts.split(",") if h.strip()]
    return [os.environ.get(legacy_url_env, legacy_default)]


VAULT_BACKENDS = _backend_urls("VAULT_HOSTS", os.environ.get("VAULT_BACKEND_PORT", "8090"),
                               "VAULT_BACKEND_URL", "http://hyperion:8090")
AUDIO_BACKENDS = _backend_urls("AUDIO_HOSTS", os.environ.get("AUDIO_BACKEND_PORT", "8091"),
                               "AUDIO_BACKEND_URL", "http://hyperion:8091")

# What /api/config advertises and the startup banner prints; also updated in
# place to the last backend that actually answered.
VAULT_BACKEND = VAULT_BACKENDS[0]
AUDIO_BACKEND = AUDIO_BACKENDS[0]

# Last backend that answered for each kind, tried first on the next request.
_live_backend = {"vault": None, "audio": None}


def _ordered_backends(kind, backends):
    """`backends` with the last-known-good one moved to the front."""
    live = _live_backend[kind]
    if live in backends and backends[0] != live:
        return [live] + [b for b in backends if b != live]
    return backends


def _vault_token():
    if os.path.exists(VAULT_TOKEN_FILE):
        with open(VAULT_TOKEN_FILE) as f:
            return f.read().strip()
    return None


def proxy_to_vault(method, path_and_query, body_bytes=None):
    """Forward a request to vault_server.py on the workstation, trying each
    VAULT_BACKENDS entry until one is reachable.
    Returns (status_code, response_body_bytes)."""
    global VAULT_BACKEND
    token = _vault_token()
    last_reason = "no vault backend configured"
    for base in _ordered_backends("vault", VAULT_BACKENDS):
        req = urllib.request.Request(base + path_and_query, data=body_bytes, method=method)
        if token:
            req.add_header("X-Vault-Token", token)
        if body_bytes:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=8) as resp:
                _live_backend["vault"] = VAULT_BACKEND = base
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            # Backend reached -- an error status is a real answer, not a
            # connectivity failure, so don't fail over to the other host.
            _live_backend["vault"] = VAULT_BACKEND = base
            return e.code, e.read()
        except urllib.error.URLError as e:
            last_reason = str(e.reason)
            continue
    msg = json.dumps({"error": f"Could not reach any vault backend "
                               f"({', '.join(VAULT_BACKENDS)}): {last_reason}"})
    return 502, msg.encode("utf-8")


# ── AUDIO GRABBER PROXY ──────────────────────────────────────────────────────
# audio_grabber_server.py runs on the workstation (so downloaded files land
# there) and does the actual yt-dlp work. Same multi-host failover as
# proxy_to_vault above. One route here (file download) returns raw audio bytes
# rather than JSON, so its Content-Type / Content-Disposition are forwarded
# through rather than hardcoded.


def _audio_token():
    if os.path.exists(AUDIO_TOKEN_FILE):
        with open(AUDIO_TOKEN_FILE) as f:
            return f.read().strip()
    return None


def proxy_to_audio(method, path_and_query, body_bytes=None):
    """Forward a request to audio_grabber_server.py on the workstation, trying
    each AUDIO_BACKENDS entry until one is reachable.
    Returns (status_code, response_body_bytes, headers_dict)."""
    global AUDIO_BACKEND
    token = _audio_token()
    last_reason = "no audio backend configured"
    for base in _ordered_backends("audio", AUDIO_BACKENDS):
        req = urllib.request.Request(base + path_and_query, data=body_bytes, method=method)
        if token:
            req.add_header("X-Audio-Token", token)
        if body_bytes:
            req.add_header("Content-Type", "application/json")
        try:
            # Long timeout: a download route streams the file through this call.
            # After a boot switch the first audio request waits this out on the
            # now-dead host before failing over; later calls hit the live one.
            with urllib.request.urlopen(req, timeout=40) as resp:
                headers = {"Content-Type": resp.headers.get("Content-Type", "application/json")}
                cd = resp.headers.get("Content-Disposition")
                if cd:
                    headers["Content-Disposition"] = cd
                _live_backend["audio"] = AUDIO_BACKEND = base
                return resp.status, resp.read(), headers
        except urllib.error.HTTPError as e:
            _live_backend["audio"] = AUDIO_BACKEND = base
            return e.code, e.read(), {"Content-Type": "application/json"}
        except urllib.error.URLError as e:
            last_reason = str(e.reason)
            continue
    msg = json.dumps({"error": f"Could not reach any audio backend "
                               f"({', '.join(AUDIO_BACKENDS)}): {last_reason}"})
    return 502, msg.encode("utf-8"), {"Content-Type": "application/json"}


# ── MUSIC EXPLORER (Last.fm similar-artist proxy) ───────────────────────────
# musicXplorer's "related artists" needs Last.fm's artist.getsimilar, which is
# the only piece of this feature that needs an API key -- the YouTube embed
# search reuses the existing /api/audio/* -> audio_grabber_server.py yt-dlp
# proxy, and the official-site/YouTube-channel/wiki links are resolved
# straight from the browser against MusicBrainz + Wikidata, both public
# CORS-enabled JSON APIs (same pattern as the iTunes artwork lookup and the
# Random Wikipedia widget already do). The Last.fm key can't follow that
# direct-fetch pattern since it's a secret, so it stays server-side here and
# is attached to the request -- same shared-secret-file model as
# vault_token.txt/audio_token.txt, just consumed by this process instead of
# forwarded to another one.
LASTFM_API_KEY_FILE = os.path.join(STATE_DIR, "lastfm_api_key.txt")
LASTFM_API_URL = "http://ws.audioscrobbler.com/2.0/"


def _lastfm_api_key():
    if os.path.exists(LASTFM_API_KEY_FILE):
        with open(LASTFM_API_KEY_FILE) as f:
            return f.read().strip()
    return None


def get_lastfm_similar(artist):
    """Returns (status_code, json_bytes) for GET /api/musicxplorer/similar."""
    api_key = _lastfm_api_key()
    if not api_key:
        return 503, json.dumps({
            "error": "musicXplorer related-artists not configured (no lastfm_api_key.txt in STATE_DIR)"
        }).encode("utf-8")
    params = urlencode({
        "method": "artist.getsimilar",
        "artist": artist,
        "api_key": api_key,
        "format": "json",
        "limit": 12,
        "autocorrect": 1,
    })
    try:
        req = urllib.request.Request(LASTFM_API_URL + "?" + params, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=8) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except urllib.error.URLError as e:
        return 502, json.dumps({"error": f"Could not reach Last.fm: {e.reason}"}).encode("utf-8")
    try:
        data = json.loads(raw)
    except Exception:
        return 502, json.dumps({"error": "Last.fm returned invalid JSON"}).encode("utf-8")
    if "error" in data:
        # Last.fm's own error payload, e.g. artist not found -- pass its
        # message through rather than a generic failure.
        return 502, json.dumps({"error": data.get("message", "Last.fm error")}).encode("utf-8")
    artists = (data.get("similarartists") or {}).get("artist") or []
    similar = [
        {"name": a.get("name", ""), "match": float(a.get("match", 0) or 0), "url": a.get("url", "")}
        for a in artists if a.get("name")
    ]
    return 200, json.dumps({"similar": similar}).encode("utf-8")


# ── MusicBrainz release radar (Music tab → Releases) ──────────────────────────
#
# Recent indie-pop releases for the Music tab's "Releases" section. MusicBrainz
# is the only free source that carries a real release date per release-group;
# Last.fm's album.getinfo returns released=None, and iTunes' genreId parameter
# is silently ignored (it returns the same rows as no genre at all), so neither
# can answer this. Measured on the live API: the "indie pop" tag yields ~4
# albums/30d, ~20/90d, ~39/365d -- sparse but genuinely on-genre.
#
# Why the backend does this rather than the browser: resolving each artist's
# links needs one url-rels call PER ARTIST (~24 artists) and MusicBrainz
# enforces ~1 req/sec per IP. Doing that client-side means a ~25s page load and
# a 503 storm. Here it's paced, cached to disk, and refreshed in the background
# so a request is served from cache almost always.
#
# Why there's no "upcoming" list: bands don't register planned future dates in
# MusicBrainz. Measured forward-looking volume for the same query is 0 albums in
# the next 180 days. A future section would render empty, so this returns recent
# releases only, and the frontend renders the date it's actually known by.
MB_BASE = "https://musicbrainz.org/ws/2/"
MB_COVER_BASE = "https://coverartarchive.org/release-group/"
RELEASES_CACHE_FILE = os.path.join(STATE_DIR, "releases_cache.json")
RELEASES_CACHE_TTL = 6 * 3600        # MusicBrainz release tags settle slowly
RELEASES_REFRESH_LOCK = threading.Lock()

# Tags are crowd-contributed free text, so a union query pulls in punk, metal
# and post-rock. Genre is therefore scored, not trusted: a hard blocklist for
# genres that should never appear, then a weighted sum of what's left. The
# threshold is load-bearing -- at 5, "lo-fi" game-soundtrack spam floods in
# (ULTRAKILL Lofi, DELTARUNE Lofi); at 10 the survivors are recognisably the
# genre requested. Edit these in index.html? No -- this list is the backend's.
RELEASE_TAGS = ["indie pop", "twee pop", "jangle pop", "indie", "indietronica",
                "bedroom pop", "dream pop", "lo-fi", "indie rock", "shoegaze"]
RELEASE_TAG_WEIGHTS = {
    "indie pop": 10, "twee pop": 9, "jangle pop": 9, "indie": 7,
    "indietronica": 6, "bedroom pop": 6, "dream pop": 6,
    "lo-fi": 5, "indie rock": 5, "shoegaze": 4,
}
RELEASE_BLOCKED_TAGS = {
    "punk", "punk rock", "hardcore punk", "post-punk", "pop punk", "skate punk",
    "metal", "death metal", "black metal", "thrash metal", "doom metal",
    "heavy metal", "folk metal", "metalcore", "post-rock", "glam rock",
    "reggae", "reggae rock", "dancehall", "classical", "jazz", "hip hop",
    "country", "folk", "bluegrass", "blues", "disco", "funk", "soul", "trap",
}
RELEASE_MIN_SCORE = 10
# Artist link relation types, in the order we'd rather show them.
RELEASE_LINK_TYPES = ["youtube", "bandcamp", "official homepage", "soundcloud", "discogs"]


def _mb_get(path, params, timeout=20):
    """One paced MusicBrainz call. Returns parsed JSON, or None on any failure."""
    url = MB_BASE + path + "?" + urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                               "Accept": "application/json"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            # 503 is the rate limiter; back off and retry. A 404 means no such
            # entity, which won't fix itself -- stop immediately.
            if e.code == 503 and attempt < 2:
                time.sleep(2.0 * (attempt + 1))
                continue
            return None
        except Exception:
            if attempt < 2:
                time.sleep(1.5)
                continue
            return None
    return None


def _release_score(tags):
    """Weigh a release's tags, or return -1 if it's genre-blocked."""
    names = {t.lower() for t in tags}
    if names & RELEASE_BLOCKED_TAGS:
        return -1
    return sum(RELEASE_TAG_WEIGHTS.get(n, 0) for n in names)


def _release_cover_url(release_group_id):
    """Front-cover thumbnail URL from the Cover Art Archive, or None."""
    try:
        req = urllib.request.Request(
            MB_COVER_BASE + release_group_id + "?fmt=json",
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=12) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None
    for img in data.get("images", []):
        if "Front" in (img.get("types") or []):
            return img.get("thumbnails", {}).get("250") or img.get("resource")
    images = data.get("images") or []
    return images[0].get("resource") if images else None


def _release_artist_links(artist_id):
    """Public links for an artist, best-first. Note the URL lives at
    relation['url']['resource'] -- NOT relation['resource'], which reads as
    'this artist has no links' for every artist and is easy to get wrong."""
    data = _mb_get("artist/" + artist_id, {"fmt": "json", "inc": "url-rels"})
    if not data:
        return {}
    found = {}
    for rel in data.get("relations") or []:
        url = (rel.get("url") or {}).get("resource")
        rtype = rel.get("type")
        if url and rtype in RELEASE_LINK_TYPES and rtype not in found:
            found[rtype] = url
    return found


def _fetch_releases():
    """Build the release list from MusicBrainz. Slow (~30s: one paced call per
    artist) — always called behind the cache, never on a user request path."""
    now = time.time()
    today = time.strftime("%Y-%m-%d")
    # 120 days back is enough to fill a list without paging indefinitely; the
    # window is a Lucene range so MusicBrainz does the filtering server-side.
    window_start = time.strftime("%Y-%m-%d", time.localtime(now - 120 * 86400))
    tag_clause = " OR ".join('tag:"%s"' % t for t in RELEASE_TAGS)
    query = "(%s) AND firstreleasedate:[%s TO %s]" % (tag_clause, window_start, today)

    data = _mb_get("release-group", {"query": query, "fmt": "json",
                                    "limit": 100, "inc": "artist-credits+tags+genres"})
    if not data:
        return None

    seen_artists = set()
    releases = []
    for rg in data.get("release-groups", []):
        # Singles outnumber albums and dilute the list; keep albums and EPs.
        if rg.get("primary-type") not in ("Album", "EP"):
            continue
        tags = [t.get("name", "") for t in (rg.get("tags") or [])]
        if _release_score(tags) < RELEASE_MIN_SCORE:
            continue
        credit = rg.get("artist-credit") or []
        if not credit:
            continue
        artist = credit[0].get("artist") or {}
        artist_id = artist.get("id")
        name = artist.get("name", "")
        if not artist_id or not name:
            continue
        # One artist can front several qualifying releases in the window;
        # link lookup is per-artist, so resolve each artist exactly once.
        if artist_id not in seen_artists:
            seen_artists.add(artist_id)
            links = _release_artist_links(artist_id)
        else:
            links = {}
        releases.append({
            "title": rg.get("title", ""),
            "artist": name,
            "artist_id": artist_id,
            "date": rg.get("first-release-date", ""),
            "type": rg.get("primary-type", ""),
            "tags": sorted(t for t in tags if t.lower() in RELEASE_TAG_WEIGHTS)[:4],
            "cover": _release_cover_url(rg["id"]),
            "youtube": links.get("youtube"),
            "bandcamp": links.get("bandcamp"),
            "homepage": links.get("official homepage"),
            "soundcloud": links.get("soundcloud"),
            "discogs": links.get("discogs"),
        })
        # Spread the work: one artist + one cover-art lookup per release, and
        # MusicBrainz's limiter counts coverartarchive against the same budget.
        time.sleep(1.05)

    # Some first-release-date values are a bare year ("2026") -- sort tolerates
    # short strings, but newest-first must treat them as least precise.
    releases.sort(key=lambda r: (r["date"] or "", r["artist"].lower()), reverse=True)
    return {"updated_at": now, "updated": time.strftime("%Y-%m-%d %H:%M"),
            "source": "musicbrainz", "tags": RELEASE_TAGS,
            "releases": releases}


def get_releases():
    """Cached release list. Serves stale cache, refreshes in the background."""
    cached = None
    try:
        with open(RELEASES_CACHE_FILE) as f:
            cached = json.load(f)
    except Exception:
        cached = None

    fresh = cached and (time.time() - cached.get("updated_at", 0) < RELEASES_CACHE_TTL)
    if fresh:
        return 200, json.dumps(cached).encode("utf-8")

    if cached:
        # Stale but present: answer immediately, refresh for next time. A cold
        # refresh takes ~30s, which is far too long to block a page load on.
        if RELEASES_REFRESH_LOCK.acquire(blocking=False):
            def _bg():
                try:
                    fresh_data = _fetch_releases()
                    if fresh_data:
                        tmp = RELEASES_CACHE_FILE + ".tmp"
                        with open(tmp, "w") as f:
                            json.dump(fresh_data, f)
                        os.replace(tmp, RELEASES_CACHE_FILE)   # atomic
                finally:
                    RELEASES_REFRESH_LOCK.release()
            threading.Thread(target=_bg, daemon=True).start()
        cached["stale"] = True
        return 200, json.dumps(cached).encode("utf-8")

    # Cold cache: the first load has to wait for the fetch, once.
    if RELEASES_REFRESH_LOCK.acquire(blocking=False):
        try:
            data = _fetch_releases()
            if data:
                tmp = RELEASES_CACHE_FILE + ".tmp"
                with open(tmp, "w") as f:
                    json.dump(data, f)
                os.replace(tmp, RELEASES_CACHE_FILE)
                return 200, json.dumps(data).encode("utf-8")
            # Fetch failed -- fall through to a clear error rather than an
            # empty list, which would read as "no indie pop exists".
            return 502, json.dumps({
                "error": "Could not reach MusicBrainz for releases"}).encode("utf-8")
        finally:
            RELEASES_REFRESH_LOCK.release()
    return 503, json.dumps({"error": "Release list is being built, try again shortly"}).encode("utf-8")


# ── SERVER HOST (for generating correct URLs in /api/config) ────────────────────
# Used by the frontend to construct absolute URLs for proxied services.
# Defaults to "localhost" — override with SERVER_HOST env var.
SERVER_HOST = os.environ.get("SERVER_HOST", "localhost")
SERVER_PORT = os.environ.get("SERVER_PORT", "8080")

CERT_FILE = os.path.join(STATE_DIR, "cert.pem")


# ── BACKUP EVENTS (Backup Pipeline tab) ─────────────────────────────────────
# The external backup scripts (a separate repo, run on hyperion/popcorn) call
# emit_event.py, which POSTs each event to POST /api/backup-events below with a
# shared-secret token. store_backup_event() prunes and writes this file
# atomically; GET /api/backup-events just reads it straight off disk. This
# replaced a RabbitMQ broker + backup_event_worker.py drainer -- same "talk
# directly, don't stand up new infra" bias as the vault/audio token proxies.
BACKUP_EVENTS_FILE   = os.path.join(STATE_DIR, "backup_events.json")
BACKUP_TOKEN_FILE    = os.path.join(STATE_DIR, "backup_token.txt")
BACKUP_KEEP_PER_KEY  = 2     # keep this many events per distinct (stage, name)
BACKUP_MAX_EVENTS    = 500   # flat cap after per-key pruning
_backup_events_lock  = threading.Lock()

# Mail-arrival notifications for the Helm mail widget.
#
# These are deliberately NOT the same store as the app state: mail arrives
# from a machine that only has the Helm bearer token and cannot PUT state, so
# a server-side append-only file (like backup_events.json above) is the only
# shape that works. Bodies are never stored — only who/what/when — because
# this file is included in state backups and synced between devices.
NOTIFICATIONS_FILE      = os.path.join(STATE_DIR, "notifications.json")
NOTIFICATIONS_MAX       = 100     # flat cap, oldest pruned first
_notifications_lock     = threading.Lock()


def get_notifications():
    """Read the notifications list from disk. Missing/corrupt file => empty."""
    try:
        with open(NOTIFICATIONS_FILE) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    items = data.get("notifications")
    return items if isinstance(items, list) else []


def store_notification(n):
    """Append one notification, cap the list, write atomically (tmp + replace).

    De-duplicates on (subject, from, receivedAt): postfix can invoke the pipe
    more than once for a single message during a queue flush, and a duplicated
    row is visible as a duplicated notification in the widget.
    """
    with _notifications_lock:
        items = get_notifications()
        ident = (n.get("subject"), n.get("from"), n.get("receivedAt"))
        for existing in items:
            if (existing.get("subject"), existing.get("from"),
                    existing.get("receivedAt")) == ident:
                return len(items), False
        items.append(n)
        items = items[-NOTIFICATIONS_MAX:]
        tmp = NOTIFICATIONS_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"notifications": items, "updated_at": time.time()}, f)
        os.replace(tmp, NOTIFICATIONS_FILE)
        return len(items), True


def mark_notifications_read():
    """Mark every notification read and return how many were flipped.

    Reading mail actually happens in mutt on hyperion, so Helm never learns
    about it. This is the explicit "I have seen it" action from the title-bar
    badge instead — the user asserting the unread state is now stale.

    Notifications are kept, not deleted: the badge count is what the user wants
    gone, and keeping the rows means a reload does not resurrect a count the
    user already dismissed.
    """
    with _notifications_lock:
        items = get_notifications()
        changed = 0
        for n in items:
            if not n.get("read"):
                n["read"] = True
                changed += 1
        if changed:
            tmp = NOTIFICATIONS_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"notifications": items, "updated_at": time.time()}, f)
            os.replace(tmp, NOTIFICATIONS_FILE)
        return changed, len(items)


def get_backup_events():
    if not os.path.exists(BACKUP_EVENTS_FILE):
        return {"events": [], "updated_at": None}
    try:
        with open(BACKUP_EVENTS_FILE) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {"events": [], "updated_at": None}


def _backup_token():
    if os.path.exists(BACKUP_TOKEN_FILE):
        with open(BACKUP_TOKEN_FILE) as f:
            return f.read().strip()
    return None


def _prune_backup_events(events):
    """Keep only the last BACKUP_KEEP_PER_KEY events per distinct (stage, name),
    preserving chronological order. A chatty stage (many per-folder events in
    one run) must not crowd out rarer stages, so group first, then cap. Empty
    string `name` is its own group. Order is preserved because the frontend
    relies on "last matching element = most recent" for a stage's status card.
    (Moved verbatim from the old backup_event_worker.prune_events.)"""
    groups = defaultdict(list)
    for i, ev in enumerate(events):
        groups[(ev.get("stage"), ev.get("name", ""))].append(i)
    keep = set()
    for _key, idxs in groups.items():
        keep.update(idxs[-BACKUP_KEEP_PER_KEY:])
    return [ev for i, ev in enumerate(events) if i in keep]


def store_backup_event(event):
    """Append one event, prune per key, apply the flat cap, write atomically
    (tmp + os.replace, same as _write_state_to_disk). Returns the new count."""
    with _backup_events_lock:
        events = get_backup_events().get("events", [])
        events.append(event)
        events = _prune_backup_events(events)[-BACKUP_MAX_EVENTS:]
        tmp = BACKUP_EVENTS_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"events": events, "updated_at": time.time()}, f)
        os.replace(tmp, BACKUP_EVENTS_FILE)
        return len(events)


KEY_FILE = os.path.join(STATE_DIR, "key.pem")

# Rolling backup settings
BACKUP_DIR      = os.path.join(STATE_DIR, "helm-backups")
BACKUP_KEEP     = 10    # number of snapshots to retain
BACKUP_MIN_SECS = 3600  # minimum seconds between automatic snapshots (1 hour)

ALLOWED_SCHEMES = ("http://", "https://")
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def _host_is_public(host):
    """Resolve `host` and return False if any resolved address is loopback,
    private, link-local (incl. the 169.254.169.254 cloud-metadata endpoint),
    multicast, reserved, or unspecified. /api/proxy is unauthenticated and
    returns the full upstream body, so without this it is a read-SSRF into
    localhost, the container network, the LAN/tailnet, and instance metadata.
    """
    try:
        infos = _socket.getaddrinfo(host, None)
    except Exception:
        return False
    if not infos:
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0].split("%")[0])
        except ValueError:
            return False
        if (ip.is_loopback or ip.is_private or ip.is_link_local
                or ip.is_multicast or ip.is_reserved or ip.is_unspecified):
            return False
    return True


class _GuardedRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Follow redirects (many real feeds 301 http->https or through a feed
    proxy), but re-check every hop so an allowed host can't bounce the proxy
    to an internal address."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        host = urlparse(newurl).hostname
        if not host or not _host_is_public(host):
            raise urllib.error.HTTPError(newurl, code,
                                         "redirect to disallowed host", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_proxy_opener = urllib.request.build_opener(_GuardedRedirectHandler)

# In-memory cache of state + a lock so concurrent requests from multiple
# devices don't corrupt the file or race on the version counter.
_state_lock = threading.Lock()
_state_cache = None  # { state, version, updatedAt } once loaded


def _load_state_from_disk():
    global _state_cache
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                _state_cache = json.load(f)
                return
        except Exception:
            pass
    # No file yet, or it was corrupt — start with an empty envelope.
    # The client seeds real defaults on first load; we just need a valid shape.
    _state_cache = {"state": None, "version": 0, "updatedAt": 0}


def _write_state_to_disk():
    tmp_path = STATE_FILE + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(_state_cache, f)
    os.replace(tmp_path, STATE_FILE)  # atomic on POSIX and Windows
    _maybe_write_backup()


def _backup_files():
    """Return list of backup files sorted oldest-first."""
    if not os.path.isdir(BACKUP_DIR):
        return []
    files = [
        f for f in os.listdir(BACKUP_DIR)
        if f.startswith("helm-backup-") and f.endswith(".json")
    ]
    files.sort()
    return files


def _maybe_write_backup():
    """Write a timestamped snapshot if enough time has passed since the last one."""
    try:
        os.makedirs(BACKUP_DIR, exist_ok=True)
        existing = _backup_files()

        # Check time since the most recent backup
        if existing:
            last = existing[-1]
            last_path = os.path.join(BACKUP_DIR, last)
            age = time.time() - os.path.getmtime(last_path)
            if age < BACKUP_MIN_SECS:
                return  # too soon

        # Write new snapshot
        stamp = time.strftime("%Y%m%d-%H%M%S")
        name = f"helm-backup-{stamp}.json"
        path = os.path.join(BACKUP_DIR, name)
        tmp  = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_state_cache, f)
        os.replace(tmp, path)

        # Prune oldest snapshots, keeping only BACKUP_KEEP
        existing = _backup_files()
        for old in existing[:-BACKUP_KEEP]:
            try:
                os.remove(os.path.join(BACKUP_DIR, old))
            except OSError:
                pass

        print(f"[Helm] Backup written: {name} "
              f"({len(existing)} snapshot(s) kept, max {BACKUP_KEEP})")
    except Exception as e:
        print(f"[Helm] Backup failed (non-fatal): {e}")


_load_state_from_disk()


# ── SYSTEM STATS ─────────────────────────────────────────────────────────────
# Dependency-free host stats: CPU load, memory, disk, uptime. Uses os/platform
# stdlib plus /proc on Linux where available; falls back gracefully elsewhere
# (e.g. Windows, where /proc doesn't exist) rather than requiring psutil.

import platform
import shutil


def _get_cpu_percent():
    """Best-effort instantaneous CPU usage. Linux: read /proc/stat twice with
    a short delay. Other platforms: fall back to load average if available,
    or report None if no signal can be obtained without extra dependencies."""
    try:
        if platform.system() == "Linux" and os.path.exists("/proc/stat"):
            def read_cpu_times():
                with open("/proc/stat") as f:
                    parts = f.readline().split()[1:]
                return [int(x) for x in parts]

            t1 = read_cpu_times()
            time.sleep(0.2)
            t2 = read_cpu_times()
            idle1, idle2 = t1[3], t2[3]
            total1, total2 = sum(t1), sum(t2)
            total_delta = total2 - total1
            idle_delta = idle2 - idle1
            if total_delta <= 0:
                return None
            return round(100 * (1 - idle_delta / total_delta), 1)
    except Exception:
        pass

    try:
        load1, _, _ = os.getloadavg()
        cores = os.cpu_count() or 1
        return round(min(100, (load1 / cores) * 100), 1)
    except Exception:
        return None


def _get_memory():
    """Returns (used_bytes, total_bytes) or (None, None) if unavailable."""
    try:
        if platform.system() == "Linux" and os.path.exists("/proc/meminfo"):
            info = {}
            with open("/proc/meminfo") as f:
                for line in f:
                    key, val = line.split(":", 1)
                    info[key.strip()] = int(val.strip().split()[0]) * 1024  # kB -> bytes
            total = info.get("MemTotal", 0)
            available = info.get("MemAvailable", info.get("MemFree", 0))
            used = total - available
            return used, total
    except Exception:
        pass
    return None, None


def _get_disk():
    """Returns (used_bytes, total_bytes) for the filesystem state is stored on."""
    try:
        usage = shutil.disk_usage(STATE_DIR)
        return usage.used, usage.total
    except Exception:
        return None, None


def _get_uptime_seconds():
    try:
        if platform.system() == "Linux" and os.path.exists("/proc/uptime"):
            with open("/proc/uptime") as f:
                return float(f.readline().split()[0])
    except Exception:
        pass
    return None


def gather_system_stats():
    cpu_pct = _get_cpu_percent()
    mem_used, mem_total = _get_memory()
    disk_used, disk_total = _get_disk()
    uptime = _get_uptime_seconds()
    try:
        load_avg = list(os.getloadavg())
    except (AttributeError, OSError):
        load_avg = None  # not available on this platform (e.g. Windows)

    return {
        "hostname": platform.node(),
        "platform": platform.system(),
        "cpuPercent": cpu_pct,
        "cpuCount": os.cpu_count(),
        "memUsedBytes": mem_used,
        "memTotalBytes": mem_total,
        "diskUsedBytes": disk_used,
        "diskTotalBytes": disk_total,
        "uptimeSeconds": uptime,
        "loadAvg": load_avg,
    }


import subprocess

# ── SERVICE MONITORING ────────────────────────────────────────────────────────
# Helm runs as a container now, so everything worth watching is a sibling
# container reached through the mounted Docker socket. The systemd-user /
# systemd / systemd-timer branches in gather_services_status / gather_logs /
# control_service are kept for the generic type-dispatch (add an entry here to
# use them) but nothing ships with those types anymore.
MONITORED_SERVICES = [
    {
        "id":        "helm",
        "label":     "Helm",
        "type":      "docker",
        "container": "helm",
        # Not controllable: stop/restart would kill the process serving this
        # very request. Monitor-only.
        "controllable": False,
    },
    {
        "id":        "searxng-core",
        "label":     "SearXNG",
        "type":      "docker",
        "container": "searxng-core",
        "controllable": True,
    },
    {
        "id":        "leafwiki",
        "label":     "LeafWiki",
        "type":      "docker",
        "container": "leafwiki",
        "controllable": True,
    },
    {
        "id":        "leafwiki-proxy",
        "label":     "LeafWiki Proxy",
        "type":      "docker",
        "container": "leafwiki-proxy",
        "controllable": True,
    },
    {
        "id":        "dailytxt",
        "label":     "DailyTxT",
        "type":      "docker",
        "container": "dailytxt",
        "controllable": True,
    },
    {
        "id":        "dailytxt-proxy",
        "label":     "DailyTxT Proxy",
        "type":      "docker",
        "container": "dailytxt-proxy",
        "controllable": True,
    },
    {
        "id":        "slskd",
        "label":     "Soulseek (slskd)",
        "type":      "docker",
        "container": "slskd",
        "controllable": True,
    },
    {
        "id":        "freshrss",
        "label":     "FreshRSS",
        "type":      "docker",
        "container": "freshrss",
        "controllable": True,
    },
    {
        "id":        "freshrss-proxy",
        "label":     "FreshRSS Proxy",
        "type":      "docker",
        "container": "freshrss-proxy",
        "controllable": True,
    },
    {
        "id":        "forgejo",
        "label":     "Forgejo",
        "type":      "docker",
        "container": "forgejo",
        "controllable": True,
    },
    {
        "id":        "stalwart",
        "label":     "Stalwart (mail)",
        "type":      "docker",
        "container": "stalwart",
        "controllable": True,
    },
]

# ── Docker socket helpers ─────────────────────────────────────────────────────
# Talk to the Docker daemon directly over its Unix socket rather than shelling
# out to the docker CLI. This works as long as the socket is readable by the
# process owner (i.e. carl is in the docker group at the OS level), without
# needing SupplementaryGroups in the systemd unit file.

import socket as _socket
import http.client as _http_client


class _UnixSocketHTTPConnection(_http_client.HTTPConnection):
    """HTTPConnection that connects over a Unix domain socket."""
    def __init__(self, socket_path):
        super().__init__("localhost")
        self._socket_path = socket_path

    def connect(self):
        sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        sock.connect(self._socket_path)
        self.sock = sock


def _docker_api(path, method="GET", body=None, socket_path="/var/run/docker.sock"):
    """Make a request to the Docker API via Unix socket. Returns (data_dict, error_str)."""
    try:
        conn = _UnixSocketHTTPConnection(socket_path)
        headers = {"Content-Type": "application/json", "Host": "localhost"}
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        raw = resp.read().decode("utf-8", errors="replace")
        conn.close()
        return json.loads(raw) if raw else {}, None
    except PermissionError:
        return None, "permission denied on /var/run/docker.sock — is carl in the docker group?"
    except FileNotFoundError:
        return None, "Docker socket not found at /var/run/docker.sock"
    except Exception as e:
        return None, str(e)


def _run(cmd, timeout=10, env=None):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
        return r.stdout.strip(), r.stderr.strip(), r.returncode
    except subprocess.TimeoutExpired:
        return "", "timeout", 1
    except Exception as e:
        return "", str(e), 1


def _format_duration(seconds):
    seconds = int(seconds)
    if seconds < 0:
        return "0s"
    days    = seconds // 86400
    hours   = (seconds % 86400) // 3600
    minutes = (seconds % 3600) // 60
    secs    = seconds % 60
    if days > 0:
        return f"{days}d {hours}h {minutes}m"
    if hours > 0:
        return f"{hours}h {minutes}m"
    if minutes > 0:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def _get_systemd_status(unit, user=False):
    """Return status dict for a systemd unit (system or user)."""
    cmd_prefix = ["systemctl", "--user"] if user else ["systemctl"]
    stdout, _, _ = _run(cmd_prefix + ["show", unit,
                         "--property=ActiveState,SubState,ExecMainStartTimestamp"])
    props = {}
    for line in stdout.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            props[k] = v
    active  = props.get("ActiveState", "unknown")
    sub     = props.get("SubState", "unknown")
    running = active == "active" and sub == "running"
    uptime_str = ""
    started_at = props.get("ExecMainStartTimestamp", "")
    if started_at and started_at != "n/a":
        try:
            from datetime import datetime
            parts  = started_at.split()
            dt_str = " ".join(parts[1:4])
            started = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S")
            uptime_str = _format_duration((datetime.now() - started).total_seconds())
        except Exception:
            uptime_str = started_at
    journal_cmd = ["journalctl"] + (["--user"] if user else []) + ["-u", unit, "-n", "20", "--no-pager", "--output=short-iso"]
    logs_out, _, _ = _run(journal_cmd)
    return {"running": running, "status": f"{active} ({sub})", "uptime": uptime_str, "logs": logs_out}


def _get_systemd_user_status(unit):
    return _get_systemd_status(unit, user=True)


def _get_docker_status(container):
    data, err = _docker_api(f"/containers/{container}/json")
    if err:
        return {"running": False, "status": err, "uptime": "", "logs": ""}
    if data is None or "State" not in data:
        return {"running": False, "status": "container not found", "uptime": "", "logs": ""}

    state      = data["State"]
    is_running = state.get("Running", False)
    status_str = state.get("Status", "unknown")
    started_at = state.get("StartedAt", "")

    uptime_str = ""
    if is_running and started_at:
        try:
            from datetime import datetime, timezone
            started = datetime.strptime(started_at[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
            uptime_str = _format_duration((datetime.now(timezone.utc) - started).total_seconds())
        except Exception:
            uptime_str = started_at

    # Fetch last 20 log lines via Docker API (stdout+stderr, timestamps)
    logs_data, log_err = _docker_api(
        f"/containers/{container}/logs?stdout=1&stderr=1&tail=20&timestamps=1"
    )
    # Docker log endpoint returns raw multiplexed stream, not JSON
    # We get it as a string via our basic client
    logs_str = ""
    if log_err:
        logs_str = f"(could not fetch logs: {log_err})"
    else:
        # _docker_api tries to json.loads — for logs endpoint we need raw text
        # Retry with raw fetch
        try:
            conn = _UnixSocketHTTPConnection("/var/run/docker.sock")
            conn.request("GET", f"/containers/{container}/logs?stdout=1&stderr=1&tail=20&timestamps=1",
                         headers={"Host": "localhost"})
            resp = conn.getresponse()
            raw = resp.read()
            conn.close()
            # Docker multiplexed stream: each frame has an 8-byte header; strip it
            lines = []
            i = 0
            while i < len(raw):
                if i + 8 > len(raw):
                    break
                size = int.from_bytes(raw[i+4:i+8], "big")
                chunk = raw[i+8:i+8+size].decode("utf-8", errors="replace")
                lines.append(chunk)
                i += 8 + size
            logs_str = "".join(lines).strip()
        except Exception as e:
            logs_str = f"(log fetch error: {e})"

    # Memory + restart count. RestartCount comes from the inspect payload we
    # already fetched; memory does NOT — Docker only populates MemoryStats on
    # the separate /stats endpoint, and returns an empty dict from /json even
    # for a running container. So one extra socket call, one per container per
    # poll. stream=false makes it a single sample rather than a subscription.
    #
    # Worth surfacing on a box this size: with 8 containers and a few hundred
    # MB to spare, "which one is eating the memory" is the question the
    # Services tab exists to answer, and running/not-running alone can't
    # answer it. Subtract inactive_file cache — that memory is reclaimable
    # under pressure, so it isn't what competes for RAM.
    memory_mb = None
    stats_data, stats_err = _docker_api(
        f"/containers/{container}/stats?stream=false&one-shot=true"
    )
    if not stats_err and stats_data:
        try:
            ms = stats_data.get("memory_stats") or {}
            raw_bytes = ms.get("usage") or 0
            if isinstance(raw_bytes, str):
                raw_bytes = int(raw_bytes, 0)
            inactive = (ms.get("stats") or {}).get("inactive_file") or 0
            if isinstance(inactive, str):
                inactive = int(inactive, 0)
            memory_mb = round(max(raw_bytes - inactive, 0) / (1024 * 1024), 1)
        except Exception:
            pass

    return {"running": is_running, "status": status_str, "uptime": uptime_str,
            "logs": logs_str, "memory_mb": memory_mb,
            "restarts": data.get("RestartCount", 0)}


def _get_timer_status(timer_unit, service_unit):
    stdout, _, _ = _run(["systemctl", "--user", "show", timer_unit,
                         "--property=ActiveState"])
    props = {}
    for line in stdout.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            props[k] = v
    active  = props.get("ActiveState", "unknown")
    running = active == "active"
    next_out, _, _ = _run(["systemctl", "--user", "list-timers", timer_unit, "--no-pager"])
    next_str = ""
    last_str = ""
    for line in next_out.splitlines():
        if timer_unit in line:
            cols = line.split()
            if len(cols) >= 6:
                next_str = " ".join(cols[:2])
                last_str = " ".join(cols[4:6])
            break
    logs_out, _, _ = _run(
        ["journalctl", "--user", "-u", service_unit, "-n", "20", "--no-pager", "--output=short-iso"]
    )
    return {"running": running, "status": active, "uptime": "", "next_run": next_str, "last_run": last_str, "logs": logs_out}


# ── LOG VIEWER ────────────────────────────────────────────────────────────────
# Combines recent log lines across all monitored services into one structured,
# leveled, chronologically-sorted feed for the Logs page. Systemd units get
# real severity levels straight from the journal's PRIORITY field; Docker
# containers have no structured severity, so lines are classified by keyword
# as a best-effort approximation.

def _parse_journal_json_lines(raw):
    entries = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except Exception:
            continue
    return entries


def _priority_to_level(priority):
    """Map syslog priority (0=emerg .. 7=debug) to a simple 3-level scheme."""
    try:
        p = int(priority)
    except (TypeError, ValueError):
        return "info"
    if p <= 3:   # emerg, alert, crit, err
        return "error"
    if p == 4:   # warning
        return "warning"
    return "info"  # notice, info, debug


def _get_systemd_logs(unit, user, lines):
    cmd = ["journalctl"] + (["--user"] if user else []) + \
          ["-u", unit, "-n", str(lines), "--no-pager", "-o", "json"]
    stdout, _, rc = _run(cmd, timeout=15)
    if rc != 0 or not stdout:
        return []
    results = []
    for obj in _parse_journal_json_lines(stdout):
        ts_micro = obj.get("__REALTIME_TIMESTAMP")
        try:
            ts = float(ts_micro) / 1_000_000 if ts_micro else None
        except (TypeError, ValueError):
            ts = None
        msg = obj.get("MESSAGE", "")
        if isinstance(msg, list):
            # journalctl -o json sometimes emits MESSAGE as a byte array for
            # non-UTF8 output — best-effort decode.
            try:
                msg = bytes(msg).decode("utf-8", errors="replace")
            except Exception:
                msg = str(msg)
        results.append({
            "timestamp": ts,
            "level": _priority_to_level(obj.get("PRIORITY")),
            "message": msg,
        })
    return results


def _classify_docker_line(text):
    lower = text.lower()
    if any(k in lower for k in ("error", "fatal", "critical", "panic", "traceback")):
        return "error"
    if "warn" in lower:
        return "warning"
    return "info"


def _get_docker_logs(container, lines):
    try:
        conn = _UnixSocketHTTPConnection("/var/run/docker.sock")
        conn.request(
            "GET",
            f"/containers/{container}/logs?stdout=1&stderr=1&tail={lines}&timestamps=1",
            headers={"Host": "localhost"},
        )
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
    except Exception:
        return []

    # Strip Docker's 8-byte multiplexed stream frame headers
    text_parts = []
    i = 0
    while i < len(raw):
        if i + 8 > len(raw):
            break
        size = int.from_bytes(raw[i+4:i+8], "big")
        chunk = raw[i+8:i+8+size].decode("utf-8", errors="replace")
        text_parts.append(chunk)
        i += 8 + size
    full_text = "".join(text_parts)

    from datetime import datetime, timezone
    results = []
    for line in full_text.splitlines():
        if not line.strip():
            continue
        ts = None
        msg = line
        # Docker's --timestamps prefixes each line with an RFC3339 timestamp
        # followed by a space, e.g. "2026-07-21T12:00:00.123456789Z message"
        # The trailing Z means UTC — parsing this into a naive datetime and
        # calling .timestamp() on it directly is a real bug: Python then
        # assumes the naive datetime is in the *system's local timezone*,
        # silently shifting every entry by popcorn's UTC offset. Explicitly
        # attaching UTC tzinfo before calling .timestamp() avoids that.
        if len(line) > 20 and line[4] == "-" and "T" in line[:20]:
            try:
                ts_str, rest = line.split(" ", 1)
                dt = datetime.strptime(ts_str[:26], "%Y-%m-%dT%H:%M:%S.%f")
                ts = dt.replace(tzinfo=timezone.utc).timestamp()
                msg = rest
            except Exception:
                msg = line
        results.append({
            "timestamp": ts,
            "level": _classify_docker_line(msg),
            "message": msg,
        })
    return results


def gather_logs(service_filter=None, lines_per_service=50):
    """Return a combined, newest-first list of recent log entries across all
    monitored services, or a single service if service_filter is given."""
    all_entries = []
    for svc in MONITORED_SERVICES:
        if service_filter and service_filter != "all" and svc["id"] != service_filter:
            continue
        try:
            if svc["type"] == "systemd-user":
                entries = _get_systemd_logs(svc["unit"], user=True, lines=lines_per_service)
            elif svc["type"] == "systemd":
                entries = _get_systemd_logs(svc["unit"], user=False, lines=lines_per_service)
            elif svc["type"] == "docker":
                entries = _get_docker_logs(svc["container"], lines=lines_per_service)
            elif svc["type"] == "systemd-timer":
                entries = _get_systemd_logs(svc["service_unit"], user=True, lines=lines_per_service)
            else:
                entries = []
        except Exception:
            entries = []
        for e in entries:
            e["service"] = svc["id"]
            e["serviceLabel"] = svc["label"]
        all_entries.extend(entries)

    # Newest first; entries with no parseable timestamp sink to the bottom
    all_entries.sort(key=lambda e: (e["timestamp"] is None, -(e["timestamp"] or 0)))
    return all_entries[:400]  # hard cap so the response stays a reasonable size


def gather_services_status():
    results = []
    for svc in MONITORED_SERVICES:
        try:
            if svc["type"] == "systemd-user":
                info = _get_systemd_status(svc["unit"], user=True)
            elif svc["type"] == "systemd":
                info = _get_systemd_status(svc["unit"], user=False)
            elif svc["type"] == "docker":
                info = _get_docker_status(svc["container"])
            elif svc["type"] == "systemd-timer":
                info = _get_timer_status(svc["unit"], svc["service_unit"])
            else:
                info = {"running": False, "status": "unknown type", "uptime": "", "logs": ""}
        except Exception as e:
            info = {"running": False, "status": f"error: {e}", "uptime": "", "logs": ""}
        results.append({"id": svc["id"], "label": svc["label"], "type": svc["type"],
                        "controllable": svc.get("controllable", False), **info})
    return results


def control_service(service_id, action):
    svc = next((s for s in MONITORED_SERVICES if s["id"] == service_id), None)
    if not svc:
        return False, "Unknown service"
    if not svc.get("controllable", False):
        return False, "This service cannot be controlled"
    if action not in ("start", "stop", "restart"):
        return False, "Invalid action"

    if svc["type"] == "systemd-user":
        _, err, rc = _run(["systemctl", "--user", action, svc["unit"]], timeout=15)
        return rc == 0, err if rc != 0 else f"{action} successful"

    elif svc["type"] == "systemd":
        _, err, rc = _run(["sudo", "systemctl", action, svc["unit"]], timeout=15)
        return rc == 0, err if rc != 0 else f"{action} successful"

    elif svc["type"] == "docker":
        container = svc["container"]
        _, err = _docker_api(f"/containers/{container}/{action}", method="POST", body="")
        if err:
            return False, err
        return True, f"{action} successful"

    return False, "Unsupported service type"


def get_network_info():
    """Return public and private IPv4/IPv6 addresses."""
    import socket as _sock
    import urllib.request

    result = {
        "private_ipv4": [],
        "private_ipv6": [],
        "public_ipv4":  None,
        "public_ipv6":  None,
    }

    # ── Private addresses via getaddrinfo ─────────────────────────────────────
    try:
        hostname = _sock.gethostname()
        infos = _sock.getaddrinfo(hostname, None)
        seen = set()
        for info in infos:
            addr = info[4][0]
            if addr in seen:
                continue
            seen.add(addr)
            # Skip loopback
            if addr.startswith("127.") or addr == "::1":
                continue
            if ":" in addr:
                result["private_ipv6"].append(addr)
            else:
                result["private_ipv4"].append(addr)
    except Exception as e:
        result["private_error"] = str(e)

    # Also grab IPs from all interfaces via socket trick
    try:
        s = _sock.socket(_sock.AF_INET, _sock.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        if ip not in result["private_ipv4"]:
            result["private_ipv4"].insert(0, ip)
    except Exception:
        pass

    # ── Public addresses via external lookup ──────────────────────────────────
    def fetch_ip(url, timeout=5):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Helm/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read().decode().strip()
                # Some services return extra data after the IP — take only the first line
                return raw.splitlines()[0].strip()
        except Exception:
            return None

    result["public_ipv4"] = fetch_ip("https://api4.ipify.org")
    result["public_ipv6"] = fetch_ip("https://api6.ipify.org")

    return result


TAILSCALE_SOCK = "/var/run/tailscale/tailscaled.sock"


def get_tailscale_status():
    """Return tailnet peer status from tailscaled's LocalAPI.

    Talks to tailscaled over its own unix socket rather than shelling out to
    the `tailscale` CLI: the Helm container doesn't ship that binary, but the
    socket can be bind-mounted in. Reuses _UnixSocketHTTPConnection — same
    reason the Docker helpers don't shell out to `docker ps`.

    LocalAPI over the unix socket needs no bearer token (access is already
    gated on filesystem perms), so there's no secret to store here.

    Degrades to {"available": False, "error": ...} rather than raising: a box
    without Tailscale is a normal config, not a server fault.
    """
    result = {"available": False, "peers": []}
    try:
        conn = _UnixSocketHTTPConnection(TAILSCALE_SOCK)
        # The Host header must be the LocalAPI magic value or tailscaled 404s.
        conn.request("GET", "/localapi/v0/status",
                     headers={"Host": "local-tailscaled.sock"})
        resp = conn.getresponse()
        raw = resp.read().decode("utf-8", errors="replace")
        conn.close()
        data = json.loads(raw)
    except FileNotFoundError:
        result["error"] = "tailscaled socket not mounted — bind-mount " + TAILSCALE_SOCK
        return result
    except Exception as e:
        result["error"] = str(e)
        return result

    self_node = (data.get("Self") or {}).get("ID")
    result["available"] = True
    result["backend_state"] = data.get("BackendState", "")
    result["self"] = {
        "hostname": (data.get("Self") or {}).get("HostName", ""),
        "ips":      (data.get("Self") or {}).get("TailscaleIPs", []),
    }

    for key, peer in (data.get("Peer") or {}).items():
        result["peers"].append({
            "id":       key,
            "hostname": peer.get("HostName", ""),
            "dns_name": peer.get("DNSName", ""),
            "os":       peer.get("OS", ""),
            "ips":      peer.get("TailscaleIPs", []),
            "online":   bool(peer.get("Online")),
            # "is_self" lets the frontend mark/exclude the node Helm runs on
            # without re-deriving it from the IP list.
            "is_self":  key == self_node,
            "last_seen": peer.get("LastSeen", ""),
            "exit_node": bool(peer.get("ExitNode")),
        })
    # Online first, then by hostname, so the list is stable between polls.
    result["peers"].sort(key=lambda p: (not p["online"], p["hostname"]))
    return result


class HelmHandler(SimpleHTTPRequestHandler):
    # A client that disappears mid-connection without a clean TCP close (e.g.
    # a laptop sleeping/losing wifi) would otherwise leave the handler thread
    # blocked in a read forever. With the single-threaded HTTPServer this
    # used to run under, that one stale connection wedged the whole server —
    # nobody else could connect until the process was restarted. Now that
    # ThreadingHTTPServer gives each connection its own thread, this timeout
    # just makes sure that thread eventually exits instead of leaking.
    timeout = 60

    def log_message(self, fmt, *args):
        path = args[0] if args else ""
        if "/api/" in path:
            super().log_message(fmt, *args)

    # ── Auth ────────────────────────────────────────────────────────────────
    def _client_authorized(self):
        """True if the request carries the shared Helm bearer token, or if no
        helm_token.txt is configured (auth disabled — same fail-open stance the
        vault/audio proxies take when their token file is absent)."""
        expected = _helm_token()
        if not expected:
            return True
        got = self.headers.get("Authorization", "")
        if got.startswith("Bearer "):
            got = got[7:]
        return hmac.compare_digest(got, expected)

    def _require_auth(self):
        if self._client_authorized():
            return True
        self.send_json(401, {"error": "missing or invalid Helm access token"})
        return False

    def _backup_token_valid(self):
        """True if the request carries a valid X-Backup-Token. The backup
        pipeline (separate repo, runs on hyperion/popcorn) already holds this
        secret for POST /api/backup-events; accepting it on the read-only
        /api/backups* routes too means those hosts don't also need the Helm
        bearer token just to pull snapshots for the R2 sync."""
        expected = _backup_token()
        return bool(expected) and hmac.compare_digest(
            self.headers.get("X-Backup-Token", ""), expected)

    def _require_auth_or_backup_token(self):
        if self._client_authorized() or self._backup_token_valid():
            return True
        self.send_json(401, {"error": "missing or invalid Helm access token"})
        return False

    def _read_body(self):
        """Read a request body, refusing anything past MAX_BODY_BYTES rather
        than buffering an unbounded amount of memory on an unauthenticated
        (or newly authenticated) request."""
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length > MAX_BODY_BYTES:
            self.send_json(413, {"error": "request body too large"})
            return None
        return self.rfile.read(length) if length else b""

    # ── GET ──────────────────────────────────────────────────────────────────
    def do_GET(self):
        parsed = urlparse(self.path)

        if parsed.path == "/api/health":
            self.send_json(200, {"status": "ok", "service": "marks-local-server"})
            return

        if parsed.path == "/api/backups" or parsed.path.startswith("/api/backups/"):
            # Readable with either the Helm bearer token or the backup-pipeline
            # X-Backup-Token (see _backup_token_valid).
            if not self._require_auth_or_backup_token():
                return
        elif parsed.path.startswith("/api/") and not self._require_auth():
            return

        if parsed.path == "/api/slskd/staging":
            scripts_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                       "scripts")
            if scripts_dir not in sys.path:
                sys.path.insert(0, scripts_dir)
            import slskd_live as LIVE
            self.send_json(200, {"entries": LIVE.staging_list()})
            return

        if parsed.path.startswith("/api/slskd/search/"):
            # Poll a live search started by POST /api/slskd/search.
            scripts_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                       "scripts")
            if scripts_dir not in sys.path:
                sys.path.insert(0, scripts_dir)
            import slskd_live as LIVE
            result = LIVE.get_search(parsed.path[len("/api/slskd/search/"):])
            if result is None:
                self.send_json(404, {"error": "unknown search id"})
            else:
                self.send_json(200, result)
            return

        if parsed.path == "/api/proxy":
            self.handle_proxy(parsed)
            return

        if parsed.path == "/api/sysstats":
            self.send_json(200, gather_system_stats())
            return

        if parsed.path == "/api/backups":
            files = _backup_files()
            payload = []
            for f in reversed(files):  # newest first
                p = os.path.join(BACKUP_DIR, f)
                try:
                    size = os.path.getsize(p)
                    mtime = os.path.getmtime(p)
                except OSError:
                    continue
                payload.append({"filename": f, "size": size, "createdAt": mtime})
            self.send_json(200, {"backups": payload})
            return

        if parsed.path.startswith("/api/backups/"):
            filename = parsed.path[len("/api/backups/"):]
            # Safety: reject any path traversal attempts
            if "/" in filename or "\\" in filename or not filename.startswith("helm-backup-"):
                self.send_json(400, {"error": "Invalid filename"})
                return
            path = os.path.join(BACKUP_DIR, filename)
            if not os.path.isfile(path):
                self.send_json(404, {"error": "Backup not found"})
                return
            with open(path, "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.end_headers()
            self.wfile.write(data)
            return

        if parsed.path.startswith("/api/audio/"):
            status, data, headers = proxy_to_audio("GET", self.path)
            self.send_response(status)
            for k, v in headers.items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        if parsed.path == "/api/musicxplorer/similar":
            qs = parse_qs(parsed.query)
            artist = (qs.get("artist", [""])[0] or "").strip()
            if not artist:
                self.send_json(400, {"error": "Missing artist parameter"})
                return
            status, data = get_lastfm_similar(artist)
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        if parsed.path == "/api/releases":
            status, data = get_releases()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        if parsed.path == "/api/config":
            # Expose runtime configuration to the frontend so it can construct
            # correct URLs without hardcoding hostnames. Values come from env
            # vars set in docker-compose.yml (SERVER_HOST / SEARXNG_URL /
            # VAULT_BACKEND_URL / AUDIO_BACKEND_URL), each with a fallback.
            # VAULT_BACKEND / AUDIO_BACKEND are already bare "scheme://host:port"
            # origins (see how they're used above: BACKEND + path_and_query), so
            # they're passed through as-is — an earlier rsplit("/", 1)[0] here
            # was wrong and turned "http://host:8090" into "http:/".
            self.send_json(200, {
                "server_host": SERVER_HOST,
                "server_port": SERVER_PORT,
                "searxng_url": os.environ.get("SEARXNG_URL", f"http://{SERVER_HOST}:{SERVER_PORT}"),
                "vault_url": VAULT_BACKEND,
                "audio_url": AUDIO_BACKEND,
                "leafwiki_url": os.environ.get("LEAFWIKI_URL", f"https://{SERVER_HOST}:9004"),
                "dailytxt_url": os.environ.get("DAILYTXT_URL", f"https://{SERVER_HOST}:9005"),
                "freshrss_url": os.environ.get("FRESHRSS_URL", f"https://{SERVER_HOST}:9007/i/"),
            })
            return

        if parsed.path == "/api/network":
            self.send_json(200, get_network_info())
            return

        if parsed.path == "/api/tailscale":
            self.send_json(200, get_tailscale_status())
            return

        if parsed.path == "/api/services":
            self.send_json(200, {"services": gather_services_status()})
            return

        if parsed.path == "/api/logs":
            qs = parse_qs(parsed.query)
            service_filter = qs.get("service", ["all"])[0]
            try:
                lines_per_service = min(200, max(10, int(qs.get("lines", ["50"])[0])))
            except ValueError:
                lines_per_service = 50
            self.send_json(200, {"entries": gather_logs(service_filter, lines_per_service)})
            return

        if parsed.path == "/api/state":
            with _state_lock:
                self.send_json(200, dict(_state_cache))
            return

        if parsed.path == "/api/backup-events":
            self.send_json(200, get_backup_events())
            return

        if parsed.path == "/api/notifications":
            # Newest first, so the frontend can render without sorting.
            items = sorted(get_notifications(),
                           key=lambda n: n.get("receivedAt") or 0, reverse=True)
            self.send_json(200, {"notifications": items})
            return

        if parsed.path == "/api/vault/status" or parsed.path.startswith("/api/vault/search") \
                or parsed.path.startswith("/api/vault/entry/") or parsed.path == "/api/vault/health":
            status, data = proxy_to_vault("GET", self.path)
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        super().do_GET()

    # ── DELETE — mark notifications read ─────────────────────────────────────
    # The action behind clicking the title-bar mail badge. Notifications are
    # marked read rather than removed, so a page reload cannot resurrect a
    # count the user already dismissed.
    def do_DELETE(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/notifications":
            if not self._require_auth():
                return
            changed, total = mark_notifications_read()
            self.send_json(200, {"ok": True, "markedRead": changed, "total": total})
            return

        self.send_json(404, {"error": "Not found"})

    # ── PUT — last-write-wins, no version conflict rejection ──────────────────
    # The server is the single source of truth. Any client can write at any
    # time; the most recent write always wins. Clients pull on startup and
    # poll every few seconds, so divergence is short-lived and bounded.
    # Version numbers are still incremented so clients can detect that
    # something changed since their last pull, but a stale version from the
    # client is never rejected — it is simply overwritten.
    def do_PUT(self):
        parsed = urlparse(self.path)
        if parsed.path != "/api/state":
            self.send_json(404, {"error": "Not found"})
            return

        if not self._require_auth():
            return

        raw = self._read_body()
        if raw is None:
            return  # _read_body already sent 413
        try:
            body = json.loads(raw)
        except Exception:
            self.send_json(400, {"error": "Invalid JSON body"})
            return

        incoming_state = body.get("state")
        if incoming_state is None:
            self.send_json(400, {"error": "Body must include state"})
            return

        with _state_lock:
            new_version = _state_cache.get("version", 0) + 1
            _state_cache["state"] = incoming_state
            _state_cache["version"] = new_version
            _state_cache["updatedAt"] = time.time()
            _write_state_to_disk()
            _maybe_write_backup()
            self.send_json(200, {"version": new_version, "updatedAt": _state_cache["updatedAt"]})

    def do_POST(self):
        parsed = urlparse(self.path)

        # /api/backup-events carries its own X-Backup-Token (checked below);
        # everything else under /api/ needs the shared Helm bearer token.
        if parsed.path.startswith("/api/") and parsed.path != "/api/backup-events":
            if not self._require_auth():
                return

        parts  = parsed.path.strip("/").split("/")
        if len(parts) == 3 and parts[0] == "api" and parts[1] == "services":
            service_id = parts[2]
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length)) if length else {}
            except Exception:
                body = {}
            action = body.get("action", "")
            ok, msg = control_service(service_id, action)
            self.send_json(200 if ok else 400, {"ok": ok, "message": msg})
            return

        if parsed.path == "/api/backup-events":
            # Ingest one backup-pipeline event from emit_event.py. Token-authed
            # like the vault/audio proxies (X-Backup-Token vs X-Vault-Token).
            # Fail closed: this is a write endpoint and the port is reachable
            # over Tailscale, so no token file means reject everything.
            expected = _backup_token()
            if not expected:
                self.send_json(503, {"error": "backup ingest not configured (no backup_token.txt in STATE_DIR)"})
                return
            if not hmac.compare_digest(self.headers.get("X-Backup-Token", ""), expected):
                self.send_json(401, {"error": "bad or missing X-Backup-Token"})
                return
            length = int(self.headers.get("Content-Length", 0))
            try:
                event = json.loads(self.rfile.read(length)) if length else None
            except Exception:
                event = None
            if not isinstance(event, dict) or not event.get("stage") or not event.get("status"):
                self.send_json(400, {"error": "body must be a JSON object with non-empty stage and status"})
                return
            # Fill out the backup_events.json schema so a minimal poster still
            # round-trips through the frontend (which multiplies ts by 1000).
            event.setdefault("id", "")
            event.setdefault("ts", time.time())
            event.setdefault("host", "")
            event.setdefault("name", "")
            event.setdefault("message", "")
            event.setdefault("data", {})
            self.send_json(200, {"ok": True, "stored": store_backup_event(event)})
            return

        if parsed.path in ("/api/slskd/staging/remove", "/api/slskd/send", "/api/slskd/synced"):
            # Staging list, favorites send, and the hyperion Inbox report.
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length)) if length else {}
            except Exception:
                body = {}
            scripts_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                       "scripts")
            if scripts_dir not in sys.path:
                sys.path.insert(0, scripts_dir)
            import slskd_live as LIVE
            if parsed.path == "/api/slskd/staging/remove":
                eid = str(body.get("id") or "")
                if not LIVE.remove_entry(eid):
                    self.send_json(404, {"error": "no such staged entry"})
                    return
                self.send_json(200, {"ok": True})
                return
            if parsed.path == "/api/slskd/synced":
                paths = body.get("paths")
                if not isinstance(paths, list):
                    self.send_json(400, {"error": "body must include a paths list"})
                    return
                self.send_json(200, {"ok": True, "total": LIVE.record_synced(paths)})
                return
            artist = (body.get("artist") or "").strip()
            title = (body.get("song") or "").strip()
            kind = body.get("kind")
            if not artist or not title or kind not in ("track", "album"):
                self.send_json(400, {"error": "body must include artist, song and kind (track|album)"})
                return
            eid = LIVE.start_send(artist, title, (body.get("album") or "").strip(), kind)
            self.send_json(202, {"ok": True, "id": eid})
            return

        if parsed.path in ("/api/slskd/search", "/api/slskd/download"):
            # Live Soulseek search and download, run in this process. Needs the
            # slskd config mounted into the container (see docker-compose.yml).
            # The older /api/slskd/offers and /api/slskd/pick paths are the
            # mail-driven flow on the VPS drainer, and are left alone.
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length)) if length else {}
            except Exception:
                body = {}
            scripts_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                       "scripts")
            if scripts_dir not in sys.path:
                sys.path.insert(0, scripts_dir)
            import slskd_live as LIVE
            if parsed.path == "/api/slskd/search":
                query = body.get("query")
                if not isinstance(query, str) or not query.strip():
                    self.send_json(400, {"error": "body must include a query"})
                    return
                sid = LIVE.start_search(query.strip())
                self.send_json(202, {"searchId": sid, "status": "searching"})
                return
            try:
                index = int(body.get("index") or 0)
            except (TypeError, ValueError):
                index = 0
            album = body.get("album") or None
            ok, err = LIVE.start_pick(str(body.get("searchId") or ""), index, album)
            if not ok:
                self.send_json(400, {"error": err})
                return
            self.send_json(202, {"ok": True, "started": True})
            return

        if parsed.path == "/api/slskd/offers":
            # Read persisted offers for a query. Used by the UI to render results
            # with Download buttons instead of requiring a mail reply.
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length)) if length else {}
            except Exception:
                body = {}
            query = (body.get("query") or "").strip()
            if not query:
                self.send_json(400, {"error": "body must include a query"})
                return

            # scripts/ is on the same level as this file but not on sys.path for
            # the container process — add it FIRST so the imports below work.
            scripts_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                       "scripts")
            if scripts_dir not in sys.path:
                sys.path.insert(0, scripts_dir)

            import slskd_offers as OFF
            import slskd_search as S
            state_dir = os.environ.get("HELM_STATE_DIR", "/app/state")
            offers = OFF.get_offers(state_dir, query=query)
            if not offers:
                self.send_json(200, {"query": query, "offers": []})
                return

            # Only the fields needed for UI rendering
            ui_offers = []
            for o in offers:
                # Derive album groups from the file list
                import slskd_search as S
                groups = {}
                for f in o.get("files") or []:
                    ext = S.file_ext(f)
                    if ext not in S.AUDIO_EXT:
                        continue
                    _artist, album = S.guess_artist_album(f.get("filename"))
                    groups.setdefault(album or "(unknown)", []).append(f)
                album_list = []
                for album, files in sorted(groups.items(),
                                           key=lambda kv: -len(kv[1]))[:5]:
                    mb = sum(f.get("size") or 0 for f in files) / 1e6
                    album_list.append({
                        "name": album,
                        "tracks": len(files),
                        "size_mb": round(mb, 1),
                        "quality": S.quality_label(files),
                    })
                ui_offers.append({
                    "index": int(o["id"].split(":")[-1]) + 1,
                    "username": o["username"],
                    "score": o.get("score"),
                    "albums": album_list
                })
            self.send_json(200, {"query": query, "offers": ui_offers})
            return

        if parsed.path == "/api/slskd/pick":
            # Download a source from the offers the drainer persisted. Reached
            # by the mail pipe on hyperion when you reply `slskd-get: [N] query`.
            #
            # Runs the pick in the background: enqueueing tries several peers and
            # each attempt can take slskd's ~5s peer timeout, so a synchronous
            # handler would hold the HTTP request (and the postfix pipe) open
            # for a minute or more. The pick handler mails its own outcome.
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length)) if length else {}
            except Exception:
                body = {}
            query = body.get("query")
            if not isinstance(query, str) or not query.strip():
                self.send_json(400, {"error": "body must include a query"})
                return
            try:
                index = int(body.get("index") or 1)
            except (TypeError, ValueError):
                index = 1
            album = body.get("album") or None

            # Helm runs in a container and the pick must run on the HOST, so it
            # cannot spawn the process itself. It also cannot be given the key:
            # /home/isaboo/soulseek/data/slskd.yml is not mounted into the
            # container, so a pick started there fails with "Could not read the
            # slskd API key" — the path does not exist inside.
            #
            # Instead, record the request in the bind-mounted state dir and let
            # the host-side drainer (already running every 5 min with the right
            # environment) pick it up. The key therefore never leaves the host.
            try:
                # `time` is shadowed by a local elsewhere in this method, so
                # reach the module explicitly rather than importing it here
                # (a local `import time` would itself raise UnboundLocalError).
                import time as _time_mod
                picks_path = os.path.join(
                    os.environ.get("HELM_STATE_DIR", "/app/state"),
                    "slskd_picks.jsonl")
                rec = {"query": query.strip(), "index": index, "album": album,
                       "at": _time_mod.time()}
                with open(picks_path, "a") as fh:
                    fh.write(json.dumps(rec) + "\n")
            except Exception as e:  # noqa: BLE001
                self.send_json(500, {"error": f"could not record pick: {e}"})
                return
            self.send_json(202, {"ok": True, "started": True,
                                 "query": query.strip(), "index": index})
            return

        if parsed.path == "/api/slskd/queue":
            # Append Soulseek search queries to the pending queue. Reached by
            # the mail pipe on hyperion, which has no shared filesystem with
            # this host (/mnt/SharedStuff is a local NTFS mount on hyperion
            # only), so the queue lives here and the pipe posts to it over the
            # tailnet.
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length)) if length else {}
            except Exception:
                body = {}
            queries = body.get("queries")
            if not isinstance(queries, list):
                self.send_json(400, {"error": "body must include a queries list"})
                return
            source = str(body.get("source") or "api")[:40]
            # The queue module lives in scripts/ next to this file, which is not
            # on sys.path for a container process.
            scripts_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                       "scripts")
            if scripts_dir not in sys.path:
                sys.path.insert(0, scripts_dir)
            try:
                import slskd_queue
            except Exception as e:  # noqa: BLE001
                self.send_json(500, {"error": f"queue module unavailable: {e}"})
                return
            try:
                added = slskd_queue.enqueue([q for q in queries if isinstance(q, str)],
                                            source=source)
            except Exception as e:  # noqa: BLE001
                # An unhandled exception here drops the connection with no
                # response, which the client sees as a transport error rather
                # than as a server fault. Return a real status.
                self.send_json(500, {"error": f"enqueue failed: {e}"})
                return
            self.send_json(200, {"ok": True, "added": added})
            return

        if parsed.path == "/api/notifications":
            # A mail arrived on hyperion; its postfix pipe reports the headers
            # here so the Helm widget can show it. Reached over the tailnet
            # with the normal Helm bearer token (the gate at the top of
            # do_POST already covers it) — no separate secret to provision on a
            # second host.
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length)) if length else {}
            except Exception:
                body = {}
            if not isinstance(body, dict) or not body.get("subject"):
                self.send_json(400, {"error": "body must be a JSON object with a subject"})
                return
            note = {
                "subject": str(body.get("subject", ""))[:300],
                "from": str(body.get("from", ""))[:300],
                "to": str(body.get("to", ""))[:300],
                "receivedAt": body.get("receivedAt") or int(time.time() * 1000),
                "size": int(body.get("size") or 0),
                "read": False,
            }
            count, added = store_notification(note)
            self.send_json(200, {"ok": True, "count": count, "added": added})
            return

        if parsed.path.startswith("/api/audio/"):
            length = int(self.headers.get("Content-Length", 0))
            body_bytes = self.rfile.read(length) if length else None
            status, data, headers = proxy_to_audio("POST", self.path, body_bytes)
            self.send_response(status)
            for k, v in headers.items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        if parsed.path == "/api/vault/lock" or parsed.path == "/api/vault/generate" \
                or parsed.path.startswith("/api/vault/entry/"):
            length = int(self.headers.get("Content-Length", 0))
            body_bytes = self.rfile.read(length) if length else None
            status, data = proxy_to_vault("POST", self.path, body_bytes)
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        self.send_json(404, {"error": "Not found"})

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Methods", "GET, PUT, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()  # end_headers() adds Access-Control-Allow-Origin iff the
                            # request Origin is in ALLOWED_ORIGINS

    # ── Feed proxy ─────────────────────────────────────────────────────────
    def handle_proxy(self, parsed):
        qs = parse_qs(parsed.query)
        target = qs.get("url", [None])[0]

        if not target or not target.startswith(ALLOWED_SCHEMES):
            self.send_json(400, {"error": "Missing or invalid url parameter"})
            return

        # SSRF guard: this endpoint is unauthenticated-by-URL (it still needs
        # the Helm bearer token to reach) and streams the upstream body back
        # verbatim, so refuse targets that resolve to internal space.
        host = urlparse(target).hostname
        if not host or not _host_is_public(host):
            self.send_json(403, {"error": "target host not allowed"})
            return

        req = urllib.request.Request(target, headers={"User-Agent": USER_AGENT})
        try:
            with _proxy_opener.open(req, timeout=10) as resp:
                resp_body = resp.read()
                content_type = resp.headers.get("Content-Type", "text/plain")
        except urllib.error.HTTPError as e:
            self.send_json(e.code, {"error": f"Upstream returned HTTP {e.code}"})
            return
        except urllib.error.URLError as e:
            self.send_json(502, {"error": f"Could not reach target: {e.reason}"})
            return
        except Exception as e:
            self.send_json(500, {"error": str(e)})
            return

        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(resp_body)

    def send_json(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def end_headers(self):
        # No wildcard CORS: the dashboard is same-origin and needs no ACAO
        # header at all. Reflect an Origin only when it's explicitly allow-listed
        # (HELM_ALLOWED_ORIGINS) — otherwise any site the user visits could read
        # /api/state and the vault proxy responses.
        origin = self.headers.get("Origin")
        if origin and origin in ALLOWED_ORIGINS:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Credentials", "true")
        # upgrade-insecure-requests instructs the browser to automatically
        # upgrade any http:// sub-resource requests (favicons, external images,
        # feed URLs before the proxy rewrites them) to https://, preventing
        # the "connection not secure" mixed-content indicator.
        # Ref: https://www.w3.org/TR/upgrade-insecure-requests/
        self.send_header("Content-Security-Policy", "upgrade-insecure-requests")
        super().end_headers()


def main():
    # ThreadingHTTPServer, not plain HTTPServer: a single-threaded server
    # means one client connection that never cleanly closes (e.g. a laptop
    # losing network without sending FIN/RST) blocks the accept loop forever
    # — every other client then gets connection timeouts with no error in
    # the logs, until the process is restarted. See git history for the
    # incident this fixed.
    server = ThreadingHTTPServer(("0.0.0.0", PORT), HelmHandler)

    use_tls = os.path.exists(CERT_FILE) and os.path.exists(KEY_FILE)
    scheme = "http"

    if use_tls:
        try:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(certfile=CERT_FILE, keyfile=KEY_FILE)
            server.socket = ctx.wrap_socket(server.socket, server_side=True)
            scheme = "https"
        except Exception as e:
            print(f"Found cert.pem/key.pem but failed to load them: {e}")
            print("Falling back to plain HTTP.")
            use_tls = False

    print(f"Helm server running at {scheme}://localhost:{PORT}")
    if use_tls:
        print(f"  HTTPS enabled using {CERT_FILE}")
        print(f"  Other devices on your network can reach this at https://<this-machine's-LAN-IP>:{PORT}")
        print(f"  Browsers will warn about the self-signed cert on first visit — that's expected, click through it.")
    else:
        print(f"  Running in plain HTTP mode.")
        print(f"  Note: 'Save Encrypted' only works over HTTPS, or over plain HTTP from localhost on this machine.")
        print(f"  To enable HTTPS for all devices, generate cert.pem and key.pem next to this script (see setup notes).")
    print(f"  Static files served from current directory")
    print(f"  Feed proxy available at /api/proxy?url=<encoded-url>")
    print(f"  State sync available at /api/state (GET/PUT)")
    print(f"  State persisted to {STATE_FILE}")
    print(f"  Rolling backups (up to {BACKUP_KEEP}, hourly) in {BACKUP_DIR}")
    print(f"  Vault proxied to {' / '.join(VAULT_BACKENDS)}")
    print(f"  Audio Grabber proxied to {' / '.join(AUDIO_BACKENDS)}")
    if _helm_token():
        print(f"  Auth: ENABLED — /api/* needs the token in {HELM_TOKEN_FILE}")
    else:
        print(f"  Auth: DISABLED — no {HELM_TOKEN_FILE}; every /api/* route is open")
    print(f"  CORS allow-list: {ALLOWED_ORIGINS or '(same-origin only)'}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping.")
        server.shutdown()


if __name__ == "__main__":
    main()
