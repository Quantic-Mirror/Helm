# Helm

Self-hosted personal dashboard. Single-file frontend (`index.html`) served by a
stdlib-only Python backend (`helm_server.py`), plus a few small standalone
Python helper services. No build step, no framework, no npm/pip dependency
chain for the core app.

> This file is tracked in git (the repo is public). Do not put secrets here —
> tokens and keys live in `STATE_DIR` (`*_token.txt`, `cert.pem`/`key.pem`),
> which stay gitignored.

## Topology

- **popcorn** — the host `helm_server.py` runs on; serves the dashboard itself.
- **hyperion** — separate host where `pass` + gpg-agent live; `vault_server.py`
  runs there and `helm_server.py` proxies `/api/vault/*` to it over HTTP with a
  shared-secret token (`vault_token.txt`, copied to both machines).
- User on the host is `carl` (see docker-group comments, IRC log paths).
- Don't assume everything runs on one machine — if you're about to shell out
  to something host-specific (`pass`, gpg, a systemd unit), check whether it's
  actually meant to run on popcorn or gets proxied to hyperion instead.
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
  IRC alerts read The Lounge's own SQLite log file read-only
  (`sqlite3.connect(..., mode=ro)`) rather than running a bot or reaching
  into the IRC iframe. The password vault shells out to `pass` rather than
  reimplementing GPG handling — "so it inherits gpg-agent's cache/timeout/
  lock behavior automatically" (vault_api.py:3-4).
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

## Other conventions

- **Atomic writes.** Every place state is persisted to disk writes to a
  `<path>.tmp` file first, then `os.replace(tmp, path)` — this is atomic on
  POSIX and Windows and avoids a torn/corrupt file if the process dies
  mid-write. Follow this for any new persisted file (see
  `_write_state_to_disk`, `_maybe_write_backup`, `_irc_save_ack` in
  `helm_server.py`).
- **Section headers in Python files** use a `# ── NAME ──────...` banner
  comment to delimit major regions of `helm_server.py` (VAULT / AUDIO PROXY, IRC
  ALERTS, SYSTEM STATS, SERVICE MONITORING, LOG VIEWER, ...). Add new
  functionality under an existing banner if it fits, or add a new one rather
  than interleaving unrelated logic.
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
