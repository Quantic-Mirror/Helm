"""Live Soulseek search and download for the Music tab, run inside Helm.

The drainer path (search queued, drainer runs every 5 min, offers stored,
mail sent) is too slow for the Soulseek panel, so this runs the search in the
Helm process instead. A search takes 15-90 seconds, so it runs in a background
thread and the UI polls for the result.

Every download started from here is recorded in a staging list, persisted in
the state dir so it survives a restart. Each entry's status is worked out on
read:
  queued      slskd has the file(s), not finished yet
  downloaded  slskd finished them, into the VPS downloads/ folder
  synced      hyperion's Inbox sync reported them in its Inbox
  failed      slskd gave up on a file, or the search or enqueue failed

Needs the slskd config mounted into the container (SLSKD_CONFIG) for the API
key. Downloads land in slskd's downloads/ folder on the VPS; the hyperion sync
copies them and reports back via POST /api/slskd/synced.
"""
import json
import os
import threading
import time
import uuid

import slskd_drain as D
import slskd_pick as P
import slskd_search as S

MAX_KEPT = 20
# More than the mail digest shows (D.MAX_SOURCES): the panel hides busy and
# locked sources by default, so it needs spares to fill the list.
LIVE_SOURCES = 30
MAX_SYNCED = 20000

_LOCK = threading.Lock()
_SEARCHES = {}

STATE_DIR = os.environ.get("HELM_STATE_DIR", "/app/state")
STAGING_PATH = os.path.join(STATE_DIR, "slskd_staging.json")


# ── staging store ───────────────────────────────────────────────────────────

def _load_staging():
    try:
        with open(STAGING_PATH) as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        data = {}
    if not isinstance(data.get("entries"), list):
        data["entries"] = []
    if not isinstance(data.get("synced"), list):
        data["synced"] = []
    return data


def _save_staging(data):
    # Same atomic-write pattern as the rest of the state files.
    tmp = STAGING_PATH + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(data, fh)
    os.replace(tmp, STAGING_PATH)


def _rel_path(filename):
    """The VPS downloads/ path for a slskd file: its folder and name.

    slskd files a download under the last folder of the remote path, so
    `Music\\Artist\\Album\\01.flac` lands at `Album/01.flac`.
    """
    parts = (filename or "").replace("\\", "/").split("/")
    return "/".join(parts[-2:]) if len(parts) >= 2 else (parts[-1] if parts else "")


def stage(entry):
    """Add an entry to the staging list. Returns its id."""
    entry.setdefault("id", uuid.uuid4().hex[:12])
    entry.setdefault("createdAt", time.time())
    with _LOCK:
        data = _load_staging()
        data["entries"].insert(0, entry)
        _save_staging(data)
    return entry["id"]


def _update_entry(eid, **fields):
    with _LOCK:
        data = _load_staging()
        for e in data["entries"]:
            if e["id"] == eid:
                e.update(fields)
        _save_staging(data)


def remove_entry(eid):
    """Drop one entry from the list. Does not cancel or delete any download."""
    with _LOCK:
        data = _load_staging()
        before = len(data["entries"])
        data["entries"] = [e for e in data["entries"] if e["id"] != eid]
        if len(data["entries"]) == before:
            return False
        _save_staging(data)
        return True


def record_synced(paths):
    """Merge in the VPS-relative paths hyperion has copied into its Inbox."""
    with _LOCK:
        data = _load_staging()
        seen = set(data["synced"])
        for p in paths:
            if isinstance(p, str) and p and p not in seen:
                data["synced"].append(p)
                seen.add(p)
        data["synced"] = data["synced"][-MAX_SYNCED:]
        _save_staging(data)
        return len(data["synced"])


def _file_state(transfer_state):
    s = transfer_state or ""
    if "Succeeded" in s:
        return "downloaded"
    if "Completed" in s:
        return "failed"  # Completed but not Succeeded: aborted, errored, cancelled
    return "queued"


def _transfer_states(client, username):
    """filename -> slskd state, for every transfer slskd holds for one user."""
    d = client._call(f"/api/v0/transfers/downloads/{username}")
    out = {}
    for group in (d or {}).get("directories", []) if isinstance(d, dict) else []:
        for f in group.get("files", []):
            out[f.get("filename")] = f.get("state")
    return out


def staging_list():
    """Every staged entry with its current status, newest first."""
    with _LOCK:
        data = _load_staging()
    synced = set(data["synced"])
    entries = data["entries"]

    client = None
    by_user = {}
    try:
        client = S.Client()
    except S.SlskdError:
        pass
    for e in entries:
        if e.get("state") == "queued" and client and e.get("username"):
            by_user.setdefault(e["username"], None)
    for username in by_user:
        try:
            by_user[username] = _transfer_states(client, username)
        except S.SlskdError:
            by_user[username] = None

    out = []
    for e in entries:
        item = dict(e)
        if e.get("state") == "queued":
            states = by_user.get(e.get("username")) or {}
            file_states = []
            for f in e.get("files", []):
                fs = _file_state(states.get(f["filename"])) if states else "queued"
                # Only hyperion's report counts as synced: it means the file is in the Inbox.
                if _rel_path(f["filename"]) in synced:
                    fs = "synced"
                file_states.append(fs)
            if "failed" in file_states:
                item["status"] = "failed"
            elif file_states and all(s == "synced" for s in file_states):
                item["status"] = "synced"
            elif file_states and all(s in ("downloaded", "synced") for s in file_states):
                item["status"] = "downloaded"
            else:
                item["status"] = "queued"
            item["fileCounts"] = {s: file_states.count(s) for s in set(file_states)}
        else:
            item["status"] = e.get("state")
        out.append(item)
    return out


# ── search ──────────────────────────────────────────────────────────────────

def _ui_offer(offer, index):
    """One source as the panel renders it. `index` is 1-based, as try_sources expects."""
    groups = {}
    for f in offer.get("files") or []:
        _artist, album = S.guess_artist_album(f.get("filename"))
        groups.setdefault(album or "(unknown)", []).append(f)
    albums = []
    for album, files in sorted(groups.items(), key=lambda kv: -len(kv[1]))[:5]:
        albums.append({
            "name": album,
            "tracks": len(files),
            "size_mb": round(sum(f.get("size") or 0 for f in files) / 1e6, 1),
            "quality": S.quality_label(files),
        })
    return {
        "index": index,
        "username": offer["username"],
        "score": offer.get("score"),
        "hasFreeSlot": bool(offer.get("has_free_slot")),
        "queueLength": offer.get("queue_length"),
        "locked": offer.get("locked") or 0,
        "albums": albums,
    }


def _prune():
    """Drop the oldest searches so the registry cannot grow without bound."""
    if len(_SEARCHES) <= MAX_KEPT:
        return
    for sid in sorted(_SEARCHES, key=lambda k: _SEARCHES[k]["started"])[:-MAX_KEPT]:
        del _SEARCHES[sid]


def _search_offers(query):
    """Run one slskd search and return its ranked sources, in offer form."""
    client = S.Client()
    rsid, _rec = client.search(query)
    responses = client.responses(rsid)
    ranked = S.rank(query, responses, limit=LIVE_SOURCES)
    return D.build_offers(query, {"id": rsid, "ranked": ranked})


def _run_search(sid, query):
    try:
        offers = _search_offers(query)
        status, error = "done", None
    except S.SlskdError as e:
        offers, status, error = [], "error", str(e)
    except Exception as e:  # noqa: BLE001
        # Never let a thread die silently: the UI is polling for a status.
        offers, status, error = [], "error", f"unexpected: {e}"
    with _LOCK:
        rec = _SEARCHES.get(sid)
        if rec is not None:
            rec.update(status=status, error=error, offers=offers)


def start_search(query):
    """Start a search in the background. Returns the id to poll with."""
    sid = uuid.uuid4().hex[:12]
    with _LOCK:
        _SEARCHES[sid] = {"query": query, "status": "searching", "error": None,
                          "offers": [], "picks": {}, "started": time.time()}
        _prune()
    threading.Thread(target=_run_search, args=(sid, query), daemon=True).start()
    return sid


def get_search(sid):
    """The current state of a search, or None if the id is unknown."""
    with _LOCK:
        rec = _SEARCHES.get(sid)
        if rec is None:
            return None
        return {
            "searchId": sid,
            "query": rec["query"],
            "status": rec["status"],
            "error": rec["error"],
            "offers": [_ui_offer(o, i) for i, o in enumerate(rec["offers"], 1)],
            "picks": dict(rec["picks"]),
        }


def _run_pick(sid, index, album):
    key_str = str(index)
    with _LOCK:
        rec = _SEARCHES[sid]
        offers = list(rec["offers"])
        query = rec["query"]
        rec["picks"][key_str] = {"state": "starting", "notes": []}

    notes = []
    try:
        key = S.Client().key
        offer, chosen_album, files, notes = P.try_sources(
            offers, key, index, query, album_hint=album, log=lambda m: None)
        state = "failed" if offer is None else "queued"
    except S.SlskdError as e:
        offer, state, notes = None, "failed", [str(e)]
    except Exception as e:  # noqa: BLE001
        offer, state, notes = None, "failed", [f"unexpected: {e}"]

    pick = {"state": state, "notes": notes}
    if state == "queued":
        pick.update(username=offer["username"], album=chosen_album,
                    files=len(files))
        stage({"kind": "album", "query": query, "album": chosen_album or "",
               "username": offer["username"], "state": "queued",
               "files": [{"filename": f["filename"], "size": f.get("size") or 0}
                         for f in files]})
    with _LOCK:
        rec = _SEARCHES.get(sid)
        if rec is not None:
            rec["picks"][key_str] = pick


def start_pick(sid, index, album=None):
    """Queue a download from one source of a finished search.

    Returns (ok, error). The outcome lands in get_search()["picks"] once the
    background enqueue attempts finish.
    """
    with _LOCK:
        rec = _SEARCHES.get(sid)
        if rec is None:
            return False, "unknown search id (the server may have restarted)"
        if rec["status"] != "done":
            return False, f"search is {rec['status']}, not done"
        if not (1 <= index <= len(rec["offers"])):
            return False, f"no source [{index}] in this search"
    threading.Thread(target=_run_pick, args=(sid, index, album), daemon=True).start()
    return True, None


# ── send from favorites ─────────────────────────────────────────────────────

def start_send(artist, title, album, kind):
    """Queue a favorited track or its whole album. Returns the staging id.

    kind "track" queues the one file whose name matches the title, from the
    first source that has it. kind "album" queues the album from the best
    source, the same way a panel download does.
    """
    query = f"{artist} {album}" if kind == "album" and album else f"{artist} {title}"
    eid = stage({"kind": kind, "query": query, "artist": artist,
                 "title": title, "album": album or "", "username": None,
                 "state": "searching", "files": []})
    threading.Thread(target=_run_send,
                     args=(eid, query, title, album, kind), daemon=True).start()
    return eid


def _run_send(eid, query, title, album, kind):
    try:
        offers = _search_offers(query)
        if not offers:
            _update_entry(eid, state="failed", error="no sources found")
            return
        key = S.Client().key
        if kind == "album":
            offer, chosen_album, files, notes = P.try_sources(
                offers, key, 1, query, album_hint=album or None, log=lambda m: None)
            if offer is None:
                _update_entry(eid, state="failed", error="; ".join(notes) or "no source")
                return
            staged = [{"filename": f["filename"], "size": f.get("size") or 0} for f in files]
            _update_entry(eid, state="queued", username=offer["username"],
                          album=chosen_album or album or "", files=staged)
            return

        tokens = S._tokens(title)
        notes = []
        for offer in offers:
            match = [f for f in offer["files"]
                     if tokens and tokens <= S._tokens(f.get("filename") or "")]
            if not match:
                continue
            f = match[0]
            ok, msg = P.enqueue(offer["username"],
                                [{"filename": f["filename"], "size": f.get("size") or 0}], key)
            if ok:
                _update_entry(eid, state="queued", username=offer["username"],
                              files=[{"filename": f["filename"], "size": f.get("size") or 0}])
                return
            notes.append(f"{offer['username']}: {msg}")
        _update_entry(eid, state="failed",
                      error="no source had that track" if not notes else "; ".join(notes))
    except S.SlskdError as e:
        _update_entry(eid, state="failed", error=str(e))
    except Exception as e:  # noqa: BLE001
        _update_entry(eid, state="failed", error=f"unexpected: {e}")
