#!/usr/bin/env python3
"""Queue a Soulseek search over HTTP (for the hyperion mail pipe).

There is NO shared filesystem between hyperion and the VPS: /mnt/SharedStuff is
a local NTFS mount on hyperion only. So a file-based queue cannot be shared —
the mail pipe on hyperion cannot append to a file the VPS's drainer reads.

The queue therefore lives on the VPS, beside slskd and the drainer, and this
posts to it over the tailnet using the same bearer token as every other Helm
API call. The queue module itself stays a plain file on the VPS; only the way
in is HTTP.

Falls back to a LOCAL file queue when SLSKD_QUEUE_URL is unset, so the script
is still usable standalone (and testable) without the VPS.
"""
import json
import os
import ssl
import sys
import urllib.error
import urllib.request

QUEUE_URL = os.environ.get("SLSKD_QUEUE_URL", "").rstrip("/")
TOKEN_FILE = os.environ.get("HELM_TOKEN_FILE", "/home/isaboo/.config/helm/token")
TIMEOUT = int(os.environ.get("SLSKD_QUEUE_TIMEOUT", "10"))


def read_token():
    try:
        with open(TOKEN_FILE) as f:
            return f.read().strip()
    except OSError:
        return None


def _post(path, payload):
    """POST JSON to the VPS with the Helm bearer token. Returns (ok, body)."""
    if not QUEUE_URL:
        return False, "SLSKD_QUEUE_URL is not set"
    token = read_token()
    if not token:
        return False, f"no token at {TOKEN_FILE}"
    req = urllib.request.Request(
        QUEUE_URL + path,
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + token,
        },
        method="POST",
    )
    ctx = None
    if QUEUE_URL.startswith("https://"):
        ctx = ssl.create_default_context()
        # Helm's tailnet cert is self-signed; Tailscale already encrypts the
        # path. Same trust model as the rest of Helm.
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=ctx) as r:
            raw = r.read().decode()
            return True, (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}: {e.read().decode()[:200]}"
    except Exception as e:  # noqa: BLE001
        return False, str(e)[:200]


def post_queries(queries, source="mail"):
    """POST queries to the VPS queue. Returns the list accepted."""
    if not queries:
        return []
    if not QUEUE_URL:
        # No URL configured: use the local file queue directly.
        sys.path.insert(0, os.environ.get("SLSKD_SCRIPTS", os.path.dirname(
            os.path.abspath(__file__))))
        import slskd_queue
        return slskd_queue.enqueue(queries, source=source)

    ok, data = _post("/api/slskd/queue",
                     {"queries": queries, "source": source})
    if not ok:
        print(f"slskd queue: {data}", file=sys.stderr)
        return []
    # _post returns a parsed dict on success, but guard anyway: a non-JSON 200
    # would otherwise raise AttributeError inside the pipe.
    return (data.get("added") or []) if isinstance(data, dict) else []


def post_pick(query, index=1, album=None):
    """Ask the VPS to download a source from its persisted offers.

    The pick handler runs on the VPS because the offers and slskd live there.
    Returns (ok, message).
    """
    payload = {"query": query, "index": int(index)}
    if album:
        payload["album"] = album
    return _post("/api/slskd/pick", payload)

