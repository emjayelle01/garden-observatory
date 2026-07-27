# Task 7 — Database foundation, schema migration and health check

## Status

Defined. Implementation follows in a separate commit on
`task-007-database-foundation`.

## Authoritative definition

> **Database — Create the first schema migration and a database health check.**

Task 7 is the **database foundation only**. The wider architecture calls for
SQLite, WAL mode, short transactions, integrity checking, backup/restore
support, atomic media handling and eventual entities for cameras, events,
assets, detections, sightings and reviews — none of which are implemented here
beyond what current functionality already requires.

## Starting point

| Item | Value |
| ---- | ----- |
| Branch | `main` |
| HEAD / `origin/main` | `02cb56fb935a32ef6ec29e5e4e97c1308e0d2cbe` |
| Baseline `ruff check .` | passed |
| Baseline `mypy src` | passed (35 source files) |
| Baseline `pytest` | 389 passed |

## Architecture review summary

The repository does **not** start from an empty database. Before Task 7 it
already had:

- `src/mgo/core/database.py` — a raw-`sqlite3` connection helper and an ordered,
  file-based migration runner keyed on a `schema_migrations` table;
- `migrations/001_initial_observation_engine.sql` — `schema_migrations`,
  `observations` and four observation indexes;
- `migrations/002_capture_archive.sql` — `captures` and one index;
- `src/mgo/core/observations.py` and `src/mgo/captures/archive.py` — both
  persisting through the shared `database_connection` helper;
- `apply_migrations(...)` invoked from the FastAPI lifespan before any other
  service is constructed.

Task 7 therefore **hardens and completes** that foundation rather than
replacing it. Introducing Alembic or a second migration mechanism would
duplicate working infrastructure and is explicitly rejected.

### Gaps this task closes

1. **Migrations are not transactional.** The runner uses
   `sqlite3.Connection.executescript`, which issues an implicit `COMMIT` before
   running the script. A migration that fails part-way therefore leaves its
   already-executed statements **committed**, and the surrounding
   `database_connection` rollback cannot undo them.
2. **No schema-version constant and no compatibility gate.** A database written
   by a newer application version is silently accepted.
3. **No legacy adoption path.** An existing database carrying the supported
   schema but no `schema_migrations` history is not explicitly recognised.
4. **WAL is requested but never verified.** `PRAGMA journal_mode = WAL` is
   executed and its result discarded; an in-memory database silently reports
   `memory` instead.
5. **The busy timeout is hard-coded** rather than configured.
6. **There is no database health check**, so `/health` cannot report database
   accessibility, schema version or integrity.

## Objective

A production-safe, versioned SQLite foundation that preserves all current
behaviour and existing observations, migrates transactionally, records and
exposes the schema version, configures SQLite safely for the Raspberry Pi, and
reports truthful database health through the existing health model.

## Scope

### In scope

- transactional, ordered, idempotent migration execution with real rollback;
- a `CURRENT_SCHEMA_VERSION` constant and schema-version reporting;
- safe adoption of an existing unversioned database after shape verification;
- fail-closed rejection of a database newer than the application supports;
- verified journal mode, foreign-key enforcement and a bounded busy timeout;
- explicit in-memory database handling;
- a typed database-health model and a read-only health checker;
- a background database-health monitor and lifecycle integration;
- `GET /health` database component and top-level aggregation;
- a read-only `GET /database/status` endpoint;
- `[database]` configuration, documentation and tests.

### Out of scope

Event lifecycle, motion-triggered capture, pre/post-event frame windows, bird or
object detection, species identification, sightings, review workflows,
notification rules or real providers, dashboard redesign, backup/restore
scripts, retention deletion, media reconciliation, another database engine, a
heavy migration framework, a mutating migration CLI, and Task 8 or later work.

## Compatibility requirement

Both a brand-new empty database and an existing production database from
`main` containing observation data must be supported. No observation may be
lost, rewritten or duplicated; tables are never dropped or recreated merely to
introduce versioning; an unversioned database is never assumed to be empty; and
a schema that cannot be adopted unambiguously fails clearly, leaving the
database unchanged.

## Validation

```bash
uv sync
uv run ruff check .
uv run mypy src
uv run pytest
```

All 389 existing tests plus the new migration, health, lifecycle and API tests
must pass. No Raspberry Pi hardware, network access or live production path may
be involved.

## Production safety

No Raspberry Pi access, no edit to `/etc/garden-observatory/mgo.toml`, no change
under `/var/lib/garden-observatory`, no real production database is touched, and
the branch is neither deployed nor merged. `main` is not modified.
