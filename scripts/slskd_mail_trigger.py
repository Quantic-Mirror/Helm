#!/usr/bin/env python3
"""Postfix pipe: turn an incoming `slskd:` mail into queued search queries.

Installed as a second command in hyperion's ~/.forward alongside the existing
pipe to Helm, so a single message can notify BOTH: "slskd: aphex twin" would
appear in the Helm mail badge and queue a Soulseek search.

Only acts on mail that actually asked for it:
  - the subject or body contains the `slskd:` marker, OR
  - it is addressed to the search alias
Everything else exits 0 immediately. Silent no-op is the important part: this
pipe runs on EVERY incoming message, and mailing about it would turn "mail me
about my backups" into a new mail about my backups.

Never enqueues a download. This only queues searches; the drainer mails results
and a download happens only from an explicit pick.

Always exits 0 even on failure. A non-zero exit makes postfix hold the message
and retry, which would delay the Helm notification the other pipe sends.
"""
import os
import sys

sys.path.insert(0, os.environ.get(
    "SLSKD_SCRIPTS", "/home/isaboo/repos/Helm/scripts"))

# Import defensively: a broken queue module must not take the Helm pipe down
# with it, because both run from the same ~/.forward.
try:
    import slskd_queue as Q
except Exception as e:  # noqa: BLE001
    print(f"slskd pipe: queue module unavailable ({e}); skipping",
          file=sys.stderr)
    Q = None

MAGIC = "slskd:"
ALIASES = {"slskd@hyperion", "search@hyperion", "slskd-search@hyperion"}


def has_marker(text):
    """True if `slskd:` appears as a standalone token, not inside a word.

    A plain substring test matches "myslskd:plans" and "re: slskd-ish", which
    would queue ordinary mail as a search. The marker has to start a line (or
    follow whitespace) to count.
    """
    import re
    return re.search(r"(?:^|\s)" + re.escape(MAGIC), text or "", re.I) is not None



def main():
    raw = sys.stdin.buffer.read()
    if not raw.strip():
        return 0

    from email import message_from_bytes
    from email.utils import parsedate_to_datetime

    try:
        msg = message_from_bytes(raw)
    except Exception as e:  # noqa: BLE001
        print(f"slskd pipe: unparseable message: {e}", file=sys.stderr)
        return 0

    def header(name):
        for v in msg.get_all(name) or []:
            try:
                s = " ".join(str(v).split())
            except Exception:
                continue
            if s:
                return s
        return ""

    subject = header("Subject")
    to = header("To").lower()
    sender = header("From")

    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                try:
                    body += part.get_payload(decode=True).decode(
                        "utf-8", errors="replace")
                except Exception:
                    continue
    else:
        try:
            payload = msg.get_payload(decode=True)
            body = payload.decode("utf-8", errors="replace") if payload else ""
        except Exception:
            body = ""

    # Does this message actually ask for a search?
    addressed_to_alias = any(a in to for a in ALIASES)
    if not (addressed_to_alias or has_marker(subject) or has_marker(body)):
        return 0

    if Q is None:
        return 0

    # The subject is searched too: "slskd: aphex twin" as a subject line is the
    # natural way to mail this from a phone, with no body at all. Only prepend
    # it when it carries the marker, so an unrelated subject never becomes a
    # query.
    haystack = body
    if addressed_to_alias or has_marker(subject):
        haystack = subject + "\n" + body

    queries = Q.extract_queries(haystack)
    # Strip the marker from the subject before parsing so "slskd: X" is not
    # double-counted with an identical body line.
    seen, uniq = set(), []
    for q in queries:
        k = q.lower()
        if k not in seen:
            seen.add(k)
            uniq.append(q)

    if not uniq:
        print("slskd pipe: marker present but no usable query", file=sys.stderr)
        return 0

    source = "mail"
    try:
        # Prefer a subject-derived source so the notification can say where it
        # came from without re-parsing the address later.
        if sender and "root@" not in sender.lower():
            source = "mail"
        added = Q.enqueue(uniq, source=source)
    except Exception as e:  # noqa: BLE001
        print(f"slskd pipe: enqueue failed: {e}", file=sys.stderr)
        return 0

    if added:
        print(f"slskd pipe: queued {len(added)} quer"
              f"{'y' if len(added) == 1 else 'ies'}: {', '.join(added)}",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # noqa: BLE001
        # Belt and braces: this pipe must never fail a delivery.
        print(f"slskd pipe: unexpected error: {e}", file=sys.stderr)
        sys.exit(0)
