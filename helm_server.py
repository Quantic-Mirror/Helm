#!/usr/bin/env python3
"""
Helm local server — serves the Helm dashboard, proxies feed/page fetches,
and holds the canonical app state on disk for multi-device sync.

This replaces:
  1. `python -m http.server` (for serving index.html, manifest.json, etc.)
  2. The public CORS proxy chain Helm falls back to in the browser
  3. Manual Save Config / Load Config file shuttling between devices

State sync:
  GET  /api/state          -> returns { state, version, updatedAt }
  PUT  /api/state          -> body: { state, version }
                               saves if version matches what the server has,
                               otherwise returns 409 with the server's current
                               state so the client can resolve the conflict.
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
import re
import time
import ssl
import threading
import urllib.request
import urllib.error
from http.server import HTTPServer, SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, quote

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(SCRIPT_DIR, "marks_state.json")

# ── VAULT PROXY ──────────────────────────────────────────────────────────────
# pass + the GPG agent live on hyperion, not here. Requests to /api/vault/*
# get forwarded there rather than handled locally. Change VAULT_BACKEND if
# hyperion's LAN hostname/IP or the vault_server.py port ever changes.
VAULT_BACKEND = os.environ.get("VAULT_BACKEND_URL", "http://hyperion:8090")
VAULT_TOKEN_FILE = os.path.join(SCRIPT_DIR, "vault_token.txt")


def _vault_token():
    if os.path.exists(VAULT_TOKEN_FILE):
        with open(VAULT_TOKEN_FILE) as f:
            return f.read().strip()
    return None


def proxy_to_vault(method, path_and_query, body_bytes=None):
    """Forward a request to vault_server.py running on hyperion.
    Returns (status_code, response_body_bytes)."""
    token = _vault_token()
    url = VAULT_BACKEND + path_and_query
    req = urllib.request.Request(url, data=body_bytes, method=method)
    if token:
        req.add_header("X-Vault-Token", token)
    if body_bytes:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except urllib.error.URLError as e:
        msg = json.dumps({"error": f"Could not reach vault backend on hyperion: {e.reason}"})
        return 502, msg.encode("utf-8")


CERT_FILE = os.path.join(SCRIPT_DIR, "cert.pem")

# ── SIGNAL INTEGRATION ────────────────────────────────────────────────────────
# Talks to a locally-running signal-cli-rest-api Docker container (same host
# as this server, so no cross-machine token proxy needed like the vault).
#
# signal-cli-rest-api's /v1/receive endpoint DRAINS its queue on every call —
# it is not a scrollback API. To show real conversation history across page
# reloads, a background thread here polls it periodically and persists
# messages into a local JSON store, grouped by conversation (sender or
# group). The frontend only ever reads from that persisted store, never
# directly from signal-cli-rest-api's receive endpoint.

SIGNAL_API_BASE = os.environ.get("SIGNAL_API_URL", "http://127.0.0.1:8082")
SIGNAL_STORE_FILE = os.path.join(SCRIPT_DIR, "signal_messages.json")
SIGNAL_POLL_INTERVAL = 3  # seconds
SIGNAL_MAX_MESSAGES_PER_CONV = 200

_signal_lock = threading.Lock()
_signal_store = {"number": None, "conversations": {}}


def _signal_load_store():
    global _signal_store
    if os.path.exists(SIGNAL_STORE_FILE):
        try:
            with open(SIGNAL_STORE_FILE, "r", encoding="utf-8") as f:
                _signal_store = json.load(f)
                return
        except Exception:
            pass
    _signal_store = {"number": None, "conversations": {}}


def _signal_write_store():
    tmp = SIGNAL_STORE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(_signal_store, f)
    os.replace(tmp, SIGNAL_STORE_FILE)


def _signal_api_get(path, timeout=20):
    try:
        req = urllib.request.Request(SIGNAL_API_BASE + path)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return (json.loads(raw) if raw else None), None
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code}"
    except Exception as e:
        return None, str(e)


def _signal_api_get_raw(path, timeout=20):
    """Like _signal_api_get but returns raw bytes + content-type instead of
    JSON-decoding — needed for binary attachment downloads (images, etc.)."""
    try:
        req = urllib.request.Request(SIGNAL_API_BASE + path)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            content_type = resp.headers.get("Content-Type", "application/octet-stream")
            return resp.read(), content_type, None
    except urllib.error.HTTPError as e:
        return None, None, f"HTTP {e.code}"
    except Exception as e:
        return None, None, str(e)


def _signal_api_post(path, payload, timeout=20):
    try:
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            SIGNAL_API_BASE + path, data=body, method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return (json.loads(raw) if raw else {}), None
    except urllib.error.HTTPError as e:
        try:
            return None, e.read().decode("utf-8", errors="replace")
        except Exception:
            return None, f"HTTP {e.code}"
    except Exception as e:
        return None, str(e)


def _signal_get_number():
    """The linked number, cached in the store once discovered."""
    if _signal_store.get("number"):
        return _signal_store["number"]
    data, err = _signal_api_get("/v1/accounts")
    if data and len(data) > 0:
        _signal_store["number"] = data[0]
        return data[0]
    return None


# ── Contact name resolution ───────────────────────────────────────────────────
# Sync messages (outgoing messages sent from the phone or any other linked
# device) only carry the recipient's number, never a display name — Signal's
# protocol just doesn't include that in the sync envelope. To show a name
# instead of a bare number for those conversations, look the number up
# against the account's contact list. Cached for an hour since this rarely
# changes and every conversation would otherwise trigger its own fetch.

SIGNAL_CONTACTS_TTL = 3600  # seconds
_signal_contacts_cache = {"ts": 0, "by_number": {}}


def _signal_refresh_contacts():
    number = _signal_get_number()
    if not number:
        return
    data, err = _signal_api_get(f"/v1/contacts/{quote(number, safe='+')}?all_recipients=true")
    if err or not isinstance(data, list):
        return
    by_number = {}
    for entry in data:
        num = entry.get("number") or entry.get("recipient")
        if not num:
            continue
        # Try several plausible field names rather than hard-coding one
        # schema — signal-cli-rest-api's exact contact JSON shape has
        # shifted across versions, and falling through to the number is
        # always a safe default if none of these are present.
        name = entry.get("name") or entry.get("profile_name") or entry.get("profileName")
        if not name:
            given = entry.get("given_name") or entry.get("givenName")
            family = entry.get("family_name") or entry.get("familyName")
            if given or family:
                name = " ".join(p for p in [given, family] if p)
        if name:
            by_number[num] = name
    _signal_contacts_cache["by_number"] = by_number
    _signal_contacts_cache["ts"] = time.time()


def _signal_resolve_contact_name(number):
    if not number:
        return None
    if time.time() - _signal_contacts_cache["ts"] > SIGNAL_CONTACTS_TTL:
        _signal_refresh_contacts()
    return _signal_contacts_cache["by_number"].get(number)


def _signal_extract_attachments(raw_attachments):
    """Normalize signal-cli-rest-api's attachment list into just what the
    frontend needs to display or link to them. The actual bytes stay in the
    container and are fetched on demand via /api/signal/attachment/<id>."""
    if not raw_attachments:
        return []
    out = []
    for a in raw_attachments:
        att_id = a.get("id")
        if not att_id:
            continue
        out.append({
            "id": att_id,
            "content_type": a.get("contentType", "application/octet-stream"),
            "filename": a.get("filename"),
            "size": a.get("size"),
        })
    return out


def _signal_extract_message(envelope):
    """Normalize both envelope shapes signal-cli-rest-api sends into one
    common structure. Returns None if this envelope isn't a message worth
    showing at all (e.g. it's a receipt or typing indicator with neither
    text nor an attachment).

    - dataMessage: an INCOMING message sent to this account. The
      conversation is keyed by the sender.
    - syncMessage.sentMessage: an OUTGOING message that was sent from any
      of this account's linked devices (including the phone itself, not
      just this widget). Multi-device sync delivers a copy of every sent
      message this way so all linked devices stay consistent. The
      conversation is keyed by the recipient, not the sender (the sender
      is always "this account" in this case).

    A message with a picture and no caption has message == null but a
    populated attachments array — it must still produce an entry so the
    picture shows up, not just messages that happen to have text.
    """
    data_msg = envelope.get("dataMessage")
    if data_msg and (data_msg.get("message") or data_msg.get("attachments")):
        group_info = data_msg.get("groupInfo") or {}
        group_id = group_info.get("groupId")
        peer = envelope.get("sourceNumber") or envelope.get("source")
        return {
            "text": data_msg.get("message") or "",
            "attachments": _signal_extract_attachments(data_msg.get("attachments")),
            "group_id": group_id,
            "group_name": group_info.get("groupName"),
            "peer_number": peer,
            "peer_name": envelope.get("sourceName"),
            "outgoing": False,
            "timestamp": envelope.get("timestamp", 0),
        }

    sent_msg = (envelope.get("syncMessage") or {}).get("sentMessage")
    if sent_msg and (sent_msg.get("message") or sent_msg.get("attachments")):
        group_info = sent_msg.get("groupInfo") or {}
        group_id = group_info.get("groupId")
        peer = sent_msg.get("destinationNumber") or sent_msg.get("destination")
        return {
            "text": sent_msg.get("message") or "",
            "attachments": _signal_extract_attachments(sent_msg.get("attachments")),
            "group_id": group_id,
            "group_name": group_info.get("groupName"),
            "peer_number": peer,
            "peer_name": None,
            "outgoing": True,
            "timestamp": sent_msg.get("timestamp") or envelope.get("timestamp", 0),
        }

    return None


def _signal_poll_once():
    number = _signal_get_number()
    if not number:
        return
    data, err = _signal_api_get(f"/v1/receive/{quote(number, safe='+')}")
    if err or not data:
        return
    with _signal_lock:
        changed = False
        for item in data:
            envelope = item.get("envelope") or {}
            msg = _signal_extract_message(envelope)
            if not msg:
                continue  # receipts, typing indicators, reactions, etc — not a text message

            group_id = msg["group_id"]
            if group_id:
                conv_id = "group:" + group_id
            elif msg["peer_number"]:
                conv_id = "dm:" + msg["peer_number"]
            else:
                continue  # no way to identify who this is to/from — skip

            conv = _signal_store["conversations"].setdefault(conv_id, {
                "name": conv_id,
                "is_group": bool(group_id),
                "group_id": group_id,
                "peer_number": None if group_id else msg["peer_number"],
                "messages": [],
            })
            if not group_id:
                conv["peer_number"] = msg["peer_number"]
                name_is_unresolved = conv["name"] == conv_id or conv["name"] == msg["peer_number"]
                if msg["peer_name"]:
                    conv["name"] = msg["peer_name"]
                elif name_is_unresolved:
                    resolved = _signal_resolve_contact_name(msg["peer_number"])
                    conv["name"] = resolved or msg["peer_number"] or conv_id
            elif msg["group_name"] and (not conv.get("name") or conv["name"] == conv_id):
                conv["name"] = msg["group_name"]

            msg_id = str(msg["timestamp"]) + (":out" if msg["outgoing"] else ":in:" + str(msg["peer_number"]))
            if any(m["id"] == msg_id for m in conv["messages"][-20:]):
                continue  # de-dupe against recently-seen messages

            # An outgoing sync message can arrive shortly after we've already
            # optimistically appended the same send from this widget's own
            # POST /api/signal/send handler. Since the two paths assign
            # different timestamps (our local clock vs Signal's), exact ID
            # matching won't catch it — so also skip if the most recent
            # message in this conversation is already an outgoing message
            # with identical text within the last 10 seconds. Pictures sent
            # from the widget aren't supported yet (see /api/signal/send),
            # so this comparison is text-only — it simply won't fire for
            # attachment-only messages, which is fine since those can only
            # arrive via sync right now anyway.
            if msg["outgoing"] and msg["text"] and conv["messages"]:
                last = conv["messages"][-1]
                if last["outgoing"] and last["text"] == msg["text"] \
                        and abs(last["timestamp"] - msg["timestamp"]) < 10000:
                    continue

            conv["messages"].append({
                "id": msg_id,
                "from": "You" if msg["outgoing"] else (msg["peer_name"] or msg["peer_number"] or "?"),
                "text": msg["text"],
                "attachments": msg["attachments"],
                "timestamp": msg["timestamp"],
                "outgoing": msg["outgoing"],
            })
            conv["messages"] = conv["messages"][-SIGNAL_MAX_MESSAGES_PER_CONV:]
            changed = True
        if changed:
            _signal_write_store()


def _signal_poll_loop():
    while True:
        try:
            _signal_poll_once()
        except Exception as e:
            print(f"[Helm] Signal poll error (non-fatal): {e}")
        time.sleep(SIGNAL_POLL_INTERVAL)


_signal_load_store()
threading.Thread(target=_signal_poll_loop, daemon=True).start()


KEY_FILE = os.path.join(SCRIPT_DIR, "key.pem")

# Rolling backup settings
BACKUP_DIR      = os.path.join(SCRIPT_DIR, "helm-backups")
BACKUP_KEEP     = 10    # number of snapshots to retain
BACKUP_MIN_SECS = 3600  # minimum seconds between automatic snapshots (1 hour)

ALLOWED_SCHEMES = ("http://", "https://")
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

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
    """Returns (used_bytes, total_bytes) for the filesystem this script lives on."""
    try:
        usage = shutil.disk_usage(SCRIPT_DIR)
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
    }


import subprocess

# ── SERVICE MONITORING ────────────────────────────────────────────────────────
MONITORED_SERVICES = [
    {
        "id":    "helm",
        "label": "Helm",
        "type":  "systemd-user",
        "unit":  "helm.service",
        "controllable": True,
    },
    {
        "id":    "thelounge",
        "label": "The Lounge (IRC)",
        "type":  "systemd",
        "unit":  "thelounge.service",
        "controllable": True,
    },
    {
        "id":    "searxng-core",
        "label": "SearXNG",
        "type":  "docker",
        "container": "searxng-core",
        "controllable": True,
    },
    {
        "id":    "r2-sync",
        "label": "Cloudflare R2 Sync",
        "type":  "systemd-timer",
        "unit":  "popcorn-r2-sync.timer",
        "service_unit": "popcorn-r2-sync.service",
        "controllable": False,
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


def _run(cmd, timeout=10):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
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

    return {"running": is_running, "status": status_str, "uptime": uptime_str, "logs": logs_str}


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

    from datetime import datetime
    results = []
    for line in full_text.splitlines():
        if not line.strip():
            continue
        ts = None
        msg = line
        # Docker's --timestamps prefixes each line with an RFC3339 timestamp
        # followed by a space, e.g. "2026-07-21T12:00:00.123456789Z message"
        if len(line) > 20 and line[4] == "-" and "T" in line[:20]:
            try:
                ts_str, rest = line.split(" ", 1)
                ts = datetime.strptime(ts_str[:26], "%Y-%m-%dT%H:%M:%S.%f").timestamp()
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


class HelmHandler(SimpleHTTPRequestHandler):
    def log_message(self, fmt, *args):
        path = args[0] if args else ""
        if "/api/" in path:
            super().log_message(fmt, *args)

    # ── GET ──────────────────────────────────────────────────────────────────
    def do_GET(self):
        parsed = urlparse(self.path)

        if parsed.path == "/api/proxy":
            self.handle_proxy(parsed)
            return

        if parsed.path == "/api/health":
            self.send_json(200, {"status": "ok", "service": "marks-local-server"})
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
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(data)
            return

        if parsed.path == "/api/network":
            self.send_json(200, get_network_info())
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

        if parsed.path == "/api/vault/status" or parsed.path.startswith("/api/vault/search") \
                or parsed.path.startswith("/api/vault/entry/") or parsed.path == "/api/vault/health":
            status, data = proxy_to_vault("GET", self.path)
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        if parsed.path == "/api/signal/status":
            number = _signal_get_number()
            self.send_json(200, {"linked": bool(number), "number": number})
            return

        if parsed.path == "/api/signal/conversations":
            with _signal_lock:
                convs = []
                for cid, c in _signal_store["conversations"].items():
                    last = c["messages"][-1] if c["messages"] else None
                    convs.append({
                        "id": cid,
                        "name": c["name"],
                        "is_group": c["is_group"],
                        "last_message": last["text"] if last else "",
                        "last_timestamp": last["timestamp"] if last else 0,
                    })
                convs.sort(key=lambda c: -c["last_timestamp"])
            self.send_json(200, {"conversations": convs})
            return

        if parsed.path.startswith("/api/signal/messages/"):
            conv_id = parsed.path[len("/api/signal/messages/"):]
            with _signal_lock:
                conv = _signal_store["conversations"].get(conv_id)
                messages = list(conv["messages"]) if conv else []
            self.send_json(200, {"messages": messages})
            return

        if parsed.path.startswith("/api/signal/attachment/"):
            att_id = parsed.path[len("/api/signal/attachment/"):]
            # signal-cli-rest-api attachment IDs are generated filenames —
            # reject anything that isn't a plain safe token, to rule out
            # path traversal or SSRF into the container's own API surface.
            if not att_id or not re.match(r"^[A-Za-z0-9_-]+$", att_id):
                self.send_json(400, {"error": "Invalid attachment id"})
                return
            raw, content_type, err = _signal_api_get_raw(f"/v1/attachments/{att_id}")
            if err or raw is None:
                self.send_json(404, {"error": err or "Attachment not found"})
                return
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "private, max-age=3600")
            self.end_headers()
            self.wfile.write(raw)
            return

        super().do_GET()

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

        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length))
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

        if parsed.path == "/api/signal/send":
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length)) if length else {}
            except Exception:
                self.send_json(400, {"error": "Invalid JSON body"})
                return
            conv_id = body.get("conv_id", "")
            text = body.get("text", "")
            if not conv_id or not text:
                self.send_json(400, {"error": "conv_id and text are required"})
                return
            number = _signal_get_number()
            if not number:
                self.send_json(503, {"error": "Signal not linked yet"})
                return
            with _signal_lock:
                conv = _signal_store["conversations"].get(conv_id)
            if not conv:
                self.send_json(404, {"error": "Unknown conversation"})
                return
            payload = {"message": text, "number": number}
            payload["recipients"] = [conv["group_id"]] if conv["is_group"] else [conv["peer_number"]]
            data, err = _signal_api_post("/v2/send", payload)
            if err:
                self.send_json(502, {"error": err})
                return
            with _signal_lock:
                conv["messages"].append({
                    "id": str(int(time.time() * 1000)) + ":outgoing",
                    "from": "You",
                    "text": text,
                    "timestamp": int(time.time() * 1000),
                    "outgoing": True,
                })
                conv["messages"] = conv["messages"][-SIGNAL_MAX_MESSAGES_PER_CONV:]
                _signal_write_store()
            self.send_json(200, {"ok": True})
            return

        self.send_json(404, {"error": "Not found"})

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, PUT, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    # ── Feed proxy (unchanged from previous version) ────────────────────────
    def handle_proxy(self, parsed):
        qs = parse_qs(parsed.query)
        target = qs.get("url", [None])[0]

        if not target or not target.startswith(ALLOWED_SCHEMES):
            self.send_json(400, {"error": "Missing or invalid url parameter"})
            return

        req = urllib.request.Request(target, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
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
        self.send_header("Access-Control-Allow-Origin", "*")
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
        self.send_header("Access-Control-Allow-Origin", "*")
        # upgrade-insecure-requests instructs the browser to automatically
        # upgrade any http:// sub-resource requests (favicons, external images,
        # feed URLs before the proxy rewrites them) to https://, preventing
        # the "connection not secure" mixed-content indicator.
        # Ref: https://www.w3.org/TR/upgrade-insecure-requests/
        self.send_header("Content-Security-Policy", "upgrade-insecure-requests")
        super().end_headers()


def main():
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
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping.")
        server.shutdown()


if __name__ == "__main__":
    main()
