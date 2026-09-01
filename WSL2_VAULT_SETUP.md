# WSL2 Vault + Audio Backend Setup

The Windows side of the dual-boot workstation. When the machine is booted into
Windows, `vault_server.py` (port 8090) and `audio_grabber_server.py` (port 8091)
run inside WSL2 and are reached from the Helm container over the network — the
same role they fill natively on the Linux boot.

**No USB drive, no mounting the Windows filesystem into WSL2.** The pass store is
cloned from your private Git remote into WSL2-native `ext4`. `/mnt/c/...` in WSL2
is `drvfs` — slow for a tree of many small `.gpg` files and lock-prone for git —
and if the Linux boot keeps its store on a partition shared with Windows,
pointing WSL2 at that same `.git` makes two machines write one repo. A per-OS
clone avoids both; `gopass sync` reconciles the two copies through the remote.

## Prerequisites

- WSL2 with a distro: `wsl --install -d Ubuntu` (reboot if prompted).
- Tailscale on the **Windows host**, signed in. Accept any subnet route your
  deployment relies on (tray icon → Preferences).
- Your GPG **secret** key, exported on the Linux box and copied here over the
  tailnet/LAN — treat the file as a live credential:
  ```bash
  # on the Linux box:
  gpg --export-secret-keys --armor <keyid> > ~/gpgkey.asc && chmod 600 ~/gpgkey.asc
  scp ~/gpgkey.asc <user>@<this-machine>:~/gpgkey.asc
  ```
- Your private **pass-store repo** reachable over SSH from this machine (an
  account SSH key, or a read-only deploy key).
- The `vault_token.txt` / `audio_token.txt` strings from the Linux box — they
  must match **byte-for-byte** on every host (they are the shared secret the
  Helm container authenticates with).

## Step 1 — packages + GPG (in WSL2)

```bash
sudo apt update && sudo apt install -y pass gopass gnupg python3 python3-pip git
pip install --user yt-dlp

gpg --import ~/gpgkey.asc
gpg --list-secret-keys                 # verify the key is present
shred -u ~/gpgkey.asc                  # and shred the copy on the Linux box too

export GPG_TTY=$(tty)                  # so gpg can prompt for the passphrase in WSL2
echo 'pinentry-program /usr/bin/pinentry' > ~/.gnupg/gpg-agent.conf
gpg-connect-agent reloadagent /bye
```

Add `export GPG_TTY=$(tty)` to `~/.bashrc` so new shells keep it.

> Use the `/usr/bin/pinentry` **dispatcher**, not a hardcoded backend. It shows a
> GUI prompt when a display is available (WSLg) and falls back to a terminal
> prompt otherwise. Hardcoding `pinentry-curses` breaks any caller with no
> controlling TTY (e.g. a browser extension) with
> `gpg: decryption failed: Inappropriate ioctl for device`.

## Step 2 — clone the pass store into WSL2-native storage

```bash
export PASSWORD_STORE_DIR=$HOME/.password-store
gopass clone git@github.com:<owner>/<pass-repo>.git "$PASSWORD_STORE_DIR"
gopass config core.autopush true
gopass config core.autoimport true
pass ls                                # pass and gopass share this directory
```

Put `export PASSWORD_STORE_DIR=$HOME/.password-store` in `~/.bashrc` too — both
backends read the store location from the environment.

## Step 3 — Helm checkout + tokens

```bash
git clone https://github.com/<owner>/Helm.git ~/helm    # a read-only clone is fine
printf '%s' 'PASTE_vault_token_from_linux' > ~/helm/vault_token.txt
printf '%s' 'PASTE_audio_token_from_linux' > ~/helm/audio_token.txt
```

## Step 4 — startup script

`~/start-helm-backends.sh`:

```bash
#!/bin/bash
# Starts both Helm backends in WSL2. gopass sync pulls the latest pass-store
# state from the remote before serving, so a password added on the Linux boot
# (or from the dashboard) is visible here.
export PASSWORD_STORE_DIR=$HOME/.password-store
gopass sync >/tmp/gopass-sync.log 2>&1 || true
pkill -f 'vault_server.py 8090'         2>/dev/null
pkill -f 'audio_grabber_server.py 8091' 2>/dev/null
nohup python3 "$HOME/helm/vault_server.py"         8090 >/tmp/vault.log 2>&1 &
nohup python3 "$HOME/helm/audio_grabber_server.py" 8091 >/tmp/audio.log 2>&1 &
```

```bash
chmod +x ~/start-helm-backends.sh
~/start-helm-backends.sh
curl -s -H "X-Vault-Token: $(cat ~/helm/vault_token.txt)" localhost:8090/api/vault/status
curl -s -H "X-Audio-Token: $(cat ~/helm/audio_token.txt)" localhost:8091/api/audio/jobs
```

## Step 5 — expose the ports to the LAN/tailnet (admin PowerShell)

A WSL2 listener is not reachable from other hosts by default, and the WSL2 VM
IP changes on every boot. Save as `C:\Users\<WIN_USER>\helm-portproxy.ps1`:

```powershell
$wsl = (wsl -d Ubuntu hostname -I).Trim().Split(' ')[0]
netsh interface portproxy delete v4tov4 listenport=8090 listenaddress=0.0.0.0 2>$null
netsh interface portproxy delete v4tov4 listenport=8091 listenaddress=0.0.0.0 2>$null
netsh interface portproxy add v4tov4 listenaddress=0.0.0.0 listenport=8090 connectaddress=$wsl connectport=8090
netsh interface portproxy add v4tov4 listenaddress=0.0.0.0 listenport=8091 connectaddress=$wsl connectport=8091
New-NetFirewallRule -DisplayName "Helm vault 8090" -Direction Inbound -LocalPort 8090 -Protocol TCP -Action Allow -ErrorAction SilentlyContinue
New-NetFirewallRule -DisplayName "Helm audio 8091" -Direction Inbound -LocalPort 8091 -Protocol TCP -Action Allow -ErrorAction SilentlyContinue
```

Run it once now. Step 6 re-runs it at each logon (because of the IP churn).

## Step 6 — autostart at logon (Task Scheduler)

Task Scheduler → **Create Task** (not *Create Basic Task*):

- **General:** "Run whether user is logged on or not".
- **Triggers:** At log on.
- **Actions** (add two):
  1. Program `C:\Windows\System32\wsl.exe`
     — arguments `-d Ubuntu -e /home/<user>/start-helm-backends.sh`
  2. Program `powershell.exe`
     — arguments `-WindowStyle Hidden -File C:\Users\<WIN_USER>\helm-portproxy.ps1`

## Step 7 — verify from the Helm container host

```bash
curl -s -H "X-Vault-Token: $(cat data/vault_token.txt)" http://<this-machine>:8090/api/vault/status
curl -s -H "X-Audio-Token: $(cat data/audio_token.txt)" http://<this-machine>:8091/api/audio/jobs
```

In the dashboard: the Vault tab unlocks and the Audio Grabber tab lists jobs.

## Dual-boot note (Linux vs Windows)

One box, two OSes, one Helm container pointed at it:

- **Linux boot:** both backends run natively; the pass store is a local clone
  (or lives on a partition shared with Windows).
- **Windows boot:** both run in WSL2, as above.

`VAULT_HOST` / `AUDIO_HOST` in the container's `.env` must resolve to the **same
address on both boots**. Either give the machine one fixed LAN IP (a DHCP
reservation on the shared NIC covers both OSes) and route it to the container
host via a subnet router, or pin one OS's Tailscale IP and accept that the other
boot loses vault/audio. See `CONTAINER_SETUP.md`.

## GPG agent caching

The keyring and gpg-agent are separate per environment (Linux vs WSL2), so you
re-enter the passphrase once per boot per environment. gpg-agent caches it for
the configured TTL after that.
