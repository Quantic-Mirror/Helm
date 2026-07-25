#!/usr/bin/env python3
"""
gopher_tls_proxy.py — minimal TLS-terminating reverse proxy in front of the
gopherproxy Docker container.

Why this exists: prologic/gopherproxy is a small Go web server with no
built-in TLS support, and Helm itself is served over HTTPS. Browsers block
loading plain-HTTP iframe content from an HTTPS page (mixed content), so
something needs to terminate TLS in front of gopherproxy before Helm can
iframe it directly — same problem The Lounge had, solved the same way:
reuse Helm's own certificate rather than generating or managing a new one.

This deliberately doesn't reach for nginx/Caddy — it's the same ssl +
http.server pattern already used by helm_server.py itself, so there's
nothing new to install or learn, and it reuses cert files that are already
trusted by your browsers.

Usage:
    python3 gopher_tls_proxy.py [listen_port] [backend_port]

Defaults: listens on 9001, forwards to gopherproxy on 127.0.0.1:8083.
"""

import sys
import ssl
import ssl as _ssl
import http.server
import urllib.request
import urllib.error
import os

LISTEN_PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 9001
BACKEND_PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 8083
BACKEND_BASE = f"http://127.0.0.1:{BACKEND_PORT}"

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CERT_FILE = os.path.join(SCRIPT_DIR, "cert.pem")
KEY_FILE = os.path.join(SCRIPT_DIR, "key.pem")


class ProxyHandler(http.server.BaseHTTPRequestHandler):
    def _proxy(self, method):
        url = BACKEND_BASE + self.path
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else None
        req = urllib.request.Request(url, data=body, method=method)
        for header in ("Content-Type", "Cookie", "User-Agent"):
            if header in self.headers:
                req.add_header(header, self.headers[header])
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                self.send_response(resp.status)
                for k, v in resp.getheaders():
                    if k.lower() not in ("transfer-encoding", "connection"):
                        self.send_header(k, v)
                self.end_headers()
                self.wfile.write(resp.read())
        except urllib.error.HTTPError as e:
            self.send_response(e.code)
            self.end_headers()
            self.wfile.write(e.read())
        except Exception as e:
            self.send_response(502)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(f"Proxy error: {e}".encode())

    def do_GET(self):
        self._proxy("GET")

    def do_POST(self):
        self._proxy("POST")

    def log_message(self, format, *args):
        pass  # quiet — this is just a dumb pipe, not worth logging


def main():
    server = http.server.ThreadingHTTPServer(("0.0.0.0", LISTEN_PORT), ProxyHandler)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile=CERT_FILE, keyfile=KEY_FILE)
    server.socket = ctx.wrap_socket(server.socket, server_side=True)
    print(f"gopher_tls_proxy listening on https://0.0.0.0:{LISTEN_PORT} -> {BACKEND_BASE}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping.")


if __name__ == "__main__":
    main()
