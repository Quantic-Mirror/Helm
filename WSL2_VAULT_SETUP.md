# WSL2 Vault Server Autostart Setup

This guides you through making `vault_server.py` auto-start when WSL2 boots
on the Windows side, with the pass store on a USB drive.

## Step 1: Mount the USB drive in WSL2

WSL2 auto-mounts Windows drives at `/mnt/<DRIVE_LETTER>`. If your USB drive
is always `E:`, it will appear at `/mnt/e/` in WSL2.

To get a consistent mount point, add this to `/etc/wsl.conf` inside WSL2:

```ini
[automount]
enabled = true
mountFsT = false
root = /mnt/
options = "metadata,umask=22,fmask=111"
mount.0 = "E: /mnt/usb"
```

Then run `wsl --shutdown` from PowerShell and restart WSL2.

## Step 2: Import your GPG key

Export from Linux:
```bash
# On the Linux host (e.g. your dual-boot machine):
gpg --export-secret-keys your@email.com > /mnt/usb/gpg-secret.key
```

Import in WSL2:
```bash
# In WSL2 Ubuntu:
gpg --import /mnt/usb/gpg-secret.key
# Verify:
gpg --list-secret-keys
```

## Step 3: Install pass + set up the store

In WSL2:
```bash
sudo apt update
sudo apt install pass gnupg

# Point pass at the USB-mounted store:
export PASSWORD_STORE_DIR=/mnt/usb/password-store
pass init your@email.com   # writes the .gpg-id file
# Your .gpg files are already on the USB drive
```

## Step 4: Create the autostart script

Create `start-vault.sh` in WSL2 (path is flexible):

```bash
#!/bin/bash
# Auto-starts vault_server.py when WSL2 boots.
# The Helm container on the VPS proxies /api/vault/* to this host.

export PASSWORD_STORE_DIR=/mnt/usb/password-store
export HOME=/home/$(whoami)

cd /opt/helm
python3 vault_server.py 8090
```

Make it executable:
```bash
chmod +x ~/start-vault.sh
```

## Step 5: Auto-run on WSL2 startup

Windows doesn't have a traditional init system for WSL2. The cleanest approach:

**Method A: Task Scheduler entry in Windows**

1. Open Task Scheduler → Create Basic Task
2. Trigger: "At log on" (or "On a schedule" if you want it every boot)
3. Action: Start a program
   - Program: `wsl.exe`
   - Arguments: `-e /home/isaboo/start-vault.sh`
4. Check "Run with highest privileges" (for USB mount access)

**Method B: WSL2 /etc/profile.d (loads on every shell, less clean)**

Add to `/etc/profile.d/start-vault.sh` inside WSL2:
```bash
# Only start if not already running
if ! pgrep -f "vault_server.py" > /dev/null; then
    nohup /home/isaboo/start-vault.sh > /tmp/vault-server.log 2>&1 &
fi
```

Method A is preferred — it starts once when Windows boots, not on every
shell launch. Method B works but may spawn multiple instances.

## Step 6: Verify

From any machine on the Tailscale network:
```bash
curl http://<WINDOWS_HOST_TAILSCALE_IP>:8090/health
# Should return: {"status":"ok","service":"vault"}
```

The Helm container on the VPS proxies `/api/vault/*` to
`VAULT_BACKEND_URL` from the `.env` file (e.g. `http://100.99.88.77:8090`).

## Cross-platform notes

- **Linux boot:** vault_server.py runs natively on the host (same as before)
- **Windows boot:** vault_server.py runs in WSL2, accessible at
  `http://<HOST_TAILSCALE_IP>:8090` via Tailscale over the tailnet
- **GPG agent caching:** separate in each environment (Linux vs WSL2).
  You'll re-enter your GPG passphrase once per boot per environment.
- **USB drive:** Must be formatted with a filesystem both OSes can read.
  exFAT works well if it supports your key file sizes. NTFS works too
  but file permission mapping can be quirky in WSL2.
