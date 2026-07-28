# MGO Database

MGO stores everything it records in a single SQLite database: the observation
timeline, the capture catalogue, and the schema-migration history that says
which shape that database is in.

This document covers where the file lives, how the schema is versioned and
migrated, how SQLite is configured, what the health check actually measures,
and what an operator should do when it reports something other than `healthy`.

---

## 1. Location

The database file is named by **`[storage].database_path`** — the single source
of truth for that path. There is deliberately no second setting under
`[database]` naming the same file.

| Environment | Path |
| ----------- | ---- |
| Development (tracked default) | `data/mgo.db`, relative to the repository root |
| Production (Raspberry Pi) | `/var/lib/garden-observatory/db/mgo.db` |

In production that directory is owned by the dedicated `mgo` runtime account
(see [`Service-Identity.md`](Service-Identity.md)). SQLite writes two sidecar
files alongside the database while it is open — `mgo.db-wal` and `mgo.db-shm` —
so the **directory**, not just the file, must stay writable by the service.
Those sidecars are runtime state: they are ignored by Git and must never be
committed, backed up in isolation, or copied without the main file.

A relative `database_path` is resolved against the repository root, matching
every other path in the configuration.

---

## 2. Schema version and migrations

### The mechanism

The schema is versioned by **ordered, numbered SQL files** under `migrations/`:

```text
migrations/001_initial_observation_engine.sql
migrations/002_capture_archive.sql
```

A file's version is the integer prefix of its name. Migrations are applied in
ascending version order, and each one that runs writes a row into the
`schema_migrations` table:

| Column | Meaning |
| ------ | ------- |
| `version` | The migration's numeric version (primary key). |
| `name` | The migration file that produced it. |
| `applied_at` | UTC ISO-8601 timestamp of when it was applied. |

The database's **schema version** is simply `MAX(version)` from that table. The
application's expected version is the constant
`mgo.core.database.CURRENT_SCHEMA_VERSION`, currently **2**. A test asserts the
constant stays in step with the migration files, so a new migration cannot land
without moving the application's notion of "current".

### Why `schema_migrations` and not `PRAGMA user_version`

> **`PRAGMA user_version` is intentionally unused and always reads `0`.**
>
> **A `user_version` of `0` is expected on a fully migrated, healthy database.
> It does *not* mean the database is unversioned, unmigrated or damaged.**

`schema_migrations` is the **sole authoritative source** of the schema version.
Every runtime decision resolves to it and to nothing else:

- the migration runner's "which migrations are still pending" calculation;
- the fail-closed compatibility gate that refuses a database newer than
  `CURRENT_SCHEMA_VERSION`;
- the legacy-adoption path;
- the health check's `schema_version` and `migration_status`;
- the `schema_version` reported by `GET /health` and `GET /database/status`.

Nothing in the application reads or writes `PRAGMA user_version`, so it keeps
whatever value SQLite gives a new database — `0`.

The rationale for a single authority:

- **The existing migration framework already used `schema_migrations`.** The
  table and the runner keyed on it predate this task. Moving the authority to
  `user_version` would mean stamping a *new* authority into every existing
  production database and reading both during the transition to decide which to
  trust when they disagree — a schema-authority migration, and a far larger risk
  than anything else this foundation carries.
- **The table records *which* migrations ran, not just how far the schema
  got.** `user_version` is a single integer. `version` + `name` + `applied_at`
  gives an auditable history, so an incident yields
  `002_capture_archive.sql applied 2026-07-27T14:03Z` rather than `2`. Adoption
  of a legacy unversioned database is visible after the fact for the same
  reason.
- **Two independent version authorities can diverge.** A hand-edited pragma, a
  partially restored database, or a future migration that updates one and not
  the other would produce two different answers to the same question with no
  rule for which wins. One authority cannot disagree with itself. This is the
  same reasoning that keeps the database path solely in
  `[storage].database_path` (§7).

For completeness: `PRAGMA user_version` is *not* rejected for being unsafe. It
is transactional and would roll back correctly inside a migration's
transaction. It is simply not the authority here, and is therefore left alone
rather than maintained as a second number that adds no information.

### What the initial migration contains

Migration **001** creates the schema-migration history plus the observation
timeline:

- `schema_migrations`
- `observations` — `id`, `observed_at`, `kind`, `source`, `status`, `summary`,
  `payload_json`, `correlation_id`, `created_at`
- indexes on `observed_at`, `kind`, `source` and `correlation_id`

Migration **002** adds the capture catalogue:

- `captures` — `id`, `filename`, `absolute_path` (`UNIQUE`), `captured_at_utc`,
  `width`, `height`, `filesize_bytes`, `camera_backend`, `created_at_utc`,
  `extra_metadata`
- an index on `captured_at_utc`

That is the entire schema. Tables for events, detections, sightings and reviews
are **not** created in advance: each arrives with the task that implements it.

### When migrations run

`apply_migrations(...)` runs **once at application startup**, before any other
service is constructed, from the FastAPI lifespan. It is idempotent: an
already-current database applies nothing and returns an empty list.

There is no migration CLI and no HTTP endpoint that can trigger a migration.
The only way the schema changes is by starting the application.

### Ordering and atomicity

Each migration runs inside its **own** `BEGIN IMMEDIATE` … `COMMIT`
transaction, together with the history row that records it. If any statement
fails, the whole migration is rolled back and startup fails with an error
naming the file and version. The database is therefore always at the version
*before* or the version *after* a migration — never part-way through, and never
recorded as current when it is not.

This is why the runner executes migrations statement by statement rather than
with `executescript`: `executescript` issues an implicit `COMMIT` before running
its script, which would leave a half-applied migration permanently committed.

---

## 3. Starting points the runner supports

### A brand-new (or absent) database

Every migration is applied in order. Parent directories are created.

### An existing database written by a previous release

Its `schema_migrations` history is read and only the missing migrations are
applied. Existing data is never touched, and no table is dropped or recreated
to introduce versioning.

### An existing *unversioned* database

A database that predates schema versioning has real data but no
`schema_migrations` history. It is **never assumed to be empty**. Instead the
runner:

1. reads the tables that actually exist;
2. works out the highest version those tables satisfy, in order;
3. verifies each of those tables has **exactly** the expected column set;
4. records the corresponding history rows in one transaction — creating and
   modifying **nothing else**;
5. then applies whatever migrations remain.

Adoption is logged at warning level so it appears in the journal.

If the shape cannot be recognised unambiguously the runner raises
`IncompatibleSchemaError` and **leaves the database exactly as it found it**.
That covers a known table with unexpected columns, a version's tables only
partly present, and a later version's tables existing without the ones beneath
them.

### A database from a newer build

If the recorded schema version is **higher** than
`CURRENT_SCHEMA_VERSION`, the runner fails closed with
`IncompatibleSchemaError` and changes nothing. Downgrading the application
below the data it has already written is not something MGO will do silently.
Upgrade the application, or restore a compatible database.

---

## 4. SQLite runtime configuration

Every read-write connection is opened with:

```sql
PRAGMA foreign_keys = ON;     -- enforcement is per-connection, so always set
PRAGMA journal_mode = WAL;    -- requested, then verified rather than assumed
PRAGMA busy_timeout = 5000;   -- milliseconds, from configuration
```

**Foreign keys** are off by default in SQLite and the setting is
per-connection, so it is applied on every connection rather than once at
creation time.

**WAL** lets readers proceed while a writer holds the write lock, which matters
because the observation writers, the capture archive and the health check all
touch the same file. The requested mode is *verified*: the application reads
the journal mode back and reports what SQLite actually granted. An in-memory
database silently reports `memory` — it cannot use WAL — and a file-backed
database that is not in WAL mode is reported as **degraded**, not quietly
accepted.

**The busy timeout** is a finite bound, never a retry loop. It defaults to
**5 seconds**, which comfortably covers this application's short
single-statement transactions on Raspberry Pi SD-card storage while still
failing fast enough that a genuinely stuck writer surfaces as
`database is locked` instead of hanging a request. Values must be positive and
at most 60 seconds.

Transactions are short by construction: `mgo.core.observations` and
`mgo.captures.archive` each open a bounded connection per operation, run one
statement, commit and close. No long-lived connection is held, and all SQL is
parameterised.

### In-memory databases

`:memory:` is recognised explicitly. The migration runner works against it, but
each connection to `:memory:` is a *different* empty database, so it is
transient, non-shareable and **cannot be health-checked** — the health check
reports it as unhealthy and says why, rather than fabricating a result. It is
not a supported production configuration.

---

## 5. Health check

### What it measures

A background monitor runs one read-only check every
`[database].health_check_interval_seconds` (default 60) and stores the result
in application state. Each check reports:

- whether the database is accessible;
- the `PRAGMA quick_check(1)` integrity verdict;
- the recorded schema version and the version the application expects;
- the migration status (`current`, `pending`, `ahead`, `unknown`);
- the actual journal mode;
- whether foreign-key enforcement is active;
- a bounded human-readable detail string and the check timestamp.

### What it will never do

The check opens the database through SQLite's `mode=ro` URI. It therefore
**cannot**:

- create a missing database file or a missing directory;
- create a missing table;
- change the journal mode;
- repair corruption, vacuum, or rewrite anything.

It also holds no long lock, runs no unbounded operation, and always closes its
connection — including on failure.

### `quick_check` vs `integrity_check`

The production policy is **`PRAGMA quick_check(1)`**.

`quick_check` verifies page structure and record sanity but skips the
index-content cross-checks that make `integrity_check` expensive; the cost of
the full check grows with the database and would be paid on every poll on a
Raspberry Pi. The `(1)` caps the report at a single error so a corrupt database
cannot produce an unbounded result. `quick_check` reliably detects the
page-level and structural corruption an SD-card failure produces, which is the
realistic failure mode here.

A full `integrity_check` belongs to operator-invoked diagnostics, which is a
later task. **Backup and restore are not implemented in this task.**

### States and their exact meanings

| State | Meaning | `/health` contribution |
| ----- | ------- | ---------------------- |
| `healthy` | Reachable, structurally sound, at the expected schema version, foreign keys enforced, WAL in use. | `healthy` |
| `degraded` | Reachable and sound, but running with a documented deviation: schema behind the application, foreign keys not enforced, or a file-backed database not in WAL. Reads and writes still work. | `warning` |
| `unhealthy` | Not usable: unreachable, corrupt, carrying no schema history at all, or recording a version newer than this build supports. | `critical` |

`unhealthy` is also the state reported before the first check has run — the
default is never optimistic.

---

## 6. API

### `GET /health`

The response gains a `database` section and the database now contributes to the
top-level `status`:

```json
{
  "status": "healthy",
  "database": {
    "status": "healthy",
    "accessible": true,
    "schema_version": 2,
    "expected_schema_version": 2,
    "migration_status": "current",
    "integrity": "ok"
  },
  "camera": { "...": "unchanged" },
  "preview": { "...": "unchanged" }
}
```

Every pre-existing field keeps its name and meaning. Camera readiness is still
reported entirely on its own terms, so a database fault is never mislabelled as
a camera failure — or the reverse.

### `GET /database/status`

The full result, following the same read-only pattern as `/camera/status`,
`/motion/status` and `/notifications/status`:

```json
{
  "status": "healthy",
  "accessible": true,
  "database": "mgo.db",
  "schema_version": 2,
  "expected_schema_version": 2,
  "migration_status": "current",
  "journal_mode": "wal",
  "foreign_keys": true,
  "integrity": "ok",
  "detail": "Database is at schema version 2 with wal journalling and foreign keys enforced.",
  "checked_at": "2026-07-27T10:00:00+00:00"
}
```

It returns HTTP `200` whenever the application is serving — including when the
database itself is unhealthy, which is reported in `status` rather than as an
HTTP error. It reads cached state and performs **no database I/O per request**,
so polling it can never add load to a struggling database.

`database` is the file's **name**, not its absolute path. The status endpoints
expose no filesystem layout, and an operator already has the configured path
from the configuration file and the service logs.

Neither endpoint can run a migration or mutate anything. There is no public
database mutation endpoint.

---

## 7. Configuration

```toml
[storage]
database_path = "/var/lib/garden-observatory/db/mgo.db"   # the file itself

[database]
health_check_interval_seconds = 60   # how often the read-only check runs
busy_timeout_seconds = 5.0           # finite wait for a competing writer
```

The `[database]` section is **optional**: a configuration file without it loads
unchanged, and the defaults are exactly the values the application used before
the section existed, so an existing deployment behaves identically.

Invalid values fail at startup with a clear error: an interval below 10 seconds,
a non-positive busy timeout, or a busy timeout above 60 seconds.

The production configuration is selected by `MGO_CONFIG_PATH` exactly as before.
No secret belongs in this section.

---

## 8. Logging

The database layer logs migration start and completion, each version applied,
legacy adoption (at warning level), and **material** health transitions — a
change of health state or migration status. It does **not** log successful
health polls, database contents, SQL parameters, or unbounded exception output.

A stable database therefore produces no log noise however often it is polled.

---

## 9. Observation policy

Database-health transitions are recorded in the observation timeline, but
**asymmetrically and deliberately**:

- a material transition **into** a usable state (`healthy` or `degraded`),
  including recovery from `unhealthy`, is persisted as a `database_health`
  observation carrying the state it recovered from;
- a material transition **into** `unhealthy` is **logged only**;
- an unchanged status writes nothing, however often it is polled.

The reason is recursion: writing "the database is unhealthy" *into that
database* fails for the same reason the check did, and retrying on every poll
would produce a write storm against already-struggling storage. The unhealthy
period is still visible — the recovery observation records what it recovered
from, and the service journal carries the failure itself.

---

## 10. Startup ordering

1. Configuration is loaded and validated.
2. The database path is resolved and its parent directory created.
3. Migrations are applied (or the schema is adopted / rejected).
4. The capture archive is attached.
5. The **initial** database-health check runs, so `/health` and
   `/database/status` are truthful from the first request.
6. Background monitors start (health, database, camera, and motion if enabled).
7. The rest of the application lifecycle starts.

A migration failure propagates out of the lifespan, so **the application
refuses to start** rather than serving against a schema it cannot trust. There
is no partially migrated running state.

Camera absence cannot affect any of this: the database is migrated and checked
before the camera is even detected.

On shutdown the database monitor is stopped with the same stop-event and
`gather` mechanism as the existing monitors; no thread, task or connection is
leaked, and camera, preview, motion and notification shutdown are unchanged.

---

## 11. Operator troubleshooting

### Inspect the schema version safely

There are exactly **two** supported ways to check the schema version. Use
either; they always agree.

Ask the application, which reads cached state and never opens a connection:

```bash
curl -s http://localhost:8080/database/status
```

Or query the authoritative table directly — read-only, on the Pi, without
touching the running service:

```bash
sudo -u mgo sqlite3 -readonly /var/lib/garden-observatory/db/mgo.db "SELECT version, name FROM schema_migrations ORDER BY version;"
```

Add `applied_at` to that `SELECT` when you also want to know *when* each
migration ran.

> **Do not use `PRAGMA user_version` to check the schema version.**
>
> It is intentionally unused by MGO and **always returns `0`**, including on a
> fully migrated, healthy database. A `0` there is expected and means nothing
> about the schema — see
> [§2, Why `schema_migrations` and not `PRAGMA user_version`](#why-schema_migrations-and-not-pragma-user_version).
> The authoritative version is `MAX(version)` from `schema_migrations`, which is
> what every runtime compatibility check uses.

### `status: unhealthy`, `accessible: false`

The file or its directory is missing, or the service cannot read it. Check
ownership and the state directory:

```bash
sudo bash scripts/deploy/verify-service-identity.sh
```

### `status: unhealthy`, `integrity` is not `ok`

The database is corrupt — on a Raspberry Pi this usually means the SD card. Stop
the service before doing anything else, and preserve the file (plus its `-wal`
and `-shm` sidecars) before attempting any recovery. Recovery tooling is not
part of this task.

### `status: unhealthy`, `migration_status: ahead`

The database was written by a newer build of the application than the one
running. Deploy the newer build; do not try to force it open.

### `status: unhealthy`, `migration_status: unknown`

The database has no `schema_migrations` history and was not adoptable. Check the
startup log for the `IncompatibleSchemaError` explaining which table did not
match. The database has not been modified.

### `status: degraded`, `migration_status: pending`

The schema is behind the application, which normally means a migration failed at
startup. The startup log names the failing migration and version.

### `status: degraded`, journal mode is not `wal`

The filesystem holding the database did not accept WAL. Confirm the database is
on local storage, not a network mount.

### `database is locked`

A writer held the lock for longer than `busy_timeout_seconds`. On this
application's short transactions that indicates a stuck process rather than
contention — check for a second instance of the service.

---

## 12. Known limitations and what remains deferred

Implemented here is the database **foundation** only.

**Backup and restore were delivered later, by Task 10.** See
[`Operations.md`](Operations.md): a consistent online backup taken while the API
keeps serving, a JSON manifest, checksum verification, isolated restore testing,
bounded retention and a daily timer. Production *restore* remains deliberately
manual — `restore-test` proves recoverability, and restoring over the live
database is an explicit operator procedure. Nothing in that tooling changes the
schema, the migration files or any row: the backup reads through SQLite's
`mode=ro` URI, so it cannot modify, checkpoint or vacuum this database.

Not implemented, and not claimed to be:

- full `integrity_check` diagnostics, `VACUUM`, or any repair capability;
- retention or deletion of old rows;
- media reconciliation between the `captures` table and files on disk;
- schema entities for events, detections, sightings, identifications and
  reviews — each arrives with the task that implements it;
- any migration **down**: migrations are forward-only.

Other limitations worth knowing:

- an in-memory database is not health-checkable and is not a supported
  production configuration;
- adoption of a legacy unversioned database matches on exact column sets, so a
  hand-modified schema is refused rather than adopted;
- the health check's `quick_check` will not detect index-content corruption that
  a full `integrity_check` would.

---

## 13. Raspberry Pi validation procedure

To be run by an operator on the Pi **after** the branch is reviewed and merged.
It is non-destructive and read-only apart from the normal service restart.

1. **Back up the existing database first**, while the service is stopped:

   ```bash
   sudo systemctl stop mgo.service
   ```

   ```bash
   sudo -u mgo cp -a /var/lib/garden-observatory/db/mgo.db /var/lib/garden-observatory/db/mgo.db.pre-task7
   ```

2. Record the observation count before the upgrade:

   ```bash
   sudo -u mgo sqlite3 -readonly /var/lib/garden-observatory/db/mgo.db "SELECT COUNT(*) FROM observations;"
   ```

3. Deploy the new revision and start the service:

   ```bash
   sudo systemctl start mgo.service
   ```

4. Confirm startup migrated cleanly and nothing failed:

   ```bash
   sudo journalctl -u mgo.service -n 100 --no-pager
   ```

5. Confirm the reported database health:

   ```bash
   curl -s http://localhost:8080/database/status
   ```

   Expect `status: healthy`, `schema_version: 2`, `migration_status: current`,
   `journal_mode: wal`, `foreign_keys: true`, `integrity: ok`.

   **This is the schema-version check.** Do not cross-check it with
   `PRAGMA user_version` — that pragma is intentionally unused and returns `0`
   on a correctly migrated database (see §2 and §11). To confirm against the
   database directly, query `schema_migrations`:

   ```bash
   sudo -u mgo sqlite3 -readonly /var/lib/garden-observatory/db/mgo.db "SELECT version, name FROM schema_migrations ORDER BY version;"
   ```

   Expect rows `1 | 001_initial_observation_engine.sql` and
   `2 | 002_capture_archive.sql`.

6. Confirm `/health` reports the database and an unchanged overall contract:

   ```bash
   curl -s http://localhost:8080/health
   ```

7. Confirm no observation was lost — the count must match step 2, plus the new
   startup rows:

   ```bash
   sudo -u mgo sqlite3 -readonly /var/lib/garden-observatory/db/mgo.db "SELECT COUNT(*) FROM observations;"
   ```

8. Confirm the WAL sidecars are present and owned by the service account:

   ```bash
   sudo ls -l /var/lib/garden-observatory/db/
   ```

9. Restart once more and confirm the second start applies **no** migrations
   (proving idempotency in production):

   ```bash
   sudo systemctl restart mgo.service; sudo journalctl -u mgo.service -n 50 --no-pager
   ```

If any step fails, stop the service and restore `mgo.db.pre-task7`.
