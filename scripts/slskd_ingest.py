#!/usr/bin/env python3
"""Hyperion side of the Soulseek pipeline: pull finished albums, import with beets.

    VPS slskd downloads/  --rsync-->  /mnt/SharedStuff/Music/Inbox  --beets-->  library

Run on Hyperion (the library and beets live here; the VPS cannot reach Hyperion,
but Hyperion can reach the VPS). Safe to run repeatedly -- a lock stops overlap.

Stage 1, pull. Skipped entirely while slskd has any transfer that has not
finished: a half-downloaded album must not be pulled and imported as if it were
whole. Files are deleted from the VPS only after rsync reports them transferred.
(Limit: one stuck remote-queued transfer blocks pulls until it clears or is
cancelled; --force pulls anyway.)

Stage 2, import. `beet import` in quiet mode with quiet_fallback=skip: albums
beets matches confidently are tagged, renamed Artist - Album (Year), and moved
into the library; anything it is unsure about is left in the Inbox untouched for
you to run `beet import` on by hand. Nothing unsure is ever auto-imported.

A mail summarises each run that did something. Runs that found nothing are silent.
"""
import argparse
import fcntl
import json
import os
import shutil
import subprocess
import sys

from activity import emit
from slskd_inbox_sync import report_synced

HERE = os.path.dirname(os.path.abspath(__file__))
VPS = os.environ.get("SLSKD_VPS", "vps")
REMOTE_DL = os.environ.get("SLSKD_REMOTE_DOWNLOADS",
                           "/home/isaboo/soulseek/data/downloads/")
INBOX = os.path.expanduser(os.environ.get("SLSKD_INBOX", "/mnt/SharedStuff/Music/Inbox"))
LIBRARY = os.environ.get("SLSKD_LIBRARY", "/mnt/SharedStuff/Music")
BEETS_CONFIG = os.path.join(HERE, "beets-inbox.yaml")
BEETS_DB = os.path.expanduser(os.environ.get(
    "SLSKD_BEETS_DB", "~/.cache/helm/beets-inbox.db"))
LOCK = os.path.expanduser(os.environ.get(
    "SLSKD_INGEST_LOCK", "~/.cache/helm/slskd_ingest.lock"))
NOTIFY = os.path.join(HERE, "notify.py")
AUDIO = {".flac", ".mp3", ".m4a", ".ogg", ".opus", ".wav", ".aac", ".wma", ".alac"}

# Runs on the VPS (it holds the slskd API key). Prints how many transfers are
# not finished, or -1 if it could not tell. -1 is treated as "busy" by the
# caller: guessing that downloads are done is the one wrong answer here.
REMOTE_CHECK = r"""
import json, sys, urllib.request
sys.path.insert(0, "/home/isaboo/helm/scripts")
import slskd_search as S
try:
    req = urllib.request.Request(
        "http://100.77.126.57:5030/api/v0/transfers/downloads",
        headers={"X-API-Key": S.api_key()})
    data = json.load(urllib.request.urlopen(req, timeout=20))
    n = sum(1 for u in data for d in u.get("directories", [])
            for f in d.get("files", []) if "Completed" not in str(f.get("state")))
    print(n)
except Exception as e:
    print(-1)
"""


def log(msg):
    print(msg, file=sys.stderr)


def mail(subject, body):
    if not os.path.exists(NOTIFY):
        return
    to = os.environ.get("DIGEST_TO", "isaboo@hyperion")
    r = subprocess.run([sys.executable, NOTIFY, subject, body, "--to", to],
                       capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        log(f"ingest: mail failed: {r.stderr.strip()[:200]}")


def transfers_in_progress():
    """Unfinished slskd transfers (-1 = unknown)."""
    try:
        r = subprocess.run(["ssh", "-o", "BatchMode=yes", VPS, "python3", "-"],
                           input=REMOTE_CHECK, capture_output=True, text=True,
                           timeout=60)
        return int(r.stdout.strip().splitlines()[-1])
    except Exception as e:  # noqa: BLE001
        log(f"ingest: could not check slskd transfers: {e}")
        return -1


def pull(source, force=False, dry_run=False):
    """rsync completed files into the Inbox. Returns (ok, files_moved, note, rels).

    rels are the moved files' paths relative to the VPS downloads/ folder, in
    the form Helm's Soulseek panel matches against (see report_synced).
    """
    remote = ":" in source.split("/")[0]
    if remote:                                # remote source: ask slskd first
        n = transfers_in_progress()
        if n != 0 and not force:
            why = "could not reach slskd" if n < 0 else f"{n} transfer(s) unfinished"
            return True, 0, f"waiting: {why}", []
    os.makedirs(INBOX, exist_ok=True)
    # --remove-source-files only deletes a source file once it is fully
    # transferred. The audio filter keeps stray .nfo/.jpg/.m3u from being pulled
    # (and then deleted from the VPS) as if they were an album.
    cmd = ["rsync", "-a", "--itemize-changes", "--prune-empty-dirs",
           "--remove-source-files", "--partial-dir=.rsync-partial",
           "--include=*/"]
    cmd += [f"--include=*{e}" for e in sorted(AUDIO)]
    cmd += ["--exclude=*", source, INBOX + "/"]
    if dry_run:
        cmd.insert(1, "--dry-run")
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    if r.returncode != 0:
        return False, 0, f"rsync failed ({r.returncode}): {r.stderr.strip()[:200]}", []
    # itemized lines are "<flags> <path>"; a received file starts with ">f".
    rels = [ln.split(" ", 1)[1] for ln in r.stdout.splitlines()
            if ln.startswith(">f") and " " in ln]
    moved = len(rels)
    if moved and not dry_run and remote:
        # Remove the now-empty album folders left on the VPS.
        host, path = source.split(":", 1)
        subprocess.run(["ssh", host, "find", path, "-mindepth", "1", "-type", "d",
                        "-empty", "-delete"], capture_output=True, timeout=60)
    return True, moved, "", rels


def album_dirs(root):
    """Top-level Inbox folders that contain audio."""
    out = []
    if not os.path.isdir(root):
        return out
    for name in sorted(os.listdir(root)):
        p = os.path.join(root, name)
        if name.startswith(".") or not os.path.isdir(p):
            continue
        if any(os.path.splitext(f)[1].lower() in AUDIO
               for _d, _s, fs in os.walk(p) for f in fs):
            out.append(p)
    return out


def import_albums(dry_run=False):
    """Run beets on each album folder. Returns (imported, left_for_review)."""
    imported, review = [], []
    os.makedirs(os.path.dirname(BEETS_DB), exist_ok=True)
    for path in album_dirs(INBOX):
        name = os.path.basename(path)
        if dry_run:
            review.append(name)
            continue
        cmd = ["beet", "-c", BEETS_CONFIG, "-l", BEETS_DB, "-d", LIBRARY,
               "import", path]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
        # beets exits 0 whether it imported or skipped, so judge by what is left
        # on disk: a moved album leaves no audio behind; a skipped one does.
        still_there = os.path.isdir(path) and any(
            os.path.splitext(f)[1].lower() in AUDIO
            for _d, _s, fs in os.walk(path) for f in fs)
        if r.returncode != 0 and still_there:
            review.append(f"{name}  (beets error: {r.stderr.strip()[-120:]})")
        elif still_there:
            review.append(name)
        else:
            imported.append(name)
            shutil.rmtree(path, ignore_errors=True)   # beets leaves the empty shell
    return imported, review


def main():
    ap = argparse.ArgumentParser(description="Pull finished Soulseek albums and import with beets")
    ap.add_argument("--source", default=f"{VPS}:{REMOTE_DL}")
    ap.add_argument("--force", action="store_true",
                    help="pull even if slskd has unfinished transfers")
    ap.add_argument("--no-pull", action="store_true")
    ap.add_argument("--no-mail", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    os.makedirs(os.path.dirname(LOCK), exist_ok=True)
    lock = open(LOCK, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log("ingest: another run is in progress")
        return 0

    moved, note, rels = 0, "", []
    if not args.no_pull:
        ok, moved, note, rels = pull(args.source, args.force, args.dry_run)
        if not ok:
            log(f"ingest: {note}")
            if not args.no_mail:
                mail("Soulseek: pull from the VPS failed", note)
            return 1
        if note:
            log(f"ingest: {note}")
        if rels and not args.dry_run:
            # The Soulseek panel shows a file as "synced" only once Helm has been
            # told it reached the Inbox. This replaces the old inbox-sync timer's report.
            report_synced(rels)

    imported, review = import_albums(args.dry_run)
    if not args.dry_run:
        # beets prunes directories it leaves empty, up to the library root, and the
        # Inbox sits inside the library -- so importing the last album deletes it.
        # Put it back so it is always there to drop files into.
        os.makedirs(INBOX, exist_ok=True)
    log(f"ingest: pulled {moved} file(s), imported {len(imported)}, "
        f"{len(review)} left for review")

    if not args.dry_run:
        if moved:
            emit("music", "pulled", "pulled", f"{moved} file(s) pulled from the VPS")
        for name in imported:
            emit("music", "imported", name, f"imported {name}")
        for name in review:
            emit("music", "review", name, f"left for beets review: {name}")

    if (imported or review or moved) and not args.no_mail and not args.dry_run:
        lines = []
        if imported:
            lines += [f"Imported into {LIBRARY}:"] + [f"  + {n}" for n in imported]
        if review:
            lines += ["", f"Left in {INBOX} (beets was not confident -- run "
                      "`beet import` on them yourself):"] + [f"  ? {n}" for n in review]
        mail(f"Soulseek: {len(imported)} imported, {len(review)} need review",
             "\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
