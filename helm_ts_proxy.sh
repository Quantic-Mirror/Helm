#!/usr/bin/env python3
"""
helm_tls_proxy.py — generic TLS-terminating reverse proxy for any local
HTTP backend, reusing Helm's own certificate.

Why this exists: several self-hosted apps Helm iframes (Wiki.js, Forgejo,
Element Web, Elk, etc.) don't ship their own TLS support the way Helm does,
and browsers block loading plain-HTTP iframe content from an HTTPS page
(mixed content). Rather than writing a bespoke wrapper per service (as the
first version of this did for gopherproxy specifically), this is a single
generic tool: point it at any local backend port, give it its own listen
port, and it terminates TLS with Helm's cert and forwards everything —
GET/POST/PUT/DELETE/PATCH — through unchanged.

This does NOT proxy WebSocket upgrades. Most of what Helm iframes (Wiki.js,
gopherproxy, Forgejo's basic web UI) works fine over plain HTTP request/
response without needing that. If a future service specifically needs
WebSocket support through this proxy, that's a real enhancement to make
here rather than working around — flag it rather than guessing silently.

Usage:
    python3 helm_tls_proxy.py <listen_port> <backend_port> [backend_host]

Defaults: backend_host is 127.0.0.1.
"""

import sys
import ssl
import http.server
import urllib.request
import urllib.error
import os

LISTEN_PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 9001
BACKEND_PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 8083
BACKEND_HOST = sys.argv[3] if len(sys.argv) > 3 else "127.0.0.1"
BACKEND_BASE = f"http://{BACKEND_HOST}:{BACKEND_PORT}"

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CERT_FILE = os.path.join(SCRIPT_DIR, "cert.pem")
KEY_FILE = os.path.join(SCRIPT_DIR, "key.pem")


class ProxyHandler(http.server.BaseHTTPRequestHandler):
    def _proxy(self, method):
        url = BACKEND_BASE + self.path
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else None
        req = urllib.request.Request(url, data=body, method=method)
        # Forward headers the backend actually needs; skip hop-by-hop ones
        # that only make sense for the client<->proxy leg.
        skip = {"host", "content-length", "transfer-encoding", "connection"}
        for k, v in self.headers.items():
            if k.lower() not in skip:
                req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                self.send_response(resp.status)
                for k, v in resp.getheaders():
                    if k.lower() not in ("transfer-encoding", "connection"):
                        self.send_header(k, v)
                self.end_headers()
                self.wfile.write(resp.read())
        except urllib.error.HTTPError as e:
            self.send_response(e.code)
            body = e.read()
            for k, v in e.headers.items():
                if k.lower() not in ("transfer-encoding", "connection"):
                    self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)
        except Exception as e:
            self.send_response(502)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(f"Proxy error: {e}".encode())

    def do_GET(self):    self._proxy("GET")
    def do_POST(self):   self._proxy("POST")
    def do_PUT(self):    self._proxy("PUT")
    def do_DELETE(self): self._proxy("DELETE")
    def do_PATCH(self):  self._proxy("PATCH")
    def do_HEAD(self):   self._proxy("HEAD")

    def log_message(self, format, *args):
        pass  # quiet — this is just a dumb pipe, not worth logging


def main():
    server = http.server.ThreadingHTTPServer(("0.0.0.0", LISTEN_PORT), ProxyHandler)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile=CERT_FILE, keyfile=KEY_FILE)
    server.socket = ctx.wrap_socket(server.socket, server_side=True)
    print(f"helm_tls_proxy listening on https://0.0.0.0:{LISTEN_PORT} -> {BACKEND_BASE}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping.")


if __name__ == "__main__":
    main()
