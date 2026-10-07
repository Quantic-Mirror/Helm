#!/bin/sh
# Keep Tailscale reachable while Windscribe is connected. Two independent
# problems, both re-applied idempotently because Windscribe rebuilds its
# routing rules and nft table on every (re)connect / firewall toggle:
#
#  1. Windscribe's policy-routing rule (table 51820) outranks Tailscale's
#     (table 52, prio 5270), so tailnet traffic (100.64.0.0/10) gets sent
#     into the VPN tunnel. Fix: an earlier rule that looks up table 52 for
#     that range. Table 52 only holds tailnet peers, so a miss falls through
#     to Windscribe's rules and normal traffic is unaffected.
#  2. Windscribe's firewall (table inet windscribe) has policy drop on
#     input/output and never accepts tailscale0. Fix: accept rules in its
#     st_in/st_out chains (the split-tunnel hooks it jumps to last).
#     A separate nft table can't do this: an accept in one base chain doesn't
#     override a drop in another at the same hook.
#
# Must run as root. Safe to run repeatedly.

TS_IF=tailscale0
TS_RANGE=100.64.0.0/10

# Windscribe picks its rule priorities relative to whatever rules already
# exist (5208-5210 when only Tailscale's 5270 was there, 98-99 once ours sat
# at 100), so a fixed priority eventually loses to it. Instead, read the
# priority of its table-51820 rule and keep ours strictly below that.
WG_PRIO=$(ip rule show | awk '/lookup 51820/ { sub(":", "", $1); print $1; exit }')
MY_PRIO=$(ip rule show | awk -v r="$TS_RANGE" '$0 ~ ("to " r " lookup 52") { sub(":", "", $1); print $1; exit }')
if [ -n "$WG_PRIO" ]; then
    WANT=$((WG_PRIO - 1))
    [ "$WANT" -lt 1 ] && WANT=1
else
    WANT=100
fi
# With the VPN down there is nothing to stay below, so reset to 100. Without
# this the priority ratchets down ~2 per reconnect, since Windscribe always
# slots itself just under our rule.
if [ -z "$MY_PRIO" ] ||
   { [ -n "$WG_PRIO" ] && [ "$MY_PRIO" -ge "$WG_PRIO" ]; } ||
   { [ -z "$WG_PRIO" ] && [ "$MY_PRIO" -ne "$WANT" ]; }; then
    while ip rule del to "$TS_RANGE" lookup 52 2>/dev/null; do :; done
    ip rule add to "$TS_RANGE" lookup 52 priority "$WANT"
fi

if nft list table inet windscribe >/dev/null 2>&1; then
    nft list chain inet windscribe st_in 2>/dev/null | grep -q "\"$TS_IF\"" ||
        nft add rule inet windscribe st_in iifname "$TS_IF" accept
    nft list chain inet windscribe st_out 2>/dev/null | grep -q "\"$TS_IF\"" ||
        nft add rule inet windscribe st_out oifname "$TS_IF" accept
fi
