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
The `operations/` wrappers are not otherwise environment-neutral: they select
the production configuration when the caller has not (see below).

| Script | Run as | What it does |
| ------ | ------ | ------------ |
| `ssh/verify-key-auth.sh` | normal user | **Read-only** checks that key-based login is ready: `~/.ssh` and `authorized_keys` exist with the right permissions and at least one usable public key is installed. Reports the current `PasswordAuthentication` value for information only. Changes nothing. |
| `deploy/mgo-validate` | **root** (`sudo`, caller `claude` only) | The **approved deployment gateway**, installed to `/usr/local/sbin/mgo-validate`. Three actions and no others: `show-approval` prints the approved SHA; `deploy-main` deploys `origin/main` at that SHA; `restart-api` restarts the service at the already-deployed approved SHA. The root-owned approval file is the authority — the gateway only reads it. Proves the remote SHA against the approval **before** fetching, accepts only a strict fast-forward, runs Git and `uv sync --frozen` back down as `claude`, uses root for the restart alone, requires an active unit *and* a healthy endpoint within a bound, and restores the preview state it found. Any failure after the first change restores the captured commit, environment and preview state, and still reports failure. Takes one action word and nothing else. See [`docs/Deployment-Gateway.md`](../docs/Deployment-Gateway.md). |
| `deploy/update-main.sh` | normal user | Thin wrapper around `sudo -n /usr/local/sbin/mgo-validate deploy-main`. Contains no Git, `uv`, permission or `systemctl` logic and has **no fallback**: if the gateway is missing it says so and stops. Refuses to run as root. `exec`s the gateway so its exit code and output reach you unchanged. |
| `deploy/install-mgo-validate.sh` | **root** (`sudo`), or anyone with `--dry-run` | Installs the gateway (`0755 root:root`) and its sudoers policy (`0440 root:root`). Validates the gateway with `bash -n` and the policy with `visudo -cf` **before** installing either, writes both atomically, then verifies checksum, owner and mode on disk. Rolls the gateway back if the policy cannot be installed, so the host never has half a control plane. Idempotent. Never touches the approval file, the repository or `mgo.service`. |
| `deploy/install-service-identity.sh` | **root** (`sudo`) | Provisions the production runtime identity: the non-login `mgo` system account and group, `/etc/garden-observatory`, `/var/lib/garden-observatory` (with `db`, `media/captures`, `queues`, `state`), `/var/log/garden-observatory`, `/var/backups/garden-observatory`, their ownership and permissions, and the rendered systemd units. Validates the virtual environment **per selected target** — `uvicorn` for `mgo.service`, `python` for `mgo-backup.service` — and refuses to install a unit against an unusable one (a relocated checkout would fail with `status=203/EXEC`); it reports the fix rather than repairing it. `--no-unit` skips only the API unit, so a broken environment still stops the run while the backup service is selected. Timer activation is checked at every step and the enabled/active states are verified, so a failed activation is never reported as success. Idempotent; never overwrites an existing production configuration; backs up any existing unit. Supports `--dry-run`, `--no-operations`, `--keep N` (validated `1..3650`, matching the runtime) and `--backup-dir`. |
| `deploy/verify-service-identity.sh` | normal user (more checks with `sudo`) | **Read-only** verification of the service identity: account type, non-login shell, locked password, group membership, directory ownership/modes, configuration readable-but-not-writable, unit identity directives, and the owner of the running process. Exits non-zero on any problem. Changes nothing. |
| `deploy/verify-operations.sh` | normal user (more checks with `sudo`) | **Read-only** verification of the Task 10 operations provisioning: backup directory ownership/mode, the backup unit's type, identity, sandbox and writable paths, the timer's schedule/persistence/enablement/next run, and the logrotate policy's location, mode, target confinement and bounded retention. Reports journal disk usage for information. Takes no backup, forces no rotation, enables nothing, repairs nothing. |
| `operations/backup-database.sh` | `mgo` (via `sudo -u mgo`) | Operator wrapper for the backup CLI: `backup`, `verify`, `restore-test`, `list`. Produces a **three-file recovery set** — database snapshot, production configuration snapshot and manifest — taken **while the API keeps serving**; it never stops `mgo.service`. Makes no backup decisions; forwards every argument to Python and preserves its exit status. Defaults `MGO_CONFIG_PATH` to `/etc/garden-observatory/mgo.toml` when unset (see below). There is deliberately no `restore` subcommand. |
| `operations/create-support-bundle.sh` | `mgo` (via `sudo -u mgo`) | Operator wrapper for the diagnostic bundle CLI. Produces a bounded `mgo-support-<timestamp>.tar.gz` (mode `0600`) containing status, a bounded journal slice and a redacted configuration summary — never the database, media, raw configuration or credentials, and never the recovery set's configuration snapshot. Collection is literal-loopback only, proxies disabled, redirects refused. Exit `0` complete, `1` partial, `2` no bundle. Defaults `MGO_CONFIG_PATH` the same way. Uploads nothing; **inspect the archive before sending it**. |

## The operator wrappers and `MGO_CONFIG_PATH`

The two `operations/` wrappers make **one** execution-environment decision:
which configuration an operator means. When `MGO_CONFIG_PATH` is unset they
export `/etc/garden-observatory/mgo.toml` before invoking Python.

```text
explicit --config PATH              wins over everything
caller-provided MGO_CONFIG_PATH     preserved exactly
wrapper-supplied production default applied only when the variable is unset
```

A set-but-empty or whitespace-only value is **preserved**, not replaced, so the
CLI still reports it as the configuration error it is. `sudo` clears the
environment, so without this default every documented manual command fell
through to the tracked *development* configuration; `mgo-backup.service` was
always explicit and was never affected. Direct Python execution on a development
machine still uses `config/mgo.toml`.

Backup, verification, retention, redaction and collection decisions remain in
Python, where they are typed and unit-tested.

Tracked deployment assets that are installed rather than run:

| File | Installed to | Notes |
| ---- | ------------ | ----- |
| `deploy/mgo.service.template` | `/etc/systemd/system/mgo.service` | The API unit. A template — do not copy it into place by hand. Unchanged by Task 10. |
| `deploy/mgo-backup.service.template` | `/etc/systemd/system/mgo-backup.service` | One-shot backup job, `Type=oneshot`, runs as `mgo:mgo`. Never starts, stops or requires the API. |
| `deploy/mgo-backup.timer` | `/etc/systemd/system/mgo-backup.timer` | Daily 02:30 local, `Persistent=true`, randomised 30 m delay. Installed verbatim. |
| `deploy/garden-observatory.logrotate` | `/etc/logrotate.d/garden-observatory` | Rotates only `/var/log/garden-observatory/*.log`. Does **not** rotate the journal. |

These installed files live in `/etc` and are **outside Git**. Changing the
checkout back to `main` does not remove them, does not disable the timer and
does not stop it — it only removes the code they point at. A pre-merge
validation install must therefore be undone explicitly before returning to
`main` (`docs/Operations.md` §13.14); permanent installation belongs after the
merge. The same applies to the running service: a checkout does not reload
modules a live process already imported, so `mgo.service` is restarted **after**
the checkout (§13.17) and the return to `main` is not complete until it has.

See [`docs/Operations.md`](../docs/Operations.md) for the backup architecture,
the deliberate absence of an automatic production restore, the disaster-recovery
outline, bundle contents and exclusions, and the Raspberry Pi validation
procedure.
