# Helm

Self-hosted personal dashboard. Single-file frontend (`index.html`) served by a
stdlib-only Python backend (`helm_server.py`), plus a few small standalone
Python helper services. No build step, no framework, no npm/pip dependency
chain for the core app.

> This file is tracked in git (the repo is public). Do not put secrets here —
> tokens and keys live in `STATE_DIR` (`*_token.txt`, `cert.pem`/`key.pem`),
> which stay gitignored.

## Topology

- **the VPS** — the host `helm_server.py` runs on (via `docker-compose.yml`);
  serves the dashboard itself. (Not popcorn — an earlier version of this file
  said popcorn, but the live `helm` container runs on the VPS.)
- **hyperion** — separate host where `pass` + gpg-agent live; `vault_server.py`
  runs there and `helm_server.py` proxies `/api/vault/*` to it over HTTP with a
  shared-secret token (`vault_token.txt`, copied to both machines).
- **leafwiki** / **leafwiki-proxy** — containers in the same
  `docker-compose.yml` stack as `helm` itself, not on hyperion or a separate
  host. `leafwiki-proxy` (`helm_tls_proxy.py`) fronts the LeafWiki container
  so the Wiki tab can iframe it cross-origin — see "helm_tls_proxy.py
  cookie-rewriting pattern" below. The Wiki tab cycled through Memos →
  TriliumNext → SilverBullet before landing here — see git history if you
  need the reasons those didn't work out. LeafWiki's pages are plain
  Markdown files under `./leafwiki-data`, and its admin account is set
  deterministically via `LEAFWIKI_JWT_SECRET`/`LEAFWIKI_ADMIN_PASSWORD` env
  vars on first boot — no browser setup wizard, unlike TriliumNext's earlier
  broken login flow.
- **dailytxt** / **dailytxt-proxy** — containers in the same `docker-
  compose.yml` stack, wired the same way as leafwiki/leafwiki-proxy above:
  `dailytxt-proxy` (`helm_tls_proxy.py`) fronts the DailyTxT container so the
  Journal tab can iframe it cross-origin. DailyTxT replaces an earlier native
  client-side-Web-Crypto Journal tab (removed — see git history) with a
  dedicated encrypted-diary app; its admin account is set deterministically
  via `DAILYTXT_SECRET_TOKEN`/`DAILYTXT_ADMIN_PASSWORD` env vars on first
  boot, same no-setup-wizard posture as LeafWiki.
- **wg_control_server.py** — runs natively on the VPS host itself, NOT in
  Docker (unlike leafwiki/dailytxt above) — it manages `wg0` (a personal
  WireGuard VPN server hyperion/shrike connect into with their own native
  WireGuard clients) plus a set of WireGuard client "circuits" to Windscribe
  access points, switching which one (or "direct") wg0's client traffic
  exits through. It needs the VPS's own real network namespace, which a
  Docker container doesn't get even with `NET_ADMIN` unless using
  `network_mode: host` — see "WireGuard VPN proxy" below for why that
  alternative was rejected. `helm_server.py` proxies `/api/vpn/*` to it over
  a Unix socket (`/run/helm-wg/control.sock`), not TCP+token like
  vault/audio, since this backend is always local — see the "WireGuard VPN
  proxy" banner in `helm_server.py` and `CONTAINER_SETUP.md`'s VPN section
  for the one-time host setup this requires.
- User on the host is `carl` (see docker-group comments).
- Don't assume everything runs on one machine — if you're about to shell out
  to something host-specific (`pass`, gpg, a systemd unit), check whether it's
  actually meant to run on the VPS (where `helm_server.py` itself runs) or
  gets proxied to hyperion instead.
- Hermes is not integrated into Helm — no Hermes tab, no embedded webui iframe,
  no `hermes-page` section in `index.html`. The desktop app connects to the
  Hermes backend on popcorn over an SSH tunnel instead. Do not add a Hermes tab
  or re-embed hermes-webui; use the desktop app for agent interactions.

## Frontend: the TDZ bug pattern (`index.html`)

The whole app is one `<script>` block (~6000 lines) with top-level `function
render*()` definitions and top-level `let`/`const` state. Function
declarations are hoisted, so a `render*()` defined later in the file can
still be *called* early — but the `const`/`let` variables it closes over are
**not** hoisted (temporal dead zone). If a render function that references a
module-level `const`/`let` ends up running earlier than expected (e.g. via a
startup hash check, a saved theme replaying old state, a middle-click opening
a tab immediately), it throws a ReferenceError even though the same code
worked fine when it only ever ran from a later user click.

**Fix pattern already established in this file**: move the declaration up
into the early declarations block near the top of the script (around
index.html:3106–3178), *before* any code that might synchronously reach it
during startup. Leave a comment at the old spot and the new spot explaining
why, e.g.:

```js
// Moved up from later in the file — switchTab() can now run this early
// (via the startup hash check, for a tab opened via middle-click) and
// calls renderFeeds()/renderNews(), which touch all five of these. They
// used to be safe declared later, back when Feeds/News were only ever
// rendered in response to a later user click, by which point the whole
// script had already finished its first pass.
const FEEDS_CACHE = {};
```

When adding new module-level state that a render function closes over: if
there's *any* code path where that function could fire before its normal
place in file order (new startup hooks, new hash-routing, new "restore last
session" logic), declare the state in the early block up front rather than
waiting to hit the TDZ error. If you do hit it, the fix is always "move the
declaration up," not "wrap it in `var`" or "guard with `typeof` checks."

## Frontend: profiles (Personal / Work)

Helm has two fixed profiles. **Per-profile** data is the bookmark board
(`state.columns` + `state.bookmarks` + `state.collapsedCols`) and the YouTube
feed list (`state.feeds`). **Shared** across both: News/RSS (`newsSources`),
dashboard `widgets`, `calendarEvents`, `workouts`/`workoutRoutines`, and
`settings` (theme included). The two profiles differ **only** in that
per-profile data — every tab is visible in both, and all other content is
identical.

Design decisions, each load-bearing — don't undo them without a reason:

- **Per-profile data is stored in two *top-level* `state` keys**,
  `state.profilePersonal` and `state.profileWork` (each
  `{columns, bookmarks, collapsedCols, feeds}`), **not** a nested
  `state.profiles` map. The multi-device sync merge is per-top-level-key
  (see the LWW note under *Other conventions*), so two devices editing
  *different* profiles at once each touch a different key and both edits
  survive. A single nested map would be one merge unit — the second pusher
  would clobber the other profile.
- **The live keys stay the working copy.** `state.columns` / `bookmarks` /
  `collapsedCols` / `feeds` are what every render/edit site reads and writes,
  unchanged (~90 call sites). They mirror the active profile's sub-object.
  `hydrateActiveProfile()` deep-copies sub-object → live keys;
  `writeBackActiveProfile()` copies live keys → sub-object and runs first
  thing in `save()`. Do **not** refactor the call sites to accessors
  (`pf().bookmarks`) — that's the high-risk change this indirection exists to
  avoid.
- **The active profile is per-device**: `localStorage['helm_active_profile']`
  (`'personal'` | `'work'`), never part of synced `state`. A laptop can be on
  Work while a phone stays on Personal; only the profiles' *contents* sync.
- **Visual cue**: `applyProfileClass()` toggles `body.profile-work`, which
  warm-tints `#topbar` (explicit `#d9822b` amber, not `var(--accent)`, so it
  reads under every theme). Called from `switchProfile()` and once in
  `load()` — not from `afterStateSwap()`, since a state swap never changes the
  per-device active profile.
- **`afterStateSwap()` is mandatory after any wholesale `state` reassignment.**
  It replaces the `render() + renderDashboard() + renderUpcomingEvents()` trio
  (the df4b55d rule) at every site that swaps `state` from the wire or a file
  — `initSync`, `pollForRemoteChanges`, `pushStateToBackend`'s merge branch,
  `restoreBackup`, `loadConfig`/`loadConfigEncrypted`, `importJSON`. It
  re-runs `ensureProfiles()` + `hydrateActiveProfile()` (the wire blob's live
  keys may hold the *other* profile's data, or a pre-profiles blob may have no
  sub-objects at all), re-renders, and re-persists locally. New `state`-swap
  sites must call it too.
- **No tab hiding.** Both profiles show every tab; `switchTab()` and the
  startup hash router do no profile-based redirect. (An earlier version hid
  `backups`/`workout`/`journal` under Work via `PROFILE_HIDDEN_TABS` +
  `data-profile-hide` — removed after testing; don't reintroduce it without
  being asked.)
- **Migration** is `ensureProfiles()`, idempotent, run on `load()` and inside
  `afterStateSwap()`: a pre-profiles blob's existing data becomes Personal;
  Work is seeded with **one empty "Work" column** (non-empty on purpose, so
  `load()`'s "no columns → `seedDefaults()`" path can't fire against a
  freshly-switched Work profile and clobber `state`). Config export/import
  carry `profilePersonal`/`profileWork` through.
- The profile constants (`ACTIVE_PROFILE`, `PROFILE_STATE_KEYS`,
  `PROFILE_LIVE_KEYS`) live in the early declaration block for the usual TDZ
  reason — `switchTab()` reaches `ACTIVE_PROFILE` during the startup hash check.

## Prefer lightweight/stdlib over heavier stacks

This is the dominant engineering bias in the backend and proxy scripts.
Recurring pattern in the code: reuse something that already exists and
already works, rather than adding a service, a dependency, or reimplementing
behavior a battle-tested tool already has.

Examples already in the codebase — follow this precedent for new work:

- **No pip dependencies for the core server.** `helm_server.py` is stdlib
  only. System stats read `/proc` directly on Linux rather than requiring
  `psutil`. `zxcvbn` in `vault_api.py` is the one optional exception, wrapped
  in `try/except ImportError` so its absence degrades a single feature
  (password strength scoring) instead of breaking the server.
- **Talk to existing stores directly instead of standing up new services.**
  The password vault shells out to `pass` rather than reimplementing GPG
  handling — "so it inherits gpg-agent's cache/timeout/lock behavior
  automatically" (vault_api.py:3-4).
- **Talk to daemons over their native socket instead of shelling out to a
  CLI.** Docker status/logs/control go through the Unix socket API directly
  (`_UnixSocketHTTPConnection` in helm_server.py) instead of parsing
  `docker ps`/`docker logs` output.
- **Reuse infra that already exists instead of adding new infra.** The TLS
  proxies (`helm_tls_proxy.py`, `gopher_tls_proxy.py`) deliberately don't
  reach for nginx/Caddy: "it's the same ssl + http.server pattern already
  used by helm_server.py itself, so there's nothing new to install or learn,
  and it reuses cert files that are already trusted by your browsers"
  (gopher_tls_proxy.py:13-16). They reuse Helm's own `cert.pem`/`key.pem`
  rather than generating or managing separate certs.
- **Generalize instead of writing bespoke one-offs**, but only once a second
  case shows up. `gopher_tls_proxy.py` was the first, single-purpose version
  of this idea; `helm_tls_proxy.py` replaced the *pattern* going forward with
  one generic tool once Wiki.js/Forgejo/Element needed the same thing
  ("Rather than writing a bespoke wrapper per service ... this is a single
  generic tool," helm_tls_proxy.py:9-13). The old single-purpose script is
  left in place rather than deleted/forced into the generic one.

When you're about to add a dependency, a new microservice, or reimplement
something a running daemon already exposes: stop and check whether there's a
socket, log file, or CLI you can read/drive directly instead. That's the
default here, not an exception.

## `helm_tls_proxy.py` cookie-rewriting pattern

Several self-hosted apps Helm iframes were never designed to be embedded
cross-origin, which breaks in two independent ways that both need fixing at
the proxy layer:

1. **Framing is blocked.** Apps ship their own `X-Frame-Options` or CSP
   `frame-ancestors` that blocks embedding from Helm's origin.
   `_rewrite_framing_headers()` strips `X-Frame-Options` entirely and
   rewrites `frame-ancestors` to `'self' <Helm's allowed origins>` —
   `'self'` is kept (not replaced) because some apps iframe their *own*
   content internally (e.g. kiwix-serve's viewer), and dropping it would
   silently break that while fixing the external case.
2. **Session cookies get silently discarded.** Even once framing is allowed,
   browsers require `SameSite=None; Secure` on any cookie used inside a
   cross-origin iframe. Most self-hosted apps don't set this, so login
   *succeeds* server-side but the browser drops the session cookie right
   after — every subsequent request then looks logged-out. Fixed by
   `_rewrite_cookie_for_iframe()`: strip any existing `SameSite=`/`Secure`
   attribute the backend sent and re-add `SameSite=None; Secure`
   unconditionally on every `Set-Cookie`.

Both rewrites happen in `_send_headers()`, applied uniformly to every
response the proxy relays. `ALLOWED_FRAME_ORIGINS` is overridable via the
`HELM_FRAME_ORIGINS` env var — needed because `frame-ancestors` requires an
*exact* origin match (scheme+host+port), so a hostname and its own IP count
as different origins and both must be listed if Helm is reachable via either.

**When wiring a new iframed service through this proxy**: you get both fixes
for free by pointing it at `helm_tls_proxy.py` — don't write a new proxy or
hand-patch the target app's config. If the new service needs WebSocket
upgrades, that's explicitly *not* supported yet ("flag it rather than
guessing silently," helm_tls_proxy.py:17-19) — raise it rather than faking
around the gap.

## WireGuard VPN proxy (`wg_control_server.py`)

`helm_server.py` runs in a Docker container with no `NET_ADMIN`/host-network
access, so it can't create or control WireGuard interfaces directly.
Following the vault/audio precedent of "a feature needs real privileged host
access → small native helper process that helm_server.py proxies to,"
`wg_control_server.py` runs natively on the VPS host (not in Docker) and
owns `wg0` (the personal VPN server hyperion/shrike connect to) plus a set
of WireGuard client "circuits" to Windscribe access points.

Two things make this different from the vault/audio proxy shape, both
deliberate:

1. **Unix socket, not TCP+token.** vault/audio run on a *different* host
   (hyperion), so `proxy_to_vault`/`proxy_to_audio` need a real network
   call and a shared-secret token. `wg_control_server.py` runs on the
   *same* host as the `helm` container — just a different privilege domain
   — so it listens on `/run/helm-wg/control.sock` instead, the same idea as
   the existing Docker-socket access (`_docker_api()`): socket file
   permissions (root:`wgctl`, mode 0660 on the socket, 0750 on its parent
   directory; the `helm` container joins `wgctl` via `group_add`, mirroring
   `DOCKER_GID`) are the access boundary, no token needed. The container
   bind-mounts the socket's *parent directory*, not the socket file itself
   — a single-file mount would pin the container to whatever inode existed
   at container start, so every restart of `wg_control_server.py` (which
   deletes and recreates its socket file) would otherwise leave the
   container looking at a dead socket until it was restarted too.
2. **A Docker container with `network_mode: host` + `NET_ADMIN`, controlled
   via `docker exec` over the already-mounted `docker.sock`, was considered
   and rejected.** Docker's exec API takes an arbitrary `Cmd: [...]` — that
   would mean anything reaching `helm_server.py`'s existing Docker-proxy
   code path gets arbitrary root-on-host-network command execution, which
   defeats the point of introducing a privilege boundary at all.
   `wg_control_server.py` exposes exactly two verbs (`GET /status`,
   `POST /circuit`) with fixed server-side logic instead — a compromise of
   `helm_server.py` can at most flip which circuit is active, never run an
   arbitrary command.

**Kill switch, by design, not by exception handling.** A custom routing
table (`windscribe`) always has exactly one occupant for its default
route — either `blackhole` or a specific circuit's interface — swapped
atomically with `ip route replace` (never an add/delete pair, which would
create a window with zero or two candidate routes; an earlier draft that
tried to out-rank a blackhole route via a higher route *metric* had this
backwards — metric 0, the default, already wins). The `ip rule` selecting
that table only exists while a circuit is meant to be active; removing it
(not leaving it pointed at a blackhole) is what lets wg0 traffic fall
through to the main table for "Direct" mode. `wg_api.py`'s `switch_to()`,
the watchdog's `fail_closed_now()`, and startup's `reconcile()` are all one
function used three ways, not three independently-written state machines —
see the module docstring and comments in `wg_api.py` for the full ordering
(blackhole first, then bring the new interface up, then confirm a real
handshake before ever un-blackholing). Every Windscribe circuit config
needs `Table = off` (or `wg-quick` installs its *own* full-tunnel policy
routing and hijacks the host's entire default route, not just the intended
slice — including the admin's own SSH session) and `PersistentKeepalive =
25` (or an idle-but-healthy tunnel's aging handshake timestamp
false-positives the watchdog's staleness check) — see `CONTAINER_SETUP.md`.

## Other conventions

- **Atomic writes.** Every place state is persisted to disk writes to a
  `<path>.tmp` file first, then `os.replace(tmp, path)` — this is atomic on
  POSIX and Windows and avoids a torn/corrupt file if the process dies
  mid-write. Follow this for any new persisted file (see
  `_write_state_to_disk`, `_maybe_write_backup` in `helm_server.py`).
- **Section headers in Python files** use a `# ── NAME ──────...` banner
  comment to delimit major regions of `helm_server.py` (VAULT / AUDIO PROXY,
  SYSTEM STATS, SERVICE MONITORING, LOG VIEWER, ...). Add new functionality
  under an existing banner if it fits, or add a new one rather than
  interleaving unrelated logic.
- **Comments explain *why*, not *what*.** The codebase leans heavily on
  comments that justify a non-obvious design decision or flag a subtle bug
  that was avoided (the UTC-vs-naive-datetime note in `_get_docker_logs`,
  the TDZ notes above, the frame-ancestors `'self'` note). Don't add
  comments that just restate the code; do add one when a future reader would
  otherwise "fix" something that's deliberately written that way.
- **Declarative service list.** `MONITORED_SERVICES` in `helm_server.py` is
  the single source of truth for what appears on the Services/Logs pages and
  what can be started/stopped. Adding a new monitored service means adding
  an entry there (systemd-user / systemd / docker / systemd-timer), not new
  branching logic in `gather_services_status`/`gather_logs`/`control_service`
  — those already dispatch generically on `type`.
- **Multi-device state sync is last-write-wins, deliberately.** `/api/state`
  PUT never rejects on version mismatch — it just overwrites and bumps the
  version counter. Don't reintroduce conflict rejection/409 handling; the
  design accepts short-lived divergence since clients pull every 5s and push
  within 600ms of a local change.
- **CORS proxy allowlist.** `handle_proxy`/`ALLOWED_SCHEMES` only permits
  `http://`/`https://` targets — keep new proxy-style endpoints similarly
  scheme-restricted rather than fetching arbitrary URLs unchecked.
- **Path-traversal guards on file-serving endpoints.** `/api/backups/<file>`
  rejects any filename containing `/` or `\` or not matching the expected
  prefix before touching the filesystem. Match this pattern for any new
  endpoint that maps a URL segment onto a file path.
- **Legacy naming residue — expect "marks", not just "helm".** The project
  predates its current name: the state file is `marks_state.json` (not
  `helm_state.json`, despite the README/docstrings saying otherwise), the
  localStorage key is `marks_v1`, and `/api/health` reports
  `"service": "marks-local-server"`. This is intentional inertia, not a bug —
  don't "fix" the naming without being asked, since it'd change the on-disk
  state filename and break existing deployments' persisted data.
- **No pip `requirements.txt`.** `vault_api.py`'s `zxcvbn` is the only
  optional third-party import anywhere in the backend, and it's guarded. If
  a task seems to need a new pip dependency, treat that as a signal to look
  for a stdlib or shell-out alternative first.
