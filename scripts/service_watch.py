#!/usr/bin/env python3
"""Record Helm service up/down transitions as activity events.

Runs on the VPS on a timer. It asks Helm's own /api/services for the current
status of each monitored service, compares against the last snapshot, and
emits an event on each change. The morning digest reads those events, so a
service that went down and came back in the same day still shows up.

Timers are skipped. A systemd timer reports "not running" between firings, so
treating that as down would alert every hour for nothing.

Config (env):
    HELM_API_URL     base URL of helm_server.py (default: the VPS tailnet address)
    HELM_TOKEN_FILE  bearer token file (default: helm_token.txt in the state dir)
"""
import json
import os
import ssl
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from activity import emit, REPO, HELM_URL  # noqa: E402

API_URL = os.environ.get("HELM_API_URL", HELM_URL)
TOKEN_FILE = os.environ.get("HELM_TOKEN_FILE", os.path.join(REPO, "data", "helm_token.txt"))
SNAPSHOT = os.environ.get("SERVICE_WATCH_STATE", os.path.join(REPO, "data", "service_watch.json"))
TIMEOUT = 20


def fetch_services():
    try:
        with open(TOKEN_FILE) as f:
            token = f.read().strip()
    except OSError:
        token = ""
    req = urllib.request.Request(API_URL + "/api/services",
                                 headers={"Authorization": "Bearer " + token})
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with urllib.request.urlopen(req, timeout=TIMEOUT, context=ctx) as r:
        return json.loads(r.read()).get("services", [])


def load_snapshot():
    try:
        with open(SNAPSHOT) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def save_snapshot(snap):
    # Same atomic write as the rest of the state files.
    tmp = SNAPSHOT + ".tmp"
    with open(tmp, "w") as f:
        json.dump(snap, f)
    os.replace(tmp, SNAPSHOT)


def main():
    try:
        services = fetch_services()
    except Exception as e:  # noqa: BLE001 -- unattended; one skipped run, not a crash
        print(f"service-watch: could not fetch /api/services: {e}", file=sys.stderr)
        return 1

    current = {s["id"]: {"label": s.get("label", s["id"]), "running": bool(s.get("running")),
                         "status": s.get("status", ""), "type": s.get("type")}
               for s in services if s.get("type") != "systemd-timer"}

    prev = load_snapshot()
    if prev is None:
        # First run: record the baseline without announcing every service as "up".
        save_snapshot(current)
        return 0

    for sid, now in current.items():
        before = prev.get(sid)
        if before is None or before["running"] == now["running"]:
            continue
        if now["running"]:
            emit("service", "up", now["label"], f"{now['label']} is back up")
        else:
            emit("service", "down", now["label"], f"{now['label']} is down: {now['status']}")

    save_snapshot(current)
    return 0


if __name__ == "__main__":
    sys.exit(main())
