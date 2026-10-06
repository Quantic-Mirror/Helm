#!/usr/bin/env python3
"""Mail a daily backup-pipeline digest, and warn when a stage goes stale.

Reads Helm's own /api/backup-events (the same data the Backup Pipeline tab
renders) and mails a summary to hyperion. Replaces the tab for the "did my
backups run, and did they fail" question without a second source of truth —
there is no new state here, only a different rendering of the existing events.

Two distinct conditions, because they mean different things:

  FAILED  — a stage emitted status "error". Something ran and broke. Mail it.
  STALE   — a stage has emitted nothing at all for STALE_HOURS. This is the
            failure mode that emits no event: a dead timer, a cron that stopped,
            a host that was down when the job should have run. Nothing in the
            event stream can report it, which is exactly why it needs its own
            check. A backup that silently stops is worse than one that fails
            loudly.

Both are mail-only. Nothing here writes to Helm, and the events themselves are
left untouched — this is a reader.

Config (env):
    HELM_API_URL     base URL of helm_server.py
    HELM_TOKEN_FILE  bearer token file
    STALE_HOURS      staleness threshold in hours (default 26)
    DIGEST_TO        recipient for the daily digest
    ALERT_TO         recipient for failure/stale warnings (defaults to DIGEST_TO)
    NOTIFY           path to notify.py
"""
import json
import os
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request

HELM_API_URL = os.environ.get("HELM_API_URL", "https://100.77.126.57:8443")
HELM_TOKEN_FILE = os.environ.get("HELM_TOKEN_FILE", "/home/isaboo/.config/helm/token")
NOTIFY = os.environ.get("NOTIFY", "/home/isaboo/repos/Helm/scripts/notify.py")
STALE_HOURS = float(os.environ.get("STALE_HOURS", "26"))

# The two stages the Backup Pipeline tab tracks. Kept in sync with
# BACKUP_STAGES in index.html — if a stage is added there and not here, it is
# simply never reported (safe failure: no mail, no false alarm).
STAGES = [
    ("hyperion_backup", "Hyperion -> GitHub"),
    ("r2_sync", "popcorn -> R2"),
]

TIMEOUT = 20


def read_token():
    try:
        with open(HELM_TOKEN_FILE) as f:
            return f.read().strip()
    except OSError:
        return None


def fetch_events(token):
    """GET /api/backup-events. Returns a list; [] on any failure.

    A failure here must not raise: this runs unattended, and a transient
    network blip should produce one skipped run, not a stack trace in cron.
    """
    req = urllib.request.Request(
        HELM_API_URL + "/api/backup-events",
        headers={"Authorization": "Bearer " + token},
    )
    ctx = None
    if HELM_API_URL.startswith("https://"):
        ctx = ssl.create_default_context()
        # Helm's tailnet cert is self-signed; the transport is already
        # encrypted by Tailscale, same trust model as the rest of Helm.
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=ctx) as r:
            data = json.loads(r.read())
        ev = data.get("events")
        return ev if isinstance(ev, list) else []
    except Exception as e:
        print(f"backup-digest: could not fetch events: {e}", file=sys.stderr)
        return None


def stage_summary(events, stage_id):
    """(status_text, timestamp, detail) for a stage's most recent event."""
    evs = [e for e in events if e.get("stage") == stage_id]
    if not evs:
        return None, None, None
    last = evs[-1]
    return last.get("status"), last.get("ts"), last.get("name")


def notify(subject, body, recipient):
    if not os.path.exists(NOTIFY):
        print(f"backup-digest: notify.py missing at {NOTIFY}", file=sys.stderr)
        return False
    r = subprocess.run(
        ["python3", NOTIFY, subject, body, "--to", recipient],
        capture_output=True, text=True, timeout=60,
    )
    if r.returncode != 0:
        print(f"backup-digest: notify failed: {r.stderr.strip()[:200]}", file=sys.stderr)
        return False
    return True


def human_age(ts, now):
    if not ts:
        return "never"
    s = max(0, now - ts)
    if s < 3600:
        return f"{int(s // 60)}m ago"
    if s < 86400:
        return f"{int(s // 3600)}h ago"
    return f"{int(s // 86400)}d ago"


def main():
    token = read_token()
    if not token:
        print(f"backup-digest: no token at {HELM_TOKEN_FILE}", file=sys.stderr)
        return 1

    events = fetch_events(token)
    if events is None:
        # Fetch failed: our problem, not the pipeline's. Nothing is mailed,
        # but exit nonzero so systemd retries (boot race with Tailscale).
        return 1
    if not events:
        # Distinguish "no events ever" from "could not reach Helm". Only the
        # former is a pipeline fact worth reporting; the second is our own
        # problem and must not be mailed as if backups were broken.
        print("backup-digest: no events available; skipping", file=sys.stderr)
        return 0

    now = time.time()
    digest_to = os.environ.get("DIGEST_TO", "isaboo@hyperion")
    alert_to = os.environ.get("ALERT_TO", digest_to)

    failures, stale = [], []
    rows = []

    for stage_id, label in STAGES:
        status, ts, name = stage_summary(events, stage_id)
        age_txt = human_age(ts, now)

        if status is None:
            rows.append(f"  {label:<22} no events recorded")
            stale.append(f"{label}: no events have ever been recorded")
            continue

        mark = {"ok": "OK", "started": "RUNNING", "error": "FAILED"}.get(status, status)
        when = ""
        if ts:
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))
        detail = " ".join(x for x in (when, f"({age_txt})", name) if x)
        rows.append(f"  {label:<22} {mark:<8} {detail}")

        if status == "error":
            failures.append(f"{label}: last run FAILED ({age_txt})")
        elif ts is not None and (now - ts) > STALE_HOURS * 3600:
            # Guard ts explicitly: a malformed event with no timestamp would
            # otherwise raise here and abort the whole run, losing the digest.
            stale.append(
                f"{label}: nothing for {STALE_HOURS:.0f}h (last event {age_txt}) "
                f"— the job may not be running at all"
            )

    # 1. Failure notice — highest priority, sent regardless of staleness.
    if failures:
        body = (
            "A backup pipeline stage reported a failure.\n\n"
            + "\n".join(f"  - {f}" for f in failures)
            + "\n\nStage status now:\n"
            + "\n".join(rows)
            + "\n\nDetails: ssh vps, then open the Backup Pipeline tab, or read\n"
            + "~/helm/data/backup_events.json on the VPS.\n"
        )
        notify("Backup FAILED: " + "; ".join(f.split(":")[0] for f in failures), body, alert_to)

    # 2. Staleness — no event at all, which no failure path can report.
    if stale:
        body = (
            "A backup pipeline stage has gone quiet.\n\n"
            + "\n".join(f"  - {s}" for s in stale)
            + "\n\nA stage that stops running emits no error event, so nothing\n"
            + "else would tell you. Check the timer/cron on the host that owns it:\n"
            + "  systemctl --user list-timers\n\n"
            + "Stage status now:\n" + "\n".join(rows) + "\n"
        )
        notify("Backup STALE: " + "; ".join(s.split(":")[0] for s in stale), body, alert_to)

    # 3. Daily digest — always, so there is a heartbeat proving the pipeline is
    #    alive even on a day with no failures.
    health = "PROBLEM" if (failures or stale) else "all OK"
    body = (
        f"Backup pipeline digest — {health}\n\n"
        + "\n".join(rows)
        + "\n\n"
        + f"{len(events)} events on record. Staleness threshold: {STALE_HOURS:.0f}h.\n"
    )
    if failures or stale:
        body += "\nProblems are detailed in the alerts above.\n"
    if not notify(f"Backup digest: {health}", body, digest_to):
        return 1  # let systemd retry; the heartbeat must not be lost

    return 0


if __name__ == "__main__":
    sys.exit(main())