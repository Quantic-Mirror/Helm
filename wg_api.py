"""
wg_api.py — backend logic for the Helm VPN tab. Manages a WireGuard server
(wg0, for hyperion/shrike's personal VPN) and a set of WireGuard client
"circuits" to Windscribe access points, switching which one (or none —
"direct") wg0's client traffic exits through.

Place this file next to wg_control_server.py on the VPS host (NOT inside
Docker — WireGuard needs the host's own real network namespace). Imported
by wg_control_server.py; has no HTTP awareness of its own, mirroring
vault_api.py's split from vault_server.py.

Fail-closed by design: table `windscribe` always has exactly one occupant
for its default route — either `blackhole` or a specific circuit interface,
swapped atomically with `ip route replace` (never add/delete pairs, which
would create a window with either zero or two candidate routes). The
`ip rule` selecting that table only exists while a circuit should be
active; removing it lets wg0 traffic fall through to the main table
(normal "direct" egress) instead.
"""

import ipaddress
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

WG_DIR = Path(os.environ.get("WG_DIR", "/etc/wireguard"))
WG0_IFACE = "wg0"
RT_TABLE = "windscribe"

# The subnet wg0 hands out to its peers (hyperion/shrike) — required so we
# know what to match in `ip rule from <subnet>`. No default: guessing wrong
# here would silently scope the kill switch to the wrong traffic.
WG0_SUBNET = os.environ.get("WG0_SUBNET")

# The VPS's own internet-facing interface, for the always-on "Direct" NAT
# rule. Auto-detected from the main routing table if not set explicitly.
_EGRESS_IFACE_ENV = os.environ.get("WG_EGRESS_IFACE")

STATE_DIR = Path(os.environ.get("WG_STATE_DIR", "/etc/helm-wg"))
STATE_FILE = STATE_DIR / "state.json"

# WireGuard rekeys on its own schedule only when traffic flows — circuits
# need `PersistentKeepalive` set (see CONTAINER_SETUP.md) for an idle tunnel
# to keep handshaking. This threshold assumes that's in place.
HANDSHAKE_STALE_SECONDS = 180
HANDSHAKE_CONFIRM_TIMEOUT = 10
HANDSHAKE_CONFIRM_POLL_INTERVAL = 1


class WgError(Exception):
    """Raised for any circuit-switch failure the caller should see as an error."""


def _run(cmd, timeout=10, check=False):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if check and r.returncode != 0:
            raise WgError(f"{' '.join(cmd)} failed: {r.stderr.strip() or r.stdout.strip()}")
        return r.stdout, r.stderr, r.returncode
    except subprocess.TimeoutExpired:
        raise WgError(f"{' '.join(cmd)} timed out after {timeout}s")


def _egress_iface():
    if _EGRESS_IFACE_ENV:
        return _EGRESS_IFACE_ENV
    out, _, rc = _run(["ip", "-j", "route", "show", "default"])
    if rc == 0 and out.strip():
        try:
            routes = json.loads(out)
            if routes:
                return routes[0]["dev"]
        except (json.JSONDecodeError, KeyError, IndexError):
            pass
    raise WgError("could not auto-detect the VPS's egress interface — set WG_EGRESS_IFACE")


def _require_wg0_subnet():
    if not WG0_SUBNET:
        raise WgError("WG0_SUBNET is not set — required to scope the kill switch correctly")
    return WG0_SUBNET


# ── circuit discovery ────────────────────────────────────────────────────────

# Linux caps network interface names at IFNAMSIZ-1 = 15 characters.
# wg-quick derives the interface name from the config filename and just
# fails with a generic "does not exist" (not a length complaint) if it's
# too long — so validate this ourselves at discovery time instead of
# listing a circuit that's guaranteed to fail the moment it's selected.
MAX_IFACE_LEN = 15


def list_circuit_ids():
    """Circuit id == the .conf filename stem, e.g. windscribe-1.conf -> 'windscribe-1'."""
    if not WG_DIR.is_dir():
        return []
    ids = []
    for p in sorted(WG_DIR.glob("windscribe-*.conf")):
        if len(p.stem) > MAX_IFACE_LEN:
            print(f"WARNING: skipping {p.name} — interface name '{p.stem}' is "
                  f"{len(p.stem)} chars, over Linux's {MAX_IFACE_LEN}-char limit "
                  f"for network interface names. Rename the file to something "
                  f"shorter (e.g. windscribe-atl.conf).", file=sys.stderr)
            continue
        ids.append(p.stem)
    return ids


def _circuit_conf_path(circuit_id):
    if not re.fullmatch(r"windscribe-[A-Za-z0-9_-]+", circuit_id or ""):
        raise WgError(f"invalid circuit id: {circuit_id!r}")
    path = WG_DIR / f"{circuit_id}.conf"
    if not path.is_file():
        raise WgError(f"no such circuit: {circuit_id}")
    return path


# ── status ───────────────────────────────────────────────────────────────────

def _wg_show_all_dump():
    """Parse `wg show all dump` into {iface: {"peers": [{...}]}}."""
    out, _, rc = _run(["wg", "show", "all", "dump"])
    ifaces = {}
    if rc != 0:
        return ifaces
    for line in out.splitlines():
        fields = line.split("\t")
        if len(fields) == 5:
            # interface line: iface, private-key, public-key, listen-port, fwmark
            iface = fields[0]
            ifaces.setdefault(iface, {"peers": []})
        elif len(fields) == 9:
            # peer line: iface, public-key, preshared-key, endpoint, allowed-ips,
            # latest-handshake, rx, tx, persistent-keepalive
            iface = fields[0]
            ifaces.setdefault(iface, {"peers": []})
            ifaces[iface]["peers"].append({
                "public_key": fields[1],
                "endpoint": fields[3] if fields[3] != "(none)" else None,
                "allowed_ips": fields[4],
                "latest_handshake": int(fields[5]),
                "rx_bytes": int(fields[6]),
                "tx_bytes": int(fields[7]),
            })
    return ifaces


def _handshake_age(iface_data):
    if not iface_data or not iface_data["peers"]:
        return None
    ts = max((p["latest_handshake"] for p in iface_data["peers"]), default=0)
    if ts == 0:
        return None
    return max(0, int(time.time()) - ts)


def _current_windscribe_target():
    """What table `windscribe`'s default route currently points at, or None (blackhole/absent)."""
    out, _, rc = _run(["ip", "-j", "route", "list", "table", RT_TABLE])
    if rc != 0 or not out.strip():
        return None
    try:
        routes = json.loads(out)
    except json.JSONDecodeError:
        return None
    for r in routes:
        if r.get("dst") == "default" and r.get("dev"):
            return r["dev"]
    return None


def load_persisted_target():
    if not STATE_FILE.is_file():
        return None
    try:
        data = json.loads(STATE_FILE.read_text())
        return data.get("active_circuit")
    except (json.JSONDecodeError, OSError):
        return None


def _persist_target(circuit_id_or_none):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps({"active_circuit": circuit_id_or_none}))
    os.replace(tmp, STATE_FILE)


def get_status():
    """active_circuit / fail_closed are computed from OBSERVED kernel state
    (the ip rule + table windscribe's current target), not from the
    persisted record — the persisted record is only a hint for reconcile()
    at startup. This avoids the record and reality ever disagreeing about
    what the dashboard displays: a watchdog trip or a failed switch_to()
    both clear the rule-exists/blackholed combination the same way, so
    there is exactly one source of truth for "are we actually blocked"."""
    dump = _wg_show_all_dump()
    wg0 = dump.get(WG0_IFACE)
    circuits = []
    for cid in list_circuit_ids():
        iface_data = dump.get(cid)
        circuits.append({
            "id": cid,
            "up": iface_data is not None,
            "handshake_age_s": _handshake_age(iface_data),
        })
    rule_present = bool(WG0_SUBNET) and _rule_exists(WG0_SUBNET)
    current_target = _current_windscribe_target() if rule_present else None
    if not rule_present:
        active_circuit, fail_closed = "direct", False
    elif current_target:
        active_circuit, fail_closed = current_target, False
    else:
        active_circuit, fail_closed = None, True
    return {
        "wg0": {
            "up": wg0 is not None,
            "peers": (wg0 or {}).get("peers", []),
        },
        "circuits": circuits,
        "active_circuit": active_circuit,
        "fail_closed": fail_closed,
    }


# ── idempotent primitives ────────────────────────────────────────────────────

def _rule_exists(subnet):
    # Filter by table NAME at the `ip` command level rather than comparing
    # a "table" field from unfiltered JSON output ourselves — `ip -j rule
    # show` (no filter) reports table as its raw numeric id (e.g. 200), not
    # the resolved name, so comparing that field against RT_TABLE ("wind-
    # scribe") as a string never matched and made this check a permanent
    # false negative — every switch attempt then tried to blindly re-add an
    # already-present rule and failed with "RTNETLINK answers: File exists".
    out, _, rc = _run(["ip", "-j", "rule", "show", "table", RT_TABLE])
    if rc != 0:
        return False
    try:
        rules = json.loads(out)
    except json.JSONDecodeError:
        return False
    net = ipaddress.ip_network(subnet, strict=False)
    for r in rules:
        src = r.get("src")
        if not src:
            continue
        # `ip -j rule show` splits the source network into separate "src"
        # (bare address, e.g. "10.66.0.0") and "srclen" (prefix length,
        # e.g. 24) fields — it does NOT give a combined "10.66.0.0/24"
        # string. Parsing r["src"] alone silently defaults to a /32 host
        # match that can never equal an actual /24 subnet, which made this
        # check a permanent false negative even after filtering by table.
        cidr = f"{src}/{r['srclen']}" if "srclen" in r else src
        try:
            if ipaddress.ip_network(cidr, strict=False) == net:
                return True
        except ValueError:
            continue
    return False


def _ensure_rule(subnet):
    if not _rule_exists(subnet):
        _run(["ip", "rule", "add", "from", subnet, "lookup", RT_TABLE, "priority", "100"], check=True)


def _ensure_no_rule(subnet):
    # ip rule del only removes one match per call; loop until none remain
    # (defensive — normal operation never creates duplicates because
    # _ensure_rule checks first).
    for _ in range(10):
        if not _rule_exists(subnet):
            return
        _run(["ip", "rule", "del", "from", subnet, "lookup", RT_TABLE], check=True)


def _set_windscribe_default(target_iface):
    """The single mutable slot for table `windscribe`'s default route.
    target_iface=None -> blackhole (fail closed). Uses `ip route replace`
    (upsert) so there is never a moment with zero or two candidate routes —
    an add/delete pair, or fighting over route metrics, both risk a window
    where either nothing matches (accidental fail-open via fallthrough) or
    two routes race on priority."""
    if target_iface:
        _run(["ip", "route", "replace", "default", "dev", target_iface, "table", RT_TABLE], check=True)
    else:
        _run(["ip", "route", "replace", "blackhole", "default", "table", RT_TABLE], check=True)


def _masquerade_exists(iface, subnet):
    _, _, rc = _run(["iptables", "-t", "nat", "-C", "POSTROUTING", "-s", subnet, "-o", iface, "-j", "MASQUERADE"])
    return rc == 0


def _ensure_masquerade(iface, subnet):
    if not _masquerade_exists(iface, subnet):
        _run(["iptables", "-t", "nat", "-A", "POSTROUTING", "-s", subnet, "-o", iface, "-j", "MASQUERADE"], check=True)


def _ensure_no_masquerade(iface, subnet):
    while _masquerade_exists(iface, subnet):
        _run(["iptables", "-t", "nat", "-D", "POSTROUTING", "-s", subnet, "-o", iface, "-j", "MASQUERADE"], check=True)


def _forward_exists(in_iface, out_iface):
    _, _, rc = _run(["iptables", "-C", "FORWARD", "-i", in_iface, "-o", out_iface, "-j", "ACCEPT"])
    return rc == 0


def _ensure_forward(in_iface, out_iface):
    if not _forward_exists(in_iface, out_iface):
        _run(["iptables", "-A", "FORWARD", "-i", in_iface, "-o", out_iface, "-j", "ACCEPT"], check=True)
    # established/related return traffic
    _, _, rc = _run(["iptables", "-C", "FORWARD", "-i", out_iface, "-o", in_iface,
                      "-m", "state", "--state", "ESTABLISHED,RELATED", "-j", "ACCEPT"])
    if rc != 0:
        _run(["iptables", "-A", "FORWARD", "-i", out_iface, "-o", in_iface,
              "-m", "state", "--state", "ESTABLISHED,RELATED", "-j", "ACCEPT"], check=True)


def _ensure_no_forward(in_iface, out_iface):
    while _forward_exists(in_iface, out_iface):
        _run(["iptables", "-D", "FORWARD", "-i", in_iface, "-o", out_iface, "-j", "ACCEPT"], check=True)


# ── circuit up/down ──────────────────────────────────────────────────────────

def _bring_up(circuit_id):
    _circuit_conf_path(circuit_id)  # validates id + existence
    _run(["wg-quick", "up", circuit_id], check=True)


def _tear_down(circuit_id):
    # Best-effort — a circuit that's already down (or was never up) is fine.
    _run(["wg-quick", "down", circuit_id])


def _wait_for_handshake(circuit_id, timeout=HANDSHAKE_CONFIRM_TIMEOUT):
    deadline = time.time() + timeout
    while time.time() < deadline:
        dump = _wg_show_all_dump()
        age = _handshake_age(dump.get(circuit_id))
        if age is not None and age < timeout + 5:
            return True
        time.sleep(HANDSHAKE_CONFIRM_POLL_INTERVAL)
    return False


def _teardown_active_windscribe(except_id=None):
    """Bring down every currently-up windscribe-* interface (except one, if given)
    and remove its NAT/forward rules, so exactly zero or one is ever left running."""
    dump = _wg_show_all_dump()
    for cid in list_circuit_ids():
        if cid == except_id:
            continue
        if cid in dump:
            _ensure_no_masquerade(cid, _require_wg0_subnet())
            _ensure_no_forward(WG0_IFACE, cid)
            _tear_down(cid)


# ── public operations ────────────────────────────────────────────────────────

def switch_to(circuit_id):
    """circuit_id is "direct" or one of list_circuit_ids(). Always leaves the
    system in a definite state: either the requested circuit is up with a
    confirmed handshake and installed as the default for table `windscribe`,
    or everything windscribe-related is torn down and table `windscribe`
    stays/reverts to blackhole (fail closed) — never a partial state."""
    subnet = _require_wg0_subnet()

    if circuit_id == "direct":
        _ensure_no_rule(subnet)
        _teardown_active_windscribe()
        _set_windscribe_default(None)
        _persist_target("direct")
        return get_status()

    if circuit_id not in list_circuit_ids():
        raise WgError(f"unknown circuit: {circuit_id}")

    current = get_status()
    if current["active_circuit"] == circuit_id and not current["fail_closed"]:
        return current  # already there with a confirmed handshake — nothing to do

    # Blackhole first — mid-transition traffic is blocked, not leaked.
    _ensure_rule(subnet)
    _set_windscribe_default(None)
    _teardown_active_windscribe(except_id=circuit_id)

    try:
        _bring_up(circuit_id)
    except WgError:
        _persist_target(None)
        raise

    if not _wait_for_handshake(circuit_id):
        _tear_down(circuit_id)
        _persist_target(None)
        raise WgError(f"{circuit_id} did not complete a handshake in time — traffic remains blocked")

    _ensure_masquerade(circuit_id, subnet)
    _ensure_forward(WG0_IFACE, circuit_id)
    _set_windscribe_default(circuit_id)
    _persist_target(circuit_id)
    return get_status()


def fail_closed_now(reason=""):
    """Watchdog trip: drop straight to blackhole and tear down whatever
    windscribe circuit was active, without attempting anything fancier.
    The ip rule is deliberately left in place (not removed) — that's what
    makes this fail CLOSED rather than falling through to Direct."""
    _set_windscribe_default(None)
    _teardown_active_windscribe()
    _persist_target(None)
    status = get_status()
    status["reason"] = reason
    return status


def reconcile():
    """Startup/crash-recovery entry point. Always starts from a known-safe
    baseline, then attempts to restore the persisted circuit for real
    (never trusts the record without re-verifying a live handshake)."""
    egress = _egress_iface()
    subnet = _require_wg0_subnet()
    _ensure_masquerade(egress, subnet)          # base Direct NAT, always on
    _ensure_forward(WG0_IFACE, egress)
    _set_windscribe_default(None)               # boot fail-closed, always

    target = load_persisted_target()
    if not target or target == "direct":
        switch_to("direct")
        return
    try:
        switch_to(target)
    except WgError:
        # switch_to() already leaves state blackholed + persisted None on failure.
        pass


def watchdog_check():
    """Called periodically. Returns True if it just tripped the kill switch."""
    target = load_persisted_target()
    if not target or target == "direct":
        return False
    dump = _wg_show_all_dump()
    age = _handshake_age(dump.get(target))
    if age is None or age > HANDSHAKE_STALE_SECONDS:
        fail_closed_now(reason=f"{target} handshake stale ({age}s)" if age is not None else f"{target} interface down")
        return True
    return False
