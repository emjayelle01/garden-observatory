# MGO Remote Access — SSH Key Authentication (trusted LAN)

Operator guide for administering the Raspberry Pi that hosts Matt's Garden
Observatory (MGO) over SSH, using **key-based authentication for convenience**.

**Current posture.** The Pi operates only on Matthew's trusted private home
network. It is not exposed to the Internet, there is no port forwarding, and no
public SSH service. SSH keys are introduced here to make routine administration
quicker and more repeatable — **not** as a security-hardening exercise.
**Password authentication intentionally remains enabled** as a simple recovery
and fallback method. Disabling passwords or other SSH hardening is **out of
current scope**; see [§10](#10-security-posture) for when to revisit it.

This is an operator procedure. Nothing here changes application behaviour: MGO,
its API, the preview/streaming subsystem, capture, and the `mgo.service` systemd
unit all run exactly as before. The repository documents the operator actions
and provides optional helper scripts (see
[`scripts/`](../scripts/README.md)); it never edits the Pi's live SSH
configuration and never commits machine-specific secrets.

---

## 1. Assumptions and placeholders

- The Pi runs **Raspberry Pi OS** (Bookworm or later) on a trusted LAN.
- The MGO repository lives at `/opt/garden-observatory` on the Pi (owned by the
  administrative account, readable by the runtime group) and the service is
  `mgo.service`. See the project README,
  `config/mgo.production.example.toml` and
  [`Service-Identity.md`](Service-Identity.md).
- The service itself runs as the dedicated non-login `mgo` account, **not** as
  `<pi-user>`. Everything in this document concerns the *administrative*
  identity only; the two never overlap.
- The admin workstation is Matthew's **Windows** development machine with the
  built-in OpenSSH client (`ssh`, `ssh-keygen`) available in PowerShell or Git
  Bash. Verify with `ssh -V`.

Replace these placeholders throughout:

| Placeholder   | Meaning                                   | Example                      |
| ------------- | ----------------------------------------- | ---------------------------- |
| `<pi-user>`   | The Pi login account you administer with. | `pi`                         |
| `<pi-host>`   | The Pi hostname or LAN IP.                | `mgo.local` / `192.168.1.42` |

Find the Pi's address on the Pi with `hostname -I`, and its user with `whoami`.

---

## 2. Generate an Ed25519 key (on the Windows workstation)

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

## 3. Install the public key on the Pi

You will authenticate with your password **once** to install the key (this is
fine — passwords stay enabled). From the workstation (PowerShell):

```powershell
$pub = Get-Content $env:USERPROFILE\.ssh\mgo_pi_ed25519.pub
ssh <pi-user>@<pi-host> "install -d -m 700 ~/.ssh && printf '%s\n' '$pub' >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys && sort -u ~/.ssh/authorized_keys -o ~/.ssh/authorized_keys"
```

This creates `~/.ssh` with mode `700`, appends the public key to
`~/.ssh/authorized_keys` (mode `600`), and de-duplicates it so re-running is
safe (idempotent).

> On a Linux/macOS/Git-Bash shell, `ssh-copy-id` also works:
> `ssh-copy-id -i ~/.ssh/mgo_pi_ed25519.pub <pi-user>@<pi-host>`.

On the Pi you can confirm the setup non-destructively at any time:

```bash
bash /opt/garden-observatory/scripts/ssh/verify-key-auth.sh
```

---

## 4. Verify key-only authentication (from Windows)

Prove the key works **without** silently falling back to a password:

```powershell
ssh -i $env:USERPROFILE\.ssh\mgo_pi_ed25519 -o IdentitiesOnly=yes -o PreferredAuthentications=publickey -o PasswordAuthentication=no <pi-user>@<pi-host> "echo MGO key auth OK; hostname; whoami"
```

If you see `MGO key auth OK`, key authentication works. If it is rejected, see
[§9 Troubleshooting](#9-troubleshooting) — and note that password login still
works in the meantime, so you are never locked out.

---

## 5. Recommended `~/.ssh/config` entry

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

---

## 6. Connect using the alias

```powershell
ssh mgo-pi "echo connected as $(whoami) on $(hostname)"
```

Routine administration is then just `ssh mgo-pi`. Because password
authentication remains enabled, you can still log in with a password from any
machine if a key is unavailable.

---

## 7. Git over SSH (optional)

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
cd /opt/garden-observatory
git remote set-url origin git@github.com:emjayelle01/garden-observatory.git
git remote -v
```

Normal Git operations then work over SSH: `git fetch`, `git pull --ff-only`,
`git push` (for branches you are permitted to push).

---

## 8. Deployment — align the Pi to `main`

Deployment is unchanged in shape: align to `main` and restart the service. Use
the helper:

```bash
bash /opt/garden-observatory/scripts/deploy/update-main.sh
```

or the equivalent manual steps:

```bash
cd /opt/garden-observatory
git fetch origin
git checkout main
git pull --ff-only origin main
git rev-parse HEAD
uv sync
sudo chgrp -R mgo /opt/garden-observatory
sudo chmod -R g+rX /opt/garden-observatory
sudo systemctl restart mgo.service
systemctl --no-pager --full status mgo.service
```

The `chgrp`/`chmod` step keeps newly pulled or synced files readable by the
runtime account; the helper does it for you.

The deploy helper is **non-destructive**: it refuses to run with a dirty working
tree, only fast-forwards `main`, and prints the service status plus probes of
the four status endpoints afterwards. It never changes SSH configuration.

The **first** deployment onto the dedicated service identity has extra one-time
steps (moving the checkout to `/opt`, provisioning the account and directories,
carrying existing data across). Those are in
[`Service-Identity.md` §7](Service-Identity.md#7-deployment).

---

## 9. Troubleshooting

Diagnose from the workstation with verbose output:

```powershell
ssh -vvv -i $env:USERPROFILE\.ssh\mgo_pi_ed25519 <pi-user>@<pi-host>
```

Common causes of key auth being refused (password login still works while you
fix these):

- **Wrong permissions on the Pi.** `~/.ssh` must be `700`, `authorized_keys`
  must be `600`, and the home directory must not be group/other-writable. Fix:
  `chmod 700 ~/.ssh && chmod 600 ~/.ssh/authorized_keys`.
- **Key not offered.** Add `IdentitiesOnly yes` and the correct `IdentityFile`
  to your SSH config, or `ssh-add` the key to the agent.
- **Wrong user or host.** Confirm `<pi-user>` (`whoami` on the Pi) and
  `<pi-host>` (`hostname -I`).
- **Deeper issue** — check `sudo journalctl -u ssh -n 50` on the Pi for the real
  reason.

---

## 10. Security posture

- **Today:** the Pi is on a trusted private LAN with no Internet exposure. Keys
  are for convenience; **password authentication stays enabled** as a simple
  fallback and recovery method. This is a deliberate, appropriate choice for the
  current requirement — not an incomplete or insecure state.
- **Private keys** never leave the workstation and are never committed; a
  passphrase-protected key plus the OS agent is recommended.
- **Reconsider hardening** (for example disabling password authentication, or
  adding fail2ban / firewalling) **only if the risk profile changes** — e.g. the
  Pi is exposed beyond the trusted LAN, reached over the Internet, port-forwarded,
  or moved to an untrusted network. At that point, revisit this document and add
  the hardening that the new requirement actually justifies.
- This task does **not** add authentication to the MGO web UI or API — remote
  administration is via SSH only, and the API stays on the LAN as before.
- **The runtime identity is separate.** The service runs as the non-login `mgo`
  account, which has no password, no shell, no capabilities and no
  administrative group membership — it cannot be used to log in over SSH or
  otherwise. Compromising the service does not yield the administrative account.
  See [`Service-Identity.md`](Service-Identity.md).
