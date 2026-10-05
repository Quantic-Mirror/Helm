"""Live Soulseek search and download for the Music tab, run inside Helm.

The drainer path (search queued, drainer runs every 5 min, offers stored,
mail sent) is too slow for the Soulseek panel, so this runs the search in the
Helm process instead. A search takes 15-90 seconds, so it runs in a background
thread and the UI polls for the result.

Needs the slskd config mounted into the container (SLSKD_CONFIG) for the API
key. Results live in memory only: they are gone after a container restart,
which is fine for a search you just ran. Downloads land in slskd's downloads/
folder on the VPS, and the hyperion Inbox sync copies them from there.
"""
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
_LOCK = threading.Lock()
_SEARCHES = {}


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


def _run_search(sid, query):
    try:
        client = S.Client()
        rsid, _rec = client.search(query)
        responses = client.responses(rsid)
        ranked = S.rank(query, responses, limit=LIVE_SOURCES)
        offers = D.build_offers(query, {"id": rsid, "ranked": ranked})
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
        if offer is None:
            state = "failed"
        else:
            state = "queued"
    except S.SlskdError as e:
        state, notes = "failed", [str(e)]
    except Exception as e:  # noqa: BLE001
        state, notes = "failed", [f"unexpected: {e}"]

    with _LOCK:
        pick = {"state": state, "notes": notes}
        if state == "queued":
            pick.update(username=offer["username"], album=chosen_album,
                        files=len(files))
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
