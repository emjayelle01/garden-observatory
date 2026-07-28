# MGO Operations

How Matt's Garden Observatory is backed up, how its logs are bounded, and how a
fault is diagnosed without attaching a monitor and keyboard to the Raspberry Pi.

> **Status.** Everything here is implemented and covered by automated tests on
> the development machine. The Raspberry Pi validation in
> [§13](#13-raspberry-pi-validation) has **not yet been performed**. Until
> Matthew has run it and confirmed the result, treat backups as *configured*,
> not as *proven on the Pi*.

## 1. Scope

Task 10 delivers the operations foundation:

- a consistent SQLite backup that runs while the API keeps serving, **paired
  with a snapshot of the production configuration**;
- backup verification, isolated restore testing and bounded retention;
- a scheduled backup service and timer;
- a log-rotation policy for MGO-owned file logs;
- a bounded, privacy-safe diagnostic support bundle;
- structured operational logging for the tools above;
- installer and verifier integration.

It deliberately does **not** add an API endpoint, a dashboard control, a
configuration field, a database migration, media backup, remote/cloud backup or
an automatic production restore.

## 2. Service architecture

### 2.1 The API service is unchanged

`mgo.service` already existed before this task and was audited directive by
directive against the Task 10 requirement. It already provides:

| Requirement | Existing directive |
| ----------- | ------------------ |
| Automatic restart | `Restart=on-failure` |
| Sensible backoff | `RestartSec=5` |
| Bounded shutdown | `TimeoutStopSec=20` |
| Start at boot | `WantedBy=multi-user.target` |
| Unprivileged runtime | `User=mgo`, `Group=mgo`, no capabilities |
| Camera access | `SupplementaryGroups=video` |

**No defect was found, so the unit was not edited.** It is byte-for-byte
identical to the version on `main`, and a test
(`test_the_api_unit_is_byte_for_byte_unchanged_from_main`) asserts that. Editing
a working production unit so that a task appears to have touched the service
would be a regression risk taken for cosmetic reasons.

The API therefore needs **no restart** to adopt Task 10.

### 2.2 Why there is no worker service

The original plan anticipated "API and worker service templates". There is no
worker unit, deliberately.

Camera monitoring, database health, motion analysis and notification publication
all run as `asyncio` tasks inside the API process's lifespan
([`src/mgo/api/app.py`](../src/mgo/api/app.py)), sharing its state objects and
its single camera owner. **No standalone worker executable exists.** A unit
written today would have to be an empty service, a placeholder binary, a process
that sleeps, or a service that imports the API merely to look functional. Each
is a lie told in a systemd unit, and each would have to be undone when a real
worker arrives.

OPS-01 is recorded as **satisfied for the API service and intentionally
incomplete for future workers**. A worker unit will be added when an actual
worker process and lifecycle contract exist — not before.

`mgo-backup.service` is not a substitute: it runs real functionality delivered
by this task.

### 2.3 Units and files

| Path | What it is |
| ---- | ---------- |
| `/etc/systemd/system/mgo.service` | The API. Unchanged by Task 10. |
| `/etc/systemd/system/mgo-backup.service` | One-shot backup job. |
| `/etc/systemd/system/mgo-backup.timer` | Daily schedule. |
| `/etc/logrotate.d/garden-observatory` | Rotation policy for MGO `*.log` files. |
| `/var/backups/garden-observatory` | Backup root, `mgo:mgo`, `0750`. |

## 3. Backup architecture

### 3.0 Complete backup sets

A **recovery set is three files**, all sharing one UTC timestamp:

```text
mgo-20260728T023000Z.db              the database snapshot
mgo-20260728T023000Z.config.toml     the production configuration snapshot
mgo-20260728T023000Z.manifest.json   the manifest that binds them
```

The configuration is part of the set because the plan requires the database
*and* the configuration to be backed up, and because a database restored onto a
machine whose configuration is gone is only half a recovery: an operator would
have to reconstruct `/etc/garden-observatory/mgo.toml` from memory during an
incident.

**A set is complete only when all three files are present and mutually
consistent.** The manifest is written **last** and is the completion marker — if
publication fails part-way, the recovery files already written are removed, so
no manifest can survive claiming a set that is not there.

#### Which configuration is captured

The application's existing resolution rules, and no others:

1. an explicit `--config`;
2. `MGO_CONFIG_PATH`;
3. the tracked development default.

The scheduled backup unit sets both `MGO_CONFIG_PATH` and `--config`, so in
production this is always `/etc/garden-observatory/mgo.toml`. One answer serves
both purposes — locating the database and choosing the configuration — so a set
can never pair a database with a configuration that does not belong to it.

#### Safety and secrecy

> **The configuration snapshot is a protected disaster-recovery artefact, not a
> diagnostic one.** It may contain credentials that future transports add, so it
> is the most sensitive file in the backup directory. It is **never** included in
> a support bundle.

The capture refuses to proceed when the configuration:

- does not exist, or is not a regular file;
- is a **symlink** — the target could be changed between unattended runs;
- cannot be read;
- exceeds **1 MiB** (`MAX_CONFIGURATION_BYTES`) — MGO's configuration is a few
  kilobytes, and an unattended job must not read an unbounded file;
- **changes while it is being copied** — the snapshot would be neither the old
  file nor the new one.

The file is validated through its own file descriptor after opening rather than
through the path, so the thing that was checked is the thing that was read.

Bytes are preserved **exactly**: the file is never parsed, normalised or
rewritten. A configuration round-tripped through a TOML parser would lose its
comments and its ordering, and a restore would hand the operator something
merely equivalent rather than identical.

Contents never appear in a structured event, the manifest, the command summary
or a support bundle. The manifest records only four safe descriptors:

```text
configuration_source_name     a basename, never a path
configuration_filename
configuration_size_bytes
configuration_sha256
```

The snapshot is written `0640`, from a temporary file that is `0600` before any
bytes are written — so the contents are never briefly group-readable.

### 3.1 Consistency without stopping the service

The production database runs in **WAL mode** with the API writing to it
continuously. Two obvious approaches are both wrong:

- **`cp mgo.db`** captures the main database file but not the committed pages
  still sitting in the write-ahead log. The result looks like a database and is
  missing recent transactions.
- **Stopping the service** for a cold copy trades availability for consistency,
  which is unnecessary.

MGO uses SQLite's **online backup API** (`sqlite3.Connection.backup`), driven
from a `mode=ro` source connection. That yields one transactionally consistent
snapshot while the API keeps reading and writing, and the read-only connection
means the tooling cannot modify, checkpoint, vacuum or re-mode the production
database even by accident.

The whole copy is taken in a single step rather than incrementally: the
observation database is small, one step gives the strongest consistency
guarantee, and it removes any possibility of a busy writer restarting the copy
repeatedly.

### 3.2 The backup is a single self-contained file

This detail is load-bearing and was measured, not assumed.

The *destination* copy inherits WAL journalling from the copied header, so
closing it leaves `-wal` and `-shm` sidecars beside the backup. A single
published `.db` file would then not be the whole database, and its checksum
would describe only part of its contents.

The copy is therefore switched to `journal_mode=DELETE` before it is closed,
which checkpoints everything into one file. This happens entirely on the copy;
the source is never touched. The manifest records the observed value in
`journal_mode_of_backup`, so the artefact is described truthfully.

### 3.3 Atomic publication

1. build into a temporary file **in the destination filesystem**, so the final
   rename is atomic rather than a cross-device copy;
2. open the copy independently and prove it is a sound, compatible database;
3. checksum it and flush it;
4. `os.replace()` it into its final name;
5. write the manifest the same way;
6. only then apply retention.

A failure at any point before step 4 leaves nothing but a temporary file, which
is removed. **A failed backup never leaves a file whose name claims it
succeeded**, and an existing completed backup is never overwritten.

### 3.4 Naming

```text
mgo-20260728T023000Z.db
mgo-20260728T023000Z.manifest.json
```

UTC, second precision, no separators a shell would find awkward. The format
sorts lexically in chronological order, which is what makes retention a simple
"keep the newest N". Names carry no secret, username or host path.

### 3.5 The manifest

Every successful recovery set has a JSON manifest recording:

| Field | Meaning |
| ----- | ------- |
| `format_version` | Manifest schema version (currently `1`). |
| `created_at` | UTC ISO 8601 start time. |
| `application` / `application_version` | Which build took it. |
| `source_database_name` | The source file's **name**, never its path. |
| `backup_filename` | The published file. |
| `backup_size_bytes` | Size, checked on verification. |
| `sha256` | Checksum, checked on verification. |
| `schema_version` / `expected_schema_version` | Recorded and supported schema. |
| `integrity` | `quick_check` verdict at the time of writing. |
| `journal_mode_of_backup` | The copy's journal mode (`delete`). |
| `table_row_counts` | Row counts for **exactly** the expected tables. |
| `configuration_source_name` | The configuration's **name**, never its path. |
| `configuration_filename` | The snapshot in this set. |
| `configuration_size_bytes` | Size, checked on verification. |
| `configuration_sha256` | Checksum, checked on verification. |

A manifest travels with its set, and a set may be copied somewhere less private
than the Pi. It therefore contains **no** absolute path, configuration
*content*, environment value, username, hostname or database row content. Row
*counts* are included; row *contents* never are.

Only the exact supported `format_version` is accepted. There is deliberately no
forward- or backward-compatibility policy: one would have to define which fields
may be absent and what they default to, and accepting a manifest of another
version means accepting one whose meaning this build cannot actually know.

### 3.6 Validation before publication

Before a set is published the database copy is opened independently and must
pass:

- it is a valid SQLite database;
- `PRAGMA quick_check(1)` reports `ok`;
- a schema version can be read from the `schema_migrations` authority;
- that version is **not newer** than this build supports;
- the expected tables (`schema_migrations`, `observations`, `captures`) exist;
- the manifest checksum matches the final file.

The configuration passes its own checks (§3.0) before anything is published. A
set that fails any of these is not published.

### 3.6.1 What verification actually checks

`verify` is **binding**, not merely structural. Being parseable is not enough:
a manifest that is internally tidy but describes a *different* set must fail.

Structural validation refuses a manifest with missing fields, wrong types,
booleans used as integers, negative sizes or versions, malformed SHA-256 values,
filenames that are not names this tooling produces, source names that are
absolute or contain a directory separator, invalid timestamps, an unsupported
format version, or a `table_row_counts` map that does not cover exactly the
required tables.

Semantic binding then compares every recorded value against the artefact it
claims to describe:

```text
backup_filename            == the actual database filename
configuration_filename     == the actual configuration filename
backup_size_bytes          == the actual database size
configuration_size_bytes   == the actual configuration size
sha256                     == the actual database SHA-256
configuration_sha256       == the actual configuration SHA-256
schema_version             == the actual database schema version
expected_schema_version    == CURRENT_SCHEMA_VERSION
integrity                  == the actual integrity result
journal_mode_of_backup     == the actual backup journal mode
table_row_counts           == the exact actual expected-table counts
```

The row-count comparison is exact and in **both** directions. Comparing only the
keys a manifest happens to carry would let an empty `table_row_counts` object
verify against any database at all.

### 3.7 Retention

- Default **14** complete recovery sets (`--keep`, validated as `1..3650` — the
  same bound the installer applies, so a unit can never be written with a value
  the runtime would reject).
- Runs **only after** a validated set has been published.
- A set's three files are removed together, **manifest first**. That order
  matters: the manifest is the completion marker, so an interrupted deletion
  leaves recognisable orphans rather than a manifest still advertising a set
  whose database has already gone. Every partial deletion is reported.
- Only files matching the strict backup name pattern are ever candidates.
  Support bundles, operator notes and unrelated files sharing the directory are
  never touched. Orphans and in-progress temporary files are not counted as
  backups and are not deleted.
- Retention failure is reported through the exit status and the structured
  output, and **does not invalidate the backup just taken** — that backup
  already exists and is valid.

### 3.8 Concurrency

Overlapping runs are prevented by an atomic lock file
(`.mgo-backup.lock`) in the backup directory, created with `O_CREAT | O_EXCL`.

- **No waiting.** One attempt; a blocked run reports `BACKUP_LOCKED` and exits.
  A backup that queues behind another backup is no more useful than one that
  says so, and a job that never blocks cannot pile up behind a stuck
  predecessor.
- **Stale reclamation is age-based only** (default 6 hours), **never PID-based**.
  A PID is not portable, can be reused, and "I cannot see that process" is not
  proof it is dead. The threshold is far beyond any plausible backup duration,
  so reclaiming a lock only ever recovers from a power cut or a `SIGKILL`.
- **Release only removes a lock still owned by this run**, checked by a token
  written at acquisition. Without that, a process whose lock had already been
  reclaimed would delete the new owner's lock on its way out.

The application database is never used as the lock.

### 3.9 Where backups live, and why not under `/var/lib`

```text
/var/backups/garden-observatory     mgo:mgo  0750
```

The backup root sits **outside** `/var/lib/garden-observatory` on purpose. A
backup kept inside the tree it protects is lost to the same `rm -rf`, the same
failed migration and the same accidental `StateDirectory=` cleanup as the
original. A separate root also means moving backups to other storage later is a
mount, not a rewrite.

Backups and manifests are `0640`. Nothing is world-readable or world-writable.

## 4. Scheduling

### 4.1 The timer

```text
OnCalendar=*-*-* 02:30:00     # local time — Africa/Johannesburg on the Pi
RandomizedDelaySec=30m
Persistent=true
AccuracySec=1m
```

02:30 local is well after dusk (no capture or motion activity competing for the
SD card) and well before dawn. The randomised delay avoids writing to the same
SD-card region at exactly the same wall-clock second every day for years.

`Persistent=true` matters because a Raspberry Pi is not always powered: a missed
backup is caught up once the Pi is back, rather than silently skipped.

### 4.2 Installing the timer does not run a backup

`Persistent=true` makes systemd catch up a run it believes was missed. With no
timestamp file and today's 02:30 already past, enabling a new timer would fire a
backup **immediately**. Installing a schedule must never be the thing that
starts a backup, so the installer creates
`/var/lib/systemd/timers/stamp-mgo-backup.timer` *before* enabling the timer, as
if it had just run.

The installer enables and starts the **timer**; it never starts the backup
service. Take the first backup by hand when convenient.

### 4.3 Failure isolation

A failed backup:

- leaves `mgo.service` running and untouched;
- does not trigger any restart of the API;
- exits non-zero with a stable error code in the journal;
- leaves no apparently complete corrupt backup.

The unit has no `Requires=`, `Wants=` or `BindsTo=` on `mgo.service` — only
`After=` for ordering. A backup is most valuable when the API is unwell, so it
must run whether or not the API is up. API availability never depends on backup
success.

### 4.4 The unit's writable paths

```text
ReadWritePaths=/var/backups/garden-observatory /var/lib/garden-observatory/db
```

The backup root is obvious. The **database directory is not**, and is
load-bearing: SQLite cannot read a WAL database without shared-memory access, so
a reader must be able to create or map the `-shm` file, which requires the
directory holding the database to be writable *even for a purely read-only
reader*. Without it, the backup would fail with "unable to open database file"
against a perfectly healthy database.

This grants **filesystem** write access, not **database** write access: the
connection is still opened through SQLite's `mode=ro` URI, so no statement it
issues can modify, checkpoint or vacuum production data.

## 5. Operator commands

Run these on the Pi from the application root.

```bash
cd /opt/garden-observatory
```

### 5.1 Take a backup

```bash
scripts/operations/backup-database.sh backup
```

No step in the normal backup procedure requires stopping `mgo.service`.

### 5.2 List backups

```bash
scripts/operations/backup-database.sh list
```

Reports complete sets newest first with their verification state, plus orphaned
database files, orphaned **configuration snapshots**, orphaned manifests and
in-progress temporary files — each reported separately, because a database with
no configuration is a different problem from a manifest with no database, and
never treated as backups.

### 5.3 Verify a recovery set

```bash
scripts/operations/backup-database.sh verify /var/backups/garden-observatory/mgo-20260728T023000Z.db
```

Read-only. Names the database file; the configuration and manifest are located
beside it. Performs the full structural and binding validation described in
§3.6.1.

### 5.4 Restore-test a recovery set

```bash
scripts/operations/backup-database.sh restore-test /var/backups/garden-observatory/mgo-20260728T023000Z.db
```

**Verifies the complete set first**, and stops if verification fails — a missing
manifest, a missing configuration snapshot, a checksum mismatch or a
semantically inconsistent manifest all fail *before a single byte is copied*.

It then copies both artefacts into an isolated directory under fixed names:

```text
restored.db
restored-mgo.toml
```

and checks database integrity, schema compatibility, expected tables, exact row
counts, the restored configuration's checksum, and that neither source changed
during the test.

The restored configuration is **checked, never activated**: nothing is pointed
at it, no API is started and no production path is written.

If either target file already exists in a caller-supplied `--work-directory`,
the test refuses before writing rather than overwriting it. Add `--preserve` to
keep the restored copies for inspection after a failure.

### 5.5 Generate a support bundle

```bash
scripts/operations/create-support-bundle.sh --output-directory /tmp
```

### 5.6 systemd and journal

```bash
systemctl status mgo-backup.timer
```

```bash
systemctl list-timers mgo-backup.timer
```

```bash
journalctl -u mgo-backup.service
```

```bash
journalctl -u mgo.service
```

```bash
journalctl --disk-usage
```

## 6. Restore: the deliberate boundary

**There is no `restore` command, and that is a design decision.**

- `restore-test` proves a backup **can** be recovered. It is safe, isolated and
  runs unattended.
- **Production restore is an explicit operator disaster-recovery procedure.**

Restoring over a live production database has consequences a script cannot
weigh: it needs the service stopped, a judgement that the failure is really
corruption rather than a full disk or a permissions problem, and a copy of the
damaged database kept for analysis. A one-line command that silently overwrites
the observation history is exactly the tool that turns a recoverable incident
into an unrecoverable one.

### 6.1 Disaster-recovery outline

Read this fully before starting. Nothing below is automated.

1. **Stop making it worse.** Do not run a backup, and do not delete anything.

   ```bash
   sudo systemctl stop mgo.service
   ```

2. **Preserve the evidence.** Keep the damaged database — it may be partly
   recoverable and it explains what happened.

   ```bash
   sudo -u mgo cp -a /var/lib/garden-observatory/db/mgo.db /var/lib/garden-observatory/db/mgo.db.damaged
   ```

3. **Choose a backup and prove it before trusting it.**

   ```bash
   scripts/operations/backup-database.sh list
   ```

   ```bash
   scripts/operations/backup-database.sh restore-test /var/backups/garden-observatory/<chosen>.db
   ```

4. **Put it in place**, as the runtime account, with the service stopped. Remove
   the stale WAL sidecars: they belong to the old database and would be
   misapplied to the restored one.

   ```bash
   sudo -u mgo rm -f /var/lib/garden-observatory/db/mgo.db-wal /var/lib/garden-observatory/db/mgo.db-shm
   ```

   ```bash
   sudo -u mgo cp /var/backups/garden-observatory/<chosen>.db /var/lib/garden-observatory/db/mgo.db
   ```

5. **Restore the configuration too, if it was lost.** The set contains it. Diff
   before replacing — the live file may be newer than the snapshot.

   ```bash
   diff /etc/garden-observatory/mgo.toml /var/backups/garden-observatory/<chosen>.config.toml
   ```

   ```bash
   sudo install -o root -g mgo -m 0640 /var/backups/garden-observatory/<chosen>.config.toml /etc/garden-observatory/mgo.toml
   ```

6. **Start and confirm.**

   ```bash
   sudo systemctl start mgo.service
   ```

   ```bash
   curl -s http://127.0.0.1:8080/database/status
   ```

   Expect `status: healthy` and the schema version the manifest recorded. The
   application applies any pending migrations at startup.

7. **Only once the restore is confirmed good**, decide what to do with
   `mgo.db.damaged`. Observations recorded between the backup and the failure
   are lost; that gap is the backup interval.

## 7. Logging

### 7.1 journald is the primary log

MGO logs to stdout/stderr and systemd captures that into the **journal**. That
is where production logs actually live:

```bash
journalctl -u mgo.service
journalctl -u mgo-backup.service
journalctl --disk-usage
```

Journal retention is a **host-level** setting in `/etc/systemd/journald.conf`
(`SystemMaxUse=`, `MaxRetentionSec=`), and it governs **every service on the
machine**. Task 10 deliberately does not change it: quietly shrinking the whole
host's log retention from an application deployment would be a surprising,
system-wide change. Inspect the current policy with:

```bash
systemd-analyze cat-config systemd/journald.conf
```

### 7.2 No duplicate file logger was added

It would have been easy to add an always-on file handler purely to give
logrotate something to rotate. That was rejected: it would double SD-card
writes, create two authorities for the same events, complicate incident review
and let the two records diverge.

### 7.3 What the logrotate policy does and does not do

`/etc/logrotate.d/garden-observatory` rotates **only**:

```text
/var/log/garden-observatory/*.log
```

- daily, keeping **14** rotations (the same window as backup retention);
- `compress` with `delaycompress`;
- `dateext` (`mgo.log-20260728` rather than `mgo.log.7`);
- `create 0640 mgo mgo` — never world-readable;
- `su mgo mgo`, required because the log directory is owned by `mgo:mgo`;
- `missingok` and `notifempty`, so it is a safe no-op when the glob matches
  nothing.

**It does not rotate the journal.** logrotate cannot, and this policy does not
pretend to. Because journald is primary there may currently be **no `*.log`
files at all** — that is the expected state, not a fault. The policy is
installed now so that it is correct both today and if a file-based log is ever
added.

`copytruncate` is deliberately not used: it is only needed when a writer holds
the file open and cannot be told to reopen it, and it trades a race (lines
written between the copy and the truncate are lost) for that convenience.
Nothing currently writes these files. If a file logger is added, the correct fix
is a `postrotate` reopen signal, not `copytruncate`.

## 8. Structured operational events

The Task 10 tools emit **one JSON object per line** to stderr, captured by
journald. Every event carries six required fields, always present and always
first:

```json
{
  "timestamp": "2026-07-28T02:30:04.117221+00:00",
  "service": "mgo-backup",
  "severity": "INFO",
  "event_id": "backup.completed",
  "message": "The backup was validated and published.",
  "error_code": null,
  "backup_filename": "mgo-20260728T023000Z.db",
  "backup_size_bytes": 49152,
  "duration_ms": 303
}
```

- `timestamp` is UTC ISO 8601.
- `severity` is a closed vocabulary: `DEBUG`, `INFO`, `WARNING`, `ERROR`,
  `CRITICAL`.
- `event_id` is stable and machine-readable.
- `error_code` is `null` for success and a stable code for a known failure, so
  "did anything fail?" is a null check rather than a string comparison.

Values whose field names look sensitive (`secret`, `token`, `password`,
`credential`, `private`, `api_key`, `key`, `auth`, …) are redacted. No event
carries a raw environment dump, full configuration, database row or media
filename. Emission never raises and never produces a line that is not valid
JSON, even for a message containing newlines, quotes or non-ASCII text.

### 8.1 Scope of structured logging

Structured events are introduced **for the Task 10 tools only**
(`mgo-backup`, `mgo-support-bundle`). The rest of the application still uses
Python's `logging` to the journal.

Application-wide structured logging is **explicitly deferred**: mass-editing
unrelated runtime modules for stylistic consistency was out of scope for this
task. Uvicorn's access-log contract is unchanged.

## 9. Diagnostic support bundle

### 9.1 What it is

```text
mgo-support-<UTC timestamp>.tar.gz     mode 0600
```

A bounded archive suitable for sending to a trusted support engineer, so a fault
can be diagnosed without attaching a monitor and keyboard to the Pi.

### 9.2 Contents

| Member | Source |
| ------ | ------ |
| `manifest.json` | Every other member's name, size and SHA-256. |
| `generation-summary.json` | Outcome, files included, files skipped. |
| `application-identity.json` | `GET /` |
| `application-version.json` | `GET /version` |
| `health.json` | `GET /health` |
| `database-status.json` | `GET /database/status` |
| `camera-status.json` | `GET /camera/status` |
| `preview-status.json` | `GET /camera/preview/status` |
| `motion-status.json` | `GET /motion/status` |
| `notifications-status.json` | `GET /notifications/status` |
| `service-status.json` | A reviewed list of `systemctl show` properties. |
| `journal.log` | A bounded slice of the MGO unit's journal. |
| `journal-disk-usage.txt` | `journalctl --disk-usage` |
| `configuration-summary.json` | A safe summary — never the raw TOML. |
| `storage-summary.json` | Aggregates only: sizes and counts. |
| `errors.json` | Every source that could not be collected. |

### 9.3 Why nothing is copied from disk

**Every member is generated in memory.** No file is ever read from disk and
added to the archive.

That is what makes the privacy guarantees structural rather than aspirational. A
bundle built by *copying* files needs an exclusion filter that stays perfect
forever — one that remembers `mgo.db` and also `mgo.db-wal`, `mgo.db-shm`, every
image extension, `.ssh`, `.git`, the raw TOML, and whatever the next task adds.
A bundle built only from computed values cannot include those things at all.

For the same reason no archive member can be a symlink, an absolute path or
contain `..`: member names are constants in the source. Ownership is zeroed, so
no real account name travels with the archive.

### 9.4 Exclusions

A bundle never contains the database or its `-wal`/`-shm` sidecars, any JPEG,
video or audio file, any media **filename**, the raw production configuration,
an environment dump, SSH keys, `known_hosts`, `authorized_keys`, shell history,
Git credentials, Git configuration, repository remotes, tokens, passwords,
private keys, cookies or browser data. Tests inspect generated archive members
and their bytes to prove it.

> **The configuration snapshot taken by a backup is never included.** Adding the
> configuration to recovery sets (§3.0) deliberately did **not** add it to
> bundles: a recovery set is protected storage on the Pi, while a bundle is a
> file intended to be handed to someone else. The bundle still carries only the
> redacted *summary* described in §9.5.

### 9.5 Redaction

The configuration summary is built from the **typed** configuration, so only
fields this build knows about can appear, each chosen by hand. Beyond that:

- **Unknown sections and keys** are named with their values withheld, so a
  setting added by a future task defaults to redacted rather than exposed.
- **Suspicious key names** are redacted regardless of value — a `password` whose
  value is `None` is still redacted, so the presence of a credential cannot be
  inferred from whether a field was withheld.
- **Paths** appear verbatim only when they exactly equal a canonical, documented
  MGO deployment location. Anything else — a developer checkout, a relocated
  data directory — is reported by role plus existence and writability.
- `systemctl show` output is filtered to a reviewed property list, because
  `systemctl show` without `--property` dumps the unit's whole environment
  block.

### 9.6 Bounds

| Limit | Value |
| ----- | ----- |
| Endpoint timeout | 5 s, no retries |
| Endpoint response | 256 KiB |
| Subprocess timeout | 15 s |
| Command output | 256 KiB |
| Journal window | previous 24 hours |
| Journal lines | 2,000 |
| Journal bytes | 2 MiB |
| Storage entries inspected | 20,000 per directory |
| Storage traversal depth | 8 levels |
| Archive members | 64 |
| Archive bytes | 8 MiB (checked before **and** after compression) |

A misbehaving source cannot fill the SD card. The journal slice is scoped to the
MGO unit only — the whole system journal would carry other services' logs and
other users' activity off the Pi.

#### Bounded storage aggregation

Directory totals are gathered by an explicitly bounded iterative walk, not by
`rglob`. The captures directory is flat today, and the implementation must not
depend on that remaining true — nor spend minutes stat-ing a media archive on a
device that is already unwell.

- **Symlinked directories are never descended.** A link into `/` would otherwise
  turn a capture-directory scan into a whole-filesystem walk, and a link loop
  would run forever.
- **Symlinked files are counted but never stat-ed through**, so the size of a
  target outside the approved directory is never read.
- Files that vanish mid-walk and directories that cannot be read are skipped.
- Hitting a bound returns the counts gathered so far with `truncated: true` and
  the limit that stopped it, adds a diagnostic error so the bundle outcome
  becomes **partial**, and never fails the bundle.
- No filename is ever emitted, truncated or not.

### 9.7 Network boundary

Collection is **loopback only**, and enforced rather than assumed:

- the base URL must use `http`;
- the host must be a **literal** loopback address — `127.0.0.1` or `::1`.
  `localhost` is deliberately **refused**: resolving it is a DNS lookup governed
  by `/etc/hosts`, `nsswitch.conf` and the resolver, none of which this tooling
  controls. A literal address needs no resolution, so there is nothing to
  redirect;
- user information, query strings and fragments are refused, so credentials
  cannot be smuggled into a URL that ends up in a log and endpoint paths stay
  fixed by the reviewed table.

Validating the URL alone is not sufficient, so the HTTP client is built
explicitly:

- **proxies are disabled.** `urllib`'s default opener reads
  `HTTP_PROXY`/`HTTPS_PROXY`/`ALL_PROXY`; on a machine with a proxy configured, a
  "loopback only" request would have been sent to it — off the machine entirely;
- **redirects are never followed.** A 3xx is recorded as a failed source. Without
  this, a local service could redirect a diagnostic request to an external host
  *after* the initial URL had validated cleanly.

Only read-only status endpoints are contacted: no capture, no preview start or
stop, no stream, no notification publication. Nothing is uploaded anywhere.

### 9.7.1 Command results are believed only when they succeed

A non-zero `systemctl` or `journalctl` is treated as **unavailable**, not as an
empty success. Reporting `available: true` with no properties after `systemctl`
failed would read as "the service exists and told us nothing", which is a
different and far more alarming diagnosis than "systemctl could not answer".

Each failure retains a bounded, whitespace-collapsed detail from `stderr` (the
explanation is genuinely useful), adds a diagnostic error, and makes the bundle
outcome partial.

### 9.8 Partial success and exit codes

| Exit | Outcome | Meaning |
| ---- | ------- | ------- |
| `0` | `complete` | Every source answered. |
| `1` | `partial` | The bundle exists and is useful; something was unreachable. |
| `2` | `failed` | No bundle was created. |

The distinction between 1 and 2 is the point: a support bundle is most needed
when the system is unwell, so "the API was down" must not be treated as
"generation failed". The bundle describing a dead API is the one worth sending.

### 9.9 Inspect before sending

```bash
tar -tzf mgo-support-20260728T023000Z.tar.gz
```

```bash
tar -xzOf mgo-support-20260728T023000Z.tar.gz configuration-summary.json
```

```bash
tar -xzOf mgo-support-20260728T023000Z.tar.gz generation-summary.json
```

## 10. Error codes

Expected failures carry a stable code **and** a human-readable message. The code
is the contract; the message is the explanation.

| Code | Meaning |
| ---- | ------- |
| `BACKUP_SOURCE_UNAVAILABLE` | Source missing, not a file, in-memory, or a symlink. |
| `BACKUP_LOCKED` | Another backup is running. No second run was started. |
| `BACKUP_DESTINATION_UNWRITABLE` | The backup directory cannot be created or written. |
| `BACKUP_ALREADY_EXISTS` | A backup of that name exists; it is never overwritten. |
| `BACKUP_SQLITE_FAILED` | The online backup itself failed. |
| `BACKUP_INTEGRITY_FAILED` | `quick_check` failed, or row counts disagree. |
| `BACKUP_SCHEMA_INCOMPATIBLE` | No schema history, a newer schema, or a missing table. |
| `BACKUP_MANIFEST_FAILED` | Manifest missing, malformed, or a newer format. |
| `BACKUP_RETENTION_FAILED` | Expired sets could not be removed. |
| `BACKUP_CHECKSUM_MISMATCH` | Size or SHA-256 disagrees with the manifest. |
| `BACKUP_NOT_FOUND` | The named backup or directory does not exist. |
| `BACKUP_CONFIGURATION_UNAVAILABLE` | The configuration is missing, not a regular file, a symlink, unreadable, over 1 MiB, or changed mid-copy. |
| `BACKUP_SET_INCOMPLETE` | The three files are not all present, or do not describe each other. |
| `RESTORE_TEST_FAILED` | The restored copy did not pass its checks. |
| `RESTORE_TARGET_REJECTED` | A production data location was named as a target. |
| `RESTORE_TARGET_EXISTS` | A restore-test target file already exists; it is never overwritten. |
| `DIAGNOSTIC_OUTPUT_UNWRITABLE` | The bundle directory cannot be written. |
| `DIAGNOSTIC_SOURCE_UNAVAILABLE` | An endpoint or command could not be reached. |
| `DIAGNOSTIC_TIMEOUT` | A command did not finish in time. |
| `DIAGNOSTIC_REDACTION_FAILED` | The configuration summary could not be built safely. |
| `DIAGNOSTIC_ARCHIVE_FAILED` | The archive could not be written. |
| `DIAGNOSTIC_LIMIT_EXCEEDED` | A bound was exceeded. |
| `INVALID_ARGUMENT` | A supplied argument is out of range or unsafe. |
| `CONFIGURATION_UNAVAILABLE` | The configuration could not be loaded. |
| `UNEXPECTED_ERROR` | Something the tooling did not anticipate. |

`UNEXPECTED_ERROR` is deliberately distinct: it means "we did not know this could
happen", and that distinction is worth keeping in the journal. No traceback is
printed in normal output — it is the one part that could carry a filesystem path
or a database value into a log an operator forwards to someone else.

## 11. Installation

```bash
cd /opt/garden-observatory
```

```bash
sudo bash scripts/deploy/install-service-identity.sh --dry-run
```

```bash
sudo bash scripts/deploy/install-service-identity.sh
```

The installer:

- creates `/var/backups/garden-observatory` as `mgo:mgo 0750`;
- renders and installs `mgo-backup.service`;
- installs `mgo-backup.timer` and the logrotate policy;
- validates the logrotate policy with `logrotate --debug` **before** installing
  it, because a syntax error in `/etc/logrotate.d` breaks rotation for the whole
  host;
- reloads systemd, seeds the timer stamp, then enables and starts the timer.

It remains **idempotent**: it skips rewriting a file that is already identical,
backs up any file it does replace, never overwrites the production
configuration, never deletes a backup, and never restarts `mgo.service`.

### 11.1 Installer flag semantics

The two units run **different executables**, so the virtual environment is
validated per selected target:

| Unit | Executable validated |
| ---- | -------------------- |
| `mgo.service` | `.venv/bin/uvicorn` |
| `mgo-backup.service` | `.venv/bin/python` |

| Flags | Behaviour with a broken virtual environment |
| ----- | ------------------------------------------- |
| *(default)* | **Fails** — both units are selected. |
| `--no-unit` | **Fails** — the backup service is still selected. |
| `--no-operations` | **Fails** — the API service is still selected. |
| `--no-unit --no-operations` | Continues — no systemd executable is installed. |

`--no-unit` skips `mgo.service` only. It does **not** skip Task 10
provisioning, so a broken environment still stops the run — the installer must
never point a unit at an interpreter that cannot start it.

`--dry-run` prints every change without making any. `--no-operations` skips all
Task 10 provisioning. `--keep N` sets the scheduled retention and is validated
against the same `1..3650` bound the runtime enforces, so the installer can
never write a unit that would fail every scheduled run. `--backup-dir` sets the
backup root.

### 11.2 Timer activation is strict

Installing the timer is checked at every step, in order:

1. install the unit files;
2. `systemctl daemon-reload`;
3. seed the persistence stamp (if absent);
4. `systemctl enable mgo-backup.timer`;
5. `systemctl start mgo-backup.timer`;
6. verify `is-enabled`;
7. verify `is-active`.

Success is reported **only after all seven pass**. Any failure exits non-zero
and names the step that failed. `enable` and `start` can each report success
while the resulting state is not what was asked for, which is why steps 6 and 7
confirm the state rather than inferring it from exit codes.

A missing `systemctl` is a **failure**, not a warning: unit files installed with
no scheduler is not a successful installation.

> A failed activation may leave the installed unit files behind. That is stated
> plainly in the failure message, because the files existing does **not** mean a
> backup is scheduled. Re-run the installer once the cause is fixed. No failure
> path runs the backup service or touches `mgo.service`.

## 12. Verification

```bash
bash scripts/deploy/verify-service-identity.sh
```

```bash
bash scripts/deploy/verify-operations.sh
```

Both are **read-only**. The operations verifier checks the backup directory's
ownership, mode and world-readability; the backup unit's type, identity,
sandbox, configuration and writable paths; the timer's schedule, persistence,
randomised delay, enablement, active state and next run; and the logrotate
policy's location, ownership, mode, target confinement, bounded retention,
secure creation mode and `su` line. It reports journal disk usage for
information and cross-checks that `mgo.service` is still active as `mgo`.

It never takes a backup, never forces a rotation, never enables anything and
never repairs a failed check.

## 13. Raspberry Pi validation

**Not yet performed.** Run this on the Pi, in order, and record the results.

### 13.1 Start from a clean tree

```bash
cd /opt/garden-observatory
```

```bash
git status -sb
```

```bash
git status --porcelain
```

Stop if the tree is dirty — do not discard uncommitted Pi-side work.

```bash
git fetch --prune origin
```

```bash
git checkout task-010-operations
```

```bash
git pull --ff-only origin task-010-operations
```

```bash
uv sync
```

```bash
uv run ruff check .
```

```bash
uv run mypy src
```

```bash
uv run pytest
```

### 13.2 Record the "before" state

Record every value; they are compared again at the end.

```bash
sudo -u mgo sha256sum /var/lib/garden-observatory/db/mgo.db
```

```bash
sudo -u mgo stat -c '%s %n' /var/lib/garden-observatory/db/mgo.db
```

```bash
curl -s http://127.0.0.1:8080/database/status
```

```bash
curl -s http://127.0.0.1:8080/observations?limit=1
```

```bash
curl -s http://127.0.0.1:8080/captures | head -c 400
```

```bash
systemctl show -p MainPID -p NRestarts -p ActiveState mgo.service
```

```bash
curl -s http://127.0.0.1:8080/camera/preview/status
```

Record: database SHA-256, database size, schema version, observation count,
capture count, service PID, restart count, preview state.

> If preview is **running**, note that. It must be restored after any
> intentional service restart later in this procedure.

### 13.3 Confirm the existing service is unaffected

1. `systemctl is-active mgo.service` → `active`.
2. Every endpoint still answers:

   ```bash
   for p in / /version /health /database/status /camera/status /camera/preview/status /motion/status /notifications/status /captures /observations; do printf '%s -> ' "$p"; curl -s -o /dev/null -w '%{http_code}\n' "http://127.0.0.1:8080$p"; done
   ```

3. The dashboard loads in a browser at `http://mgo-core:8080/dashboard` and
   shows live values.

### 13.3a Record the configuration state

The configuration is now part of every recovery set, so its "before" state is
recorded too.

```bash
sudo sha256sum /etc/garden-observatory/mgo.toml
```

```bash
stat -c '%U:%G %a %n' /etc/garden-observatory/mgo.toml
```

### 13.4 Install

```bash
sudo bash scripts/deploy/install-service-identity.sh --dry-run
```

Confirm it describes the backup directory, the backup unit, the timer and the
logrotate policy, and changes nothing.

Also exercise the flag combination that was previously unsafe:

```bash
sudo bash scripts/deploy/install-service-identity.sh --no-unit --dry-run
```

Confirm it still validates the environment for `mgo-backup.service` (it must
report the **backup interpreter**, `.venv/bin/python`) and does **not** claim
that no unit will reference the virtual environment.

```bash
sudo bash scripts/deploy/install-service-identity.sh
```

Confirm the run reports each timer step in turn and ends with
`mgo-backup.timer is enabled and active`. Confirm `mgo.service` was **not**
restarted (`NRestarts` and `MainPID` unchanged).

### 13.5 Check what was provisioned

```bash
stat -c '%U:%G %a %n' /var/backups/garden-observatory
```

Expect `mgo:mgo 750`.

```bash
systemctl cat mgo-backup.service
```

Confirm `ExecStart` uses `.venv/bin/python` — the interpreter the installer
validated.

```bash
systemctl cat mgo-backup.timer
```

```bash
cat /etc/logrotate.d/garden-observatory
```

```bash
bash scripts/deploy/verify-operations.sh
```

```bash
bash scripts/deploy/verify-service-identity.sh
```

### 13.6 Take a backup while the API is serving

Confirm the API is active first, and leave it running.

```bash
sudo -u mgo /opt/garden-observatory/scripts/operations/backup-database.sh backup
```

Expect exit `0`. Record the backup filename — this is the unique Task 10
validation backup. **Do not delete any pre-existing backup.**

```bash
sudo -u mgo /opt/garden-observatory/scripts/operations/backup-database.sh list
```

**Confirm the set has all three files:**

```bash
sudo ls -l /var/backups/garden-observatory/
```

Expect `<backup>.db`, `<backup>.config.toml` and `<backup>.manifest.json`
sharing one timestamp, with no orphans and no temporary files.

```bash
sudo -u mgo cat /var/backups/garden-observatory/<backup>.manifest.json
```

Confirm `schema_version`, `integrity: "ok"`, row counts for **all three**
expected tables, `journal_mode_of_backup`, and the four `configuration_*`
fields. Confirm the manifest contains **no** configuration content.

```bash
stat -c '%U:%G %a %n' /var/backups/garden-observatory/<backup>.db /var/backups/garden-observatory/<backup>.config.toml /var/backups/garden-observatory/<backup>.manifest.json
```

Expect `mgo:mgo 640` for all three, and nothing world-readable.

```bash
sudo -u mgo sha256sum /var/backups/garden-observatory/<backup>.db /var/backups/garden-observatory/<backup>.config.toml
```

Confirm these match the manifest's `sha256` and `configuration_sha256`.

**Prove the configuration snapshot is the live configuration:**

```bash
sudo sha256sum /etc/garden-observatory/mgo.toml
```

```bash
sudo diff /etc/garden-observatory/mgo.toml /var/backups/garden-observatory/<backup>.config.toml && echo "identical"
```

Both must match the value recorded in §13.3a.

```bash
sudo -u mgo /opt/garden-observatory/scripts/operations/backup-database.sh verify /var/backups/garden-observatory/<backup>.db
```

```bash
sudo -u mgo /opt/garden-observatory/scripts/operations/backup-database.sh restore-test /var/backups/garden-observatory/<backup>.db
```

Both must exit `0`. In the restore-test output confirm `set_verified`,
`restored_configuration` and `row_counts` are all `ok`, and that **nothing
reports `skipped`**.

### 13.7 Confirm production data is untouched

```bash
sudo -u mgo sha256sum /var/lib/garden-observatory/db/mgo.db
```

```bash
curl -s http://127.0.0.1:8080/database/status
```

```bash
sudo sha256sum /etc/garden-observatory/mgo.toml
```

Compare all three against §13.2 and §13.3a. Observation and capture counts must
be unchanged apart from any the running application legitimately recorded
meanwhile, and the production configuration must be **byte-identical** — a
backup reads it and never writes it.

### 13.8 Scheduled run

```bash
systemctl is-enabled mgo-backup.timer && systemctl is-active mgo-backup.timer
```

```bash
systemctl list-timers mgo-backup.timer
```

Confirm the timer reports itself both enabled and active with a next elapse —
the same two states the installer verified rather than assumed — and that **no
backup ran at install time** (the only recovery set present should be the one
taken by hand in §13.6, plus any pre-existing).

```bash
sudo systemctl start mgo-backup.service
```

```bash
systemctl status mgo-backup.service
```

```bash
journalctl -u mgo-backup.service -n 50 --no-pager
```

Confirm structured JSON events with `event_id`, `severity` and `error_code`.

### 13.9 Backup failure does not affect the API

Force a failure and confirm isolation:

```bash
sudo -u mgo /opt/garden-observatory/scripts/operations/backup-database.sh backup --database /var/lib/garden-observatory/db/does-not-exist.db
```

Expect a non-zero exit and `BACKUP_SOURCE_UNAVAILABLE`. Then:

```bash
systemctl show -p MainPID -p NRestarts -p ActiveState mgo.service
```

Unchanged from §13.2, and still `active`.

### 13.10 Support bundle

```bash
cd /tmp && sudo -u mgo /opt/garden-observatory/scripts/operations/create-support-bundle.sh --output-directory /tmp
```

```bash
tar -tzf /tmp/mgo-support-*.tar.gz
```

```bash
stat -c '%a %n' /tmp/mgo-support-*.tar.gz
```

Expect `600`.

**Inspect before copying it anywhere:**

```bash
tar -xzOf /tmp/mgo-support-*.tar.gz configuration-summary.json
```

```bash
tar -xzOf /tmp/mgo-support-*.tar.gz manifest.json
```

```bash
tar -xzOf /tmp/mgo-support-*.tar.gz storage-summary.json
```

Confirm: no `.db`, `-wal`, `-shm`, `.jpg` or media filename anywhere in the
listing; no raw configuration; no token or password; the journal member is
bounded; paths are canonical or reported by role only.

```bash
tar -tzf /tmp/mgo-support-*.tar.gz | grep -E '(\.db|-wal|-shm|\.jpg|\.jpeg|\.png|\.mp4|\.toml)$' && echo "PROBLEM" || echo "clean"
```

**Prove the raw configuration is absent** — the recovery-set snapshot must never
appear in a bundle:

```bash
sudo grep -c . /etc/garden-observatory/mgo.toml >/dev/null && tar -xzOf /tmp/mgo-support-*.tar.gz | grep -F "$(sudo sed -n '2p' /etc/garden-observatory/mgo.toml)" && echo "PROBLEM: configuration content present" || echo "clean"
```

**Confirm the storage summary is bounded**:

```bash
tar -xzOf /tmp/mgo-support-*.tar.gz storage-summary.json
```

Every directory entry must carry a `truncated` field. If any is `true`, the
bundle outcome must be `partial` and `errors.json` must say which limit was
reached.

**Confirm collection stayed on loopback.** With the API running, generation
should be `complete`; there must be no outbound connection. A proxy in the
environment must make no difference:

```bash
cd /tmp && sudo -u mgo HTTP_PROXY=http://127.0.0.1:9 ALL_PROXY=http://127.0.0.1:9 /opt/garden-observatory/scripts/operations/create-support-bundle.sh --output-directory /tmp
```

This must still exit `0` (or `1` only for genuinely unavailable sources), proving
the proxy variables were ignored rather than honoured.

### 13.11 Journal and rotation

```bash
journalctl --disk-usage
```

Record the value and confirm the host's journald retention is bounded:

```bash
systemd-analyze cat-config systemd/journald.conf | grep -E 'SystemMaxUse|MaxRetentionSec'
```

Synthetic rotation test — this creates a file **only** for the test and removes
it and its rotated copies afterwards:

```bash
sudo -u mgo bash -c 'printf "synthetic validation line\n" > /var/log/garden-observatory/task10-validation.log'
```

```bash
sudo logrotate --force /etc/logrotate.d/garden-observatory
```

```bash
ls -l /var/log/garden-observatory/
```

Confirm a rotated copy exists, is not world-readable, and is owned by `mgo:mgo`.
Then clean up **only** the synthetic file and its rotations:

```bash
sudo rm -f /var/log/garden-observatory/task10-validation.log*
```

```bash
ls -l /var/log/garden-observatory/
```

### 13.12 Restart and boot persistence

If preview was running in §13.2, it will need restarting after this.

```bash
sudo systemctl restart mgo.service
```

```bash
systemctl is-active mgo.service && curl -s http://127.0.0.1:8080/health | head -c 200
```

```bash
sudo reboot
```

After the Pi returns:

```bash
systemctl is-active mgo.service
```

```bash
systemctl is-enabled mgo-backup.timer && systemctl is-active mgo-backup.timer
```

```bash
systemctl list-timers mgo-backup.timer
```

Restore preview if it was running before:

```bash
curl -s -X POST http://127.0.0.1:8080/camera/preview/start
```

Do **not** capture an image merely to test operations.

### 13.13 Finish

```bash
journalctl -u mgo.service -n 100 --no-pager
```

```bash
journalctl -u mgo-backup.service --no-pager
```

```bash
git status -sb
```

```bash
git status --porcelain
```

The tree must be clean. Return the Pi to `main` when validation is complete:

```bash
git checkout main
```

## 14. Rollback

Task 10 is reversible with no data loss.

**Before merge** — nothing on `main` was touched; rollback is returning to
`main`.

**After a future merge**, on the Pi:

```bash
sudo systemctl disable --now mgo-backup.timer
```

```bash
sudo rm -f /etc/systemd/system/mgo-backup.timer /etc/systemd/system/mgo-backup.service
```

```bash
sudo rm -f /etc/logrotate.d/garden-observatory
```

```bash
sudo systemctl daemon-reload
```

Then in the checkout:

```bash
git revert <Task 10 commits>
```

```bash
uv sync && uv run ruff check . && uv run mypy src && uv run pytest
```

```bash
sudo bash scripts/deploy/install-service-identity.sh --no-operations
```

```bash
bash scripts/deploy/verify-service-identity.sh
```

`mgo.service` needs **no restart**, because it never changed.

Existing backups in `/var/backups/garden-observatory` are **left intact** unless
the operator explicitly chooses to remove them. No rollback step deletes the
production database, captures or support bundles, resets Git history,
force-pushes or overwrites the production configuration.

**No automatic rollback script is provided, deliberately.** A script that
deletes backups is precisely the thing that must not exist.

## 15. Troubleshooting

| Symptom | Likely cause and action |
| ------- | ----------------------- |
| `BACKUP_LOCKED` | Another backup is running. Check `systemctl status mgo-backup.service`. If nothing is running, the lock is abandoned and is reclaimed automatically after 6 hours. |
| `BACKUP_DESTINATION_UNWRITABLE` | Backup directory missing or wrong ownership. Re-run the installer and `verify-operations.sh`. |
| `BACKUP_SOURCE_UNAVAILABLE` with "symbolic link" | The configured database path is a symlink. Point the configuration at the real file; backing up through a link is refused because the target could change between runs. |
| `unable to open database file` from the timer, but manual backup works | The unit's `ReadWritePaths` is missing the database directory. SQLite needs it writable for WAL shared memory. See [§4.4](#44-the-units-writable-paths). |
| `BACKUP_SCHEMA_INCOMPATIBLE` | The database records a newer schema than this build supports — the checkout is older than the data. Update the application. |
| `BACKUP_CHECKSUM_MISMATCH` on verify | The backup changed since it was written; suspect SD-card bit rot. Verify the other backups and replace the card if more than one is affected. |
| `BACKUP_RETENTION_FAILED` | The new backup succeeded; old sets could not be removed. Check ownership in the backup directory — the directory will otherwise keep growing. |
| `BACKUP_CONFIGURATION_UNAVAILABLE` | The selected configuration is missing, a symlink, over 1 MiB, or was edited while the backup ran. Check `MGO_CONFIG_PATH` and the unit's `--config`; re-run once any edit has settled. |
| `BACKUP_SET_INCOMPLETE` | A recovery set is missing one of its three files, or the manifest describes a different set. Run `list` to see which orphans exist; the set is not restorable. |
| `RESTORE_TARGET_EXISTS` | The `--work-directory` already holds `restored.db` or `restored-mgo.toml`. Choose an empty directory; the test never overwrites. |
| Installer fails on `--keep` | The value is outside `1..3650`. The bound matches the runtime, so a rejected value would have failed every scheduled run. |
| Installer fails naming a step | The timer was not activated. The step name says which of daemon-reload / enable / start / verify-enabled / verify-active failed. Unit files may be installed, but **no backup is scheduled** — fix the cause and re-run. |
| Timer enabled but never runs | Check `systemctl list-timers mgo-backup.timer` and the Pi's clock/timezone (`timedatectl`). |
| A backup ran the moment the installer finished | The timer stamp was not seeded. Confirm `/var/lib/systemd/timers/stamp-mgo-backup.timer` exists. |
| `storage-summary.json` says `truncated: true` | A directory hit the entry or depth bound. The counts are partial and the bundle is `partial`; this is reported, not a fault. |
| Support bundle exits `1` | Partial: one or more sources were unreachable. Read `errors.json` inside the bundle — often the API being down, which is the fault itself. |
| Support bundle exits `2` | No bundle was created; the output directory is unwritable. |
| `logrotate` reports nothing to do | Expected. journald is primary and there may be no `*.log` files at all. |

## 16. Security

- Backups and configuration snapshots `0640`; backup directory `mgo:mgo 0750`;
  support bundles `0600`. Nothing is world-readable or world-writable.
- **The configuration snapshot may contain credentials.** It lives only in the
  protected backup directory, never in a support bundle. Treat a copied recovery
  set as sensitive.
- No secret, token, password, credential, private key, environment dump, raw
  configuration, database file, media file or media filename enters a **support
  bundle**.
- No external network access: literal loopback only, proxies disabled, redirects
  refused.
- No `shell=True` anywhere; every subprocess uses an argument array with an
  explicit timeout and bounded captured output.
- No archive member may be absolute, contain `..` or be a symlink.
- A symlinked source database **or configuration** is refused outright, with no
  override.
- Storage aggregation never descends a symlinked directory and never stat-s a
  symlink's target.
- `restore-test` refuses to write into the production database directory or the
  configured database's directory.
- Nothing in Task 10 runs the application as root.
- Backup and support-bundle generation are **not** exposed through the API or
  the dashboard: they are privileged operator actions, and the LAN API is
  unauthenticated.

## 17. Known limitations

- **The Raspberry Pi validation has not been performed.** Backups are configured
  and tested off-Pi; they are not yet proven on the Pi.
- **Media is not backed up.** Only the database and the configuration are. The
  capture archive is larger than the SD card can hold twice, and image retention
  policy is future work.
- **Backups stay on the same device.** There is no remote or cloud copy, so an
  SD-card failure loses both the database and its backups. Copy a recovery set
  off the Pi periodically — and remember that the configuration snapshot inside
  it may contain credentials.
- **No production restore automation**, by design ([§6](#6-restore-the-deliberate-boundary)).
- **Application-wide structured logging is deferred**; only the Task 10 tools
  emit JSON events.
- **No worker unit exists**, because no worker process exists
  ([§2.2](#22-why-there-is-no-worker-service)).
- **Host journald retention is unchanged**, deliberately.

## 18. The future worker-unit path

When a real worker arrives — a camera pipeline, a detection process, an
identification stage — it becomes a service when, and only when, it has:

1. a standalone executable entry point that does not import the API application;
2. a defined lifecycle: what it owns, what it starts with, what it shuts down;
3. a resolved camera-ownership contract, since the camera has exactly one owner;
4. its own health surface, so a failure is diagnosable;
5. a restart policy that suits its failure modes.

Until all five exist, the honest answer is the one recorded here: the API
service satisfies OPS-01 today, and worker units are future work.

---

Related: [`Service-Identity.md`](Service-Identity.md) ·
[`Database.md`](Database.md) · [`Dashboard.md`](Dashboard.md) ·
[`Remote-Access.md`](Remote-Access.md) ·
[`tasks/Task-010-Operations.md`](tasks/Task-010-Operations.md)
