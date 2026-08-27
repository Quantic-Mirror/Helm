---
name: iframe-embed-proxy
description: "Embed a non-cooperative web app inside an iframe by stripping its framing-prevention headers at the TLS reverse proxy layer."
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [iframe, embedding, reverse-proxy, tls, CSP, x-frame-options, cookies, SameSite, iframe-embedding]
    related_skills: [hermes-agent, computer-use]
---

# Iframing a non-cooperative web app behind a TLS reverse proxy

## Trigger

Use this skill when you need to embed a third-party (or internal) web application inside an `<iframe>` on another dashboard, and that embedded app sends headers that forbid framing: `X-Frame-Options: DENY` | `SAMEORIGIN`, or a CSP `frame-ancestors 'none'`.

This is the standard fix for the recurring pair of embedding failures:
1. The iframe renders blank because the browser blocks framing.
2. Login succeeds server-side but the browser drops the session cookie, so the app looks logged-out.

Treat the embedded app's security posture as immutable — **do not** edit the app's config to remove its headers. Strip/relax the headers at the proxy layer instead.

## The two failure modes (why the naive iframe never works)

1. **Framing is blocked at the policy layer.** The target app ships `X-Frame-Options: DENY` (or `SAMEORIGIN` when you are cross-origin) and/or `Content-Security-Policy: frame-ancestors 'none'`. The browser honors these *before* any of your iframe HTML is rendered.

2. **Session cookies are silently discarded.** Even when framing is allowed, browsers require `SameSite=None; Secure` on cookies used inside a cross-origin iframe. Most apps set `SameSite=Lax` | `Strict` or omit `Secure`, so login *succeeds server-side* but the browser discards the session cookie immediately after — every subsequent request looks logged-out.

Both must be addressed at the proxy layer. Fixing only #1 yields a blank page; fixing only #2 yields a login loop.

## The single established workaround

Run a thin TLS-terminating reverse proxy in front of the target app and rewrite both classes of headers on **every response that carries them**:

- **Strip `X-Frame-Options` entirely.** Do *not* replace it with `SAMEORIGIN` when you are cross-origin — that keeps blocking you. Stripping is correct when the *proxy* is the embedding origin.
- **Rewrite the CSP:** remove any `frame-ancestors 'none'` directive, or relax it to `'self' <allowed-origins>`. Keep `'self'` if the app internally frames its own content (e.g. kiwix-serve's viewer).
- **On every `Set-Cookie` header:** strip any existing `SameSite=` and `Secure` attributes the backend emitted, then re-add `SameSite=None; Secure` unconditionally. This must be applied to **every** response, not just the login response — session-refresh cookies mid-stream are also dropped otherwise.

A reference implementation of this exact pattern is Helm's `helm_tls_proxy.py`, with `_rewrite_framing_headers` + `_rewrite_cookie_for_iframe` applied uniformly in `_send_headers`. The pattern is deliberately "the same ssl + http.server template already used by the main server" — reuse that template rather than reaching for nginx/Caddy.

### Checklist for a new iframed service

1. Identify the target app's origin (scheme+host+port), e.g. `http://localhost:8290` or `https://popcorn:9002`.
2. Confirm the transport the app uses for dynamic updates:
   - **HTTP/SSE (`text/event-stream`) or long-polling** → the existing proxy handles it. Verify and proceed.
   - **Raw WebSocket upgrade** → *not* supported by `helm_tls_proxy.py`. Flag it (per the repo convention "flag it rather than guessing silently") — do not fake it. Look for `ws:`/`wss:` or `Upgrade: websocket` in the JS, or a `WebSocket(` constructor call.
3. Decide whether the target needs auth. If the embedded app has no password and lives on a trusted LAN, keep auth off — no cookies to rewrite, no login redirect, trivial embed. If auth is on, rely on the cookie rewriting described above.
4. Point a new proxy instance at the target (env-overridable origin so the upstream's hostname can change).
5. Serve the iframe from the proxy on the dashboard page:
   ```html
   <div id="my-app-page" style="display:none">
     <iframe id="my-app-frame" src="https://<proxy-host>:<proxy-port>/" allow="clipboard-write; microphone; camera"></iframe>
   </div>
   ```
6. Match the iframe sizing to the dashboard's existing embedded-app layout (typically `width:100%; height:calc(100vh - <nav-offset>); border:none; display:block`).

## Pitfalls

- **WebSocket is explicitly unsupported by the generic proxy.** Do not silently pretend you rewrote it. If the app needs WS, either switch it to SSE/long-polling at the app layer, or embed it directly on its own TLS origin (not via the proxy). The ttyd player in Helm does exactly this (its own TLS on `hyperion:8443`, iframe `src` set to that origin directly, because the proxy does not do WS).
- **`X-Frame-Options: SAMEORIGIN` only blocks the cross-origin case.** Don't "fix" a blank cross-origin iframe by setting `SAMEORIGIN` — that keeps blocking you. Strip the header outright.
- **CSP `frame-ancestors` is enforced by the browser before fetching the iframe content**, so stripping it on the proxy response is correct. (This differs from `frame-src`, which governs *what the page itself* may embed.)
- **CSP `sandbox` with `allow-scripts` but no `allow-same-origin` blocks cookie-setting.** If the embedded app can't read/write its session cookie, add that token.
- **`Secure` must go on every Set-Cookie when you set `SameSite=None`**, else browsers reject the cookie entirely. Rewriting only the login response is not enough if the app rotates the cookie mid-stream.
- **Do not touch the target app's own security config.** Its `X-Frame-Options`/`frame-ancestors` is deliberate defense against clickjacking for *its* direct users; stripping it at the proxy does not weaken that protection.
- **Verify `.gitignore` covers the proxy's own secret/config files** (`.env`, token files) rather than assuming — repos ship accidentally-committed secrets this way.

## Verification

- Network tab: the iframe document response has **no** `X-Frame-Options` header and CSP contains **no** `frame-ancestors 'none'`.
- Network tab: `Set-Cookie` on the login response carries `SameSite=None; Secure`, and subsequent responses don't *lose* the cookie.
- The iframe renders the app's full UI (not a blank box) and the app considers the user logged in across navigation within the iframe.
- If the app uses SSE, the `EventSource` stream connects and receives events; if it uses WS, confirm the proxy either supports it or is bypassed for that transport.

## References

- `references/helm-proxy-pattern.md` — excerpted notes from Helm's CLAUDE.md on the documented `helm_tls_proxy.py` header/cookie rewriting pattern.
