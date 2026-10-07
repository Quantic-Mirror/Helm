#!/usr/bin/env python3
"""Post one activity event to Helm's backup-event stream, without failing the caller.

The morning digest (scripts/morning_digest.py, on the VPS) reads what happened
in the last day from /api/backup-events. Music work runs on hyperion and the
VPS, and service transitions are seen by the VPS watcher, so each of them
reports through emit_event.py rather than writing a second file somewhere the
digest can't reach.

Every event gets a unique name. backup_events are pruned to the last two per
(stage, name), so reusing a name like "Navidrome" would quietly drop older
transitions on a service that flaps.

Non-fatal by design: a Helm outage must not make a finished import look failed.
"""
import os
import subprocess
import sys
import time

REPO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
HELM_URL = os.environ.get("HELM_URL", "https://100.77.126.57:8443")


def emit(stage, status, label, message=""):
    env = dict(os.environ)
    env.setdefault("HELM_URL", HELM_URL)
    # Helm's tailnet cert is self-signed; the link is already WireGuard-encrypted.
    env.setdefault("HELM_TLS_INSECURE", "1")
    # On the VPS the token sits in the state dir, not next to emit_event.py.
    # On hyperion this file doesn't exist and emit_event.py finds its own.
    token = os.path.join(REPO, "data", "backup_token.txt")
    if os.path.exists(token):
        env.setdefault("HELM_BACKUP_TOKEN_FILE", token)
    name = f"{label} @{time.time():.0f}"
    cmd = [sys.executable, os.path.join(REPO, "emit_event.py"), stage, status,
           "--name", name, "--message", message]
    try:
        r = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as e:
        print(f"activity: could not run emit_event.py: {e}", file=sys.stderr)
        return False
    if r.returncode != 0:
        print(f"activity: emit failed: {r.stderr.strip()[:200]}", file=sys.stderr)
        return False
    return True
