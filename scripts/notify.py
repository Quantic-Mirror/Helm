#!/usr/bin/env python3
"""Send a notification as real SMTP to the local mailbox on hyperion.

Why SMTP and not a file drop: the message should travel over the network and
land in a real mailbox, readable by pine/mutt/mail, exactly like ordinary mail.
This is the injection side only — it needs no MTA of its own, because
smtplib speaks SMTP directly.

smtplib is stdlib and still present in Python 3.14 (only the *server* half,
smtpd, was removed in 3.12), so this has no dependencies.

Usage:
    notify.py "Subject line" [body-text] [--from addr] [--to addr]

Defaults route to isaboo@hyperion over the Tailscale address.

Spooling (VPS): hyperion is not always on. When a spool directory is set --
HELM_NOTIFY_SPOOL, or ~/mail-spool if that directory exists -- a message that
cannot be delivered because hyperion is unreachable (or answers with a
temporary 4xx) is written there as an .eml file and the send counts as
successful. scripts/mail_spool_pull.py on hyperion fetches the spool over ssh
and hands each message to hyperion's own MTA, so ~/.forward still runs.
Permanent rejections (5xx) are never spooled. With no spool directory, a
failed send still exits 1, as before.
"""
import argparse
import os
import smtplib
import socket
import sys
import time
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

HYPERION_TAILNET_IP = "100.79.12.117"
SMTP_PORT = 25


def spool_dir():
    """Directory to queue undeliverable mail in, or None when spooling is off."""
    d = os.environ.get("HELM_NOTIFY_SPOOL") or os.path.expanduser("~/mail-spool")
    if os.environ.get("HELM_NOTIFY_SPOOL") or os.path.isdir(d):
        return d
    return None


def _temporary(exc):
    """True for failures that mean 'try again later', not 'rejected'."""
    if isinstance(exc, smtplib.SMTPResponseException):
        return 400 <= exc.smtp_code < 500
    return isinstance(exc, (OSError, smtplib.SMTPServerDisconnected))


def spool_message(msg, directory):
    """Write msg to the spool atomically. Returns the file path."""
    os.makedirs(directory, mode=0o700, exist_ok=True)
    name = f"{time.time_ns()}-{os.getpid()}.eml"
    tmp = os.path.join(directory, "." + name + ".tmp")
    with open(tmp, "wb") as fh:
        fh.write(msg.as_bytes())
    final = os.path.join(directory, name)
    os.replace(tmp, final)
    return final


def send(subject, body, sender, recipient, host=HYPERION_TAILNET_IP, port=SMTP_PORT,
         timeout=15, dry_run=False, attachments=()):
    """Build and send one message. Returns True if handed to the MTA.

    attachments: (filename, bytes, mime_type) tuples, added after the body.
    """
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = recipient
    msg["Date"] = formatdate(localtime=True)
    # A Message-ID makes the message a well-formed RFC 5322 mail rather than
    # something only this script's own reader can parse. Mutt and pine are
    # stricter than a naive .eml dump, so set it properly.
    msg["Message-ID"] = make_msgid(domain="helm.local")
    msg.set_content(body)
    for filename, data, mime in attachments:
        maintype, subtype = mime.split("/", 1)
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)

    if dry_run:
        print("--- dry run, not sending ---")
        print(msg.as_string())
        return True

    # Plain SMTP on the tailnet. No STARTTLS: both ends are behind Tailscale's
    # encrypted WireGuard transport, so the traffic is already encrypted in
    # transit, and postfix on a tailnet-only listener has no cert to offer.
    try:
        with smtplib.SMTP(host, port, timeout=timeout) as s:
            s.send_message(msg)
    except (smtplib.SMTPException, OSError) as e:
        d = spool_dir()
        if d is None or not _temporary(e):
            raise
        path = spool_message(msg, d)
        print(f"spooled {msg['Message-ID']} -> {recipient} ({e}); queued at {path}")
        return True

    print(f"sent {msg['Message-ID']} -> {recipient} via {host}:{port}")
    return True


def main():
    p = argparse.ArgumentParser(description="Send a Helm notification to hyperion's mailbox.")
    p.add_argument("subject", help="Subject line")
    p.add_argument("body", nargs="?", default="", help="Message body (plain text)")
    p.add_argument("--from", dest="sender",
                   default=os.environ.get("HELM_NOTIFY_FROM", "helm@hyperion"))
    p.add_argument("--to", dest="recipient",
                   default=os.environ.get("HELM_NOTIFY_TO", "isaboo@hyperion"))
    p.add_argument("--host", default=os.environ.get("HELM_NOTIFY_HOST", HYPERION_TAILNET_IP))
    p.add_argument("--port", type=int, default=int(os.environ.get("HELM_NOTIFY_PORT", SMTP_PORT)))
    p.add_argument("--dry-run", action="store_true",
                   help="print the message instead of sending it")
    p.add_argument("--attach", action="append", default=[], metavar="PATH",
                   help="attach a file (repeatable); MIME type is guessed from the name")
    args = p.parse_args()

    import mimetypes
    attachments = []
    for path in args.attach:
        mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
        with open(path, "rb") as f:
            attachments.append((os.path.basename(path), f.read(), mime))

    try:
        send(args.subject, args.body or "", args.sender, args.recipient,
             host=args.host, port=args.port, dry_run=args.dry_run,
             attachments=attachments)
    except (smtplib.SMTPException, OSError, socket.error) as e:
        # Never exit 0 on a failed notification. Cron would treat that as
        # success and the message would be silently lost, which is the one
        # failure mode a notifier must not have.
        print(f"notify: failed to send to {args.recipient} at "
              f"{args.host}:{args.port}: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())