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
import time
import ssl
import threading
import urllib.request
import urllib.error
from http.server import HTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(SCRIPT_DIR, "marks_state.json")
CERT_FILE = os.path.join(SCRIPT_DIR, "cert.pem")
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

        if parsed.path == "/api/state":
            with _state_lock:
                self.send_json(200, dict(_state_cache))
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

    def do_OPTIONS(self):
        # CORS preflight for the PUT request
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, PUT, OPTIONS")
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
        self.send_header("Access-Control-Allow-Origin", "*")
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
    server = HTTPServer(("0.0.0.0", PORT), HelmHandler)

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
