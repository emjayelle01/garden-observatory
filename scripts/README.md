# MGO operator scripts

Small, **optional, operator-run** helper scripts for administering the Raspberry
Pi that hosts Matt's Garden Observatory. They are not part of the application,
are not imported by any Python code, and do not run automatically. Run them by
hand on the Pi.

See [`docs/Remote-Access.md`](../docs/Remote-Access.md) for the SSH and
deployment workflow, and [`docs/Service-Identity.md`](../docs/Service-Identity.md)
for the production runtime account and filesystem layout.

All scripts are POSIX/Bash, use strict mode, are idempotent, quote their inputs,
and never edit files tracked in this repository or the Pi's SSH configuration.

| Script | Run as | What it does |
| ------ | ------ | ------------ |
| `ssh/verify-key-auth.sh` | normal user | **Read-only** checks that key-based login is ready: `~/.ssh` and `authorized_keys` exist with the right permissions and at least one usable public key is installed. Reports the current `PasswordAuthentication` value for information only. Changes nothing. |
| `deploy/update-main.sh` | normal user (uses `sudo` for the service restart) | Aligns the repo to `origin/main` by fast-forward, runs `uv sync`, re-asserts the runtime group's read access, restarts `mgo.service`, and probes the four status endpoints. Refuses on a dirty tree. Non-destructive. |
| `deploy/install-service-identity.sh` | **root** (`sudo`) | Provisions the production runtime identity: the non-login `mgo` system account and group, `/etc/garden-observatory`, `/var/lib/garden-observatory` (with `db`, `media/captures`, `queues`, `state`), `/var/log/garden-observatory`, their ownership and permissions, and the rendered systemd unit. Refuses to install the unit when the checkout's `.venv` belongs to another directory (a relocated checkout would fail with `status=203/EXEC`); it reports the fix rather than repairing it. Idempotent; never overwrites an existing production configuration; backs up any existing unit. Supports `--dry-run`. |
| `deploy/verify-service-identity.sh` | normal user (more checks with `sudo`) | **Read-only** verification of the service identity: account type, non-login shell, locked password, group membership, directory ownership/modes, configuration readable-but-not-writable, unit identity directives, and the owner of the running process. Exits non-zero on any problem. Changes nothing. |
| `deploy/verify-operations.sh` | normal user (more checks with `sudo`) | **Read-only** verification of the Task 10 operations provisioning: backup directory ownership/mode, the backup unit's type, identity, sandbox and writable paths, the timer's schedule/persistence/enablement/next run, and the logrotate policy's location, mode, target confinement and bounded retention. Reports journal disk usage for information. Takes no backup, forces no rotation, enables nothing, repairs nothing. |
| `operations/backup-database.sh` | `mgo` (via `sudo -u mgo`) | Operator wrapper for the backup CLI: `backup`, `verify`, `restore-test`, `list`. Takes a consistent online backup **while the API keeps serving** — it never stops `mgo.service`. Contains no business logic; forwards every argument to Python and preserves its exit status. There is deliberately no `restore` subcommand. |
| `operations/create-support-bundle.sh` | `mgo` (via `sudo -u mgo`) | Operator wrapper for the diagnostic bundle CLI. Produces a bounded `mgo-support-<timestamp>.tar.gz` (mode `0600`) containing status, a bounded journal slice and a redacted configuration summary — never the database, media, raw configuration or credentials. Exit `0` complete, `1` partial, `2` no bundle. Uploads nothing; **inspect the archive before sending it**. |

Tracked deployment assets that are installed rather than run:

| File | Installed to | Notes |
| ---- | ------------ | ----- |
| `deploy/mgo.service.template` | `/etc/systemd/system/mgo.service` | The API unit. A template — do not copy it into place by hand. Unchanged by Task 10. |
| `deploy/mgo-backup.service.template` | `/etc/systemd/system/mgo-backup.service` | One-shot backup job, `Type=oneshot`, runs as `mgo:mgo`. Never starts, stops or requires the API. |
| `deploy/mgo-backup.timer` | `/etc/systemd/system/mgo-backup.timer` | Daily 02:30 local, `Persistent=true`, randomised 30 m delay. Installed verbatim. |
| `deploy/garden-observatory.logrotate` | `/etc/logrotate.d/garden-observatory` | Rotates only `/var/log/garden-observatory/*.log`. Does **not** rotate the journal. |

See [`docs/Operations.md`](../docs/Operations.md) for the backup architecture,
the deliberate absence of an automatic production restore, the disaster-recovery
outline, bundle contents and exclusions, and the Raspberry Pi validation
procedure.
