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
import re
import sys

sys.path.insert(0, os.environ.get(
    "SLSKD_SCRIPTS", "/home/isaboo/repos/Helm/scripts"))

# Import defensively: a broken module must not take the Helm pipe down with it,
# because both run from the same ~/.forward.
try:
    import slskd_queue_client as QC
except Exception as e:  # noqa: BLE001
    print(f"slskd pipe: queue client unavailable ({e}); skipping",
          file=sys.stderr)
    QC = None

MAGIC = "slskd:"
# Pick syntax: "slskd-get: [2] caribou" or "slskd-get: [2] caribou --album Bloom".
# Deliberately NOT matched by the MAGIC check above — "slskd-get:" does not
# start with "slskd:", so a pick reply can never be mistaken for a new search.
# Verified: re.search(r"(?:^|\s)slskd:", "slskd-get: [1] x") is None.
PICK_MAGIC = "slskd-get:"
ALIASES = {"slskd@hyperion", "search@hyperion", "slskd-search@hyperion"}

# Senders whose mail this pipe must never act on. notify.py sends as
# `helm@vps`, and system mail on hyperion arrives from root@localhost. Without
# this, the system reads its own failure reports as new requests.
#
# helm@hyperion is what notify.py sends as now. It used to be helm@vps, and
# changing the sender without updating this list silently re-opened the mail
# loop (a failure report containing "slskd: <query>" gets queued as a search).
# Keep this in step with HELM_NOTIFY_FROM in notify.py -- test_slskd_trigger.py
# checks the default sender against this list.
_OWN_SENDERS = ("helm@hyperion", "helm@vps", "root@localhost", "root@hyperion",
                "isaboo@localhost")

# Captures an optional [N] and the query. The --album suffix is stripped off
# BEFORE this runs (see parse_pick), because a lazy query group swallows the
# whole rest of the line including "--album=..." — so a single combined regex
# silently treats the flag as part of the query.
_PICK_RE = re.compile(
    r"^\[?\s*(?P<index>\d+)?\s*\]?\s*(?P<query>.+?)\s*$")

# --album "X", --album=X, or --album X.
#
# Deliberately just locates the flag and takes the remainder, rather than trying
# to match the value inside one pattern. A single regex failed on
# `--album=Honey` and `--album "Up in Flames"` because the value may or may not
# be quoted and the separator may or may not be "=", so every combination needed
# its own branch.
_ALBUM_FLAG_RE = re.compile(r"--album(?:[\s=]+|(?=\S))", re.I)


def parse_pick(text):
    """Parse a pick request. Returns dict or None.

    Accepts:
        slskd-get: [1] caribou
        slskd-get: 1 caribou
        slskd-get: caribou            (no number -> the top source)
        slskd-get: [1] caribou --album Bloom
        slskd-get: [1] caribou --album "Up in Flames"
    """
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line.lower().startswith(PICK_MAGIC):
            continue
        rest = line[len(PICK_MAGIC):].strip()
        if not rest:
            continue

        album = None
        am = _ALBUM_FLAG_RE.search(rest)
        if am:
            album = rest[am.end():].strip().strip("\"'").strip() or None
            rest = rest[:am.start()].strip()

        m = _PICK_RE.match(rest)
        if not m:
            return None
        query = re.sub(r"\s+", " ", (m.group("query") or "").strip())
        if not query:
            return None
        idx = m.group("index")
        return {
            "index": int(idx) if idx else 1,
            "query": query,
            "album": album,
        }
    return None




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

    # Ignore our own mail.
    #
    # Not optional, and this bug happened live: the "no source available" report
    # ends with "re-run the search with:  slskd: <query>", and the pipe sees
    # EVERY incoming message. So one failure mail queued five fresh searches
    # ("xan: offline", "Attempts:", a sentence fragment, ...), which mailed
    # their own results, which can fail and mail another hint.
    sender = header("From").lower()
    if any(s in sender for s in _OWN_SENDERS):
        print(f"slskd pipe: ignoring own mail from {sender!r}", file=sys.stderr)
        return 0

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

    # A pick is handled before anything else, and is a different action from a
    # search: it must not also queue a query.
    pick = parse_pick(subject) or parse_pick(body)
    if pick and QC is not None:
        # The pick runs on the VPS: the persisted offers and slskd both live
        # there, and there is no shared filesystem between the two hosts. So it
        # is posted like a queue entry rather than run locally.
        print(f"slskd pipe: forwarding pick {pick} to the VPS", file=sys.stderr)
        try:
            QC.post_pick(pick["query"], pick["index"], album=pick.get("album"))
        except Exception as e:  # noqa: BLE001
            print(f"slskd pipe: could not forward pick: {e}", file=sys.stderr)
        return 0

    # Does this message actually ask for a search?
    addressed_to_alias = any(a in to for a in ALIASES)
    if not (addressed_to_alias or has_marker(subject) or has_marker(body)):
        return 0

    if QC is None:
        return 0

    # The subject is searched too: "slskd: aphex twin" as a subject line is the
    # natural way to mail this from a phone, with no body at all. Only prepend
    # it when it carries the marker, so an unrelated subject never becomes a
    # query.
    haystack = body
    if addressed_to_alias or has_marker(subject):
        haystack = subject + "\n" + body

    try:
        import slskd_queue
        queries = slskd_queue.extract_queries(haystack)
    except Exception as e:  # noqa: BLE001
        print(f"slskd pipe: could not parse queries: {e}", file=sys.stderr)
        return 0

    # The same query can arrive as both a marked subject and a body line; keep
    # one of each.
    seen, uniq = set(), []
    for q in queries:
        k = q.lower()
        if k not in seen:
            seen.add(k)
            uniq.append(q)

    if not uniq:
        print("slskd pipe: marker present but no usable query", file=sys.stderr)
        return 0

    try:
        added = QC.post_queries(uniq, source="mail")
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
