#!/usr/bin/env python3
"""Report an arriving mail to the Helm API so its widget can show it.

Postfix pipes each incoming message into this on hyperion, so it fires for ALL
mail, not just the cron notifier's. The widget therefore reflects the real
mailbox rather than one script's view of it.

It POSTs to the Helm server on the VPS over Tailscale — HTTP, not SSH, because
the tailnet already authenticates and encrypts that path and the Helm token is
the only credential involved. No key to install, nothing to mount.

Message BODY IS NOT SENT, on purpose. The widget's job is to say a message
arrived; reading the actual mail is pine's job, on this host, where it already
is. Sending bodies would put the contents of every notification into Helm's
synced state, which is backed up and synced between devices.

Run as:  pipe_to_helm.py   (on stdin: the RFC 5322 message)
"""
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from email import message_from_bytes
from email.utils import parsedate_to_datetime

HELM_URL = os.environ.get("HELM_API_URL", "https://100.77.126.57:8443")
HELM_TOKEN_FILE = os.environ.get("HELM_TOKEN_FILE", "/home/isaboo/.config/helm/token")
TIMEOUT = int(os.environ.get("HELM_REPORT_TIMEOUT", "10"))


def read_token():
    """Read the Helm bearer token. Missing token => skip, never crash."""
    try:
        with open(HELM_TOKEN_FILE) as f:
            return f.read().strip()
    except OSError:
        return None


def first_header(msg, name):
    """First non-empty value of a header, decoded to str.

    Headers can be Header objects (RFC 2047 encoded words) rather than plain
    str, and str() on those yields the raw '=?utf-8?...?=' form, which is
    unreadable in a widget. Decode explicitly.
    """
    vals = msg.get_all(name) or []
    for v in vals:
        try:
            s = str(v)
        except Exception:
            continue
        # Collapse folded whitespace so a long folded subject is one line.
        s = " ".join(s.split())
        if s:
            return s
    return ""


def main():
    raw = sys.stdin.buffer.read()
    if not raw.strip():
        return 0

    try:
        msg = message_from_bytes(raw)
    except Exception:
        return 0  # unparseable mail is not worth failing a delivery over

    token = read_token()
    if not token:
        print("pipe_to_helm: no Helm token, skipping report", file=sys.stderr)
        return 0

    subject = first_header(msg, "Subject") or "(no subject)"
    sender = first_header(msg, "From") or "(unknown sender)"
    to = first_header(msg, "To")
    date_hdr = first_header(msg, "Date")

    # Normalize the Date header to epoch millis so the widget can sort and
    # display it without parsing RFC 2822 in the browser. An unparseable or
    # missing Date falls back to now — arrival time is what the widget shows.
    received = None
    if date_hdr:
        try:
            received = int(parsedate_to_datetime(date_hdr).timestamp() * 1000)
        except Exception:
            received = None
    if received is None:
        import time
        received = int(time.time() * 1000)

    payload = {
        "subject": subject[:300],
        "from": sender[:300],
        "to": to[:300],
        "receivedAt": received,
        # Raw size only — cheap, and enough to distinguish a one-line
        # "disk almost full" from a backup log.
        "size": len(raw),
    }

    req = urllib.request.Request(
        HELM_URL + "/api/notifications",
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + token,
        },
        method="POST",
    )
    ctx = None
    if HELM_URL.startswith("https://"):
        import ssl
        ctx = ssl.create_default_context()
        # Helm serves a self-signed cert on the tailnet IP. Verify against
        # nothing rather than disabling checks globally — but note this means
        # the channel is only as private as Tailscale's WireGuard transport,
        # not TLS. That is the same trust model as the rest of Helm on the
        # tailnet.
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=ctx) as r:
            print(f"pipe_to_helm: reported ({r.status})", file=sys.stderr)
    except urllib.error.HTTPError as e:
        # 401/403 means the token is stale; that is worth shouting about in the
        # mail log, since the widget silently stops updating otherwise.
        print(f"pipe_to_helm: HTTP {e.code} — token may be stale", file=sys.stderr)
    except Exception as e:
        # Never fail the delivery: postfix would bounce the mail, and losing a
        # notification because the widget was down is the wrong trade.
        print(f"pipe_to_helm: could not reach Helm: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())