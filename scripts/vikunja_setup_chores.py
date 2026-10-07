#!/usr/bin/env python3
"""One-time setup: create a Vikunja "Chores" project with recurring tasks
that mirror the schedule in chores_digest.py — but letting Vikunja own the
recurrence natively (repeat_after/repeat_mode) instead of hand-rolled weekday
logic. Idempotent: skips any task whose title already exists in the project,
so re-running after an edit only adds what's missing.

This does NOT touch the email digest (chores_digest.py) — that keeps running
independently. Vikunja is a checkable list layered on top, not a replacement.

Usage:
    python3 vikunja_setup_chores.py --password 'the-admin-password'
    # or: VIKUNJA_ADMIN_PASSWORD=... python3 vikunja_setup_chores.py

Config (env or flags):
    VIKUNJA_URL             base URL (default http://localhost:3456)
    VIKUNJA_ADMIN_USER      default "admin"
    VIKUNJA_ADMIN_PASSWORD  required (or --password)
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

DAY = 86400
WEEK = 7 * DAY

PROJECT_TITLE = "Chores"

# (title, due_weekday (Mon=0..Sun=6, or None for "starts tomorrow"), repeat_after_seconds)
TASKS = [
    ("Kitchen deep clean + wash pet bowls", 0, WEEK),
    ("Bathroom 1 clean", 1, WEEK),
    ("Bedrooms — dust & vacuum/sweep", 2, WEEK),
    ("Bathroom 2 clean", 3, WEEK),
    ("Living room deep clean", 4, WEEK),
    ("Whole-house floors + litter deep-clean + dog bedding", 5, WEEK),
    ("Laundry + bedsheets + trash/recycling", 6, WEEK),
    ("Mop kitchen & living room floors", None, 2 * DAY),
    ("Scoop litter box(es)", None, DAY),
    ("Quick tidy — pet hair / living room", None, DAY),
]


def api(base, path, token=None, method="GET", body=None):
    url = base.rstrip("/") + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{method} {path} -> {e.code}: {e.read().decode()[:300]}") from e


def login(base, username, password):
    return api(base, "/api/v1/login", method="POST",
               body={"username": username, "password": password})["token"]


def get_or_create_project(base, token):
    projects = api(base, "/api/v1/projects", token=token)
    for p in projects:
        if p.get("title") == PROJECT_TITLE:
            return p["id"]
    created = api(base, "/api/v1/projects", token=token, method="PUT",
                  body={"title": PROJECT_TITLE})
    return created["id"]


def next_weekday(weekday, hour=7):
    """Next occurrence of `weekday` (Mon=0) at `hour` local, or tomorrow if
    weekday is None (for the no-fixed-day daily/every-2-days tasks)."""
    now = datetime.now().replace(hour=hour, minute=0, second=0, microsecond=0)
    if weekday is None:
        target = now + timedelta(days=1)
        return target
    days_ahead = (weekday - now.weekday()) % 7
    days_ahead = days_ahead or 7  # if today, start next week's occurrence
    return now + timedelta(days=days_ahead)


def existing_titles(base, token, project_id):
    tasks = api(base, f"/api/v1/projects/{project_id}/tasks", token=token)
    return {t.get("title") for t in tasks}


def create_task(base, token, project_id, title, due, repeat_after):
    body = {
        "title": title,
        "due_date": due.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "repeat_after": repeat_after,
        "repeat_mode": 0,  # fixed schedule from the due date, not from completion
    }
    api(base, f"/api/v1/projects/{project_id}/tasks", token=token, method="PUT", body=body)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--url", default=os.environ.get("VIKUNJA_URL", "http://localhost:3456"))
    p.add_argument("--user", default=os.environ.get("VIKUNJA_ADMIN_USER", "admin"))
    p.add_argument("--password", default=os.environ.get("VIKUNJA_ADMIN_PASSWORD"))
    args = p.parse_args()

    if not args.password:
        print("error: --password or VIKUNJA_ADMIN_PASSWORD required", file=sys.stderr)
        return 1

    token = login(args.url, args.user, args.password)
    project_id = get_or_create_project(args.url, token)
    have = existing_titles(args.url, token, project_id)

    created = 0
    for title, weekday, repeat_after in TASKS:
        if title in have:
            print(f"skip (exists): {title}")
            continue
        due = next_weekday(weekday)
        create_task(args.url, token, project_id, title, due, repeat_after)
        print(f"created: {title} (due {due.isoformat()}, repeats every {repeat_after // DAY}d)")
        created += 1

    print(f"\n{created} task(s) created, {len(have)} already existed. Project id: {project_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
