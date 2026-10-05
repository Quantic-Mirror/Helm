#!/usr/bin/env python3
"""Pending search queue for slskd — the single point all three inputs write to.

Three ways to ask for a Soulseek search (mail with a `slskd:` keyword, the Helm
music tab, Hermes), one queue, one drainer. The inputs are deliberately dumb:
they append a line and return. Nothing an input does can break the download
path, and adding a fourth input later is a few lines rather than new wiring.

FORMAT: one query per line, UTF-8, `#` comments and blank lines ignored.
Appending is an atomic single write() of the whole block, so two inputs
racing cannot interleave partial lines.

State lives in two files:
  <state>/slskd_queue.txt  — pending queries, oldest first
  <state>/slskd_seen.json  — recently drained queries with timestamps, so a
                              repeated request is not silently re-run forever
"""
import json
import os
import sys
import time

# Where the queue file lives.
#
# This module is imported in TWO places with different filesystem views:
#   - the helm container, where the state dir is /app/state (compose bind mount)
#   - the VPS host, where it is ./data relative to the checkout
# So the default cannot be a host path. Prefer the container's own convention
# when it exists, and let SLSKD_STATE_DIR override either way.
#
# Getting this wrong raised PermissionError on /home/isaboo from inside the
# container (that path does not exist there and /app is not writable outside
# the state mount), which surfaced as a dropped connection with no response.


def _default_state_dir():
    # Inside the container the state dir is bind-mounted at /app/state.
    if os.path.isdir("/app/state"):
        return "/app/state"
    # On the host, ./data next to this checkout.
    here = os.path.dirname(os.path.abspath(__file__))
    candidate = os.path.join(os.path.dirname(here), "data")
    if os.path.isdir(candidate):
        return candidate
    return "/tmp"


STATE = os.environ.get("SLSKD_STATE_DIR") or _default_state_dir()
QUEUE = os.path.join(STATE, "slskd_queue.txt")
SEEN = os.path.join(STATE, "slskd_seen.json")

# How long a drained query is remembered. Long enough that re-asking for the
# same album next week re-runs it (people do change their mind), short enough
# that the file does not grow forever.
SEEN_TTL_DAYS = 30
SEEN_MAX = 500

MAGIC = "slskd:"


def _atomic_append(path, text):
    """Append text to a file atomically.

    O_APPEND plus a single write() of the full block: the kernel makes the
    write atomic for sizes under PIPE_BUF-ish limits on most filesystems, and
    even if two writers interleave, whole lines survive because each block ends
    with a newline. A read-modify-write would lose entries under concurrency.
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())


def normalise_query(q):
    """Collapse whitespace, strip the magic keyword, reject junk."""
    q = (q or "").strip()
    if q.lower().startswith(MAGIC):
        q = q[len(MAGIC):].strip()
    # Collapse runs of whitespace; a query with a newline in it came from a
    # multi-line mail body and must be split before it gets here.
    q = " ".join(q.split())
    if not q or len(q) < 2:
        return None
    if len(q) > 200:
        return None
    return q


def extract_queries(text):
    """Pull queries out of a message body.

    Two shapes are supported, and they mean different things:

      slskd: aphex twin
      boards of canada
      boards of canada
             ^ these extra lines are SEPARATE queries

      aphex twin
      boards of canada
             ^ a bare list is one query per line

    So a marker line is always exactly one query, and any following non-marker
    lines are additional queries. There is no "continuation" case: a Soulseek
    search is a single short phrase, so joining lines would only ever produce a
    query nobody wanted. Blank lines and `#` comments are ignored anywhere.
    """
    out = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # Strip the marker only when it starts the line. A mid-word occurrence
        # ("myslskd:plans") is left alone, so it becomes an ordinary query
        # rather than being mangled into one.
        if line.lower().startswith(MAGIC):
            line = line[len(MAGIC):].strip()
        q = normalise_query(line)
        if q:
            out.append(q)
    return out


def enqueue(queries, source="unknown"):
    """Append queries. Returns the list actually accepted."""
    accepted = []
    for q in queries:
        nq = normalise_query(q)
        if nq:
            accepted.append(nq)
    if not accepted:
        return []
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
    block = "".join(f"{nq}\t{stamp}\t{source}\n" for nq in accepted)
    _atomic_append(QUEUE, block)
    return accepted


def _load_seen():
    """Read the drained-query index. Corrupt/missing => empty, but say so.

    A corrupt index means the dedup history is gone, and that is worth knowing:
    it is the difference between "no searches pending" and "every previously
    drained query will run again". Returning empty is still the right
    behaviour — a broken index must not stop the queue — but the caller logs
    the condition rather than silently proceeding.
    """
    global _SEEN_CORRUPT
    _SEEN_CORRUPT = False
    try:
        with open(SEEN) as f:
            data = json.load(f)
        if not isinstance(data, dict):
            _SEEN_CORRUPT = True
            return {}
        return data
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        _SEEN_CORRUPT = True
        return {}


# Set by _load_seen() when the index exists but could not be parsed.
_SEEEN_CORRUPT = False


def _save_seen(seen):
    tmp = SEEN + ".tmp"
    with open(tmp, "w") as f:
        json.dump(seen, f, indent=1)
    os.replace(tmp, SEEN)


def read_pending(limit=20, skip_recent=True):
    """Pending queries, oldest first.

    Lines are `query<TAB>timestamp<TAB>source`. Unparseable lines are skipped
    rather than fatal, because one bad line must not stop the queue draining.
    """
    try:
        with open(QUEUE) as f:
            raw = f.read()
    except OSError:
        return []

    seen = _load_seen() if skip_recent else {}
    if skip_recent and _SEEN_CORRUPT:
        # Losing the index means every previously drained query becomes pending
        # again. Degrade to running them (a duplicate search is harmless, an
        # unexplained re-run of a week's history is not) but make it visible.
        print(f"WARNING: {SEEN} is unreadable; dedup history lost, "
              f"previously-drained queries will run again", file=sys.stderr)
    now = time.time()
    out = []
    for line in raw.splitlines():
        line = line.rstrip("\n")
        if not line.strip():
            continue
        parts = line.split("\t")
        q = normalise_query(parts[0])
        if not q:
            continue
        ts = parts[1] if len(parts) > 1 else ""
        src = parts[2] if len(parts) > 2 else "unknown"
        if skip_recent and q.lower() in seen:
            last = seen[q.lower()]
            try:
                age_days = (now - float(last)) / 86400
            except (TypeError, ValueError):
                age_days = SEEN_TTL_DAYS + 1
            if age_days < SEEN_TTL_DAYS:
                continue
        out.append({"query": q, "queued_at": ts, "source": src})

    return out[:limit]


def mark_drained(queries):
    """Record queries as done so they are not re-run on the next pass."""
    seen = _load_seen()
    now = time.time()
    for q in queries:
        nq = normalise_query(q)
        if nq:
            seen[nq.lower()] = now
    if len(seen) > SEEN_MAX:
        # Drop the oldest half rather than sorting on every write.
        items = sorted(seen.items(), key=lambda kv: kv[1])
        seen = dict(items[-SEEN_MAX:])
    _save_seen(seen)


def clear_pending(queries):
    """Remove specific queries from the pending file (used after a drain)."""
    try:
        with open(QUEUE) as f:
            lines = f.read().splitlines()
    except OSError:
        return 0
    drop = set()
    for q in queries:
        nq = normalise_query(q)
        if nq:
            drop.add(nq.lower())
    kept, removed = [], 0
    for line in lines:
        q = normalise_query(line.split("\t")[0])
        if q and q.lower() in drop:
            removed += 1
            continue
        kept.append(line)
    tmp = QUEUE + ".tmp"
    with open(tmp, "w") as f:
        f.write("\n".join(kept) + ("\n" if kept else ""))
    os.replace(tmp, QUEUE)
    return removed


def status():
    try:
        with open(QUEUE) as f:
            n = len([l for l in f if l.strip()])
    except OSError:
        n = 0
    seen = _load_seen()
    return {"pending": n, "remembered": len(seen), "queue_file": QUEUE}


if __name__ == "__main__":
    # CLI: `add [query...]` | `pending` | `status`. `add` reads stdin when no
    # query is given, so a mail body can be piped straight in.
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "add":
        text = " ".join(sys.argv[2:]) if len(sys.argv) > 2 else sys.stdin.read()
        got = enqueue(extract_queries(text), "cli")
        print(json.dumps({"added": got}))
    elif cmd == "pending":
        print(json.dumps(read_pending(), indent=1))
    else:
        print(json.dumps(status(), indent=1))
