#!/usr/bin/env python3
"""
backup_event_worker.py — drains backup-pipeline events off RabbitMQ and
persists them to disk for helm_server.py's /api/backup-events to read.

Runs on popcorn (same host as the RabbitMQ broker and helm_server.py).
Polls the management HTTP API's "get messages" endpoint rather than
holding a long-lived AMQP connection -- same reasoning as emit_event.py:
stdlib urllib only, no pika/AMQP client dependency. The management
endpoint isn't meant for high-throughput consumption, but this is a
personal dashboard's backup-events feed, not a production message queue,
so a few polls a second is more than enough headroom.

On startup, declares (PUT) the queue so it exists before emit_event.py's
first publish -- requires helm_producer to have "configure" permission on
vhost /helm. If that permission is missing, declare fails loudly at
startup rather than silently dropping every event later.

State file: backup_events.json, next to this script. Bounded ring buffer
of the most recent events, written atomically (tmp + os.replace), same
pattern as marks_state.json / helm-backups in helm_server.py.

Usage:
    python3 backup_event_worker.py
"""

import base64
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import emit_event

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
EVENTS_FILE = os.path.join(SCRIPT_DIR, "backup_events.json")

POLL_INTERVAL_SECONDS = 3
MAX_EVENTS = 500
GET_BATCH_SIZE = 50


def _auth_header():
    password = emit_event.get_rabbitmq_password()
    creds = base64.b64encode(f"{emit_event.RABBITMQ_USER}:{password}".encode()).decode()
    return f"Basic {creds}"


def _mgmt_request(method, path, body=None, auth=None):
    url = f"http://{emit_event.RABBITMQ_HOST}:{emit_event.RABBITMQ_MGMT_PORT}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", auth)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=15) as resp:
        raw = resp.read()
        return json.loads(raw) if raw else None


def declare_queue(auth):
    vhost_enc = urllib.parse.quote(emit_event.RABBITMQ_VHOST, safe="")
    path = f"/api/queues/{vhost_enc}/{urllib.parse.quote(emit_event.RABBITMQ_QUEUE, safe='')}"
    body = {"durable": True}
    try:
        _mgmt_request("PUT", path, body, auth)
    except urllib.error.HTTPError as e:
        raise RuntimeError(
            f"could not declare queue '{emit_event.RABBITMQ_QUEUE}' on vhost "
            f"{emit_event.RABBITMQ_VHOST} (HTTP {e.code}): {e.read().decode(errors='replace')}. "
            f"Does {emit_event.RABBITMQ_USER} have 'configure' permission on this vhost?"
        )
    except urllib.error.URLError as e:
        raise RuntimeError(f"could not reach RabbitMQ management API: {e.reason}")


def fetch_batch(auth):
    """Pop up to GET_BATCH_SIZE messages, acking (removing) each as read."""
    vhost_enc = urllib.parse.quote(emit_event.RABBITMQ_VHOST, safe="")
    path = f"/api/queues/{vhost_enc}/{urllib.parse.quote(emit_event.RABBITMQ_QUEUE, safe='')}/get"
    body = {
        "count": GET_BATCH_SIZE,
        "ackmode": "ack_requeue_false",
        "encoding": "auto",
    }
    try:
        return _mgmt_request("POST", path, body, auth) or []
    except urllib.error.HTTPError as e:
        print(f"backup_event_worker: get failed (HTTP {e.code}): {e.read().decode(errors='replace')}", file=sys.stderr)
        return []
    except urllib.error.URLError as e:
        print(f"backup_event_worker: could not reach RabbitMQ management API: {e.reason}", file=sys.stderr)
        return []


def load_events():
    if not os.path.exists(EVENTS_FILE):
        return []
    try:
        with open(EVENTS_FILE) as f:
            return json.load(f).get("events", [])
    except (json.JSONDecodeError, OSError):
        return []


def save_events(events):
    tmp_path = EVENTS_FILE + ".tmp"
    payload = {"events": events[-MAX_EVENTS:], "updated_at": time.time()}
    with open(tmp_path, "w") as f:
        json.dump(payload, f)
    os.replace(tmp_path, EVENTS_FILE)


def main():
    print(f"backup_event_worker: vhost={emit_event.RABBITMQ_VHOST} queue={emit_event.RABBITMQ_QUEUE}")
    auth = _auth_header()
    declare_queue(auth)
    print(f"backup_event_worker: queue declared, polling every {POLL_INTERVAL_SECONDS}s")

    events = load_events()

    while True:
        messages = fetch_batch(auth)
        if messages:
            for msg in messages:
                try:
                    event = json.loads(msg["payload"])
                except (KeyError, json.JSONDecodeError):
                    print(f"backup_event_worker: skipping malformed message: {msg}", file=sys.stderr)
                    continue
                events.append(event)
            events = events[-MAX_EVENTS:]
            save_events(events)
            print(f"backup_event_worker: drained {len(messages)} event(s)")
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nbackup_event_worker: stopping.")
    except RuntimeError as e:
        print(f"backup_event_worker: fatal: {e}", file=sys.stderr)
        sys.exit(1)
