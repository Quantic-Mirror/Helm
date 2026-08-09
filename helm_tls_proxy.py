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

# Many self-hosted apps (Wiki.js, and likely Forgejo/Element/Elk later) ship
# their own clickjacking protection by default — X-Frame-Options: SAMEORIGIN
# or DENY, or an equivalent CSP frame-ancestors directive. That's the right
# default for a standalone app, but it also blocks Helm from framing it
# entirely, since Helm runs on a different port and is therefore a
# different origin as far as the browser is concerned.
#
# frame-ancestors requires an EXACT origin match — scheme, host, and port
# all identical. A hostname and its own IP address are different origins
# as far as the browser is concerned, even though they reach the same
# machine — so if Helm might be accessed via either (e.g. hostname on one
# device, raw IP on another where DNS/hosts resolution isn't set up), both
# need to be listed, not just one.
#
# Override via the HELM_FRAME_ORIGINS environment variable (space or
# comma-separated) if Helm isn't reachable at the defaults below.
_default_origins = "https://popcorn:8081 https://192.168.1.168:8081"
ALLOWED_FRAME_ORIGINS = os.environ.get("HELM_FRAME_ORIGINS", _default_origins).replace(",", " ")


def _rewrite_framing_headers(headers):
    """Given a list of (name, value) response header tuples, drop any
    framing-restriction headers from the backend and return the rest
    unchanged, plus a flag indicating whether the backend had sent its own
    Content-Security-Policy (so the caller knows whether one still needs to
    be added)."""
    out = []
    had_csp = False
    # 'self' is always included alongside Helm's own origins, not just
    # replaced by them — some apps embed their own content in their own
    # nested same-origin iframes internally (kiwix-serve's viewer loading
    # its own /content/ page is exactly this), and completely replacing
    # their frame-ancestors with only Helm's origins silently breaks that
    # internal same-origin embedding while fixing Helm's external one.
    frame_ancestors_value = f"'self' {ALLOWED_FRAME_ORIGINS}"
    for k, v in headers:
        lk = k.lower()
        if lk == "x-frame-options":
            continue  # dropped entirely — replaced by the CSP header below
        if lk == "content-security-policy":
            had_csp = True
            # Strip any existing frame-ancestors directive from the
            # backend's own policy, keep everything else it was doing,
            # then add our own scoped allowance (plus 'self', see above).
            parts = [p.strip() for p in v.split(";") if p.strip() and not p.strip().lower().startswith("frame-ancestors")]
            parts.append(f"frame-ancestors {frame_ancestors_value}")
            v = "; ".join(parts)
        out.append((k, v))
    if not had_csp:
        out.append(("Content-Security-Policy", f"frame-ancestors {frame_ancestors_value}"))
    return out


def _rewrite_cookie_for_iframe(cookie_value):
    """Rewrite a Set-Cookie header value so the cookie survives being used
    inside Helm's cross-origin iframe. Browsers require SameSite=None
    (explicitly allowing cross-site/third-party iframe use) plus Secure
    (a mandatory pairing — browsers reject SameSite=None without it) on
    any cookie that needs to work in this context. Most self-hosted apps
    don't ship this by default, since they were never designed to be
    iframed cross-origin at all — which is exactly the failure mode this
    fixes: a login can succeed server-side while the browser silently
    discards the session cookie immediately afterward, since it lacks
    these attributes, making every subsequent request look logged-out
    again despite the login itself having worked."""
    parts = [p.strip() for p in cookie_value.split(";")]
    kept = []
    for p in parts:
        low = p.lower()
        if low.startswith("samesite="):
            continue
        if low == "secure":
            continue
        kept.append(p)
    kept.append("SameSite=None")
    kept.append("Secure")
    return "; ".join(kept)


class ProxyHandler(http.server.BaseHTTPRequestHandler):
    def _send_headers(self, raw_headers):
        for k, v in _rewrite_framing_headers(list(raw_headers)):
            if k.lower() in ("transfer-encoding", "connection"):
                continue
            if k.lower() == "set-cookie":
                v = _rewrite_cookie_for_iframe(v)
            self.send_header(k, v)

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
                self._send_headers(resp.getheaders())
                self.end_headers()
                self.wfile.write(resp.read())
        except urllib.error.HTTPError as e:
            self.send_response(e.code)
            body = e.read()
            self._send_headers(e.headers.items())
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
