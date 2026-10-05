#!/usr/bin/env python3
"""Persist ranked Soulseek candidates so a later pick can act on them.

slskd drops completed searches within a couple of hours (measured: ~15 min old
still 200, a few hours old 404), so the search a results mail refers to is
usually gone by the time you read it. This stores the per-source file list at
drain time instead, which is what makes `slskd-get:` work at all.

Why this is also more correct, not just more convenient: the files recorded
here are the exact files that were ranked and mailed. A pick therefore downloads
what you were shown, rather than re-running the search and getting a different
answer from a different peer.

An "offer" is one (query, source) pair with the files to request:

    {
      "id":      "caribou:0",         stable within a query
      "query":   "caribou",
      "username": "lunastarwarp",
      "score":   102.0,
      "savedAt": 1791205871.2,
      "searchId": "...",
      "files":   [ {filename, extension, size, length, bitDepth, sampleRate}, ... ]
    }

Only the fields slskd's enqueue endpoint needs are kept, plus enough to render a
confirmation mail. Bodies/file contents are not stored.
"""
import json
import os
import time

OFFER_TTL_HOURS = 72.0
OFFER_MAX = 40

# The enqueue API needs the exact remote path, extension, size and length.
_ENQUEUE_FIELDS = ("filename", "extension", "size", "length", "bitDepth",
                   "sampleRate", "isLocked", "code")


def store_path(state_dir):
    return os.path.join(state_dir, "slskd_offers.json")


def _load(path):
    try:
        with open(path) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save_offers(state_dir, entries, ttl_hours=OFFER_TTL_HOURS, max_offers=OFFER_MAX):
    """Persist offers. `entries` is a list of dicts as described above.

    Returns the number stored. Existing entries for the same query are replaced,
    so re-running a search refreshes its offers rather than accumulating
    duplicates that make "[1]" ambiguous.
    """
    path = store_path(state_dir)
    data = _load(path)
    offers = data.get("offers")
    if not isinstance(offers, list):
        offers = []

    by_query = {}
    for o in offers:
        if isinstance(o, dict) and o.get("query"):
            by_query.setdefault(o["query"], []).append(o)

    now = time.time()
    # Replace each incoming query's offers wholesale, so re-running a search
    # refreshes its sources instead of appending duplicates that make "[1]"
    # ambiguous. Done per-query BEFORE adding, not inside the loop — wiping on
    # each iteration left only the last source standing.
    incoming = {}
    for e in entries:
        if not e or not e.get("query") or not e.get("username"):
            continue
        incoming.setdefault(e["query"], []).append(e)

    for q, group in incoming.items():
        by_query[q] = []
        for i, e in enumerate(group):
            by_query[q].append({
                "id": e.get("id") or f"{q}:{i}",
                "query": q,
                "username": e["username"],
                "score": e.get("score"),
                "savedAt": now,
                "searchId": e.get("searchId"),
                "artist": e.get("artist"),
                "files": [{k: f.get(k) for k in _ENQUEUE_FIELDS if k in f}
                          for f in (e.get("files") or [])],
            })

    flat = [o for group in by_query.values() for o in group]
    # Expire old queries, then cap by recency.
    cutoff = now - ttl_hours * 3600
    flat = [o for o in flat if (o.get("savedAt") or 0) >= cutoff]
    flat.sort(key=lambda o: -(o.get("savedAt") or 0))
    flat = flat[:max_offers]

    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"offers": flat, "updated_at": now}, f, indent=1)
    os.replace(tmp, path)
    return len(flat)


def get_offers(state_dir, query=None, ttl_hours=OFFER_TTL_HOURS):
    """All offers, or just those for one query. Expired entries are skipped."""
    data = _load(store_path(state_dir))
    offers = data.get("offers")
    if not isinstance(offers, list):
        return []
    cutoff = time.time() - ttl_hours * 3600
    out = [o for o in offers
           if isinstance(o, dict) and (o.get("savedAt") or 0) >= cutoff]
    if query:
        q = query.lower()
        out = [o for o in out if (o.get("query") or "").lower() == q]
    return out


def queries(state_dir, ttl_hours=OFFER_TTL_HOURS):
    """Distinct queries with offers, most recent first."""
    seen = {}
    for o in get_offers(state_dir, ttl_hours=ttl_hours):
        q = o.get("query")
        if q:
            seen[q] = max(seen.get(q, 0), o.get("savedAt") or 0)
    return sorted(seen, key=lambda q: -seen[q])


def resolve(state_dir, query, index, ttl_hours=OFFER_TTL_HOURS):
    """Look up one offer by query + 1-based index.

    Returns (offer, error). The index matches the number shown in the results
    mail, which is why the offer list is stored in rank order.
    """
    offers = [o for o in get_offers(state_dir, query=query, ttl_hours=ttl_hours)
              if not o.get("claimed")]
    if not offers:
        return None, (f"no stored results for {query!r} — they may have expired "
                      f"({ttl_hours:.0f}h) or the search was re-run")
    if index < 1 or index > len(offers):
        return None, (f"source [{index}] does not exist — that search offered "
                      f"{len(offers)} source(s)")
    return offers[index - 1], None


def mark_claimed(state_dir, offer, claimed=True):
    """Flag an offer as used, so a replayed pick mail is not a second download."""
    path = store_path(state_dir)
    data = _load(path)
    offers = data.get("offers")
    if not isinstance(offers, list):
        return False
    changed = False
    for o in offers:
        if isinstance(o, dict) and o.get("id") == offer.get("id") \
                and o.get("query") == offer.get("query"):
            o["claimed"] = bool(claimed)
            o["claimedAt"] = time.time() if claimed else None
            changed = True
    if changed:
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(offers and {"offers": offers,
                                  "updated_at": data.get("updated_at")}
                      or {"offers": []}, f, indent=1)
        os.replace(tmp, path)
    return changed


if __name__ == "__main__":
    import sys
    sd = sys.argv[1] if len(sys.argv) > 1 else "/home/isaboo/helm/data"
    print(json.dumps({"queries": queries(sd),
                      "offers": len(get_offers(sd))}, indent=1))
