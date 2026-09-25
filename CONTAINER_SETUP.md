# Container Setup Guide for Helm (Host-Agnostic)

## Overview

The Helm dashboard server runs in a container on **any host** that has Docker
installed. The cross-platform-yet-not-containerizable pieces stay running
natively on a separate host:

| Component | Where it runs | Why not containerized |
|---|---|---|
| `helm_server.py` | **Container (any host)** | Core server, stdlib-only, perfectly containerizes |
| SearXNG | **Container (any host)** | Powers the Search widget |
| LeafWiki (`leafwiki` + `leafwiki-proxy`) | **Container (any host)** | Markdown wiki for the Wiki tab; iframed cross-origin through `leafwiki-proxy` (`helm_tls_proxy.py`), which strips its framing headers and rewrites its session cookie — see CLAUDE.md "helm_tls_proxy.py cookie-rewriting pattern" |
| DailyTxT (`dailytxt` + `dailytxt-proxy`) | **Container (any host)** | End-to-end-encrypted diary for the Journal tab; iframed cross-origin through `dailytxt-proxy` (`helm_tls_proxy.py`), same cookie/framing rewrite as LeafWiki above |
| **Password vault** (`vault_server.py` + `pass` + gpg) | **Native on a separate host** | `pass` and gpg are Linux-only; the pass store is a git clone of a private repo |
| **Audio grabber** (`audio_grabber_server.py` + yt-dlp) | **Native on a separate host** | Depends on yt-dlp + browser cookies in `~/.local/bin` |
| **Music / Hermes tabs** (the old MPD-backed player, not musicXplorer) | **Removed** | Music needed MPD + ncmpcpp + ttyd over WebSocket (unsupported by the proxy); Hermes was removed and hasn't been re-added. |

> **Note:** The old MPD-backed Music tab (playback via ncmpcpp/ttyd) and the
> Hermes tab have been removed from Helm — for music playback, use a
> dedicated MPD client (e.g. `rmpc` or `ncmpcpp` on Linux, `Stylophone` on
> Windows) against the MPD daemon on whichever host it runs on. The Wiki tab
> has cycled through Memos → TriliumNext → SilverBullet → (now) **LeafWiki**;
> each iframed via a `helm_tls_proxy.py` proxy, same pattern the original
> (removed) Wiki.js tab used. LeafWiki's pages are plain Markdown files on
> disk (`./leafwiki-data`), and its admin account is set deterministically
> via `LEAFWIKI_JWT_SECRET`/`LEAFWIKI_ADMIN_PASSWORD` in `.env` — no browser
> setup wizard, unlike TriliumNext's earlier broken flow. The Journal tab
> similarly replaces an earlier native client-side-Web-Crypto implementation
> (removed — see git history) with **DailyTxT**, an encrypted-diary app that
> handles the crypto itself; its admin account is set the same way, via
> `DAILYTXT_SECRET_TOKEN`/`DAILYTXT_ADMIN_PASSWORD` in `.env`. See
> [WSL2_VAULT_SETUP.md](./WSL2_VAULT_SETUP.md) for the vault dual-boot setup.
>
> This is unrelated to the newer **musicXplorer** tab, which isn't a player at
> all — it's a browser for tracks favorited off the SomaFM widget (YouTube
> embed + official site/Wikipedia links + Last.fm related artists). See the
> `lastfm_api_key.txt` step below to enable its related-artist lookup.

## Tailscale-only deployment pattern

When the container host is a VPS and you access it over Tailscale (single-user
private access), you don't need Let's Encrypt certs — a self-signed cert
with `CN=<Tailscale-IP>` works because Tailscale encrypts in transit and
browsers will accept one-time cert warnings.

Key `.env` settings for this mode:
- `HELM_BIND=<Tailscale-IP>` — publish the container ports on the tailnet
  interface only, so the stack isn't exposed to the internet
- `SERVER_HOST=<Tailscale-IP>` — same value; feeds `/api/config` so the
  frontend builds correct URLs
- `VAULT_HOSTS=<ip>[,<ip>…]` — the vault host's Tailscale address; on a dual-boot
  workstation list every OS's IP, comma-separated (helm_server fails over)
- `AUDIO_HOSTS=<ip>[,<ip>…]` — same, for the audio host
- `SEARXNG_HOST=<Tailscale-IP>` — same value as `SERVER_HOST`. SearXNG runs as
  a sibling container, but the **browser** connects to it directly (the Search
  widget opens this address in a new tab), so it must be an address the browser
  can reach. `localhost` only works when you browse from the container host.

Access is via `https://<Tailscale-IP>:8443`.

How this works without hardcoded hostnames: `helm_server.py` exposes an
`/api/config` endpoint that returns `SERVER_HOST` / `SEARXNG_URL` (etc.) from
its environment, and `index.html`'s `initConfig()` fetches it at startup to
build the SearXNG search URL. The port bind interface comes from `HELM_BIND`
(default `127.0.0.1`); `HELM_PORT` / `SEARXNG_PORT`
override the whole `ip:hostport:containerport` mapping if you also need a
different host port.

All hostnames in the docker-compose stack are configurable via `.env`
variables — no hardcoding. Adapt this setup to any host.

## Prerequisites on the container host

1. **Install Docker + Compose:**
   ```bash
   curl -fsSL https://get.docker.com | sh
   sudo usermod -aG docker $USER   # log out/in after this
   docker compose version         # should work after Docker install
   ```

2. **Clone the repo:**
   ```bash
   git clone git@github.com:quantic-mirror/helm.git /opt/helm
   cd /opt/helm
   ```

3. **Set up Tailscale (recommended for single-user access):**
   ```bash
   curl -fsSL https://tailscale.com/install.sh | sh
   sudo tailscale up
   tailscale ip   # note this IP (e.g. 100.x.x.x)
   ```

4. **Create `.env`:**
   ```bash
   cp .env.example .env
   # Edit .env:
   #   HELM_BIND               — your Tailscale IP (tailnet-only), or 0.0.0.0 for public
   #   SERVER_HOST             — same Tailscale IP (from `tailscale ip -4`)
   #   VAULT_HOSTS             — Tailscale IP(s) of the host running vault_server.py,
   #                             comma-separated for a dual-boot workstation
   #   AUDIO_HOSTS             — likewise for audio_grabber_server.py
   #   SEARXNG_HOST            — same as SERVER_HOST (the browser hits SearXNG directly;
   #                             "localhost" only works when browsing from the host)
   #   HELM_UID/GID            — find with: id -u && id -g
   #   DOCKER_GID              — find with: getent group docker | cut -d: -f3
   #   LEAFWIKI_JWT_SECRET     — required, no default: openssl rand -hex 32
   #   LEAFWIKI_ADMIN_PASSWORD — required, no default; the leafwiki container
   #                             refuses to start without both
   #   DAILYTXT_SECRET_TOKEN   — required, no default: openssl rand -base64 32
   #   DAILYTXT_ADMIN_PASSWORD — required, no default (gates DailyTxT's admin
   #                             panel only, not a diary login — see .env.example)
   ```

5. **Set up the data directory (optional — it's created empty on first run):**
   ```bash
   mkdir -p data
   ```
   The code is baked into the image, so nothing needs copying here.
   `data/` is bind-mounted to `/app/state`; `marks_state.json` and
   `helm-backups/` are created automatically. `leafwiki-data/`,
   `dailytxt-data/`, `searxng-settings/`, and `searxng-data/` are likewise
   created empty on first run for their respective containers — nothing to
   pre-populate there either. Add these only if you use the corresponding
   feature:
   ```bash
   # Bring your existing bookmarks/state across (optional):
   cp /path/to/marks_state.json data/

   # Shared secrets for the vault / audio hosts (only if those are deployed):
   cp /path/to/vault_token.txt data/
   cp /path/to/audio_token.txt data/

   # API access token, gating every /api/* route (recommended for any
   # deployment reachable beyond 127.0.0.1 — see the Public/LAN section below):
   openssl rand -hex 32 > data/helm_token.txt

   # Last.fm API key for musicXplorer's related-artist lookup (optional —
   # without it, that one panel shows a "not configured" message and
   # everything else on the tab still works). Get a free key at
   # https://www.last.fm/api/account/create — the "shared secret" it also
   # gives you is only for signed/session calls (scrobbling, user auth);
   # this feature only calls the public artist.getsimilar method, so just
   # the API key is needed, not the secret:
   echo "YOUR_LASTFM_API_KEY" > data/lastfm_api_key.txt

   # Self-signed TLS cert for HTTPS on :8443 (optional):
   openssl req -x509 -newkey rsa:2048 -nodes \
     -keyout data/key.pem -out data/cert.pem -days 3650 \
     -subj "/CN=$(grep SERVER_HOST .env | cut -d= -f2)"
   ```

6. **Start the stack:**
   ```bash
   docker compose up -d
   docker compose ps   # helm, leafwiki, leafwiki-proxy, dailytxt, dailytxt-proxy, searxng — all Up
   ```

7. **DailyTxT first boot — register your diary account:**
   DailyTxT has no default account. Set `DAILYTXT_ALLOW_REGISTRATION=true` in
   `.env`, `docker compose up -d dailytxt` to apply it, open the Journal tab
   and register through DailyTxT's own login page, then set
   `DAILYTXT_ALLOW_REGISTRATION=false` (or remove the line) and restart the
   `dailytxt` container again. `DAILYTXT_ADMIN_PASSWORD` only gates DailyTxT's
   separate admin/user-management panel, not this login.

8. **Verify:**
   - Dashboard: `https://<Tailscale-IP>:8443` (import the self-signed cert once)
   - SearXNG: accessible via the Dashboard's Search widget (proxied through /api/config)
   - Wiki tab: LeafWiki loads and its admin account logs in with `LEAFWIKI_ADMIN_PASSWORD`
   - Journal tab: DailyTxT loads and the account registered in step 7 logs in

9. **Set up the vault on the separate host:** see `WSL2_VAULT_SETUP.md`.

## LeafWiki git content backup (optional)

LeafWiki has its own built-in **Git Backup** feature (experimental as of
v0.11.3): on a configurable interval it commits `root/` (pages) and `assets/`
— not the SQLite database — and pushes to a remote Git repo over SSH or
HTTP(S). This is the recommended way to get Wiki-tab content off the VPS;
without it, `leafwiki-data/` has no off-site copy (the R2 mirror pipeline in
`BACKUP-PIPELINE-GUIDE.md` doesn't cover it — see that file's note). One-time
setup, SSH variant:

1. **Create a dedicated private repo** for the backup content, e.g.
   `youruser/leafwiki-backup`.

2. **Generate a dedicated deploy key** (don't reuse an account-wide key —
   this one only needs write access to the single backup repo):
   ```bash
   ssh-keygen -t ed25519 -N '' -f ./leafwiki_backup_ed25519 -C leafwiki-git-backup
   ```
   Add `leafwiki_backup_ed25519.pub` to the backup repo's **Settings ▸ Deploy
   keys ▸ Add deploy key**, with **Allow write access** checked.

3. **Drop the private key and a known_hosts file into `./leafwiki-data/`**
   on the container host (bind-mounted to `/app/data`, gitignored — same
   pattern as `data/*_token.txt`):
   ```bash
   mv ./leafwiki_backup_ed25519 ./leafwiki-data/leafwiki_backup_ed25519
   chmod 600 ./leafwiki-data/leafwiki_backup_ed25519
   ssh-keyscan -t ed25519,rsa,ecdsa github.com > ./leafwiki-data/leafwiki_known_hosts
   ```
   (The `leafwiki` container has no `USER` in its image — it runs as root and
   its `/app/data` is world-writable — so `chmod 600` is enough; no UID
   matching needed, unlike `HELM_UID`/`HELM_GID` for the `helm` service.)

4. **Set in `.env`** (see the commented-out block there for the full list):
   ```bash
   LEAFWIKI_GIT_BACKUP=true
   LEAFWIKI_GIT_BACKUP_REMOTE=git@github.com:youruser/leafwiki-backup.git
   LEAFWIKI_GIT_BACKUP_BRANCH=main
   LEAFWIKI_GIT_BACKUP_SSH_KEY_PATH=/app/data/leafwiki_backup_ed25519
   LEAFWIKI_GIT_BACKUP_SSH_KNOWN_HOSTS=/app/data/leafwiki_known_hosts
   LEAFWIKI_GIT_BACKUP_INTERVAL=60m
   ```
   Skipping `LEAFWIKI_GIT_BACKUP_SSH_KNOWN_HOSTS` doesn't fail startup, but
   LeafWiki then disables SSH host-key verification (no `~/.ssh/known_hosts`
   exists in the container) — set it explicitly.

5. **Apply and verify:**
   ```bash
   docker compose up -d leafwiki
   docker compose logs leafwiki --tail 50   # no "Conflict — remote diverged" warning
   ```
   Or trigger one manually from LeafWiki's **Git Content Backup** admin page,
   then check the backup repo on GitHub for a new commit.

For an HTTPS + fine-grained-PAT variant instead of SSH, or the `--git-backup-path`
option (nesting the backup in a monorepo subdirectory), see the "Git Backup"
section of the [LeafWiki README](https://github.com/perber/leafwiki#git-backup-v0113-experimental).

## What runs on the separate host (setup once, per OS)

These are **not** in the docker-compose stack. They run natively on a separate
host and are reached over HTTP (via Tailscale) from the Helm container.

### Password vault (`vault_server.py`)

The vault uses `pass` + gpg. See [`WSL2_VAULT_SETUP.md`](./WSL2_VAULT_SETUP.md)
for the full WSL2 setup. In short:

- **Linux:** `python3 vault_server.py 8090` (natively)
- **Windows:** `vault_server.py` runs in WSL2, with the pass store cloned
  from its private git remote into WSL2-native storage (`~/.password-store`).

Either way, `VAULT_HOSTS=<tailscale-ip>[,<tailscale-ip>…]` in the Helm
container's `.env` points to it (list every OS's IP on a dual-boot workstation;
helm_server tries each and sticks to whichever answers). The Helm container
proxies `/api/vault/*`.

### Audio grabber (`audio_grabber_server.py`)

Same pattern — run on the separate host wherever yt-dlp + your cookies live:

```bash
python3 /path/to/helm/audio_grabber_server.py 8091
```

`AUDIO_HOSTS=<tailscale-ip>[,<tailscale-ip>…]` in the Helm container handles the
proxy (same comma-separated failover as `VAULT_HOSTS`).

### Backup pipeline events (`emit_event.py`)

The Backup Pipeline tab is fed by the external backup scripts calling
`emit_event.py`, which POSTs each event to the Helm container's token-authed
`POST /api/backup-events`. On every host that runs those scripts:

```bash
openssl rand -hex 32 > data/backup_token.txt          # on the Helm host, once
# copy that exact file to /etc/helm/backup_token.txt on each backup-script host
```

Then set for the scripts (or their systemd units):

```bash
HELM_URL=https://<HELM_TAILSCALE_IP>:8443
HELM_BACKUP_TOKEN_FILE=/etc/helm/backup_token.txt
HELM_CA_FILE=/etc/helm/helm-ca.crt      # or HELM_TLS_INSECURE=1 on a Tailscale link
```

There is no broker: an event emitted while the Helm host is unreachable is
retried for ~3 minutes and then dropped. The backup itself is unaffected —
`emit_event.py` failures are non-fatal to the calling script.

## The Services / Logs tabs

`MONITORED_SERVICES` in `helm_server.py` ships **docker-only** — the `helm` and
`searxng-core` containers, watched through the mounted Docker socket. The
Services tab is a container-health panel and the Logs tab shows `docker logs`
for those containers. The Logs filter dropdown is generated from
`/api/services`, so adding an entry to `MONITORED_SERVICES` is all it takes.

To also watch a **host** `systemd --user` unit, add a `systemd-user` (or
`systemd` / `systemd-timer`) entry — the generic dispatch still supports it —
and uncomment the `/run/user/${UID}/...` socket mounts in `docker-compose.yml`.
That needs a running user session + journal on the host
(`loginctl enable-linger <user>`); without it `docker compose up` fails on the
missing socket path, which is why those mounts ship commented out.

## Public/LAN deployment (alternative to Tailscale)

If you prefer to expose Helm publicly instead of over Tailscale:

1. Set `HELM_BIND=0.0.0.0` in `.env` (or `HELM_PORT=0.0.0.0:443:8443` to also
   move the host port)
2. Use Let's Encrypt certs (via nginx/caddy reverse proxy)
3. Point `SERVER_HOST` to your public domain
4. **Set `data/helm_token.txt`** (`openssl rand -hex 32 > data/helm_token.txt`)
   — with this file present, every `/api/*` route requires
   `Authorization: Bearer <token>` (except `GET /api/health` and
   `POST /api/backup-events`, which has its own `X-Backup-Token`). Enter the
   token once per device/browser via the Data ▾ menu's 🔑 Access Token item. With no
   such file, auth is disabled (fail-open) — strongly recommended to set this
   for any deployment reachable beyond `127.0.0.1`.

See the commented-out section in `.env.example` for the exact settings.
