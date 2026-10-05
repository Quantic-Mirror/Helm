#!/usr/bin/env python3
"""Drain the slskd queue: run each pending search and mail the candidates.

This is the consumer for everything the three inputs write. It searches, ranks,
and mails — it does NOT download. A download only happens from an explicit pick
after the results land, because a Soulseek search returns the wrong genre often
enough (a "beach house" query matches Proto House compilations) that
auto-downloading is how you end up with 40 GB of the wrong thing.

One mail per batch, not per query: mailing once per search for a 20-album list
is 20 notifications and trains you to ignore the channel.

Nothing here enqueues anything, so a failure cannot lose a queued query: the
drainer marks queries drained only after a search completes, and a query that
fails is left pending for the next pass.
"""
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.environ.get(
    "SLSKD_SCRIPTS", os.path.dirname(os.path.abspath(__file__))))

import slskd_offers as OFF
import slskd_queue as Q
import slskd_search as S

NOTIFY = os.environ.get("NOTIFY", os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "notify.py"))
RECIPIENT = os.environ.get("DIGEST_TO", "isaboo@hyperion")
MAX_QUERIES = int(os.environ.get("SLSKD_MAX_PER_PASS", "5"))
MAX_SOURCES = int(os.environ.get("SLSKD_MAX_SOURCES", "6"))

# Where ranked candidates are persisted so a later `slskd-get:` pick has
# something to work from.
#
# This is not optional bookkeeping. slskd discards completed searches far sooner
# than its documented 7-day retention — measured: a search ~15 minutes old
# still fetched 200, while two from a few hours earlier were 404. So by the time
# you read the results mail, the search it refers to is usually already gone and
# a pick handler would have nothing to download from. Saving the file list at
# drain time is what makes the pick possible at all, and it also guarantees the
# files you were shown are the files you get.
OFFERS = os.path.join(Q.STATE, "slskd_offers.json")
OFFER_TTL_HOURS = float(os.environ.get("SLSKD_OFFER_TTL_HOURS", "72"))
OFFER_MAX = 40

# Per-source summary length in the mail.
MAX_FILES_LISTED = 8


def mail(subject, body):
    if not os.path.exists(NOTIFY):
        print(f"drain: notify.py missing at {NOTIFY}", file=sys.stderr)
        return False
    r = subprocess.run(
        ["python3", NOTIFY, subject, body, "--to", RECIPIENT],
        capture_output=True, text=True, timeout=90)
    if r.returncode != 0:
        print(f"drain: notify failed: {r.stderr.strip()[:200]}", file=sys.stderr)
        return False
    return True


def search_one(client, query):
    """Run a search, return (record, ranked) or (None, error string)."""
    try:
        sid, rec = client.search(query)
    except S.SlskdError as e:
        return None, str(e)
    if not rec or not rec.get("isComplete") and "Completed" not in str(rec.get("state")):
        # Not fatal: a search that is still running has no responses yet.
        state = rec.get("state") if rec else "unknown"
        if "Completed" not in str(state):
            return None, f"search did not complete (state={state})"
    try:
        resp = client.responses(sid)
    except S.SlskdError as e:
        return None, str(e)
    ranked = S.rank(query, resp, limit=MAX_SOURCES)
    return {"id": sid, "record": rec, "ranked": ranked}, None


def render_query_block(query, result):
    """Human-readable section for one query's results."""
    rec = result["record"]
    ranked = result["ranked"]
    lines = []
    total_responses = rec.get("responseCount") or 0
    total_files = rec.get("fileCount") or 0
    lines.append(f"{query}")
    lines.append(f"  {total_responses} peers responded, {total_files} files found"
                 f"  (state: {rec.get('state')})")

    if not ranked:
        lines.append("  No relevant sources found.")
        lines.append("")
        return "\n".join(lines)

    for i, (score, info, raw) in enumerate(ranked, 1):
        lines.append("")
        lines.append(f"  [{i}] {info['username']}   (score {score:.0f})")
        bits = [f"{info['files']} audio"]
        if info["lossless_frac"] >= 0.9:
            bits.append("lossless" + (" hi-res" if info["hires"] else ""))
        else:
            bits.append(f"~{info['avg_kbps']}kbps")
        bits.append("free slot" if info["has_free_slot"] else "no free slot")
        if info["queue_length"] is not None:
            bits.append(f"queue {info['queue_length']}")
        if info["locked"]:
            bits.append(f"{info['locked']} locked")
        lines.append(f"      {' · '.join(bits)}")
        lines.append(f"      artist match {info['artist_match']:.0%}, "
                     f"relevance {info['relevance']:.0%}")

        # Group this peer's audio into album-ish folders and show the biggest,
        # so a whole album can be taken from one source.
        groups = {}
        for f in (raw.get("files") or []):
            ext = S.file_ext(f)
            if ext not in S.AUDIO_EXT:
                continue
            _artist, album = S.guess_artist_album(f.get("filename"))
            groups.setdefault(album, []).append(f)
        for album, files in sorted(groups.items(), key=lambda kv: -len(kv[1]))[:3]:
            mb = sum(f.get("size") or 0 for f in files) / 1e6
            lines.append(f"      album: {album}  ({len(files)} tracks, {mb:.0f} MB)")
            for f in files[:MAX_FILES_LISTED]:
                base = (f.get("filename") or "").split("\\")[-1].split("/")[-1]
                lines.append(f"         {base}")
            if len(files) > MAX_FILES_LISTED:
                lines.append(f"         ... and {len(files) - MAX_FILES_LISTED} more")

    lines.append("")
    return "\n".join(lines)


def build_offers(query, result):
    """Flatten a search result into persistable per-source offers.

    The whole audio set of a peer is stored, not just the albums shown in the
    mail, so a pick can request any album from that source — the mail only
    displays the top three.
    """
    entries = []
    for i, (_score, info, raw) in enumerate(result["ranked"], 1):
        # Copy with the extension filled in: enqueue sends this field back to slskd.
        audio = [dict(f, extension=S.file_ext(f)) for f in (raw.get("files") or [])
                 if S.file_ext(f) in S.AUDIO_EXT]
        if not audio:
            continue
        entries.append({
            "id": f"{query}:{i - 1}",
            "query": query,
            "username": info["username"],
            "score": info["score"],
            "searchId": result["id"],
            "artist": info["artist"],
            "has_free_slot": info["has_free_slot"],
            "queue_length": info["queue_length"],
            "locked": info["locked"],
            "files": audio,
        })
    return entries



def main():
    pending = Q.read_pending(limit=MAX_QUERIES)
    if not pending:
        print("drain: nothing pending", file=sys.stderr)
        return 0

    client = S.Client()
    st = client.status()
    print(f"drain: slskd {st.get('version')} on {st.get('server')}, "
          f"privileged={st.get('privileged')}", file=sys.stderr)

    blocks, done, failed = [], [], []
    all_offers = []

    for item in pending:
        query = item["query"]
        print(f"drain: searching {query!r} (from {item.get('source')})",
              file=sys.stderr)
        result, err = search_one(client, query)
        if err:
            # Leave it pending so a transient failure is retried, rather than
            # dropping a query the user asked for.
            print(f"drain:   FAILED: {err}", file=sys.stderr)
            failed.append((query, err))
            continue
        blocks.append(render_query_block(query, result))
        all_offers.extend(build_offers(query, result))
        done.append(query)

    if not blocks:
        print("drain: no results to report", file=sys.stderr)
        # Deliberately do NOT mark anything drained.
        return 1 if failed else 0

    # Persist candidates BEFORE mailing, so the pick mail refers to stored
    # offers even if the user replies within seconds. slskd will not keep the
    # search itself, so this file is the only durable record of what was offered.
    saved = 0
    if all_offers:
        try:
            saved = OFF.save_offers(Q.STATE, all_offers,
                                    ttl_hours=OFFER_TTL_HOURS,
                                    max_offers=OFFER_MAX)
            print(f"drain: persisted {saved} source offer(s)", file=sys.stderr)
        except Exception as e:  # noqa: BLE001
            # Losing the offers is not fatal to the mail, but a pick will then
            # not work — say so rather than shipping a mail that cannot be acted
            # on without explanation.
            print(f"drain: could not persist offers: {e}", file=sys.stderr)

    n = len(blocks)
    head = "Soulseek search results" if n == 1 else f"Soulseek: {n} search results"
    body = "\n".join(blocks)
    body += ("\nTo download, reply to this message with:\n"
             "  slskd-get: [N] <query>\n"
             "where N is the source number above. Default is the whole album from\n"
             "that source.\n")
    if not saved and all_offers:
        body += ("\nNOTE: sources could not be saved, so a pick may not work.\n"
                 "Ask me to re-run the search if so.\n")

    if failed:
        body += ("\nNot searched this pass (will retry):\n"
                 + "".join(f"  - {q}: {e}\n" for q, e in failed))

    if mail(head, body):
        # Only now, with results actually delivered, do the queries count as
        # done. Marking before sending would lose a query if the mail failed.
        Q.mark_drained(done)
        Q.clear_pending(done)
        print(f"drain: mailed {n} result set(s); marked {len(done)} drained",
              file=sys.stderr)
        return 0

    print("drain: mail failed; queries left pending for retry", file=sys.stderr)
    return 1


def process_picks():
    """Run picks recorded by Helm's /api/slskd/pick.

    Helm runs in a container and cannot read the slskd API key
    (/home/isaboo/soulseek/data/slskd.yml is not mounted there), so it appends
    the request to a file in the bind-mounted state dir instead of running the
    pick itself. This is the host side of that handover, and it already has the
    credentials and the offer store.

    Each line is removed once attempted, so a pick is never run twice, and a
    crash mid-pick cannot silently retry forever.
    """
    picks_path = os.path.join(Q.STATE, "slskd_picks.jsonl")
    if not os.path.exists(picks_path):
        return
    try:
        with open(picks_path) as fh:
            lines = [ln for ln in fh.read().splitlines() if ln.strip()]
    except OSError as e:
        print(f"drain: cannot read picks: {e}", file=sys.stderr)
        return
    if not lines:
        return

    # Truncate first: a pick that dies mid-run should not be replayed.
    try:
        with open(picks_path, "w"):
            pass
    except OSError as e:
        print(f"drain: cannot clear picks: {e}", file=sys.stderr)
        return

    # The pick script lives beside this one. Resolved from __file__ because the
    # service runs from a different cwd than the repo.
    pick_script = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "slskd_pick.py")
    if not os.path.exists(pick_script):
        print(f"drain: {pick_script} missing; cannot run picks", file=sys.stderr)
        return

    for ln in lines:
        try:
            rec = json.loads(ln)
        except Exception:
            print("drain: skipping malformed pick record", file=sys.stderr)
            continue
        query = (rec.get("query") or "").strip()
        if not query:
            continue
        try:
            index = int(rec.get("index") or 1)
        except (TypeError, ValueError):
            index = 1
        album = rec.get("album") or None
        print(f"drain: running pick [{index}] {query!r}"
              + (f" album={album!r}" if album else ""), file=sys.stderr)
        cmd = [sys.executable, pick_script, query, str(index)]
        if album:
            cmd += ["--album", str(album)]
        r = subprocess.run(cmd, timeout=900)
        if r.returncode != 0:
            # The pick handler mails its own explanation (including "no source
            # available"), so nothing more to do here beyond the log line.
            print(f"drain:   pick exited {r.returncode}", file=sys.stderr)


if __name__ == "__main__":
    process_picks()
    sys.exit(main())
