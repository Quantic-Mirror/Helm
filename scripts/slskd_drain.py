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

import slskd_queue as Q
import slskd_search as S

NOTIFY = os.environ.get("NOTIFY", os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "notify.py"))
RECIPIENT = os.environ.get("DIGEST_TO", "isaboo@hyperion")
MAX_QUERIES = int(os.environ.get("SLSKD_MAX_PER_PASS", "5"))
MAX_SOURCES = int(os.environ.get("SLSKD_MAX_SOURCES", "6"))

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
            ext = (f.get("extension") or "").lower().lstrip(".")
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
        done.append(query)

    if not blocks:
        print("drain: no results to report", file=sys.stderr)
        # Deliberately do NOT mark anything drained.
        return 1 if failed else 0

    n = len(blocks)
    head = "Soulseek search results" if n == 1 else f"Soulseek: {n} search results"
    body = "\n".join(blocks)
    body += ("\nTo download, reply with the source number and query, e.g.\n"
             "  slskd-get: [1] aphex twin\n"
             "(or open the slskd web UI to browse and pick manually)\n")

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


if __name__ == "__main__":
    sys.exit(main())
