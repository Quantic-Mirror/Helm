#!/usr/bin/env python3
"""Pull a consistent snapshot of the VPS's stateful data to hyperion (and R2).

Runs on hyperion (daily timer, `hyperion-vps-backup.timer`). Mirrors what
stoa-backup does for Stoa, for everything else on the VPS: Forgejo, Stalwart
mail, slskd, FreshRSS, DailyTxT, LeafWiki, JobSync, Helm state, Caddy site,
compose files + .env + the VPS user systemd units.

How
  1. ssh to the VPS and run scripts/vps_backup_remote.py as root inside a throwaway
     container (needed: several data dirs belong to container users). Stalwart is
     stopped for the few seconds that takes -- its RocksDB can't be copied live -- and
     ALWAYS started again by the wrapper's trap.
  2. Stream the tar.gz back to ~/backups/vps/vps-YYYY-MM-DD.tar.gz (mode 0600: it
     contains mail data and .env secrets).
  3. Verify it (gzip, tar listing, required members, integrity_check on the Forgejo DB)
     BEFORE it replaces anything or is uploaded.
  4. Keep the newest 14 locally; upload to R2 through the crypt remote (encrypted),
     keep 21 days there.
  5. Report started/ok/failed to Helm's Backup Pipeline (stage `vps_backup`).

Config (env): VPS_BACKUP_HOST (vps)  VPS_BACKUP_DIR (~/backups/vps)  VPS_BACKUP_KEEP (14)
  VPS_BACKUP_R2 (r2crypt:backups)  VPS_BACKUP_R2_DAYS (21)  VPS_BACKUP_IMAGE (helm-helm)
  VPS_BACKUP_STOP_STALWART (1)
Flags: --no-r2  skip the upload.   --no-stop  don't stop Stalwart (testing; its DB may be torn).
"""
import argparse
import datetime
import fcntl
import os
import sqlite3
import subprocess
import sys
import tarfile
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REMOTE_SCRIPT = os.path.join(HERE, "vps_backup_remote.py")
HOST = os.environ.get("VPS_BACKUP_HOST", "vps")
DEST = os.path.expanduser(os.environ.get("VPS_BACKUP_DIR", "~/backups/vps"))
KEEP = int(os.environ.get("VPS_BACKUP_KEEP", "14"))
R2 = os.environ.get("VPS_BACKUP_R2", "r2crypt:backups")
R2_DAYS = os.environ.get("VPS_BACKUP_R2_DAYS", "21")
IMAGE = os.environ.get("VPS_BACKUP_IMAGE", "helm-helm")
STOP = os.environ.get("VPS_BACKUP_STOP_STALWART", "1") == "1"
LOCK = os.path.expanduser("~/.cache/helm/vps_backup.lock")
HELM_DIR = os.environ.get("HELM_DIR", os.path.expanduser("~/repos/Helm"))
STAGE = "vps_backup"
MIN_BYTES = 5 * 1024 * 1024
REQUIRED = [  # members that must be in a good archive
    "MANIFEST.txt",
    "forgejo/data/gitea/gitea.db",
    "mail/data/CURRENT",
    "soulseek/data/slskd.yml",
    "helm/data/marks_state.json",
    "helm/.env",
    "helm/leafwiki-data/users.db",
    "helm/freshrss-data/users/isaboo/db.sqlite",
]

# Executed on the VPS (login shell). Stops Stalwart, runs the archive builder as root
# in a container with $HOME mounted, restarts Stalwart no matter how it ends.
REMOTE_CMD = r"""
S=stalwart
was=$(docker inspect -f '{{.State.Running}}' $S 2>/dev/null)
stop=__STOP__
restart() { if [ "$was" = true ] && [ "$stop" = 1 ]; then docker start $S >&2 && echo "[vps-backup] $S restarted" >&2; fi; }
trap restart EXIT
trap 'exit 143' HUP INT TERM
if [ "$was" = true ] && [ "$stop" = 1 ]; then docker stop -t 30 $S >&2 || exit 5; fi
docker run --rm -i --pull=never --user 0 -v "$HOME":/h -e HOME=/h --entrypoint python3 __IMAGE__ -
"""


def emit(status, message="", name=""):
    """Best-effort event to the Backup Pipeline tab; never fails the backup."""
    exe = os.path.expanduser("~/.local/bin/emit_event.py")
    if not os.path.exists(exe):
        exe = os.path.join(HELM_DIR, "emit_event.py")
    env = dict(os.environ)
    tok = os.path.join(HELM_DIR, "backup_token.txt")
    if os.path.exists(tok):
        env.setdefault("HELM_BACKUP_TOKEN_FILE", tok)
    cmd = [sys.executable, exe, STAGE, status, "--message", message[:300]]
    if name:
        cmd += ["--name", name]
    try:
        subprocess.run(cmd, env=env, timeout=60, capture_output=True)
    except Exception as e:  # noqa: BLE001
        print(f"warn: emit failed: {e}", file=sys.stderr)


def fail(msg, code=1):
    print(f"vps-backup FAILED: {msg}", file=sys.stderr)
    emit("failed", msg)
    sys.exit(code)


def verify(path):
    """Return None if the archive is sound, else a reason string."""
    size = os.path.getsize(path)
    if size < MIN_BYTES:
        return f"archive only {size} bytes (< {MIN_BYTES})"
    names = set()
    try:
        with tarfile.open(path, "r:gz") as tf:  # reading every header walks the whole gzip stream
            for m in tf:
                names.add(m.name[2:] if m.name.startswith("./") else m.name)
            missing = [r for r in REQUIRED if r not in names]
            if missing:
                return "missing members: " + ", ".join(missing)
            if not any(n.startswith("mail/data/") and n.endswith(".sst") for n in names):
                return "no Stalwart .sst files in the archive"
            with tempfile.TemporaryDirectory() as td:
                f = tf.extractfile(tf.getmember("forgejo/data/gitea/gitea.db"))
                db = os.path.join(td, "gitea.db")
                with open(db, "wb") as out:
                    out.write(f.read())
                res = sqlite3.connect(db).execute("PRAGMA integrity_check").fetchone()[0]
                if res != "ok":
                    return f"forgejo db integrity_check: {res[:80]}"
    except (tarfile.TarError, EOFError, OSError) as e:
        return f"unreadable archive: {e}"
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--no-r2", action="store_true")
    ap.add_argument("--no-stop", action="store_true")
    args = ap.parse_args()

    os.umask(0o077)
    os.makedirs(DEST, mode=0o700, exist_ok=True)
    os.makedirs(os.path.dirname(LOCK), exist_ok=True)
    lock = open(LOCK, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("another vps-backup is running; exiting")
        return 0

    stamp = datetime.date.today().isoformat()
    final = os.path.join(DEST, f"vps-{stamp}.tar.gz")
    partial = final + ".partial"
    emit("started", f"pulling from {HOST}")

    cmd = REMOTE_CMD.replace("__STOP__", "0" if (args.no_stop or not STOP) else "1").replace("__IMAGE__", IMAGE)
    try:
        with open(REMOTE_SCRIPT, "rb") as script, open(partial, "wb") as out:
            r = subprocess.run(
                ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=20", HOST, cmd],
                stdin=script, stdout=out, stderr=subprocess.PIPE, timeout=1800)
    except subprocess.TimeoutExpired:
        os.unlink(partial)
        fail("ssh/archive step timed out after 30 min")
    except OSError as e:
        fail(f"could not run ssh: {e}")
    log = r.stderr.decode(errors="replace").strip()
    if log:
        print(log, file=sys.stderr)
    if r.returncode != 0:
        os.unlink(partial)
        tail = " | ".join(log.splitlines()[-3:])
        fail(f"remote archive failed (exit {r.returncode}): {tail}"[:300])

    why = verify(partial)
    if why:
        os.replace(partial, final + ".rejected")
        fail(f"archive rejected: {why}")
    os.replace(partial, final)
    mb = os.path.getsize(final) / 1048576
    print(f"saved {final} ({mb:.1f} MB)")

    mine = sorted(f for f in os.listdir(DEST) if f.startswith("vps-") and f.endswith(".tar.gz"))
    for old in mine[:-KEEP]:
        os.unlink(os.path.join(DEST, old))

    if not args.no_r2:
        remote = f"{R2}/vps-{stamp.replace('-', '')}.tar.gz"
        up = subprocess.run(["rclone", "copyto", final, remote], capture_output=True, text=True, timeout=1800)
        if up.returncode != 0:
            fail(f"saved locally but R2 upload failed: {up.stderr.strip()[-200:]}")
        subprocess.run(["rclone", "delete", R2, "--include", "vps-*.tar.gz", "--min-age", f"{R2_DAYS}d"],
                       capture_output=True, text=True, timeout=600)
    emit("ok", f"{mb:.1f} MB, verified" + ("" if args.no_r2 else ", uploaded to R2"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
