# MGO Service Identity

How Matt's Garden Observatory runs in production: a dedicated, non-login
runtime account and a persistent filesystem layout that lives outside any
operator's home directory.

This is a **deployment foundation**, not an application feature. Nothing in the
API, the camera pipeline, motion detection or notifications changes — the same
code runs, under a different identity, reading and writing different paths.

---

## 1. Why

Before this, MGO ran from an operator's home directory under an administrative
login account. That conflated two very different things:

- **administration** — SSH login, `git pull`, `uv sync`, `sudo`;
- **runtime** — a long-lived network service that watches a camera.

A compromise or a bug in the service therefore had the reach of the operator's
account. Splitting them means the service can do exactly what it needs and
nothing else, and administrative access stays entirely separate.

---

## 2. The identity

| Property             | Value                                          |
| -------------------- | ---------------------------------------------- |
| Account              | `mgo`                                          |
| Type                 | System account (uid below 1000)                |
| Login shell          | `/usr/sbin/nologin`                            |
| Password             | Locked — no password login is possible         |
| Home directory       | `/var/lib/garden-observatory` (not created by `useradd`) |
| Primary group        | `mgo`                                          |
| Supplementary groups | `video` — and nothing else                     |
| Linux capabilities   | **None** (`CapabilityBoundingSet=` is empty)   |

`video` is the only supplementary group. It is what grants access to the
Raspberry Pi camera device nodes (`/dev/video*`, `/dev/media*`, `/dev/vchiq`),
and it is also what lets `vcgencmd` read the SoC temperature for `/health`.

The account is **not** a member of `sudo`, `adm`, or any other administrative
group, and it cannot log in — over SSH or otherwise. Administration continues
to use the operator's own account exactly as described in
[`Remote-Access.md`](Remote-Access.md); the two identities never overlap.

The port MGO binds (8080) is above 1024, so no `CAP_NET_BIND_SERVICE` — and no
capability at all — is required.

---

## 3. Filesystem layout

```
/etc/garden-observatory/            root:mgo  0750   configuration (read-only to the service)
└── mgo.toml                        root:mgo  0640

/var/lib/garden-observatory/        mgo:mgo   0750   persistent application data
├── db/                             mgo:mgo   0750   SQLite database + WAL/shm sidecars
│   └── mgo.db
├── media/                          mgo:mgo   0750   captured imagery
│   └── captures/
├── queues/                         mgo:mgo   0750   reserved for future async delivery
└── state/                          mgo:mgo   0750   reserved for persisted runtime markers

/var/log/garden-observatory/        mgo:mgo   0750   file-based logs (the journal is primary)
```

`queues/` and `state/` are provisioned now and unused. They exist so the layout
is settled before later tasks need them; nothing writes to either yet.

Application logging goes to stdout/stderr and is captured by journald
(`journalctl -u mgo.service`). `/var/log/garden-observatory` backs the
`storage.log_directory` setting and is available for any file destination.

### Ownership model

| Path                     | Owner  | Group | Rationale                                                                 |
| ------------------------ | ------ | ----- | ------------------------------------------------------------------------- |
| `/etc/garden-observatory` | `root` | `mgo` | The service **reads** its configuration and can never rewrite it.          |
| `/var/lib/garden-observatory` | `mgo` | `mgo` | The service owns its own data — database, captures, queues, state.     |
| `/var/log/garden-observatory` | `mgo` | `mgo` | The service writes its own logs.                                       |
| The application checkout | operator | `mgo` | The operator deploys code; the service reads and executes it, never writes it. |

### Permission model

- Directories are `0750`, files are `0640`. **Nothing is world-readable and
  nothing is world-writable.**
- The runtime account has **write** access to exactly three trees:
  `/var/lib/garden-observatory`, `/var/log/garden-observatory`, and the
  service's private `/tmp` (`PrivateTmp=yes`).
- The runtime account has **read** access to its configuration and the
  application checkout, and **no** access to anything else it does not own.
- The application checkout carries the setgid bit on its directories, so files
  created by a later `git pull` or `uv sync` stay in the `mgo` group and remain
  readable by the service.

---

## 4. Application location

The service runs the virtualenv entry point directly:

```ini
ExecStart=<app-root>/.venv/bin/uvicorn mgo.api.app:app --host 0.0.0.0 --port 8080
```

No package manager runs at service start, so the runtime account never needs
write access to the checkout or to a `uv` cache.

The checkout must be somewhere the `mgo` account can **traverse and read**.
Raspberry Pi OS creates home directories that other accounts cannot enter, so a
checkout at `~/Projects/garden-observatory` will normally be unreachable. The
recommended production location is therefore:

```
/opt/garden-observatory
```

`/opt` is world-traversable, so the service reaches the code without any
permission change to an operator's home directory. `install-service-identity.sh`
probes the whole path as the `mgo` account and tells you precisely which
directory blocks it if one does.

---

## 5. systemd unit

The unit is tracked as a template at
[`scripts/deploy/mgo.service.template`](../scripts/deploy/mgo.service.template)
and rendered to `/etc/systemd/system/mgo.service` by the install script.

Identity directives:

```ini
User=mgo
Group=mgo
SupplementaryGroups=video
Environment=MGO_CONFIG_PATH=/etc/garden-observatory/mgo.toml
```

Directory provisioning — systemd creates and chowns these on **every** start,
so a fresh machine needs no manual directory setup:

```ini
StateDirectory=garden-observatory garden-observatory/db garden-observatory/media garden-observatory/media/captures garden-observatory/queues garden-observatory/state
StateDirectoryMode=0750
LogsDirectory=garden-observatory
LogsDirectoryMode=0750
```

Least privilege:

```ini
CapabilityBoundingSet=
AmbientCapabilities=
NoNewPrivileges=yes
RestrictSUIDSGID=yes
UMask=0027
ProtectSystem=strict          # everything read-only except the state/log dirs
ProtectHome=yes               # operator home directories are unreachable
PrivateTmp=yes
ProtectProc=invisible
ProtectClock=yes
ProtectControlGroups=yes
ProtectHostname=yes
ProtectKernelLogs=yes
ProtectKernelModules=yes
ProtectKernelTunables=yes
LockPersonality=yes
RestrictNamespaces=yes
RestrictRealtime=yes
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
SystemCallArchitectures=native
```

`PrivateDevices=` is deliberately **not** enabled: the camera pipeline needs the
real device nodes. Access to them is restricted by `video` group membership
instead.

---

## 6. Configuration paths

The canonical production configuration file is now:

```
/etc/garden-observatory/mgo.toml
```

**The configuration loader is unchanged.** Selection precedence is still:

| Precedence | Source                                         |
| ---------- | ---------------------------------------------- |
| 1          | An explicit path passed to `load_config(...)`. |
| 2          | The `MGO_CONFIG_PATH` environment variable.    |
| 3          | The repository default, `config/mgo.toml`.     |

There is no implicit discovery of `/etc/garden-observatory/mgo.toml`; the
systemd unit reaches it by setting `MGO_CONFIG_PATH`. Any existing deployment
that points `MGO_CONFIG_PATH` at the previous `/etc/mgo/mgo.toml` keeps working
exactly as before — moving to the new location is a deployment step, not a code
requirement.

`config/mgo.production.example.toml` shows the layout in full:

```toml
[storage]
data_directory = "/var/lib/garden-observatory"
log_directory  = "/var/log/garden-observatory"
database_path  = "/var/lib/garden-observatory/db/mgo.db"

[camera]
capture_directory = "/var/lib/garden-observatory/media/captures"
```

---

## 7. Deployment

### 7.1 One-time: place the checkout where the service can read it

If the repository currently lives in a home directory, move it once:

```bash
sudo mkdir -p /opt
sudo mv ~/Projects/garden-observatory /opt/garden-observatory
sudo chown -R "$(id -un)":"$(id -gn)" /opt/garden-observatory
cd /opt/garden-observatory
rm -rf .venv
uv sync
```

The checkout stays owned by the operator, so `git pull` and `uv sync` still work
without `sudo`.

> **The `rm -rf .venv` is required, not optional.** A Python virtual environment
> is **location-dependent**: every launcher in `.venv/bin` hard-codes the
> absolute path of the interpreter that created it. Moving or copying a checkout
> does not update them, so a relocated `.venv` still starts with a shebang like
>
> ```
> #!/home/pi/Projects/garden-observatory/.venv/bin/python3
> ```
>
> and systemd fails the service with `status=203/EXEC`. Recreating the
> environment is the only fix — the paths cannot be safely rewritten in place.
> The same applies to restoring a checkout from a backup or copying one between
> machines.

### 7.2 Provision the identity

```bash
sudo bash /opt/garden-observatory/scripts/deploy/install-service-identity.sh
```

Preview it first with `--dry-run` if you prefer. The script is idempotent: it
creates the group and account only if missing, re-asserts ownership and
permissions every run, and **never** overwrites an existing
`/etc/garden-observatory/mgo.toml`. It backs up any existing systemd unit to
`/etc/systemd/system/mgo.service.bak-<timestamp>` before writing the new one.

It also **refuses to install the unit** if the checkout's `.venv` belongs to a
different directory, is missing, or has an unusable interpreter — installing a
unit that could never start would only surface later as a confusing
`status=203/EXEC`. It prints the remediation above and stops; it never rewrites
launcher shebangs and never deletes the environment for you, because `uv sync`
has to run as the administrative user rather than as root. Re-run the installer
after recreating the environment.

Useful options: `--app-root PATH`, `--host`, `--port`, `--no-unit`, `--dry-run`.
Under `--no-unit` an unusable `.venv` is only a warning, since no unit is being
written that could point at it.

### 7.3 Carry existing data across

If a previous in-checkout database exists, copy it before the first start
(the install script detects this and prints these commands):

```bash
sudo cp -a /opt/garden-observatory/data/mgo.db* /var/lib/garden-observatory/db/
sudo cp -a /opt/garden-observatory/data/captures/. /var/lib/garden-observatory/media/captures/
sudo chown -R mgo:mgo /var/lib/garden-observatory
```

The database schema is unchanged, so the existing observation timeline and
capture catalogue carry over as-is.

> Capture rows store an **absolute path**. Captures taken before the migration
> keep their old paths in the catalogue even after the files are copied; new
> captures use the new layout. Reconciling historical rows is deliberately out
> of scope here — no schema or data rewriting is part of this task.

### 7.4 Review the configuration and start

```bash
sudoedit /etc/garden-observatory/mgo.toml
sudo systemctl restart mgo.service
systemctl --no-pager --full status mgo.service
```

### 7.5 Verify

```bash
sudo bash /opt/garden-observatory/scripts/deploy/verify-service-identity.sh
```

Read-only. It checks the account, its shell and groups, the ownership and mode
of every directory, that the configuration is readable but **not** writable by
the service, the unit's identity directives, and that the running process is
actually owned by `mgo`. It exits non-zero on any problem.

Then confirm the application behaves exactly as before:

```bash
curl -fsS http://127.0.0.1:8080/health
curl -fsS http://127.0.0.1:8080/camera/status
curl -fsS http://127.0.0.1:8080/motion/status
curl -fsS http://127.0.0.1:8080/notifications/status
```

Check the journal for permission errors during start-up:

```bash
journalctl -u mgo.service -b --no-pager | grep -iE 'denied|permission|errno 13' || echo 'no permission errors'
```

---

## 8. Routine deployment afterwards

`scripts/deploy/update-main.sh` is unchanged in purpose. It now also re-asserts
the runtime group's read access after `uv sync` (new files must stay readable by
`mgo`) and probes all four status endpoints:

```bash
bash /opt/garden-observatory/scripts/deploy/update-main.sh
```

---

## 9. Troubleshooting

| Symptom | Cause | Fix |
| ------- | ----- | --- |
| `Failed to locate executable .../uvicorn: Permission denied` | The `mgo` account cannot traverse a parent of the checkout. | Move the checkout to `/opt/garden-observatory` and re-run the install script. |
| `status=203/EXEC`, `Failed to execute .../.venv/bin/uvicorn` | The `.venv` is missing, or belongs to a different checkout after a move/copy — its launchers still point at the old interpreter path. | `cd <app-root> && rm -rf .venv && uv sync`, then restart. Confirm with `head -1 .venv/bin/uvicorn`: the shebang must sit under the current app root. The installer refuses to write a unit in this state. |
| `sqlite3.OperationalError: unable to open database file` | The state directory is missing or wrongly owned. | `sudo bash scripts/deploy/install-service-identity.sh` then restart. |
| `FileNotFoundError: Configuration file not found` | `MGO_CONFIG_PATH` points at a file that does not exist. | Check `Environment=` in the unit and that `/etc/garden-observatory/mgo.toml` exists. |
| `camera` readiness is `waiting_for_hardware` on the Pi | The account is not in `video`, or the group was created after the account. | `sudo usermod -aG video mgo && sudo systemctl restart mgo.service` |
| `temperature` is `null` in `/health` | `vcgencmd` is unavailable to the account. | Confirm `video` membership; this degrades safely and is not an error. |

---

## 10. Operations units added later

Task 10 added operations provisioning **without changing anything above**. The
`mgo.service` template is byte-for-byte unchanged: its identity, camera access,
restart policy, boot behaviour and sandbox already satisfied the requirement, so
it was not edited, and the API needs no restart to adopt Task 10.

What the installer now also provisions:

| Item | Detail |
| ---- | ------ |
| `/var/backups/garden-observatory` | `mgo:mgo 0750`. Outside the state tree, so a backup is not lost to the same accident as the original. Holds three-file recovery sets: the database snapshot, a snapshot of `/etc/garden-observatory/mgo.toml`, and the manifest (all `0640`). **The configuration snapshot may contain credentials** — treat a copied set as sensitive; it is never included in a support bundle. |
| `mgo-backup.service` | One-shot, `mgo:mgo`, no capabilities, `NoNewPrivileges`, `ProtectSystem=strict`, `PrivateDevices=yes` (it needs no camera), `RestrictAddressFamilies=AF_UNIX`. Never starts, stops or requires `mgo.service`. |
| `mgo-backup.timer` | Daily 02:30 local, `Persistent=true`, 30-minute randomised delay. |
| `/etc/logrotate.d/garden-observatory` | Rotates only MGO-owned `*.log` files. Does not rotate the journal. |

The backup unit's `ReadWritePaths` includes the **database directory** as well
as the backup root. That is required, not incidental: SQLite needs shared-memory
access to read a WAL database, so the directory must be writable even for a
read-only reader. It grants filesystem write access, not database write access.

The installer validates the virtual environment **per selected target** — the
API unit runs `.venv/bin/uvicorn`, the backup unit runs `.venv/bin/python` — and
`--no-unit` skips only `mgo.service`, so a broken environment still stops the
run while the backup service is being installed. Timer activation is checked
step by step and its enabled/active states verified, so a failed activation can
never be reported as success.

Verify the operations provisioning with its own read-only script:

```bash
bash scripts/deploy/verify-operations.sh
```

See [`Operations.md`](Operations.md) for the full architecture, including why no
worker unit exists yet.

## 11. Scope

Out of scope for this foundation, and deliberately unchanged:

- SSH configuration and hardening (see [`Remote-Access.md`](Remote-Access.md));
- API or dashboard authentication;
- the database schema, the API surface, and every application behaviour;
- `SystemCallFilter=` and `DeviceAllow=` hardening — both are plausible next
  steps, but each risks breaking the camera subprocess pipeline and needs
  dedicated on-Pi validation rather than being bundled in here.
