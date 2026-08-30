#!/usr/bin/env python3
"""
emit_event.py — publish a backup-pipeline event to helm_server.py.

POSTs a single JSON event to helm_server.py's POST /api/backup-events with a
shared-secret header (X-Backup-Token), the same lightweight auth model as
vault_token.txt / audio_token.txt. helm_server.py stores it in
backup_events.json (pruned server-side) for the Backup Pipeline tab to read.

This replaced a RabbitMQ broker + a drainer process (backup_event_worker.py)
that existed only to buffer a handful of status events a day — same "reuse
what's already running, don't stand up new infra" bias as the rest of the
repo. Trade-off: without the broker, an event emitted while helm_server.py is
unreachable is retried for ~3 minutes and then dropped. That's acceptable —
the backup scripts already treat emit_event.py as non-fatal telemetry.

Used as a CLI, from the backup scripts (separate repo):
    python3 emit_event.py <stage> <status> [--name NAME] [--message MSG] [--data JSON]

Config (all env, with defaults):
    HELM_URL              base URL of helm_server.py   (default http://localhost:8080)
    HELM_BACKUP_TOKEN     the shared secret, inline
    HELM_BACKUP_TOKEN_FILE  path to a file holding it  (else ./backup_token.txt
                            next to this script, or ~/.config/helm/backup_token.txt)
    HELM_CA_FILE          CA cert to verify HELM_URL's TLS against
    HELM_TLS_INSECURE     set truthy to skip TLS verification (ok on a Tailscale link)
"""

import argparse
import json
import os
import socket
import ssl
import sys
import time
import urllib.error
import urllib.request
import uuid

HELM_URL          = os.environ.get("HELM_URL", "http://localhost:8080")
HELM_CA_FILE      = os.environ.get("HELM_CA_FILE", "")
HELM_TLS_INSECURE = os.environ.get("HELM_TLS_INSECURE", "").strip().lower() not in ("", "0", "false", "no")

HOSTNAME = socket.gethostname().split(".")[0]


def _backup_token():
    tok = os.environ.get("HELM_BACKUP_TOKEN", "").strip()
    if tok:
        return tok
    path = os.environ.get("HELM_BACKUP_TOKEN_FILE", "").strip()
    if not path:
        for cand in (
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "backup_token.txt"),
            os.path.expanduser("~/.config/helm/backup_token.txt"),
        ):
            if os.path.exists(cand):
                path = cand
                break
    if path and os.path.exists(path):
        with open(path) as f:
            return f.read().strip()
    raise RuntimeError(
        "no backup token: set HELM_BACKUP_TOKEN or HELM_BACKUP_TOKEN_FILE "
        "(or drop backup_token.txt next to emit_event.py)"
    )


def _tls_context(url):
    """SSL context for talking to helm_server.py. Verify against HELM_CA_FILE if
    given; else skip verification if HELM_TLS_INSECURE is set (fine over a
    Tailscale-encrypted link); else fall back to the system trust store."""
    if not url.startswith("https://"):
        return None
    if HELM_CA_FILE and os.path.exists(HELM_CA_FILE):
        return ssl.create_default_context(cafile=HELM_CA_FILE)
    if HELM_TLS_INSECURE:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    return ssl.create_default_context()


def post_event(event):
    """POST the event to helm_server.py's /api/backup-events.

    Best-effort: retry a few times over a few minutes on a network-level
    failure (connection refused, timeout) -- this keeps the empirically
    observed pattern that a scheduled run failing here succeeds on a manual
    retry moments later. An HTTP error response means the server WAS reached
    and answered, so fail immediately with the real reason.
    """
    url = HELM_URL.rstrip("/") + "/api/backup-events"
    ctx = _tls_context(url)
    token = _backup_token()
    body = json.dumps(event).encode("utf-8")

    max_attempts = 6
    delay_seconds = 30
    last_error = None

    for attempt in range(1, max_attempts + 1):
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("X-Backup-Token", token)
        try:
            with urllib.request.urlopen(req, timeout=15, context=ctx) as resp:
                resp.read()
            return
        except urllib.error.HTTPError as e:
            raise RuntimeError(
                f"{url} returned HTTP {e.code}: {e.read().decode(errors='replace')}"
            )
        except urllib.error.URLError as e:
            last_error = e.reason
            if attempt < max_attempts:
                print(
                    f"emit_event: attempt {attempt}/{max_attempts} failed "
                    f"({e.reason}), retrying in {delay_seconds}s...",
                    file=sys.stderr,
                )
                time.sleep(delay_seconds)

    raise RuntimeError(
        f"could not reach {url} after {max_attempts} attempts "
        f"over ~{max_attempts * delay_seconds}s: {last_error}"
    )


def build_event(stage, status, name="", message="", data=None):
    return {
        "id": str(uuid.uuid4()),
        "ts": time.time(),
        "host": HOSTNAME,
        "stage": stage,
        "status": status,
        "name": name,
        "message": message,
        "data": data or {},
    }


def main():
    parser = argparse.ArgumentParser(description="Emit a backup-pipeline event to helm_server.py")
    parser.add_argument("stage", help="e.g. hyperion_backup, r2_sync")
    parser.add_argument("status", help="e.g. started, ok, failed")
    parser.add_argument("--name", default="", help="backup/folder name this event is about")
    parser.add_argument("--message", default="", help="free-text detail")
    parser.add_argument("--data", default="{}", help="extra fields as a JSON object")
    args = parser.parse_args()

    try:
        extra = json.loads(args.data)
        if not isinstance(extra, dict):
            raise ValueError("--data must be a JSON object")
    except (json.JSONDecodeError, ValueError) as e:
        print(f"emit_event: invalid --data: {e}", file=sys.stderr)
        sys.exit(2)

    event = build_event(args.stage, args.status, args.name, args.message, extra)

    try:
        post_event(event)
    except Exception as e:
        print(f"emit_event: failed to post event: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
