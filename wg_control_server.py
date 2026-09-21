"""
wg_control_server.py — thin JSON-over-Unix-socket HTTP layer around wg_api.py.

Runs natively on the VPS host as root (see helm-wg-control.service) — NOT in
Docker, since WireGuard needs the host's own real network namespace. Listens
on a Unix domain socket (default /run/helm-wg/control.sock) rather than a
TCP port: unlike vault_server.py/audio_grabber_server.py (which proxy to a
different HOST and need a shared-secret token over the network), this
backend is always local to helm_server.py's container, so socket file
permissions (root:wgctl, mode 0660 — see main() below) ARE the access
control. No token needed.

Exposes exactly two verbs on purpose — GET /status, POST /circuit — so a
compromise of the caller (helm_server.py) can at most flip which circuit is
active, never run an arbitrary command with this process's privilege.

Usage: python3 wg_control_server.py [socket-path]
"""

import json
import os
import shutil
import socketserver
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler

import wg_api

DEFAULT_SOCK_PATH = "/run/helm-wg/control.sock"
SOCKET_GROUP = "wgctl"
WATCHDOG_INTERVAL_SECONDS = 30


class WgHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # no per-request access log — not sensitive here, just noise

    def send_json(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self):
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        try:
            return json.loads(self.rfile.read(length))
        except json.JSONDecodeError:
            return None

    def do_GET(self):
        if self.path == "/status":
            self.send_json(200, wg_api.get_status())
            return
        self.send_json(404, {"error": "Not found"})

    def do_POST(self):
        if self.path == "/circuit":
            body = self._read_json_body()
            if body is None:
                self.send_json(400, {"error": "Invalid JSON body"})
                return
            circuit = body.get("circuit")
            if not circuit:
                self.send_json(400, {"error": "Missing 'circuit'"})
                return
            try:
                self.send_json(200, wg_api.switch_to(circuit))
            except wg_api.WgError as e:
                self.send_json(409, {"error": str(e), **wg_api.get_status()})
            return
        self.send_json(404, {"error": "Not found"})


class _UnixHTTPServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True


def _watchdog_loop():
    while True:
        time.sleep(WATCHDOG_INTERVAL_SECONDS)
        try:
            wg_api.watchdog_check()
        except Exception:
            pass  # never let a watchdog hiccup kill the loop


def main():
    sock_path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_SOCK_PATH
    sock_dir = os.path.dirname(sock_path)

    # The socket's PARENT DIRECTORY is what gets bind-mounted into the helm
    # container (see docker-compose.yml), not the socket file itself. A
    # single-file bind mount pins the container's view to whatever inode
    # existed when the container started — every restart of this process
    # deletes and recreates the socket file (a new inode), which then
    # leaves the container looking at a dead, orphaned socket ("Connection
    # refused") until the container itself is also restarted. A directory
    # bind mount doesn't have this problem: the container sees the
    # directory's live contents, so a fresh socket file appearing inside it
    # is visible immediately, no container restart needed.
    if sock_dir:
        os.makedirs(sock_dir, exist_ok=True)
        try:
            shutil.chown(sock_dir, group=SOCKET_GROUP)
            os.chmod(sock_dir, 0o750)  # root:wgctl rwxr-x--- — group needs traverse+list to reach the socket inside
        except LookupError:
            print(f"WARNING: group '{SOCKET_GROUP}' does not exist — {sock_dir} left "
                  f"owned by whoever ran this. Run: groupadd --system {SOCKET_GROUP}",
                  file=sys.stderr)
        except PermissionError:
            print(f"WARNING: could not chgrp/chmod {sock_dir} — run this as root.", file=sys.stderr)

    if os.path.isdir(sock_path):
        # Defensive backstop from an earlier design (bind-mounting the
        # socket file directly, before the directory-mount fix above) —
        # shouldn't happen anymore, but harmless to still guard against.
        os.rmdir(sock_path)
    elif os.path.exists(sock_path):
        os.remove(sock_path)  # stale socket from a prior crash

    old_umask = os.umask(0o117)  # created 0660 — no world/other access, no permission-window race
    try:
        server = _UnixHTTPServer(sock_path, WgHandler)
    finally:
        os.umask(old_umask)

    try:
        shutil.chown(sock_path, group=SOCKET_GROUP)
    except LookupError:
        pass  # already warned about the missing group above
    except PermissionError:
        print(f"WARNING: could not chgrp {sock_path} to '{SOCKET_GROUP}' — "
              f"run this as root.", file=sys.stderr)

    print(f"wg_control_server listening on unix:{sock_path}")
    wg_api.reconcile()
    threading.Thread(target=_watchdog_loop, daemon=True).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if os.path.exists(sock_path):
            os.remove(sock_path)


if __name__ == "__main__":
    main()
