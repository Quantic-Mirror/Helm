# Container Setup Guide for Helm (Host-Agnostic)

## Overview

The Helm dashboard server runs in a container on **any host** that has Docker
installed. The cross-platform-yet-not-containerizable pieces stay running
natively on a separate host:

| Component | Where it runs | Why not containerized |
|---|---|---|
| `helm_server.py` | **Container (any host)** | Core server, stdlib-only, perfectly containerizes |
| SearXNG | **Container (any host)** | Powers the Search widget |
| **Password vault** (`vault_server.py` + `pass` + gpg) | **Native on a separate host** | `pass` and gpg are Linux-only; the pass store lives on a USB drive |
| **Audio grabber** (`audio_grabber_server.py` + yt-dlp) | **Native on a separate host** | Depends on yt-dlp + browser cookies in `~/.local/bin` |
| **Music / Wiki / Hermes tabs** | **Removed** | Music needed MPD + ncmpcpp + ttyd over WebSocket (unsupported by the proxy); Wiki.js and Hermes are no longer iframed. |

> **Note:** The Music, Wiki, and Hermes tabs have been removed from Helm.
> Helm no longer iframes any external app, so there is no `helm_tls_proxy.py`
> service in the compose stack. For music playback, use a dedicated MPD client
> (e.g. `rmpc` or `ncmpcpp` on Linux, `Stylophone` on Windows) against the MPD
> daemon on whichever host it runs on. See
> [WSL2_VAULT_SETUP.md](./WSL2_VAULT_SETUP.md) for the vault dual-boot setup.

## Tailscale-only deployment pattern

When the container host is a VPS and you access it over Tailscale (single-user
private access), you don't need Let's Encrypt certs — a self-signed cert
with `CN=<Tailscale-IP>` works because Tailscale encrypts in transit and
browsers will accept one-time cert warnings.

Key `.env` settings for this mode:
- `SERVER_HOST=<Tailscale-IP>` — the VPS's Tailscale address
- `VAULT_HOST=<hyperion-Tailscale-IP>` — the vault host's Tailscale address
- `AUDIO_HOST=<hyperion-Tailscale-IP>` — the audio host's Tailscale address
- `SEARXNG_HOST=localhost` — SearXNG runs as a sibling container

Ports bind to `127.0.0.1` only (not `0.0.0.0`), so the stack isn't directly
exposed to the internet. Access is via `https://<Tailscale-IP>:8443`.

How this works without hardcoded hostnames: `helm_server.py` exposes an
`/api/config` endpoint that returns `SERVER_HOST` / `SEARXNG_URL` (etc.) from
its environment, and `index.html`'s `initConfig()` fetches it at startup to
build the SearXNG search URL. Port bindings are driven by the
`HELM_HTTP_PORT` / `HELM_HTTPS_PORT` / `SEARXNG_PORT` variables in `.env`
(host-IP-prefixed, e.g. `127.0.0.1:8080:8080` for Tailscale-only).

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
   #   SERVER_HOST       — your Tailscale IP (from `tailscale ip`)
   #   VAULT_HOST        — Tailscale IP of the host running vault_server.py
   #   AUDIO_HOST        — Tailscale IP of the host running audio_grabber_server.py
   #   SEARXNG_HOST      — leave as "localhost" (runs in the container)
   #   HELM_UID/GID      — find with: id -u && id -g
   #   DOCKER_GID        — find with: getent group docker | cut -d: -f3
   ```

5. **Set up the data directory (optional — it's created empty on first run):**
   ```bash
   mkdir -p data
   ```
   The code is baked into the image, so nothing needs copying here.
   `data/` is bind-mounted to `/app/state`; `marks_state.json` and
   `helm-backups/` are created automatically. Add these only if you use the
   corresponding feature:
   ```bash
   # Bring your existing bookmarks/state across (optional):
   cp /path/to/marks_state.json data/

   # Shared secrets for the vault / audio hosts (only if those are deployed):
   cp /path/to/vault_token.txt data/
   cp /path/to/audio_token.txt data/

   # Self-signed TLS cert for HTTPS on :8443 (optional):
   openssl req -x509 -newkey rsa:2048 -nodes \
     -keyout data/key.pem -out data/cert.pem -days 3650 \
     -subj "/CN=$(grep SERVER_HOST .env | cut -d= -f2)"
   ```

6. **Start the stack:**
   ```bash
   docker compose up -d
   ```

7. **Verify:**
   - Dashboard: `https://<Tailscale-IP>:8443` (import the self-signed cert once)
   - SearXNG: accessible via the Dashboard's Search widget (proxied through /api/config)

8. **Set up the vault on the separate host:** see `WSL2_VAULT_SETUP.md`.

## What runs on the separate host (setup once, per OS)

These are **not** in the docker-compose stack. They run natively on a separate
host and are reached over HTTP (via Tailscale) from the Helm container.

### Password vault (`vault_server.py`)

The vault uses `pass` + gpg. See [`WSL2_VAULT_SETUP.md`](./WSL2_VAULT_SETUP.md)
for the full WSL2 setup. In short:

- **Linux:** `python3 vault_server.py 8090` (natively)
- **Windows:** `vault_server.py` runs in WSL2, with the pass store on a
  USB drive mounted at `/mnt/usb/`.

Either way, `VAULT_BACKEND_URL=http://<VAULT_HOST_TAILSCALE_IP>:8090` in the
Helm container's `.env` points to it. The Helm container proxies `/api/vault/*`.

### Audio grabber (`audio_grabber_server.py`)

Same pattern — run on the separate host wherever yt-dlp + your cookies live:

```bash
python3 /path/to/helm/audio_grabber_server.py 8091
```

`AUDIO_BACKEND_URL=http://<AUDIO_HOST_TAILSCALE_IP>:8091` in the Helm container
handles the proxy.

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

## The systemd-user monitoring caveat

`MONITORED_SERVICES` in `helm_server.py` may include `systemd-user` entries
for services running on the container host. Inside the container, these resolve
via the mounted systemd D-Bus socket (`/run/user/${UID}/systemd/private`).

If you're monitoring a service that runs differently inside vs. outside the
container, you may need to adjust the entry type (`systemd-user` → `docker`).

## Public/LAN deployment (alternative to Tailscale)

If you prefer to expose Helm publicly instead of over Tailscale:

1. Set `SERVE_VIA_TAILSCALE=false` in `.env`
2. Uncomment the port override lines to bind to `0.0.0.0`
3. Use Let's Encrypt certs (via nginx/caddy reverse proxy)
4. Point `SERVER_HOST` to your public domain
5. **Add authentication** — Helm has no built-in auth; protect `/api/*`
   behind nginx basic auth or similar

See the commented-out section in `.env.example` for the exact settings.
