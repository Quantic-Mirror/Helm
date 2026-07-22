#!/usr/bin/env python3
"""
vault_server.py — small standalone HTTP service exposing the pass-backed
vault API, meant to run on hyperion where `pass` and the GPG agent live.

helm_server.py on popcorn proxies /api/vault/* browser requests here rather
than running pass/gpg on popcorn directly.

Auth: requires a shared token in vault_token.txt (next to this script) that
must match the same file next to helm_server.py on popcorn. Generate once
and copy to both machines:
    openssl rand -hex 32 > vault_token.txt

Usage:
    python3 vault_server.py [port]

Default port: 8090
"""

import sys
import os
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import vault_api

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8090
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TOKEN_FILE = os.path.join(SCRIPT_DIR, "vault_token.txt")


def _load_token():
    if not os.path.exists(TOKEN_FILE):
        print(f"WARNING: {TOKEN_FILE} not found. Generate one with:")
        print(f"  openssl rand -hex 32 > {TOKEN_FILE}")
        print("...then copy that same file to popcorn, next to helm_server.py.")
        print("All requests will be rejected until it exists.")
        return None
    with open(TOKEN_FILE) as f:
        return f.read().strip()


VAULT_TOKEN = _load_token()


class VaultHandler(BaseHTTPRequestHandler):

    def _check_auth(self):
        if VAULT_TOKEN is None:
            self.send_json(503, {"error": "Server not configured: missing vault_token.txt"})
            return False
        supplied = self.headers.get("X-Vault-Token", "")
        if supplied != VAULT_TOKEN:
            self.send_json(401, {"error": "Invalid or missing vault token"})
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

        if parsed.path == "/api/vault/status":
            self.send_json(200, vault_api.get_status())
            return

        if parsed.path.startswith("/api/vault/search"):
            qs = parse_qs(parsed.query)
            query = qs.get("q", [""])[0]
            self.send_json(200, {"results": vault_api.search_entries(query)})
            return

        if parsed.path.startswith("/api/vault/entry/"):
            entry_path = parsed.path[len("/api/vault/entry/"):]
            if not entry_path or ".." in entry_path:
                self.send_json(400, {"error": "Invalid entry path"})
                return
            data, err = vault_api.get_entry(entry_path)
            if data is None:
                self.send_json(404, {"error": err or "Entry not found"})
                return
            self.send_json(200, data)
            return

        if parsed.path == "/api/vault/health":
            self.send_json(200, vault_api.run_health_scan())
            return

        self.send_json(404, {"error": "Not found"})

    def do_POST(self):
        if not self._check_auth():
            return
        parsed = urlparse(self.path)

        if parsed.path == "/api/vault/lock":
            self.send_json(200, vault_api.lock_vault())
            return

        if parsed.path == "/api/vault/generate":
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length)) if length else {}
            except Exception:
                body = {}
            pw = vault_api.generate_password(
                length=body.get("length", 20),
                symbols=body.get("symbols", True),
            )
            self.send_json(200, {"password": pw})
            return

        if parsed.path.startswith("/api/vault/entry/"):
            entry_path = parsed.path[len("/api/vault/entry/"):]
            if not entry_path or ".." in entry_path:
                self.send_json(400, {"error": "Invalid entry path"})
                return
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length)) if length else {}
            except Exception:
                self.send_json(400, {"error": "Invalid JSON body"})
                return
            ok, err = vault_api.create_or_update_entry(
                entry_path,
                secret=body.get("secret", ""),
                username=body.get("username", ""),
                url=body.get("url", ""),
                tags=body.get("tags", ""),
                expires=body.get("expires", ""),
                notes=body.get("notes", ""),
                force=body.get("force", False),
            )
            self.send_json(200 if ok else 400, {"ok": ok, "error": err if not ok else None})
            return

        self.send_json(404, {"error": "Not found"})

    def log_message(self, format, *args):
        # Quieter logging -- request paths are entry names (account/service
        # identifiers), so don't spam them into stdout/journalctl by default.
        pass


def main():
    server = ThreadingHTTPServer(("0.0.0.0", PORT), VaultHandler)
    print(f"Vault server running at http://0.0.0.0:{PORT}")
    print(f"Store: {vault_api.STORE_DIR}")
    if VAULT_TOKEN is None:
        print("!! No vault_token.txt found -- all requests will be rejected until one exists.")
    print("This should only be reachable from popcorn on your LAN, not the public internet.")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping.")
        server.shutdown()


if __name__ == "__main__":
    main()
