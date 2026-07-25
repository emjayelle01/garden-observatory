# MGO Remote Access — SSH Key Authentication

Operator guide for administering the Raspberry Pi that hosts Matt's Garden
Observatory (MGO) over SSH using **key-based authentication**, and for safely
disabling password authentication once keys are validated.

This is an **operator procedure**. Nothing here changes application behaviour:
MGO, its API, the preview/streaming subsystem, capture, and the `mgo.service`
systemd unit all run exactly as before. The repository documents the required
operator actions and provides optional helper scripts (see
[`scripts/`](../scripts/README.md)); it never edits the Pi's live configuration
itself and never commits machine-specific secrets.

> **Golden rule — never lock yourself out.** Keep your current SSH session (or a
> console with keyboard + monitor) open until you have verified key
> authentication in a **separate, new** session. Only then disable passwords.

---

## 0. Assumptions and placeholders

- The Pi runs **Raspberry Pi OS** (Bookworm or later).
- The MGO repository lives at `~/Projects/garden-observatory` on the Pi and the
  service is `mgo.service` (see the project README and
  `config/mgo.production.example.toml`).
- The admin workstation is Matthew's **Windows** development machine with the
  built-in OpenSSH client (`ssh`, `ssh-keygen`) available in PowerShell or Git
  Bash. Verify with `ssh -V`.

Replace these placeholders throughout:

| Placeholder   | Meaning                                   | Example                 |
| ------------- | ----------------------------------------- | ----------------------- |
| `<pi-user>`   | The Pi login account you administer with. | `pi`                    |
| `<pi-host>`   | The Pi hostname or LAN IP.                | `mgo.local` / `192.168.1.42` |

Find the Pi's address on the Pi with `hostname -I`, and its user with `whoami`.

---

## 1. Generate an SSH key (on the Windows workstation)

Use a dedicated Ed25519 key for the Pi. Run in PowerShell:

```powershell
ssh-keygen -t ed25519 -a 100 -C "matthew-mgo-workstation" -f $env:USERPROFILE\.ssh\mgo_pi_ed25519
```

- Choose a **passphrase** when prompted (recommended). Windows can cache it via
  the OpenSSH Authentication Agent so you are not prompted every time:
  ```powershell
  Get-Service ssh-agent | Set-Service -StartupType Automatic
  Start-Service ssh-agent
  ssh-add $env:USERPROFILE\.ssh\mgo_pi_ed25519
  ```
- This creates two files. The **private** key `mgo_pi_ed25519` never leaves the
  workstation. The **public** key `mgo_pi_ed25519.pub` is what you install on the
  Pi.

> Never commit private keys, and never paste a private key into the repository,
> a chat, or a ticket.

---

## 2. Install the public key on the Pi

You will authenticate with your password **once** to install the key. From the
workstation (PowerShell):

```powershell
$pub = Get-Content $env:USERPROFILE\.ssh\mgo_pi_ed25519.pub
ssh <pi-user>@<pi-host> "install -d -m 700 ~/.ssh && printf '%s\n' '$pub' >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys && sort -u ~/.ssh/authorized_keys -o ~/.ssh/authorized_keys"
```

This creates `~/.ssh` with mode `700`, appends the public key to
`~/.ssh/authorized_keys` (mode `600`), and de-duplicates it so re-running is
safe (idempotent).

> On Raspberry Pi OS, `ssh-copy-id` also works from a Linux/macOS/Git-Bash shell:
> `ssh-copy-id -i ~/.ssh/mgo_pi_ed25519.pub <pi-user>@<pi-host>`.

---

## 3. Verify key authentication (before changing anything)

From the workstation, force key-only auth so you prove the key works **without**
falling back to a password:

```powershell
ssh -i $env:USERPROFILE\.ssh\mgo_pi_ed25519 -o IdentitiesOnly=yes -o PreferredAuthentications=publickey -o PasswordAuthentication=no <pi-user>@<pi-host> "echo MGO key auth OK; hostname; whoami"
```

If you see `MGO key auth OK`, key authentication works. If it asks for a
password or is rejected, **do not** proceed to disable passwords — see
[§8 Troubleshooting](#8-troubleshooting).

On the Pi, you can sanity-check the setup non-destructively:

```bash
bash ~/Projects/garden-observatory/scripts/ssh/verify-key-auth.sh
```

---

## 4. Recommended SSH client configuration

Create or edit `%USERPROFILE%\.ssh\config` on the workstation so `ssh mgo-pi`
just works:

```sshconfig
Host mgo-pi
    HostName <pi-host>
    User <pi-user>
    IdentityFile ~/.ssh/mgo_pi_ed25519
    IdentitiesOnly yes
    ServerAliveInterval 30
    ServerAliveCountMax 3
```

Test it:

```powershell
ssh mgo-pi "echo connected as $(whoami) on $(hostname)"
```

---

## 5. Disable password authentication (only after §3 passes)

Disabling passwords is done with an additive **drop-in** file under
`/etc/ssh/sshd_config.d/`, not by editing the main `sshd_config`. This is
reversible, survives package upgrades, and is validated before it is applied.

Modern Raspberry Pi OS already includes drop-ins via this line in
`/etc/ssh/sshd_config`:

```text
Include /etc/ssh/sshd_config.d/*.conf
```

Confirm it is present (`grep -n '^Include' /etc/ssh/sshd_config`) before
continuing.

**Keep your current session open.** In it, run the helper (it validates with
`sshd -t`, backs up any existing drop-in, refuses to run if you have no
`authorized_keys`, and reloads SSH):

```bash
sudo bash ~/Projects/garden-observatory/scripts/ssh/disable-password-auth.sh
```

Then, **from a new terminal**, confirm you can still connect by key and that
passwords are refused:

```powershell
ssh mgo-pi "echo still-in"
ssh -o PreferredAuthentications=password -o PubkeyAuthentication=no <pi-user>@<pi-host> "echo should-not-happen"
```

The first must succeed; the second must be rejected (`Permission denied`). Only
once both are confirmed should you close the original session.

If you prefer to do it by hand, the drop-in content is:

```text
# /etc/ssh/sshd_config.d/99-mgo-ssh-hardening.conf
PasswordAuthentication no
KbdInteractiveAuthentication no
ChallengeResponseAuthentication no
PubkeyAuthentication yes
```

Apply manually with validation:

```bash
sudo sshd -t && sudo systemctl reload ssh
```

---

## 6. Rollback (re-enable password authentication)

If anything goes wrong, re-enable passwords by removing the drop-in:

```bash
sudo bash ~/Projects/garden-observatory/scripts/ssh/enable-password-auth.sh
```

Or manually:

```bash
sudo rm -f /etc/ssh/sshd_config.d/99-mgo-ssh-hardening.conf
sudo sshd -t && sudo systemctl reload ssh
```

If you are **locked out entirely** (no SSH session, passwords already disabled):

1. Attach a keyboard and monitor to the Pi, or remove the SD card and mount it
   on another machine.
2. Delete `/etc/ssh/sshd_config.d/99-mgo-ssh-hardening.conf` (on the card this is
   under the root filesystem partition).
3. Reboot; password authentication is restored. Re-do §2–§3, then §5.

Raspberry Pi Imager can also pre-seed an SSH key and enable SSH when reflashing,
as a last resort.

---

## 7. Git and deployment over SSH

### 7.1 Git via SSH

To pull/push without HTTPS credentials, use an SSH remote. Add an SSH key to the
relevant GitHub account (this can be a **separate** key from the Pi login key):

```bash
# On the Pi (or workstation), generate a GitHub key if needed:
ssh-keygen -t ed25519 -C "mgo-github" -f ~/.ssh/mgo_github_ed25519
cat ~/.ssh/mgo_github_ed25519.pub    # add this to GitHub → Settings → SSH keys
```

Point the client at it (append to `~/.ssh/config`):

```sshconfig
Host github.com
    User git
    IdentityFile ~/.ssh/mgo_github_ed25519
    IdentitiesOnly yes
```

Verify and switch the repository remote to SSH:

```bash
ssh -T git@github.com                 # expect a GitHub greeting (no shell)
cd ~/Projects/garden-observatory
git remote set-url origin git@github.com:emjayelle01/garden-observatory.git
git remote -v
```

Normal Git operations then work over SSH: `git fetch`, `git pull --ff-only`,
`git push` (for branches you are permitted to push).

### 7.2 Deployment workflow

Deployment is unchanged from prior tasks — align to `main` and restart the
service. Use the helper:

```bash
bash ~/Projects/garden-observatory/scripts/deploy/update-main.sh
```

or the equivalent manual steps:

```bash
cd ~/Projects/garden-observatory
git fetch origin
git checkout main
git pull --ff-only origin main
git rev-parse HEAD
uv sync
sudo systemctl restart mgo.service
systemctl --no-pager --full status mgo.service
```

The deploy helper is **non-destructive**: it refuses to run with a dirty working
tree, only fast-forwards `main`, and prints the service status and a health
probe afterwards. It never changes SSH configuration.

---

## 8. Troubleshooting

Diagnose from the workstation with verbose output:

```powershell
ssh -vvv -i $env:USERPROFILE\.ssh\mgo_pi_ed25519 <pi-user>@<pi-host>
```

Common causes of key auth being refused:

- **Wrong permissions on the Pi.** `~/.ssh` must be `700`, `authorized_keys`
  must be `600`, and the home directory must not be group/other-writable. Fix:
  `chmod 700 ~/.ssh && chmod 600 ~/.ssh/authorized_keys`.
- **Key not offered.** Add `IdentitiesOnly yes` and the correct `IdentityFile`
  to your SSH config, or `ssh-add` the key to the agent.
- **Wrong user or host.** Confirm `<pi-user>` (`whoami` on the Pi) and
  `<pi-host>` (`hostname -I`).
- **SELinux/AppArmor or an unusual home path** — rare on Raspberry Pi OS; check
  `sudo journalctl -u ssh -n 50` on the Pi for the real reason.

Always fix key authentication **before** disabling passwords, and keep a session
open during any SSH configuration change.

---

## 9. Security notes

- Private keys never leave the workstation and are never committed.
- Use a passphrase-protected key plus the OS agent.
- Disabling password authentication removes the most common brute-force vector;
  key-only access is the recommended steady state.
- This task does **not** add authentication to the MGO web UI or API — remote
  administration is via SSH only. Keep the API bound to the LAN / behind your
  network boundary as before.
