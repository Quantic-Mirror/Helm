#!/usr/bin/env python3
"""
mpv_ipc.py — thin client for mpv's JSON IPC protocol over a Unix socket.

Talks directly to mpv's own `--input-ipc-server` socket (newline-delimited
JSON request/reply) rather than pulling in the `python-mpv` package — same
"stdlib only, reuse the daemon's own interface" precedent as `emit_event.py`
talking to RabbitMQ's HTTP API instead of pika, and the vault/pass backend
instead of a GPG library. See mpv(1) `JSON IPC` section for the protocol.

Expects `mpv-player.service` (see that file) to already be running with
`--idle=yes --input-ipc-server=<socket>` — this module is just a client, it
never starts/stops the mpv process itself (that's systemd's job, same as
soma-radio.service).

A fresh connection is opened per command rather than holding one open across
calls: this server handles one request per HTTP request already (no
long-lived per-client session to hang a persistent mpv connection off of),
and a short-lived socket sidesteps having to run a background reader
thread to demux unsolicited mpv events from command replies on a shared
connection. Each command carries a `request_id` so its reply can be picked
out of the reply stream even if an event notification (e.g.
"property-change") happens to arrive first on the same connection.
"""

import itertools
import json
import os
import socket
import time

MPV_SOCKET = os.environ.get("MPV_IPC_SOCKET") or os.path.join(
    os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "mpv-player.sock"
)

_request_id_seq = itertools.count(1)


def _mpv_raw(command_list, timeout=5):
    """Send one IPC command, return (data, error_str). error_str is None on
    success. Never raises -- callers get a clear error string instead of a
    socket traceback, matching this codebase's convention of surfacing
    "the daemon isn't up" as a JSON error rather than a 500."""
    if not os.path.exists(MPV_SOCKET):
        return None, f"mpv-player.service not running or socket not found at {MPV_SOCKET}"

    request_id = next(_request_id_seq)
    payload = json.dumps({"command": command_list, "request_id": request_id}) + "\n"

    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        try:
            sock.connect(MPV_SOCKET)
            sock.sendall(payload.encode("utf-8"))

            buf = b""
            deadline = time.time() + timeout
            while time.time() < deadline:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        msg = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    # Unsolicited events (e.g. "property-change", "idle")
                    # have no request_id -- keep reading past them.
                    if msg.get("request_id") != request_id:
                        continue
                    if msg.get("error") == "success":
                        return msg.get("data"), None
                    return None, msg.get("error") or "mpv returned an error"
            return None, "timed out waiting for mpv reply"
        finally:
            sock.close()
    except OSError as e:
        return None, f"could not reach mpv IPC socket: {e}"


def mpv_get_property(name, timeout=5):
    return _mpv_raw(["get_property", name], timeout=timeout)


def mpv_set_property(name, value, timeout=5):
    _, err = _mpv_raw(["set_property", name, value], timeout=timeout)
    return err is None, err


def mpv_loadfile(path, mode="replace", timeout=5):
    """mode="replace" (default) clears the current playlist and plays path
    immediately. mode="append-play" queues it, starting playback if mpv was
    idle. mode="append" queues without affecting current playback."""
    _, err = _mpv_raw(["loadfile", path, mode], timeout=timeout)
    return err is None, err


def mpv_command(name, *args, timeout=5):
    """Generic passthrough for anything without a dedicated wrapper below,
    e.g. mpv_command("playlist-next"), mpv_command("seek", 30, "absolute"),
    mpv_command("playlist-remove", 2), mpv_command("playlist-move", 0, 3)."""
    _, err = _mpv_raw([name, *args], timeout=timeout)
    return err is None, err


# Properties bundled into a single status response. Keep this list in sync
# with what the /api/audio/player/status endpoint reports to the frontend.
_STATUS_PROPERTIES = (
    "pause",
    "time-pos",
    "duration",
    "volume",
    "path",
    "media-title",
    "playlist-pos",
    "playlist-count",
    "idle-active",
)


def mpv_status(timeout=5):
    """Bundles the get_property calls a status endpoint needs into one call
    so callers don't have to make five separate IPC round trips (and thread
    a single "mpv isn't running" error through five call sites). Returns
    (status_dict_or_None, error_str)."""
    if not os.path.exists(MPV_SOCKET):
        return None, f"mpv-player.service not running or socket not found at {MPV_SOCKET}"

    status = {}
    for prop in _STATUS_PROPERTIES:
        data, err = mpv_get_property(prop, timeout=timeout)
        if err is not None:
            # A property being unset (e.g. "path" while nothing is loaded)
            # comes back as an mpv-level error, not a connection failure --
            # treat it as "no value" rather than aborting the whole status.
            status[prop] = None
            continue
        status[prop] = data
    return status, None


def get_playlist(timeout=5):
    """Returns mpv's own internal playlist as a list of {filename, current,
    playing} dicts (mpv's native shape), or (None, error_str)."""
    return _mpv_raw(["get_property", "playlist"], timeout=timeout)
