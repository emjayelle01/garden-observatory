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

- a consistent SQLite backup that runs while the API keeps serving;
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

Every successful backup has a JSON manifest recording:

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
| `table_row_counts` | Row counts for the expected tables. |

A manifest travels with its backup, and a backup may be copied somewhere less
private than the Pi. It therefore contains **no** absolute path, configuration
value, environment value, username, hostname or database row content. Row
*counts* are included; row *contents* never are.

A manifest recording a `format_version` newer than this build understands is
refused rather than interpreted optimistically — the same rule the database
schema follows.

### 3.6 Validation before publication

Before a backup is published it is opened independently and must pass:

- it is a valid SQLite database;
- `PRAGMA quick_check(1)` reports `ok`;
- a schema version can be read from the `schema_migrations` authority;
- that version is **not newer** than this build supports;
- the expected tables (`schema_migrations`, `observations`, `captures`) exist;
- the manifest checksum matches the final file.

A backup that fails any of these is not published.

### 3.7 Retention

- Default **14** complete backup sets (`--keep`, validated as a bounded positive
  integer, minimum 1 so retention can never delete everything).
- Runs **only after** a validated backup has been published.
- A set's `.db` and manifest are removed **together**; deleting one and leaving
  the other would manufacture an orphan.
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

Reports complete sets newest first with their verification state, plus any
orphaned `.db` files, orphaned manifests and in-progress temporary files —
which are reported but never treated as backups.

### 5.3 Verify a backup

```bash
scripts/operations/backup-database.sh verify /var/backups/garden-observatory/mgo-20260728T023000Z.db
```

Read-only. Checks the manifest structure and version, the recorded size, the
SHA-256, SQLite integrity, schema compatibility, the expected tables and the
recorded row counts.

### 5.4 Restore-test a backup

```bash
scripts/operations/backup-database.sh restore-test /var/backups/garden-observatory/mgo-20260728T023000Z.db
```

Restores into an isolated temporary directory, opens it as an independent
database, runs the same integrity/schema/table/row-count checks, confirms the
source backup is unchanged, and cleans up. It never writes into the production
database directory, never replaces a configured database, never stops the
service and never modifies the backup or the configuration.

Add `--preserve` to keep the restored copy for inspection after a failure.

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

5. **Start and confirm.**

   ```bash
   sudo systemctl start mgo.service
   ```

   ```bash
   curl -s http://127.0.0.1:8080/database/status
   ```

   Expect `status: healthy` and the schema version the manifest recorded. The
   application applies any pending migrations at startup.

6. **Only once the restore is confirmed good**, decide what to do with
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
| Archive members | 64 |
| Archive bytes | 8 MiB |

A misbehaving source cannot fill the SD card. The journal slice is scoped to the
MGO unit only — the whole system journal would carry other services' logs and
other users' activity off the Pi.

### 9.7 Network boundary

Collection is **loopback only**. The base URL is validated to address
`127.0.0.1` rather than assumed, so bundle generation cannot be pointed at a
remote address. Only read-only status endpoints are contacted: no capture, no
preview start or stop, no stream, no notification publication. Nothing is
uploaded anywhere.

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
| `RESTORE_TEST_FAILED` | The restored copy did not pass its checks. |
| `RESTORE_TARGET_REJECTED` | A production data location was named as a target. |
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

`--dry-run` prints every change without making any. `--no-operations` skips all
Task 10 provisioning. `--keep N` sets the scheduled retention; `--backup-dir`
sets the backup root.

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

### 13.4 Install

```bash
sudo bash scripts/deploy/install-service-identity.sh --dry-run
```

Confirm it describes the backup directory, the backup unit, the timer and the
logrotate policy, and changes nothing.

```bash
sudo bash scripts/deploy/install-service-identity.sh
```

Confirm `mgo.service` was **not** restarted (`NRestarts` and `MainPID`
unchanged).

### 13.5 Check what was provisioned

```bash
stat -c '%U:%G %a %n' /var/backups/garden-observatory
```

Expect `mgo:mgo 750`.

```bash
systemctl cat mgo-backup.service
```

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

```bash
sudo -u mgo cat /var/backups/garden-observatory/<backup>.manifest.json
```

Confirm the manifest's `schema_version`, `integrity: "ok"`, row counts and
`journal_mode_of_backup`.

```bash
stat -c '%U:%G %a %n' /var/backups/garden-observatory/<backup>.db
```

Expect `mgo:mgo 640`.

```bash
sudo -u mgo sha256sum /var/backups/garden-observatory/<backup>.db
```

Confirm it matches the manifest's `sha256`.

```bash
sudo -u mgo /opt/garden-observatory/scripts/operations/backup-database.sh verify /var/backups/garden-observatory/<backup>.db
```

```bash
sudo -u mgo /opt/garden-observatory/scripts/operations/backup-database.sh restore-test /var/backups/garden-observatory/<backup>.db
```

Both must exit `0`.

### 13.7 Confirm production data is untouched

```bash
sudo -u mgo sha256sum /var/lib/garden-observatory/db/mgo.db
```

```bash
curl -s http://127.0.0.1:8080/database/status
```

Compare against §13.2. Observation and capture counts must be unchanged apart
from any the running application legitimately recorded meanwhile.

### 13.8 Scheduled run

```bash
systemctl list-timers mgo-backup.timer
```

Confirm the timer is enabled, active and has a next elapse — and that **no
backup ran at install time** (the only backup present should be the one taken by
hand in §13.6, plus any pre-existing).

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
tar -tzf /tmp/mgo-support-*.tar.gz | grep -E '(\.db|-wal|-shm|\.jpg|\.jpeg|\.png|\.mp4)$' && echo "PROBLEM" || echo "clean"
```

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
| Timer enabled but never runs | Check `systemctl list-timers mgo-backup.timer` and the Pi's clock/timezone (`timedatectl`). |
| A backup ran the moment the installer finished | The timer stamp was not seeded. Confirm `/var/lib/systemd/timers/stamp-mgo-backup.timer` exists. |
| Support bundle exits `1` | Partial: one or more sources were unreachable. Read `errors.json` inside the bundle — often the API being down, which is the fault itself. |
| Support bundle exits `2` | No bundle was created; the output directory is unwritable. |
| `logrotate` reports nothing to do | Expected. journald is primary and there may be no `*.log` files at all. |

## 16. Security

- Backups `0640`; backup directory `mgo:mgo 0750`; support bundles `0600`.
  Nothing is world-readable or world-writable.
- No secret, token, password, credential, private key, environment dump, raw
  configuration, database file, media file or media filename enters any
  artefact.
- No external network access; the bundle talks only to `127.0.0.1`.
- No `shell=True` anywhere; every subprocess uses an argument array with an
  explicit timeout and bounded captured output.
- No archive member may be absolute, contain `..` or be a symlink.
- A symlinked source database is refused outright, with no override.
- `restore-test` refuses to write into the production database directory or the
  configured database's directory.
- Nothing in Task 10 runs the application as root.
- Backup and support-bundle generation are **not** exposed through the API or
  the dashboard: they are privileged operator actions, and the LAN API is
  unauthenticated.

## 17. Known limitations

- **The Raspberry Pi validation has not been performed.** Backups are configured
  and tested off-Pi; they are not yet proven on the Pi.
- **Media is not backed up.** Only the database is. The capture archive is
  larger than the SD card can hold twice, and image retention policy is future
  work.
- **The configuration file is not backed up automatically.** It is
  root-owned, changes rarely and is not writable by the runtime account; copy
  `/etc/garden-observatory/mgo.toml` by hand when you change it.
- **Backups stay on the same device.** There is no remote or cloud copy, so an
  SD-card failure loses both the database and its backups. Copy a backup off the
  Pi periodically.
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
