# Task 10 — Operations foundation

## Status

**Definition. Implementation follows in separate commits on
`task-010-operations`.**

No pull request is opened, nothing is merged, and no Raspberry Pi is accessed by
this task.

## Authoritative definition

> **Operations — Create systemd service, log rotation, backup and diagnostic
> scripts.**

The corresponding work-breakdown items:

> **OPS-01 — Create systemd units.**
> Output: API and worker service templates with restart policy.
>
> **OPS-02 — Create backup and diagnostic scripts.**
> Output: Database backup, restore test and support bundle.

The project plan additionally requires that services restart automatically with
sensible backoff; that database integrity checks and backups are scheduled; that
logs are useful and bounded; that operational logs are structured with
timestamp, service, severity, event ID and error code; that a diagnostic bundle
carries logs, a configuration summary and recent health data **without** copying
the media archive; that faults are diagnosable from the dashboard and logs with
no monitor and keyboard attached; that the database and configuration are backed
up; that backup and restore are tested; and that unnecessary SD-card writes are
avoided.

## Starting point

| Item | Value |
| ---- | ----- |
| Branch | `main` |
| HEAD / `origin/main` | `0ef3d04047faef119399c46182103e6f478b8a3a` |
| Working tree | clean |
| Baseline `ruff check .` | passed |
| Baseline `mypy src` | passed (39 source files) |
| Baseline `pytest` | 633 passed |

## Current repository reality

The roadmap wording predates the repository. Task 10 does **not** start from a
deployment without a service.

Already present before this task:

- **A real production systemd service.** `scripts/deploy/mgo.service.template`
  renders to `/etc/systemd/system/mgo.service` and already provides the
  dedicated non-login `mgo:mgo` runtime identity, `SupplementaryGroups=video`,
  direct execution of `.venv/bin/uvicorn`, `MGO_CONFIG_PATH`, `Restart=on-failure`,
  `RestartSec=5`, `TimeoutStopSec=20`, `WantedBy=multi-user.target`,
  `StateDirectory=`/`LogsDirectory=` provisioning at `0750`, `UMask=0027`,
  empty `CapabilityBoundingSet=`/`AmbientCapabilities=`, `NoNewPrivileges=yes`,
  `ProtectSystem=strict`, `ProtectHome=yes`, `PrivateTmp=yes`,
  `RestrictNamespaces=yes` and a restricted `RestrictAddressFamilies=`.
- **An installer and a verifier.** `scripts/deploy/install-service-identity.sh`
  provisions the account, group, filesystem layout and unit, is idempotent,
  supports `--dry-run`, never overwrites an existing production configuration,
  backs up a replaced unit, and refuses to install a unit against a relocated
  virtual environment. `scripts/deploy/verify-service-identity.sh` is read-only.
- **Canonical deployment constants** in `mgo.core.config` as `PurePosixPath`
  values (`SYSTEM_CONFIG_PATH`, `SYSTEM_STATE_DIRECTORY`,
  `SYSTEM_DATABASE_PATH`, `SYSTEM_LOG_DIRECTORY`, …), shared by the production
  example configuration, the unit template, the scripts, the documentation and
  `tests/test_service_identity.py`.
- **A WAL SQLite database** with an explicit numbered migration runner
  (`CURRENT_SCHEMA_VERSION = 2`), a `schema_migrations` version authority, and a
  read-only health check using `PRAGMA quick_check(1)`.
- **Read-only status endpoints** (`/`, `/version`, `/health`,
  `/database/status`, `/camera/status`, `/camera/preview/status`,
  `/motion/status`, `/notifications/status`) that serve cached monitor state and
  perform no work per request.
- **journald as the log destination.** The application logs through
  `logging` to stdout/stderr; the unit sets `PYTHONUNBUFFERED=1`. No file
  handler is configured anywhere. `/var/log/garden-observatory` exists and is
  provisioned, but nothing writes to it.

What is **absent**: any backup, any restore verification, any retention, any
scheduled operations job, any log-rotation policy, any diagnostic bundle, any
structured operational log format, and any operator command-line tooling.

### OPS-01 assessment

| Element | State |
| ------- | ----- |
| API service unit with restart policy | **Already complete.** No material redesign required. |
| Worker service templates | **Intentionally not delivered.** See below. |
| Operations (backup) unit + timer | **Delivered by this task** — real functionality, not a placeholder. |

The existing API unit was audited directive by directive against the Task 10
requirement. `Restart=on-failure`, `RestartSec=5`, `TimeoutStopSec=20` and
`WantedBy=multi-user.target` already express the required restart and boot
policy, and the security sandbox already exceeds what Task 10 needs. **No defect
was found**, so the API unit is left byte-for-byte unchanged. Changing values
purely so that Task 10 appears to have touched the service would be a regression
risk taken for cosmetic reasons.

### Worker-service decision

**No worker unit is created.**

There is no standalone camera, detection, identification or notification worker
executable in the repository. Camera monitoring, database health, motion
analysis and notification publication all run as `asyncio` tasks inside the API
process's lifespan (`src/mgo/api/app.py`), sharing its state objects and its
single camera owner. Nothing exists that could truthfully be run as an
independent service.

Creating a unit now would mean shipping an empty service, a placeholder
executable, a process that sleeps, or a service that imports the API merely to
look functional. Each is a lie told in a `systemd` unit, and each would have to
be undone when the real worker arrives.

OPS-01 is therefore recorded as **satisfied for the current API service and
intentionally incomplete for future workers**. A worker unit will be introduced
only when an actual worker process and lifecycle contract exist. The backup
one-shot service and timer added here are not a substitute: they run real
functionality delivered by this task.

## Missing operations capabilities this task delivers

1. A production-safe SQLite backup tool that works while the API is running.
2. Backup verification and isolated restore testing.
3. Bounded backup retention.
4. A scheduled backup service and timer.
5. A log-rotation policy for MGO-owned file logs.
6. Truthful documentation of journald versus file-log rotation.
7. A bounded, privacy-safe diagnostic support bundle.
8. Structured operational logging for the new tools.
9. Installer and verifier integration.
10. Comprehensive hardware-free tests.
11. Operator documentation and an exact Raspberry Pi validation procedure.

## Selected architecture

A new `src/mgo/operations/` package holds all business logic, so it is
importable, type-checked and testable on the Windows development machine. Shell
wrappers are thin.

```text
src/mgo/operations/
  errors.py            stable error codes and OperationError
  events.py            structured JSON operational events + redaction
  locking.py           bounded, atomic, non-blocking operation lock
  backup.py            SQLite backup, manifest, verify, restore-test, retention
  backup_cli.py        `backup | verify | restore-test | list`
  support_bundle.py    bounded, generated-only diagnostic bundle
  support_bundle_cli.py

scripts/operations/
  backup-database.sh          wrapper for the backup CLI
  create-support-bundle.sh    wrapper for the bundle CLI

scripts/deploy/
  mgo-backup.service.template one-shot backup service
  mgo-backup.timer            daily persistent timer
  garden-observatory.logrotate  policy for /var/log/garden-observatory/*.log
```

Only the Python standard library and existing dependencies are used. No backup
package, no structured-logging framework, no archive library beyond `tarfile`.

### Rejected alternatives

| Alternative | Why rejected |
| ----------- | ------------ |
| `cp mgo.db` (file copy) | Not consistent under WAL. A copy taken while the service is writing can miss committed pages held in the WAL, producing a backup that verifies as "a file" but is not the database. |
| Stop `mgo.service` for a cold copy | Backup must not cost availability. SQLite's online backup API removes the need entirely. |
| `VACUUM INTO` | Writes to the source connection's context and rewrites page layout; the requirement forbids rewriting or vacuuming the production database to take a backup. |
| `sqlite3 .dump` to SQL text | Requires the `sqlite3` binary, produces a text artefact that cannot be integrity-checked as a database, and is far larger. |
| `.backup` via a shell wrapper | Puts business logic in Bash where it cannot be unit-tested on Windows. |
| Business logic in shell scripts | Untestable in CI, no typing, no structured error codes, fragile quoting. |
| `structlog` / `python-json-logger` | A new runtime dependency for what is ~60 lines of `json.dumps`. |
| Copying files from disk into the bundle | Every privacy exclusion then becomes a filter that must be perfect. Generating every member in memory makes the exclusions structural instead. |
| An operations API endpoint / dashboard button | Backup and bundle generation are privileged operator actions; the LAN API is unauthenticated. |
| New `mgo.toml` settings for schedule/retention | These are deployment concerns. The schedule belongs to the timer and retention to the unit's arguments. |

### Backup design decisions

- **Consistency** — `sqlite3.Connection.backup()` from a `mode=ro` source
  connection. One consistent snapshot while the service keeps writing. The
  source is never checkpointed, vacuumed, rewritten or mode-changed.
- **Self-contained artefact** — the *destination* copy inherits WAL from the
  copied header and would leave `-wal`/`-shm` sidecars, so a single published
  `.db` file would not be the whole database. The destination is therefore set
  to `journal_mode=DELETE` before closing, which checkpoints it into one file.
  This touches only the copy. The manifest records the observed value in
  `journal_mode_of_backup`, so the artefact is described truthfully.
- **Atomic publication** — build in a temporary file in the destination
  filesystem, validate, checksum, `fsync` where the platform supports it, then
  `os.replace()`. A failed backup never leaves a file whose name looks complete.
  An existing final name is never overwritten.
- **Naming** — `mgo-<YYYYMMDD>T<HHMMSS>Z.db` plus
  `mgo-<…>Z.manifest.json`; UTC, deterministic, lexically sortable, no secret,
  username or host path.
- **Symlink policy** — a source database that is a symlink is **refused**
  (`BACKUP_SOURCE_UNAVAILABLE`). There is no override flag. Following one would
  let a symlink swap redirect the read outside the approved root.
- **Retention** — runs only after a validated, published backup; keeps the
  newest N complete sets (`--keep`, default 14); deletes `.db` and manifest as
  one set; only ever touches files matching the strict backup name pattern;
  never deletes the newest set; failure is reported without invalidating the
  backup just taken.
- **Concurrency** — an atomic `O_CREAT|O_EXCL` lock file in the backup
  directory. Non-blocking, single attempt, no waiting. Stale reclamation is
  age-based only (default 6 hours) and never PID-based, because a PID is not
  portable and its absence is not proof of death. The application database is
  never used as the lock.
- **No production restore command.** `restore-test` proves recoverability into
  an isolated temporary directory; production restore remains an explicit
  operator disaster-recovery procedure documented in `docs/Operations.md`.

### Diagnostic bundle design decisions

- **Every member is generated in memory**; no file is ever read from disk and
  added to the archive. The database, WAL/shm sidecars, media, raw configuration,
  SSH material and Git files are therefore excluded *structurally* rather than
  by a filter that could be incomplete. No archive member can be a symlink, an
  absolute path or contain `..`, because member names are constants.
- **API collection** is loopback-only (`127.0.0.1`), GET-only, read-only
  endpoints only, with explicit short timeouts, no retries and a bounded read.
  No mutation endpoint is called: no capture, no preview start/stop, no
  notification publication.
- **Configuration summary** is derived from the typed `MGOConfig`, not from the
  raw TOML. Values are whitelisted by role. Unknown/future keys are listed by
  name with their values redacted, so a field added later defaults to redacted.
  Suspicious key names (`secret`, `token`, `password`, `credential`, `private`,
  `api_key`, `key`) are always redacted. Paths appear verbatim only when they
  exactly equal a canonical MGO deployment constant; anything else is reported
  as a role plus existence/writability.
- **Bounds** on endpoint response size, journal lines and bytes, command output,
  subprocess timeout, archive member count and total archive size, so no source
  can fill the SD card.
- **Outcomes** `complete` / `partial` / `failed`, with exit codes `0` / `1` /
  `2`. One failed source never prevents the rest of the bundle.

### Logging decisions

journald stays the primary runtime log. **No always-on duplicate file logger is
added** — it would double SD-card writes, create two authorities and let the two
records diverge. The logrotate policy therefore covers MGO-owned `*.log` files
under `/var/log/garden-observatory` and is written to be safe and idempotent
when the glob matches nothing, which is the current state. It does not, and
cannot, rotate journald; host journald retention is a host-level policy that
this task deliberately does not change for every service on the Pi.

Structured JSON operational events are introduced **for the Task 10 tools
only**. Application-wide structured logging is explicitly deferred: mass-editing
unrelated runtime modules for stylistic consistency is out of scope and is
recorded as a known limitation.

## Scope

### In scope

- `src/mgo/operations/` (errors, events, locking, backup, support bundle, CLIs);
- `SYSTEM_BACKUP_DIRECTORY` in `mgo.core.config`;
- `scripts/operations/` wrappers;
- `mgo-backup.service.template`, `mgo-backup.timer`, the logrotate policy;
- installer and verifier integration;
- `.gitignore` entries for generated artefacts;
- tests;
- `docs/Operations.md` plus `README.md`, `scripts/README.md`,
  `docs/Service-Identity.md` and `docs/Database.md` cross-references.

### Explicit non-goals

- a second API service, a replacement for `mgo.service`, or a competing
  deployment path;
- a fictional worker unit;
- any new API endpoint, dashboard control or bundle download route;
- database schema changes, migrations, or any write to production data;
- media backup, media reconciliation, image retention policy;
- automatic production restore;
- global host journald policy changes;
- new application configuration fields;
- authentication, reverse proxy, TLS, VPN, public exposure, remote/cloud backup;
- camera capture, preview control, motion analysis or notification delivery;
- Task 11 (camera simulator) and Task 12 (acceptance record).

## Compatibility requirements

Every existing endpoint keeps its exact path, fields, types, units and status
vocabulary: `/`, `/version`, `/health`, `/database/status`, `/camera/status`,
`/camera/capture`, `/camera/preview/status`, `/camera/preview/start`,
`/camera/preview/stop`, `/camera/preview/stream`, `/preview`, `/dashboard`,
`/motion/status`, `/notifications/status`, `/captures`, `/captures/{id}`,
`/observations`.

`mgo.service` is unchanged. `mgo.toml` gains no field. The schema stays at
version 2 and no migration is added. `pyproject.toml` gains no dependency.

## Security model

- Backups and bundles are never world-readable: backup files `0640`, the backup
  directory `mgo:mgo 0750`, support bundles `0600`.
- No secret, token, password, credential, private key, environment dump, raw
  configuration, database file, media file or media filename enters any
  artefact.
- No external network access; the bundle talks only to `127.0.0.1`.
- No `shell=True`; every subprocess uses an argument array with an explicit
  timeout and bounded captured output.
- No archive member may be absolute, contain `..`, or be a symlink — guaranteed
  by generating every member.
- Output paths are validated so a caller cannot escape the approved root, and
  `restore-test` refuses to target the production database directory.
- The backup service runs as `mgo:mgo` with no capabilities and
  `NoNewPrivileges=yes`; nothing in Task 10 runs the application as root.
- A failed backup exits non-zero and leaves no apparently complete artefact.
  API availability never depends on backup success.

## Rollback model

Reversible with no data loss.

- **Before merge** — return to `main`; nothing on `main` is touched.
- **After a future merge** — disable and remove the backup timer and service,
  remove `/etc/logrotate.d/garden-observatory`, revert the Task 10 commits,
  re-run the installer from the reverted code, and verify `mgo.service`. The API
  unit needs no restart because it never changed.

Existing backups are left intact unless the operator explicitly removes them.
No rollback step deletes the production database, captures or support bundles,
resets history, force-pushes, or overwrites the production configuration. No
automatic rollback script is provided, deliberately: a script that deletes
backups is exactly the thing that must not exist.

## Raspberry Pi validation plan

**Not executed in this task.** No Raspberry Pi is accessed, modified or deployed
to. The exact procedure Matthew runs later lives in
[`docs/Operations.md`](../Operations.md). It begins with a clean-tree check
before any branch change, records the production database SHA-256, size, schema
version, observation count, capture count, service PID, restart count and
preview state before and after, proves the database is unchanged, restores a
pre-existing preview after any intentional restart, uses a unique Task 10
validation backup, deletes no existing backup, rotates only a synthetic log file
created for the test, and confirms the support bundle contains no private
content before it is copied off the Pi.

Backups are **not** described as working until that validation has been
performed and confirmed by Matthew.
