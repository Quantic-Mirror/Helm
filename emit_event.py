#!/usr/bin/env python3
"""
emit_event.py — publish a backup-pipeline event onto RabbitMQ.

Talks straight to the RabbitMQ management HTTP API (urllib, stdlib only)
rather than pulling in an AMQP client library like pika — same "reuse the
daemon's own interface instead of adding a dependency" precedent as the
Docker Unix-socket helpers and the `pass`-backed vault in this repo. The
management plugin must be enabled on the broker:
    rabbitmq-plugins enable rabbitmq_management

Used two ways:
  1. As a CLI, from hyperion-backup.sh / rclone-sync-to-r2.sh:
       python3 emit_event.py <stage> <status> [--name NAME] [--message MSG] [--data JSON]
  2. As a module, imported by backup_event_worker.py for the RabbitMQ
     connection constants and the credential-fetching logic.

Credential fetch branches on hostname, because `pass` + gpg-agent only
live on hyperion:
  - On hyperion: `pass show infra/rabbitmq/helm_producer` directly.
  - On popcorn (no GPG/pass setup): go through helm_server.py's existing
    vault proxy instead — the same one vault_token.txt secures for the
    browser-facing /api/vault/* routes (see VAULT PROXY in helm_server.py).
    emit_event.py just hits that proxy's GET /api/vault/entry/<path> over
    loopback; helm_server.py is the one that holds vault_token.txt and
    forwards to vault_server.py on hyperion.
"""

import argparse
import base64
import json
import os
import socket
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

RABBITMQ_HOST = os.environ.get("RABBITMQ_HOST", "192.168.1.168")
RABBITMQ_MGMT_PORT = int(os.environ.get("RABBITMQ_MGMT_PORT", "15672"))
RABBITMQ_VHOST = "/helm"
RABBITMQ_USER = "helm_producer"
RABBITMQ_QUEUE = "backup.events"

PASS_ENTRY = "infra/rabbitmq/helm_producer"

# Where popcorn reaches its own helm_server.py to use the vault proxy.
# Override if this deployment runs helm_server.py on a different port, or
# behind HTTPS with a cert not signed by helm-ca.crt (see
# _https_context_for_helm_url).
HELM_URL = os.environ.get("HELM_URL", "http://localhost:8080")
HELM_CA_FILE = os.environ.get("HELM_CA_FILE", "")

HOSTNAME = socket.gethostname().split(".")[0]


def _password_via_pass():
    """hyperion: pass + gpg-agent live here, so fetch directly."""
    import subprocess

    result = subprocess.run(
        ["pass", "show", PASS_ENTRY], capture_output=True, text=True
    )
    if result.returncode != 0:
        raise RuntimeError(f"pass show {PASS_ENTRY} failed: {result.stderr.strip()}")
    secret = result.stdout.splitlines()[0] if result.stdout else ""
    if not secret:
        raise RuntimeError(f"pass show {PASS_ENTRY} returned no secret")
    return secret


def _https_context_for_helm_url(url):
    """
    Build an SSL context for talking to helm_server.py's own vault proxy.
    If HELM_CA_FILE (helm-ca.crt) is available, verify against it like a
    browser would. Otherwise, for a loopback URL only, skip verification --
    this is a same-machine call to a service we're about to trust with a
    RabbitMQ password anyway, so a missing local CA file shouldn't be a
    harder failure than not fetching the secret at all.
    """
    if not url.startswith("https://"):
        return None
    if HELM_CA_FILE and os.path.exists(HELM_CA_FILE):
        return ssl.create_default_context(cafile=HELM_CA_FILE)
    host = urllib.parse.urlparse(url).hostname or ""
    if host in ("localhost", "127.0.0.1", "::1"):
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    return ssl.create_default_context()


def _password_via_vault_proxy():
    """popcorn: no GPG/pass here, so go through helm_server.py's vault proxy.

    Retries once after a short delay on a genuine network-level failure
    (timeout, connection refused) -- not on an HTTP error response, which
    means the proxy WAS reached and answered, just not with what we
    wanted, so retrying wouldn't help. This specifically targets a real,
    reproducible pattern seen during scheduled (systemd timer-triggered)
    runs: the request consistently times out on the first attempt, in a
    way that doesn't reproduce on a manually-triggered run or a direct
    diagnostic curl moments earlier -- exact mechanism not confirmed, but
    a single retry costs nothing in the normal case and directly covers
    the observed symptom.
    """
    url = HELM_URL.rstrip("/") + "/api/vault/entry/" + urllib.parse.quote(PASS_ENTRY)
    ctx = _https_context_for_helm_url(url)

    last_error = None
    for attempt in (1, 2):
        req = urllib.request.Request(url, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=15, context=ctx) as resp:
                data = json.loads(resp.read())
            secret = data.get("secret", "")
            if not secret:
                raise RuntimeError(f"vault proxy returned no secret for {PASS_ENTRY}")
            return secret
        except urllib.error.HTTPError as e:
            # Reached the proxy, got a real HTTP error back -- retrying
            # won't change that, fail immediately with the real reason.
            raise RuntimeError(
                f"vault proxy at {url} returned HTTP {e.code}: {e.read().decode(errors='replace')}"
            )
        except urllib.error.URLError as e:
            last_error = e.reason
            if attempt == 1:
                print(f"emit_event: vault proxy attempt 1 failed ({e.reason}), retrying in 5s...", file=sys.stderr)
                time.sleep(5)

    raise RuntimeError(f"could not reach helm_server.py vault proxy at {url} after 2 attempts: {last_error}")


def get_rabbitmq_password():
    if HOSTNAME == "hyperion":
        return _password_via_pass()
    return _password_via_vault_proxy()


def publish_event(event):
    """POST the event to RabbitMQ's management API publish endpoint."""
    password = get_rabbitmq_password()
    vhost_enc = urllib.parse.quote(RABBITMQ_VHOST, safe="")
    url = f"http://{RABBITMQ_HOST}:{RABBITMQ_MGMT_PORT}/api/exchanges/{vhost_enc}/amq.default/publish"

    payload = {
        "properties": {"content_type": "application/json", "delivery_mode": 2},
        "routing_key": RABBITMQ_QUEUE,
        "payload": json.dumps(event),
        "payload_encoding": "string",
    }
    body = json.dumps(payload).encode("utf-8")
    creds = base64.b64encode(f"{RABBITMQ_USER}:{password}".encode()).decode()
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Basic {creds}")

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"RabbitMQ publish returned HTTP {e.code}: {e.read().decode(errors='replace')}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"could not reach RabbitMQ management API at {RABBITMQ_HOST}:{RABBITMQ_MGMT_PORT}: {e.reason}")

    if not result.get("routed"):
        # Published fine, but nothing was listening -- almost always means
        # backup_event_worker.py hasn't declared the queue yet (it hasn't
        # been started, or lacks configure permission on the vhost).
        print(
            f"emit_event: warning: event was not routed to a queue "
            f"(does '{RABBITMQ_QUEUE}' exist on vhost {RABBITMQ_VHOST}?)",
            file=sys.stderr,
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
    parser = argparse.ArgumentParser(description="Emit a backup-pipeline event to RabbitMQ")
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
        publish_event(event)
    except Exception as e:
        print(f"emit_event: failed to publish event: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
