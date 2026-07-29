# MGO Operations

How Matt's Garden Observatory is backed up, how its logs are bounded, and how a
fault is diagnosed without attaching a monitor and keyboard to the Raspberry Pi.

> **Status.** Direct Raspberry Pi validation completed successfully at approved
> commit `4e1e5d38ab0189b62d0763c0b1301b142d7151a6`, and the operations
> implementation **passed**. Backups are therefore *proven on the Pi*, not merely
> configured: a consistent online backup, a complete database/configuration/
> manifest recovery set, verification, isolated restore testing and forced log
> rotation were all exercised on real hardware, and the validated recovery sets
> are preserved in `/var/backups/garden-observatory` as the evidence.
>
> Validation also found **one cleanup gap and two operator-reporting defects**:
> ignored Task 10 bytecode survived the return to `main` ([§13.14](#1314-pre-merge-operational-cleanup)),
> the operations verifier mistook an unprivileged `PATH` omission for an absent
> logrotate installation, and the backup wrapper's help described
> command-specific arguments as common options. All three are corrected here.
> **That correction itself awaits focused Pi revalidation** — the operations
> implementation is validated, the corrected cleanup procedure is not yet.

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
production this is always `/etc/garden-observatory/mgo.toml`.

The operator wrappers reach the same file without the operator having to say so.
See [§5.0 Which configuration an operator command uses](#50-which-configuration-an-operator-command-uses).

#### It is read exactly once

The configuration is read **once per run**, and those exact bytes are
authoritative for everything that follows:

```text
read the file once, securely
        │
        ├─ parse it  ──► the database path this run backs up
        │
        └─ store it  ──► the configuration snapshot in the recovery set
```

This matters because the two used to be separate reads — parse the file to find
the database, open it again later to snapshot it. An administrator replacing the
configuration between those reads would have produced a set pairing a database
chosen from *one* version with bytes from *another*. Every file in that set
would be individually valid and every checksum would match, so nothing would
have detected it; the set would simply describe a pairing that never existed.

Because the bytes are the authority, editing the live configuration after a
backup has begun cannot change what that backup contains.

The configuration must also be **loadable**: a recovery set exists to restore a
working system, so one containing a configuration the application cannot parse
would not be a recovery. A parse failure fails the backup, and the error
deliberately does not quote the offending line — that line may be the one
holding a secret.

#### Secure reading

Opening is race-resistant, not merely checked:

- on **Linux** the kernel refuses to open a symbolic link at all (`O_NOFOLLOW`).
  There is deliberately no fallback to following the link afterwards — retrying
  without the flag would reinstate exactly the hole it closes;
- the file is validated through its own **descriptor**, and the path is then
  re-examined to prove it still names that same object (`st_dev`, `st_ino`);
- on **Windows** — the development machine only — `O_NOFOLLOW` does not exist,
  so the fallback takes an `lstat` before the open and compares it against the
  descriptor's `fstat`, then against a final `lstat` afterwards — **all three**
  observations, not two. A substitution is therefore **detected** on Windows
  rather than **prevented**. The Linux guarantee was not weakened to make the
  tests convenient; the difference is stated here and asserted by a test.

The pre-open comparison is load-bearing on that fallback. Without it, a regular
file could be replaced by *another regular file* between the check and the open:
from the open onwards every observation describes the replacement, and they all
agree with one another, so nothing later can notice.

Every path comparison happens **while the descriptor is still open**. Closing it
first would allow an unlinked inode to be recycled before the comparison ran —
holding the descriptor is precisely what prevents that.

A plain `path.is_symlink()` followed by `path.open()` is *not* equivalent. It is
correct only for a path that does not change, and `fstat` does not rescue it:
`fstat` proves the opened object is a regular file, not that no symlink was
followed to reach it.

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

### 3.1.1 The database that is opened is the one that was validated

SQLite opens the database **by name**, so a path validated a moment earlier
could in principle be repointed before SQLite gets to it. Validating and then
opening is two operations on a path that anything with write access to the
directory could change in between.

The backup therefore holds an **identity anchor**:

1. the database is opened with `O_NOFOLLOW` and its `(device, inode)` recorded
   — on the fallback platform, after confirming the descriptor is the object
   observed immediately before the open;
2. that descriptor is **held open** while SQLite connects — holding it is the
   load-bearing part, because it stops the kernel recycling that inode and so
   keeps the comparison meaningful;
3. once SQLite has opened the path, the path is re-examined and must still name
   the anchored file;
4. the online copy is taken;
5. the path is re-examined **again** before the copy is accepted.

Two checks, not one. The first proves SQLite opened the right file; the second
proves the path was still that file for the whole of the read. A substitution
during the copy would otherwise have had no fail-closed result.

A mismatch at either point fails with `BACKUP_SOURCE_IDENTITY_CHANGED` and
publishes nothing — no database snapshot, no configuration snapshot, no
manifest, and no temporary file. The API keeps running throughout.
That code is deliberately distinct from `BACKUP_SOURCE_UNAVAILABLE`: "the file I
checked is not the file I opened" is a very different incident from "the file is
missing", and an operator seeing it should be looking for something replacing
paths rather than for a deployment mistake.

**`immutable=1` is deliberately not used** to sidestep this. It would tell
SQLite the file cannot change, which is false for a live database carrying WAL
state, and would produce an inconsistent read rather than a safe one. A
`/proc/self/fd` URI was also rejected: it would require proving that WAL
sidecars stay correctly associated, that committed WAL data is still included
and that the source stays read-only, and the anchor achieves the same guarantee
without that.

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

### 5.0 Which configuration an operator command uses

The wrappers default `MGO_CONFIG_PATH` to `/etc/garden-observatory/mgo.toml`
**only when the variable is unset**. That default is applied before Python
starts, so a bare operator command means production.

Effective precedence, highest first:

```text
explicit --config PATH
caller-provided MGO_CONFIG_PATH
wrapper-supplied production MGO_CONFIG_PATH
```

| Environment state | What the wrapper does |
| ----------------- | --------------------- |
| unset | sets `/etc/garden-observatory/mgo.toml` |
| set to a path | preserved exactly |
| set-but-empty | preserved, so the CLI reports the error |
| whitespace only | preserved, so the CLI reports the error |

**Why this exists.** `mgo-backup.service` has always set `MGO_CONFIG_PATH` *and*
passed `--config`, so the scheduled backup was correct. The manual wrappers
supplied neither, and configuration resolution falls back to the tracked
*development* configuration. `sudo` clears the environment, so every documented
manual command — `sudo -u mgo .../backup-database.sh backup` — resolved to the
repository's development file. That could select a development database path,
fail against development-relative storage, or produce a support bundle
describing a configuration the Pi does not use. Scheduled operation was
production-safe; manual operation was not.

**Why set-but-empty is preserved.** The application treats an empty or
whitespace-only `MGO_CONFIG_PATH` as a configuration error. The wrapper uses
`[[ ! -v MGO_CONFIG_PATH ]]`, which tests whether the *name* is set, rather than
`: "${MGO_CONFIG_PATH:=...}"`, which would also replace a deliberately empty
value. Silently repairing a broken unit or a typo in a profile — by pointing the
tooling at the live system — would hide the fault it should surface.

**Development is unaffected.** The default lives in the operator wrappers, not
in `resolve_config_path()`. Running Python directly with no `--config` and no
`MGO_CONFIG_PATH` still loads `config/mgo.toml`, on every platform.

**The timer does not depend on this.** `mgo-backup.service` invokes the
interpreter directly, never the wrapper, and remains independently explicit.

To override for one command:

```bash
sudo -u mgo MGO_CONFIG_PATH=/tmp/other.toml /opt/garden-observatory/scripts/operations/backup-database.sh backup
```

### 5.1 Take a backup

```bash
scripts/operations/backup-database.sh backup
```

No step in the normal backup procedure requires stopping `mgo.service`.

The summary reports which configuration decided what was backed up:

```json
"database_source": "configuration"
```

#### `--database` is a deliberate override

```bash
scripts/operations/backup-database.sh backup --database /some/other/mgo.db
```

The recovery set still contains the selected configuration, but that
configuration is **not** what chose the database in this mode. The summary says
so (`"database_source": "explicit_override"`) and a warning event is emitted, so
nothing claims a pairing that did not happen.

Use it when you genuinely mean to back up a database other than the configured
one — recovering a copy set aside during an incident, for example. In that mode
the set's configuration documents the deployment, not the origin of the
database beside it.

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
| `BACKUP_CONFIGURATION_UNAVAILABLE` | The configuration is missing, not a regular file, a symlink, unreadable, over 1 MiB, unparseable, changed mid-read, or replaced while being read. |
| `BACKUP_SET_INCOMPLETE` | The three files are not all present, or do not describe each other. |
| `BACKUP_SOURCE_IDENTITY_CHANGED` | The database was replaced, or became a symlink, at any point between validation and the end of the copy. Nothing was published. |
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

### 13.1a Record the pre-existing operational state

**Run this before installing anything.** It establishes what this validation is
allowed to remove afterwards, and it is what makes §13.13 unambiguous:
**validation removes only the artefacts it proved were absent beforehand and
then installed itself.**

```bash
systemctl status mgo-backup.timer --no-pager 2>&1 || true
```

```bash
systemctl status mgo-backup.service --no-pager 2>&1 || true
```

```bash
test -e /etc/systemd/system/mgo-backup.timer && echo "timer exists" || echo "timer absent"
```

```bash
test -e /etc/systemd/system/mgo-backup.service && echo "service exists" || echo "service absent"
```

```bash
test -e /etc/logrotate.d/garden-observatory && echo "logrotate exists" || echo "logrotate absent"
```

For this **first** Task 10 validation the expected state is:

```text
mgo-backup.timer absent
mgo-backup.service absent
garden-observatory logrotate policy absent
```

If any of the three already exists, **stop**:

* record its content, ownership and state;
* do **not** overwrite it until its origin is understood;
* do **not** assume it belongs to this branch — it may predate it, or belong to
  a different deployment;
* do **not** delete it.

An artefact this validation did not install is not this validation's to remove.

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

### 13.5a Prove the manual wrapper selects the production configuration

Run this **before** §13.6, because every later manual step depends on it. `env
-u` removes the variable entirely, reproducing what `sudo` does to an operator's
environment.

```bash
sudo -u mgo env -u MGO_CONFIG_PATH /opt/garden-observatory/scripts/operations/backup-database.sh backup --output-directory /tmp/mgo-wrapper-default-validation
```

Expect exit `0`. The configuration snapshot in that recovery set must be
**byte-identical** to the production configuration:

```bash
sudo cmp /etc/garden-observatory/mgo.toml /tmp/mgo-wrapper-default-validation/*.config.toml
```

`cmp` must print nothing and exit `0`. If it reports a difference, the wrapper
default is not in effect and the manual commands below would be describing the
wrong machine — stop and investigate.

Confirm the summary reports `"database_source": "configuration"` and that the
database path it used is under `/var/lib/garden-observatory`, not under
`/opt/garden-observatory`.

Now prove a caller-supplied value is **preserved** rather than overridden, using
a temporary configuration well outside production:

```bash
sudo -u mgo install -m 0640 /etc/garden-observatory/mgo.toml /tmp/mgo-wrapper-custom.toml
```

```bash
sudo -u mgo MGO_CONFIG_PATH=/tmp/mgo-wrapper-custom.toml /opt/garden-observatory/scripts/operations/create-support-bundle.sh --output-directory /tmp/mgo-wrapper-default-validation
```

The bundle must be produced from that file. Then prove a deliberately empty
value is still an error rather than being silently replaced:

```bash
sudo -u mgo MGO_CONFIG_PATH= /opt/garden-observatory/scripts/operations/backup-database.sh backup --output-directory /tmp/mgo-wrapper-default-validation
```

This must exit non-zero and report that `MGO_CONFIG_PATH` is set but empty. It
must **not** fall back to production.

Remove only the temporary validation artefacts:

```bash
sudo rm -rf /tmp/mgo-wrapper-default-validation /tmp/mgo-wrapper-custom.toml
```

Nothing in this step writes to `/var/backups/garden-observatory`, so no
pre-existing backup and no retention state is affected.

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

### 13.6a Source-identity checks

These prove the hardening works on the Pi. **Both negative tests use temporary
files created outside the production locations** — the production database and
the production configuration are never replaced, moved or symlinked.

First confirm the normal cases already passed (§13.6): a real production
configuration was accepted, a live WAL database backed up, the snapshot matches
the live configuration checksum, and the database verifies. Then:

```bash
sudo -u mgo mkdir -p /tmp/mgo-identity && sudo -u mgo cp /etc/garden-observatory/mgo.toml /tmp/mgo-identity/real.toml
```

```bash
sudo -u mgo ln -s /tmp/mgo-identity/real.toml /tmp/mgo-identity/linked.toml
```

```bash
sudo -u mgo /opt/garden-observatory/scripts/operations/backup-database.sh backup --config /tmp/mgo-identity/linked.toml --output-directory /tmp/mgo-identity/out
```

Must exit non-zero with `BACKUP_CONFIGURATION_UNAVAILABLE` and the message
naming a symbolic link.

```bash
sudo -u mgo cp /var/backups/garden-observatory/<backup>.db /tmp/mgo-identity/real.db && sudo -u mgo ln -s /tmp/mgo-identity/real.db /tmp/mgo-identity/linked.db
```

```bash
sudo -u mgo /opt/garden-observatory/scripts/operations/backup-database.sh backup --database /tmp/mgo-identity/linked.db --output-directory /tmp/mgo-identity/out
```

Must exit non-zero with `BACKUP_SOURCE_UNAVAILABLE`.

**Confirm neither negative test published anything:**

```bash
sudo ls -la /tmp/mgo-identity/out 2>/dev/null; echo "exit: $?"
```

The directory must be absent or empty — no `.db`, no `.config.toml`, no
`.manifest.json`, no temporary file.

**Confirm the production locations are untouched** and the API never noticed:

```bash
sudo sha256sum /etc/garden-observatory/mgo.toml /var/lib/garden-observatory/db/mgo.db
```

```bash
systemctl is-active mgo.service
```

Both hashes must match §13.2 / §13.3a, and the service must still be `active`.

```bash
sudo rm -rf /tmp/mgo-identity
```

**Confirm the override is labelled honestly.** Take one deliberate
override backup into a temporary directory and read its summary:

```bash
sudo -u mgo /opt/garden-observatory/scripts/operations/backup-database.sh backup --database /var/lib/garden-observatory/db/mgo.db --output-directory /tmp/mgo-override
```

The summary must report `"database_source": "explicit_override"` and the journal
must carry a `backup.database_overridden` warning. Then:

```bash
sudo rm -rf /tmp/mgo-override
```

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

### 13.13 Review the journal

```bash
journalctl -u mgo.service -n 100 --no-pager
```

```bash
journalctl -u mgo-backup.service --no-pager
```

### 13.14 Pre-merge operational cleanup

**Mandatory, and it must happen before `git checkout main`.**

Installed systemd units and logrotate policies live in `/etc/systemd/system`
and `/etc/logrotate.d`. **They are outside Git.** Changing the checkout back to
`main` does not remove them, does not disable them and does not stop the timer —
it only removes the code they point at. `mgo-backup.service` has

```text
ExecStart=/opt/garden-observatory/.venv/bin/python -m mgo.operations.backup_cli ...
```

and `mgo.operations` does not exist on pre-Task-10 `main`. So a checkout without
this cleanup leaves an **enabled timer scheduled to run a module that is no
longer there**, and the next 02:30 run fails — on a branch that was never
supposed to have installed anything permanently.

Run this only after every check above, including the reboot test, has passed.

Disable and stop the timer:

```bash
sudo systemctl disable --now mgo-backup.timer
```

Confirm:

```bash
systemctl is-enabled mgo-backup.timer 2>&1 || true
```

```bash
systemctl is-active mgo-backup.timer 2>&1 || true
```

Expect `disabled` or `not-found`, and `inactive` or `not-found`.

Remove the two Task 10 unit files — and only those:

```bash
sudo rm -f /etc/systemd/system/mgo-backup.timer /etc/systemd/system/mgo-backup.service
```

Remove the Task 10 logrotate policy:

```bash
sudo rm -f /etc/logrotate.d/garden-observatory
```

Reload systemd so the removals take effect:

```bash
sudo systemctl daemon-reload
```

```bash
sudo systemctl reset-failed mgo-backup.service mgo-backup.timer 2>/dev/null || true
```

Verify the removed state:

```bash
systemctl status mgo-backup.timer --no-pager 2>&1 || true
```

```bash
systemctl status mgo-backup.service --no-pager 2>&1 || true
```

```bash
test ! -e /etc/systemd/system/mgo-backup.timer
```

```bash
test ! -e /etc/systemd/system/mgo-backup.service
```

```bash
test ! -e /etc/logrotate.d/garden-observatory
```

All three `test` commands must exit `0`. Required end state: no Task 10 backup
unit installed, no Task 10 timer enabled, no Task 10 timer active, no Task 10
logrotate policy installed.

#### Remove Task 10 compiled bytecode

**Also mandatory, and also before `git checkout main`.**

Validation imports `mgo.operations`, so Python writes compiled bytecode into
`src/mgo/operations/__pycache__/`. That bytecode is **git-ignored**, and Git
neither removes ignored files on checkout nor deletes a directory that still
contains them. `git checkout main` therefore leaves `src/mgo/operations/`
behind as a directory holding nothing but `.pyc` files — and
`git status --porcelain` reports the tree **clean**, by design, because ignored
files are exactly what it is built not to mention.

The residue is not a runtime rollback failure: no `.py` source survives, so
`import mgo.operations.backup_cli` correctly fails. But the surviving directory
**is** importable as a [PEP 420](https://peps.python.org/pep-0420/) implicit
namespace package, with `__file__` of `None` and a `__path__` pointing at the
stale directory. So `importlib.util.find_spec("mgo.operations")` returns a
specification on a checkout that is supposed to predate Task 10 entirely, and
any later check written around it would be misled.

Do this **now**, while `task-010-operations` is still checked out, because the
tracked `.py` files still exist and the deletion can therefore be confined
precisely to the Task 10 package.

Remove only compiled artefacts:

```bash
find src/mgo/operations -type f \( -name '*.pyc' -o -name '*.pyo' \) -delete
```

Then remove the `__pycache__` directories that are now empty. `-delete` implies
depth-first traversal, so nested directories are removed before their parents:

```bash
find src/mgo/operations -type d -name '__pycache__' -empty -delete
```

Now confirm nothing compiled remains:

```bash
find src/mgo/operations \( -type d -name '__pycache__' -o -type f -name '*.pyc' -o -type f -name '*.pyo' \) -print
```

**This must print nothing.** If it prints a path, a `__pycache__` directory
survived because it holds a file that is neither `.pyc` nor `.pyo`. **Stop.** Do
not delete it, do not widen the pattern, and do not continue to the checkout:
something put an unexpected file inside a build directory, and that is worth a
human looking at. A cleanup that broadens itself until its own check passes is
not a cleanup.

Note what is deliberately **not** used here:

```text
git clean -fdx        removes untracked work anywhere in the tree
git clean -fdX        removes every ignored file, including the virtualenv
rm -rf src/mgo/operations   would delete tracked source on this branch
```

Each would "work" and each would destroy far more than the compiled bytecode of
one package. The scope of this deletion is `src/mgo/operations` and the file
types are `.pyc` and `.pyo` — nothing else, and no other package.

#### What this cleanup must not touch

```text
mgo.service
/etc/garden-observatory/mgo.toml
/var/lib/garden-observatory
/var/log/garden-observatory
/var/backups/garden-observatory
```

**Removing the scheduled tooling is not the same as deleting recovery data.**
Nothing in this cleanup deletes:

* the Task 10 validation recovery set;
* any pre-existing recovery set;
* the backup directory itself;
* the production database;
* the production configuration;
* captures or observations;
* support bundles, other than the temporary validation artefacts written under
  `/tmp`;
* any log unrelated to the synthetic rotation test.

The validated recovery set **stays** in `/var/backups/garden-observatory`. It is
the evidence that the backup path worked on real hardware, and it is protected
by exactly the same rule that protects a pre-existing one.

### 13.15 Record the feature-branch runtime

Cleanup must not have disturbed the API:

```bash
systemctl is-active mgo.service
```

The API must be **active**. Now record *which process* is serving, immediately
before the checkout. Run every command in §13.15 to §13.19 in the **same shell
session**, because the recorded values are shell variables.

```bash
FEATURE_RUNTIME_PID="$(systemctl show mgo.service --property=MainPID --value)"
```

```bash
FEATURE_RUNTIME_STARTED="$(systemctl show mgo.service --property=ActiveEnterTimestamp --value)"
```

```bash
printf 'Feature runtime PID: %s\n' "$FEATURE_RUNTIME_PID"; printf 'Feature runtime started: %s\n' "$FEATURE_RUNTIME_STARTED"
```

```bash
systemctl show mgo.service --property=ActiveState --property=SubState --property=MainPID --property=NRestarts --property=ActiveEnterTimestamp
```

This is the evidence of the process that was running **feature-branch code**.
§13.17 proves it was replaced.

Do not stop the API before the operational cleanup in §13.14 is complete.

### 13.16 Return safely to `main`

Only once §13.14 has removed the installed service, timer and logrotate policy.

```bash
cd /opt/garden-observatory
```

```bash
git status -sb
```

```bash
git status --porcelain
```

The tree must be clean.

```bash
git checkout main
```

```bash
git status -sb
```

```bash
git status --porcelain
```

```bash
git branch --show-current
```

```bash
git rev-parse HEAD
```

```bash
git rev-parse origin/main
```

Required: branch `main`, `HEAD` and `origin/main` both
`0ef3d04047faef119399c46182103e6f478b8a3a`, working tree clean.

Confirm no orphaned timer survived the checkout:

```bash
systemctl list-timers --all | grep -F mgo-backup && echo "PROBLEM" || echo "clean"
```

Expect `clean`. Anything else means §13.14 did not complete and the Pi is
scheduled to run code that is no longer present.

#### Prove the Task 10 package is gone

The checkout is clean, but **a clean checkout is not proof that the Task 10
package is absent** — that is precisely what the ignored bytecode defect
exploited. Three independent checks are required, because each one alone can be
satisfied while the package is still discoverable.

The path must not exist:

```bash
test ! -e src/mgo/operations
```

This must exit `0`.

Nothing may remain there even in **ignored** state. Ordinary
`git status --porcelain` cannot answer this and must not be substituted:

```bash
git status --ignored --porcelain -- src/mgo/operations
```

Required output: nothing at all.

Finally, the package must not be importable. Run this in a **fresh** interpreter
with bytecode writing disabled, so the check cannot recreate the very artefact
it is testing for:

```bash
uv run python -B -c 'import importlib.util, sys; spec = importlib.util.find_spec("mgo.operations"); print(spec); sys.exit(0 if spec is None else 1)'
```

Required: `None`, and exit `0`. A returned specification is a **failure** even
when its `__file__` is `None`, even when no submodule can be imported, and even
when Git reports the tree clean — a namespace package is still a package, and
`find_spec` still finds it.

**The checkout is now on `main`. The running API is not.** Continue to §13.17
before treating the return to `main` as complete.

### 13.17 Restart the API onto `main`

**Git changes files on disk. It does not reload modules a running process has
already imported.** Python reads a module's source once, at import, and keeps
the compiled objects in memory for the life of the interpreter. A long-lived
Uvicorn process therefore keeps serving whatever it imported at start-up, no
matter what happens to the files underneath it.

That matters here because validation deliberately restarts §13.12 and reboots
the Pi **while `task-010-operations` is checked out** — so the process serving
the API has feature-branch `mgo.api`, `mgo.core` and `mgo.camera` modules in
memory. After §13.16, Git reports `main` and the files on disk are `main`'s, but
the live service is still running Task 10 code. Checkout plus an earlier restart
is **not** a complete rollback; the restart has to happen *after* the checkout.

Restart only once the Git checks in §13.16 have passed:

```bash
sudo systemctl restart mgo.service
```

```bash
systemctl is-active mgo.service
```

Expect `active`. Use `restart`, never `systemctl reload` — the Python process
must be **replaced**, and reloading would leave the same interpreter in place
with the same modules.

Record the new runtime:

```bash
MAIN_RUNTIME_PID="$(systemctl show mgo.service --property=MainPID --value)"
```

```bash
MAIN_RUNTIME_STARTED="$(systemctl show mgo.service --property=ActiveEnterTimestamp --value)"
```

```bash
systemctl show mgo.service --property=ActiveState --property=SubState --property=MainPID --property=NRestarts --property=ActiveEnterTimestamp
```

```bash
printf 'Feature runtime: PID %s started %s\n' "$FEATURE_RUNTIME_PID" "$FEATURE_RUNTIME_STARTED"; printf 'Main runtime:    PID %s started %s\n' "$MAIN_RUNTIME_PID" "$MAIN_RUNTIME_STARTED"
```

`MAIN_RUNTIME_STARTED` must be **later** than `FEATURE_RUNTIME_STARTED`, and the
process must have been created after the checkout. A changed PID is expected,
but the **timestamp and the successful restart are the authoritative evidence**
— an operating system is free to reuse a PID, so a differing PID alone would be
a weaker claim than it looks.

#### Prove the restarted process uses the checkout

```bash
sudo tr '\0' ' ' <"/proc/${MAIN_RUNTIME_PID}/cmdline"; printf '\n'
```

```bash
sudo readlink -f "/proc/${MAIN_RUNTIME_PID}/cwd"
```

The command must include `/opt/garden-observatory/.venv/bin/uvicorn` and
`mgo.api.app:app`; the working directory must be `/opt/garden-observatory`.

Confirm the source a fresh interpreter under the service account imports:

```bash
sudo -u mgo /opt/garden-observatory/.venv/bin/python -c 'import mgo.api.app, mgo.core.config; print(mgo.api.app.__file__); print(mgo.core.config.__file__)'
```

Both paths must be under `/opt/garden-observatory/src/mgo/`. This **supplements**
the restart rather than replacing it: it shows what the checkout would import
now, which is not the same claim as what the running process already imported.

### 13.18 Restore preview to its original state

The restart in §13.17 stopped any running preview process. Use the original
pre-validation state recorded in §13.2 — not whatever the preview happened to be
doing during validation.

```bash
curl -fsS http://127.0.0.1:8080/camera/preview/status | python -m json.tool
```

If preview **was running** before validation, restore it:

```bash
curl -fsS -X POST http://127.0.0.1:8080/camera/preview/start | python -m json.tool
```

```bash
curl -fsS http://127.0.0.1:8080/camera/preview/status | python -m json.tool
```

Required when it was originally running: `state: running`, `owner: preview`,
`last_error: null`.

If preview was originally **stopped**, leave it stopped.

Do **not** capture an image.

### 13.19 Final API check

This sweep must run **after** the checkout, the restart and the preview
restoration — anything earlier would be describing the feature-branch process.

```bash
for p in / /version /health /database/status /camera/status /camera/preview/status /motion/status /notifications/status /captures /observations /dashboard; do printf '%s -> ' "$p"; curl -s -o /dev/null -w '%{http_code}\n' "http://127.0.0.1:8080$p"; done
```

Every endpoint must return its expected successful status. Then confirm the
dashboard in a browser at `http://mgo-core:8080/dashboard`: it must show real Pi
values and continue refreshing.

### 13.20 Review the main-branch restart journal

```bash
journalctl -u mgo.service --since "10 minutes ago" --no-pager
```

Required: the intentional stop is recorded, the application shuts down cleanly,
a new process starts, start-up completes, and there is **no** traceback, import
failure, missing module, permission error or restart loop.

`mgo.operations` does not exist on `main`, and that must not matter: every
Task 10 unit was removed in §13.14, and the API has never invoked those modules.
An `ImportError` naming `mgo.operations` here would mean §13.14 did not complete.

### 13.21 Required final state

A clean Git checkout alone is **not** sufficient. All of the following must
hold:

```text
Git checkout:              main
HEAD:                      0ef3d04047faef119399c46182103e6f478b8a3a
Working tree:              clean
mgo.service:               active
mgo.service process:       started after checkout to main
API endpoints:             healthy
Dashboard:                 healthy
Preview:                   restored to original state
mgo-backup.timer:          absent
mgo-backup.service:        absent
Task 10 logrotate policy:  absent
Task 10 Python package residue: absent and not import-discoverable
Validated recovery set:    preserved
```

### 13.22 What happens next

1. The Pi checkout is clean `main`.
2. `mgo.service` has been **restarted after that checkout**.
3. The running API therefore corresponds to `main`, not to the feature branch.
4. Task 10 operational units are absent.
5. The validated recovery set remains in `/var/backups/garden-observatory`.
6. The complete validation evidence is returned.
7. Repository review happens.
8. **Only then** may a pull request be created.
9. After merge, normal production deployment installs and enables Task 10
   permanently — that installation is the one that is meant to persist.

The Pi must not be left running indefinitely from the feature branch,
pre-merge units must not be left installed while the checkout is on `main`, and
the checkout must not be left disagreeing with the running process.

## 14. Rollback

Task 10 is reversible with no data loss. There are **three** distinct states,
and they do not have the same rollback.

### 14.1 Before any Pi installation validation

Nothing on `main` was touched and no system artefact was installed. Rollback is
simply:

```bash
git checkout main
```

This stays simple **only while the Task 10 package was never imported on the
Pi**, which is what makes this state different from §14.2. Nothing imported
means Python wrote no bytecode under `src/mgo/operations`, so there is no
ignored residue for the checkout to leave behind and nothing for Git's silence
about ignored files to conceal.

That condition is checkable rather than assumed:

```bash
find src/mgo/operations -name '*.pyc' -o -name '*.pyo'
```

If that prints nothing, the checkout above is a complete rollback. If it prints
anything — a test run, a `--help`, a single import is enough — this is no longer
the simple case: use the bytecode cleanup and the three absence proofs from
§14.2 instead.

### 14.2 After pre-merge Pi installation validation, before merge

Units and policies installed under `/etc` are **external to Git** and survive a
checkout. Returning to `main` alone would leave an enabled timer pointing at a
module that no longer exists. And validation restarts and reboots the API while
the feature branch is checked out, so the **running process** holds Task 10
modules in memory that no checkout can evict. Rollback is therefore §13.14
through §13.19 in full:

1. `sudo systemctl disable --now mgo-backup.timer`;
2. remove `/etc/systemd/system/mgo-backup.timer` and
   `/etc/systemd/system/mgo-backup.service`;
3. remove `/etc/logrotate.d/garden-observatory`;
4. `sudo systemctl daemon-reload`;
5. **preserve every recovery set** — no backup is deleted at any point;
6. remove Task 10 compiled bytecode — `.pyc` and `.pyo` beneath
   `src/mgo/operations` and the `__pycache__` directories left empty — **while
   the feature branch is still checked out**, stopping if anything unexpected
   survives;
7. verify `mgo.service` is still active, and record its PID and
   `ActiveEnterTimestamp` as the feature-branch runtime;
8. `git checkout main`;
9. prove the package is gone: `test ! -e src/mgo/operations`,
   `git status --ignored --porcelain -- src/mgo/operations` empty, and
   `find_spec("mgo.operations")` is `None` in a fresh `python -B`;
10. `sudo systemctl restart mgo.service` — **after** the checkout;
11. verify the new main-branch runtime: `active`, a later
    `ActiveEnterTimestamp`, and `/proc/<pid>/cmdline` and `/proc/<pid>/cwd`
    pointing at `/opt/garden-observatory`;
12. restore preview to its original pre-validation state;
13. re-run the eleven-endpoint API sweep.

**The return to `main` is not complete until step 10 has succeeded.** Steps 1–9
leave the Pi with a `main` checkout and a feature-branch process.

Steps 6 and 9 are a pair, and the order matters as much as it does for the
units. Step 6 must precede the checkout because that is while the tracked
sources still exist and the deletion can be scoped to one package; step 9 must
follow it because Git's own cleanliness report is blind to ignored files and
cannot answer the question on its own.

### 14.3 After Task 10 is merged and deployed

The same unit removal, plus reverting the code through normal Git history:

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

Then restart the API, for the same reason as §13.17 — the **unit file** never
changed, but the revert changes `src/mgo/core/config.py`, which the API imports,
and a running interpreter does not pick that up:

```bash
sudo systemctl restart mgo.service
```

```bash
systemctl is-active mgo.service && systemctl show mgo.service --property=MainPID --property=ActiveEnterTimestamp
```

Restore preview to its previous state if it was running, then re-run the
endpoint sweep.

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
| `BACKUP_CONFIGURATION_UNAVAILABLE` | The selected configuration is missing, a symlink, over 1 MiB, not loadable, or was edited while the backup ran. Check `MGO_CONFIG_PATH` and the unit's `--config`; re-run once any edit has settled. |
| `BACKUP_SOURCE_IDENTITY_CHANGED` | The database path was repointed while the backup was opening it. Nothing was published. Investigate what is replacing files under `/var/lib/garden-observatory/db` — this is not an ordinary deployment error. |
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
  override — refused by the kernel at the open on Linux (`O_NOFOLLOW`), not by a
  check that a later open could race.
- Both sources are proven, after opening, to still be the object the path names,
  and — on the fallback platform — to be the object observed *before* the open.
  Every comparison runs while the descriptor is still held.
- The database's identity is held across SQLite's own open **and** re-verified
  after the copy, so a substitution at either point fails before publication.
- The configuration is read once; the bytes stored in a recovery set are the
  bytes that chose the database it sits beside.
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
