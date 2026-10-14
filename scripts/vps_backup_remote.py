#!/usr/bin/env python3
"""Archive builder. Runs as ROOT inside a throwaway container on the VPS.

hyperion's vps_backup.py streams this file in on stdin:
    docker run --rm -i --user 0 -v $HOME:/h -e HOME=/h --entrypoint python3 helm-helm -
(root, because several data dirs belong to container users -- FreshRSS, Forgejo SSH
host keys, Caddy certs -- and isaboo cannot read them. The helm image is used only
because it already has python3 + sqlite3 + GNU tar; nothing is pulled or installed.)

Writes ONE consistent tar.gz of the VPS's stateful data to stdout; logs go to stderr.
The host-side wrapper stops Stalwart around this run (RocksDB cannot be copied while
live) and always starts it again.

Consistency rules
  * SQLite files (Forgejo, slskd, LeafWiki, Kavita, JobSync, FreshRSS ...) are copied
    with SQLite's online-backup API and integrity-checked; the live file and its
    -wal/-shm are excluded from the tar.
  * Anything unreadable, or tar failing hard (exit >= 2), aborts with a nonzero exit.
    (tar exit 1 = "file changed while reading", expected for live JSON; tolerated.)
  * /h is mounted read-write ONLY because SQLite needs to open WAL databases (it may
    touch their -shm, exactly like `sqlite3 .backup` does). This script never writes
    to /h itself; all scratch output goes to the container's /tmp.

Not included (regenerable or backed up elsewhere): helm/data/helm-backups (popcorn
mirrors them), soulseek downloads/incomplete, Docker images.
"""
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time

HOME = os.path.expanduser("~")

SOURCES = [
    "forgejo/data", "forgejo/docker-compose.yml",
    "mail/config", "mail/certs", "mail/data", "mail/docker-compose.yml", "mail/cert-sync.sh",
    "soulseek/data", "soulseek/docker-compose.yml",
    "helm/data", "helm/dailytxt-data", "helm/freshrss-data", "helm/leafwiki-data",
    "helm/searxng-settings", "helm/.env", "helm/docker-compose.yml",
    "website", "jobsync/jobsyncdb/data", "jobsync/docker-compose.yml", "jobsync/.env",
    "stoa/docker-compose.yml",
    ".config/systemd/user",
]
EXCLUDE = [  # relative to $HOME, prefix match
    "helm/data/helm-backups",
    "soulseek/data/downloads",
    "soulseek/data/incomplete",
]
SQLITE_SUFFIXES = (".db", ".sqlite", ".sqlite3")
SIDECARS = ("-wal", "-shm", "-journal")
MAGIC = b"SQLite format 3\x00"
SNAP = "snap"  # staging subdir; renamed back to the real paths by tar --transform


def log(*a):
    print("[vps-backup]", *a, file=sys.stderr, flush=True)


def excluded(rel):
    return any(rel == e or rel.startswith(e + "/") for e in EXCLUDE)


def walk_files():
    """Return ([(rel, abs)], errors) for every file under SOURCES, honouring EXCLUDE."""
    files, errors = [], []
    for src in SOURCES:
        p = os.path.join(HOME, src)
        if not os.path.exists(p):
            log("skip (absent):", src)
            continue
        if os.path.isfile(p):
            files.append((src, p))
            continue
        for root, dirs, names in os.walk(p, onerror=lambda e: errors.append(f"{e.filename}: {e.strerror}")):
            rel_root = os.path.relpath(root, HOME)
            dirs[:] = [d for d in dirs if not excluded(os.path.join(rel_root, d))]
            for n in names:
                rel = os.path.join(rel_root, n)
                if not excluded(rel):
                    files.append((rel, os.path.join(root, n)))
    return files, errors


def is_sqlite(path):
    if not path.endswith(SQLITE_SUFFIXES):
        return False
    try:
        with open(path, "rb") as fh:
            return fh.read(16) == MAGIC
    except OSError:
        return False


def snapshot_sqlite(stage):
    """Online-backup every SQLite DB into `stage`.

    Returns (db_rels, tar_excludes, errors). tar_excludes = the live DB files plus
    their -wal/-shm/-journal sidecars, so the archive holds only the clean snapshot.
    """
    files, errors = walk_files()
    dbs, excl = [], []
    for rel, path in files:
        if not is_sqlite(path):
            continue
        dest = os.path.join(stage, SNAP, rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        try:
            src = sqlite3.connect(path, timeout=60)
            dst = sqlite3.connect(dest)
            with dst:
                src.backup(dst)
            check = dst.execute("PRAGMA integrity_check").fetchone()[0]
            src.close()
            dst.close()
        except Exception as e:  # noqa: BLE001 -- reported, then the whole run aborts
            errors.append(f"{rel}: sqlite backup failed: {e}")
            continue
        if check != "ok":
            errors.append(f"{rel}: integrity_check on snapshot: {check[:80]}")
            continue
        dbs.append(rel)
        excl.append(rel)
        excl.extend(rel + s for s in SIDECARS)
    return dbs, excl, errors


def main():
    stage = tempfile.mkdtemp(prefix="vps-backup-")
    try:
        t0 = time.time()
        dbs, excl, errors = snapshot_sqlite(stage)
        log(f"sqlite snapshots: {len(dbs)}")
        if errors:
            for e in errors[:20]:
                log("ERROR", e)
            return 3

        with open(os.path.join(stage, "MANIFEST.txt"), "w") as m:
            m.write(f"vps backup {time.strftime('%Y-%m-%dT%H:%M:%S%z')}\n")
            m.write("sources:\n" + "".join(f"  {s}\n" for s in SOURCES))
            m.write("excluded:\n" + "".join(f"  {s}\n" for s in EXCLUDE))
            m.write("sqlite snapshots (online-backup API; live copies excluded):\n"
                    + "".join(f"  {s}\n" for s in dbs))
            m.write("\nRESTORE: extract as root with --numeric-owner (preserves container uids). "
                    "Stalwart was stopped around the copy, so its RocksDB is consistent.\n")

        # Snapshots live under stage/snap/<rel> so the live-file excludes below can't also
        # swallow them (same relative path). --anchored keeps excludes from matching the
        # tail of "snap/<rel>"; --transform then strips the prefix.
        cmd = ["tar", "--numeric-owner", "-czf", "-", "--anchored"]
        for e in EXCLUDE + excl:
            cmd += ["--exclude", e]
        present = [s for s in SOURCES if os.path.exists(os.path.join(HOME, s))]
        cmd += ["--transform", f"s,^{SNAP}/,,", "-C", HOME] + present
        cmd += ["-C", stage, "MANIFEST.txt"]
        if os.path.isdir(os.path.join(stage, SNAP)):
            cmd += [SNAP]

        rc = subprocess.run(cmd, stdout=sys.stdout.buffer).returncode
        if rc >= 2:
            log(f"tar failed with exit {rc}")
            return 4
        log(f"done in {time.time() - t0:.1f}s (tar exit {rc})")
        return 0
    finally:
        shutil.rmtree(stage, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
