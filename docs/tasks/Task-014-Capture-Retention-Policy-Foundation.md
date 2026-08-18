# Task 14.1 — Capture Retention Policy Foundation

**Status: implemented, validated off-Pi, awaiting architectural and QA review.**

**Not merged. No pull request. Not deployed. No Raspberry Pi access. No
production change. No physical validation.**

---

## 1. Scope

Task 14.1 establishes the **software foundation** for safe, deterministic
retention of captured media. It provides an optional disabled-by-default
configuration, a deterministic policy planner, durable media-lifecycle state
that never erases capture history, a recoverable single-run deletion executor,
immutable retention observations, bounded destructive behaviour, a strict
filesystem boundary, process-lifetime runtime status, a read-only status
endpoint, and comprehensive automated and mutation coverage.

It does **not** schedule retention, does not enable retention on the Raspberry
Pi, does not enable motion/event capture in production, and does not physically
validate deletion.

### Why now

Task 13 proved MGO can physically perform the whole automatic-capture
transaction. That pipeline remains deliberately disabled in production, and the
blocker is media accumulation: during the bounded Task 13.2 physical validation,
**17 captures produced 42,660,151 bytes of JPEG payload during a 341-second
enabled window**. That is a point-in-time measurement, **not** a long-term rate
prediction — and it is sufficient to show that an unattended capture pipeline
cannot be enabled responsibly while captured media has no lifecycle.

---

## 2. Baseline

| Item | Value |
| --- | --- |
| Repository | `C:\AI\garden-observatory` (`emjayelle01/garden-observatory`) |
| Starting SHA | `ec21997452fbe92f42a1e085835028588d9525cc` |
| Starting branch | `main` |
| Starting `origin/main` | `ec21997452fbe92f42a1e085835028588d9525cc` |
| Task branch | `task-014-capture-retention-policy-foundation` (created from `ec21997`) |

Preflight: clean working tree, empty stash, one normal worktree, current branch
`main`, no unexpected local or remote branch, no Git operation in progress.

Baseline validation on the clean tip, before any implementation:

| Gate | Result |
| --- | --- |
| `uv sync --frozen` | Checked 36 packages |
| `uv run ruff check .` | All checks passed |
| `uv run mypy src` | Success: no issues found in 54 source files |
| `uv run pytest` | **2567 passed, 12 skipped, 0 failed** |

The 12 skips reconcile exactly to the existing accepted Windows/POSIX capability
skips (symlink creation and POSIX mode bits in the operations suites).

Production was **not** touched. Production executable code remains the
previously deployed Task 13.1 code `48bdaf39f4f903931352796c5bc159621e1f730e`
with motion disabled, event capture disabled, the pre-validation configuration
restored and deployment approval withdrawn.

---

## 3. Architectural decisions

### 3.1 A separate lifecycle table, not a column on `captures`

The `captures` table is the historical catalogue of captures that **happened**.
Reclaiming a JPEG does not un-happen the capture that produced it, so "a capture
occurred" and "its media was later reclaimed" are two different facts and live
in two places. Retention never deletes a `captures` row and never repurposes
`extra_metadata` as a hidden retention database.

The decision is also load-bearing for compatibility: the repository's
legacy-schema adoption compares an **exact** column set per table, so a retention
column added to `captures` would have made every existing unversioned version-2
database unrecognisable. Keeping migration 003 additive preserves the version-2
capture-table shape, so an unversioned version-2 database is still adopted at
version 2 and then migrated to version 3.

### 3.2 The catalogue nominates; the filesystem only vetoes

Planning uses catalogue timestamps and catalogue byte sizes, never a directory
listing. Every filesystem check in the executor can **refuse** a deletion and
none can propose one. Nothing scans a directory and decides that old-looking
JPEGs are disposable.

### 3.3 A durable intent, because two systems cannot share one transaction

Filesystem deletion and SQLite cannot be atomic together, so a committed
`pending_delete` row is written **before** the filesystem is touched. That row is
the entire difference between a recoverable interruption and an unexplained
missing file.

### 3.4 One observation engine, not two

Rather than copying an observation `INSERT` into the retention repository, the
observation module was refactored into `build_observation` (validation) and
`insert_observation` (the single `INSERT`), with `record_observation` and the new
connection-aware `record_observation_in_transaction` both built on them. The
public `record_observation(...)` behaviour is unchanged and every existing
observation test and API contract still passes.

### 3.5 A locked runtime-state holder

Unlike the event-capture holder — read and written by one event loop — a
retention run is synchronous, blocking work expected to be driven from a worker
thread while the status endpoint reads from the event loop. The holder therefore
takes a `threading.Lock`.

### 3.6 Conservative failure

A destructive run stops at the first safety-relevant failure. Completed
deletions stand; unattempted candidates are left entirely alone; the **first**
fixed category is reported. Raw exceptions are logged and never published.

---

## 4. Configuration contract

```toml
[retention]
enabled = false
# max_age_days = 30
# max_managed_bytes = 2147483648
minimum_keep_count = 100
max_deletions_per_run = 25
```

`RetentionConfig` is an immutable dataclass with `enabled`, `max_age_days`,
`max_managed_bytes`, `minimum_keep_count`, `max_deletions_per_run`. It is the
last field of `MGOConfig` and carries a default, so every existing construction
keeps working with retention off.

Safe defaults for an absent section: `enabled = false`, both bounds `None`,
`minimum_keep_count = 100`, `max_deletions_per_run = 25`. An absent section and
an explicitly empty one produce an identical configuration.

Validation:

* `max_age_days`, when supplied, must be `> 0`;
* `max_managed_bytes`, when supplied, must be `> 0`;
* `minimum_keep_count` must be `>= 1`;
* `max_deletions_per_run` must be `>= 1`;
* `enabled = true` requires `max_age_days` and/or `max_managed_bytes`; enabling
  with neither is rejected.

Retention deliberately requires **neither** `event_capture.enabled` **nor**
`camera.enabled`: it must be able to reclaim media a now-disabled capture feature
already produced, and it is a storage-lifecycle concern rather than a camera
operation.

Error messages name only the setting at fault — no configuration path, capture
directory, database location or unrelated value.

There is **no retention interval** in Task 14.1.

---

## 5. Managed-capture definition

Only captures whose stored `extra_metadata` contains exactly
`origin = "motion"` are managed. **Protected, and never selectable:** manual
captures, captures with no `origin`, captures with an unknown origin, and
captures from any future origin not exactly equal to `"motion"`.

Matching is exact equality — `"Motion"`, `"MOTION"`, `" motion"`,
`"motion_burst"` are all different origins and all protected. A non-string
`origin` is treated as absent rather than coerced with `str()`.

---

## 6. Policy semantics

`mgo.retention.policy.plan_retention` is a pure function of the catalogue, the
configuration and one instant. It opens no file, stats no path, touches no
database and mutates nothing.

1. **Manage** — keep only `origin = "motion"` captures whose media is `PRESENT`.
2. **Preserve** — order oldest-first and set aside the newest
   `minimum_keep_count`.
3. **Select** — age-expired eligible captures first, then, if the projected
   managed total is still above `max_managed_bytes`, further oldest eligible
   captures. Both rules act on the same ordering and therefore each select a
   prefix, so the union is the longer prefix and nothing is selected twice.
4. **Bound** — truncate to `max_deletions_per_run`, reporting the remainder.

**Ordering** is `captured_at_utc`, then `created_at_utc`, then capture `id` —
three levels, so SQLite's incidental row order can never decide what is deleted.

**Age boundary** is inclusive (`captured_at_utc <= now_utc - max_age_days`),
timezone-aware UTC throughout.

**Managed-byte policy** covers only the catalogue `filesize_bytes` of PRESENT
managed motion captures. It excludes manual captures, unknown-origin captures,
the database, logs, filesystem overhead and already-deleted captures. The limit
is inclusive on the permitted side. It is deliberately not a filesystem
utilisation policy.

**Reason vocabulary** is fixed and enforced by a database `CHECK`: `age`,
`managed_bytes`, `age_and_managed_bytes`. The combined reason is recorded when
both rules independently reach a capture. No free-form prose is ever persisted
as a reason.

### Minimum-keep behaviour

The newest `minimum_keep_count` managed PRESENT captures are protected
regardless of age or byte pressure and can never be selected. If the preserved
set alone exceeds `max_managed_bytes` the target cannot be met; the plan reports
`byte_target_satisfiable = false` rather than breaking the floor.

### Per-run deletion bound

No run may **newly** select more than `max_deletions_per_run` captures. When
candidates remain, `more_work_remains = true` is reported by both the plan and
the run result. The cap is never silently exceeded. Pending-deletion recovery is
separate from this bound.

---

## 7. Migration 003

`migrations/003_capture_media_lifecycle.sql` creates:

```sql
CREATE TABLE IF NOT EXISTS capture_media_lifecycle (
    capture_id TEXT PRIMARY KEY REFERENCES captures(id),
    state TEXT NOT NULL CHECK (state IN ('pending_delete', 'deleted')),
    requested_at_utc TEXT NOT NULL,
    deleted_at_utc TEXT,
    reason TEXT NOT NULL
        CHECK (reason IN ('age', 'managed_bytes', 'age_and_managed_bytes')),
    CHECK (
        (state = 'pending_delete' AND deleted_at_utc IS NULL)
        OR (state = 'deleted' AND deleted_at_utc IS NOT NULL)
    )
);
CREATE INDEX IF NOT EXISTS idx_capture_media_lifecycle_state
    ON capture_media_lifecycle(state);
```

Migrations 001 and 002 were **not modified**. No retention column was added to
`captures`. `CURRENT_SCHEMA_VERSION` is now **3**, and `_VERSION_TABLES` /
`_VERSION_TABLE_COLUMNS` recognise the new table for legacy adoption.

**Final schema version: 3.**

Absence of a lifecycle row means `PRESENT`; `pending_delete` means retention
deletion intended and incomplete; `deleted` means media reclaimed. The original
`captures` row is kept permanently.

Migration results proven by test:

| Property | Result |
| --- | --- |
| Empty database reaches version 3 | Yes (`applied == [1, 2, 3]`) |
| Normal version-2 database upgrades to 3 | Yes (`applied == [3]`) |
| Existing capture rows survive byte-for-byte | Yes (full-row comparison) |
| Existing observation rows survive | Yes (full-row comparison) |
| Repeated application idempotent | Yes |
| Database newer than 3 rejected | Yes |
| Unversioned version-2 adopted at 2, then migrated to 3 | Yes |
| Unversioned version-3 shape recognised at 3 | Yes (`applied == []`) |
| Malformed/partial legacy shapes rejected | Yes, database untouched |
| Foreign keys enabled | Yes |

---

## 8. Lifecycle state implementation

`RetentionRepository` owns the retention projection (`captures LEFT JOIN
capture_media_lifecycle`) and the conditional transitions. It does not duplicate
`CaptureArchive` and did not require widening the capture archive's public API.

* **Claim (Stage A)** — `INSERT ... ON CONFLICT(capture_id) DO NOTHING`, returning
  whether *this* call created the intent. Two overlapping executions cannot both
  claim one capture; an already-`deleted` capture cannot be claimed again.
* **Finalise (Stage C)** — `UPDATE ... WHERE capture_id = ? AND state =
  'pending_delete'`. If it matches nothing, **no observation is written** and
  `False` is returned. The transition and the immutable observation commit in one
  transaction.
* **Cancel** — `DELETE ... WHERE capture_id = ? AND state = 'pending_delete'`,
  committed with the failure observation.

Malformed stored `extra_metadata` JSON, an unrecognised stored state, an
unrecognised stored reason and a naive stored timestamp all raise
`RetentionCatalogueError` — the run **fails closed** and no deletion occurs after
catalogue parsing has become untrustworthy.

---

## 9. Deletion state machine

**Stage A** — durable intent, committed before the filesystem is touched. No
database transaction is held afterwards.

**Stage B** — full safety validation runs again as close to the deletion as the
code can get it, then exactly one file is unlinked. No database lock is held.
Validation also runs *before* Stage A, so a candidate whose media is already
missing or whose path is unsafe never acquires an intent at all.

**Stage C** — the `deleted` transition and the success observation commit
together. If Stage C fails after the file is gone, the lifecycle row remains
`pending_delete`. Nothing fabricates completion in memory.

**Deletion failure** (Stage B fails, file still present) — one transaction
cancels the intent (returning the media logically to `PRESENT`) and records the
failure observation. If that recovery transaction itself fails, the durable
`pending_delete` is left intact and the **first** category is still what the run
reports. One attempt per capture per run; nothing retries inside a run.

---

## 10. Pending recovery

Pending entries are inspected **before** new candidates are selected.

| Pending state | Behaviour |
| --- | --- |
| File still safely exists | Revalidate fully, complete the deletion, finalise. |
| File is absent | Finalise as `deleted` with `recovered_pending: true`. |
| Unsafe path / directory / symlink / size mismatch / unverifiable | Do not touch. Report the fixed category and stop the run, leaving the intent standing. |

Recovery is **not** governed by the current `minimum_keep_count`: the intent
represents an earlier durable destructive decision that may already have been
carried out.

**When `retention.enabled = false`, no recovery mutation occurs.**

`PRESENT` + missing media is **not** treated as recovery: it is reported as
`media_missing`, never marked deleted and never given a fabricated success
observation. The distinction is load-bearing.

---

## 11. Filesystem safety

Before unlinking, all of: path is absolute; no `..` component (rejected
syntactically, before normalisation could erase the evidence); catalogue filename
equals the path's final component; the target is not itself a symlink (checked
before resolution); the resolved parent is the configured capture root or lies
beneath it; the capture root itself resolves to an absolute existing directory;
the target is a regular file, not a directory; the on-disk size equals the
catalogue `filesize_bytes`.

Any failure means **do not delete, fail closed**, with a fixed category. Nothing
is deleted recursively, no `rmtree` or `rmdir` exists anywhere in the package
(asserted by test), no directory is removed, and an untracked file in the capture
folder is never touched.

### Path escape protection

Tested explicitly, all inside temporary directories: `..` escape; a `..` path
that normalises back *inside* the root; an absolute path outside the root; a
relative path, including one the working directory would resolve inside the root;
filename/path disagreement; a directory target; a symlink target; a size
mismatch; a relative capture root that really exists relative to the working
directory. Symlink and non-regular-file refusals use monkeypatched filesystem
seams rather than platform-specific skips, so they are tested on Windows too.
**No file outside the temporary test capture root is touched by any test.**

### Manual and unknown protection

Tested at the boundary: the unlink seam is replaced with a double that fails the
test if it is called at all, so manual, no-origin and unknown-origin captures are
proven never to reach the filesystem call — not merely observed to survive.

---

## 12. Dry run and disabled behaviour

`dry_run()` reads the catalogue, evaluates the policy and reports candidates,
projected recovery and satisfiability. It creates no lifecycle row, modifies
none, deletes no file, records no observation, alters no capture and moves no
counter. It is available whether or not retention is enabled.

With `enabled = false`, `run_once()` performs **no filesystem mutation and no
database mutation** — including no pending recovery — and returns a truthful
result (`executed=False`, `enabled=False`, no error). A disabled runtime state
stays in `disabled` and records no run. No startup code executes retention.

---

## 13. Concurrency

A process-level `threading.Lock` is acquired non-blocking. A second overlapping
destructive run returns `executed=False` with category `busy`, deletes nothing,
records nothing and increments no counter. Nothing queues. Combined with the
conditional database transitions, **no capture can receive two successful
retention observations for one deletion** — proven by test both at the repository
level (four finalisations produce `[True, False, False, False]` and one
observation) and end to end.

---

## 14. Observations

| Field | Value |
| --- | --- |
| `kind` | `capture_retention` |
| `source` | `mgo-retention` |
| success `status` / `summary` | `reclaimed` / `Capture media removed by retention policy` |
| failure `status` / `summary` | `failed` / `Capture media retention failed` |
| `correlation_id` | the capture id |

Success payload: `capture_id`, `filename`, `filesize_bytes`, `policy_reason`,
`captured_at`, `recovered_pending`. Failure payload: `capture_id`, `filename`,
`error_category`, `policy_reason`.

Never included: `absolute_path`, capture directory, database path, command lines,
tracebacks, raw exception strings — asserted by test against the rendered
payload.

Fixed public error categories: `unsafe_path`, `media_missing`,
`not_regular_file`, `size_mismatch`, `filesystem_delete_failed`,
`database_transition_failed`, `finalization_failed`, `catalogue_invalid`, `busy`,
`unexpected`.

---

## 15. Status API

`GET /retention/status` returns `enabled`, `state`, `total_runs`,
`total_captures_deleted`, `total_bytes_reclaimed`, `last_run_at`,
`last_run_candidate_count`, `last_run_deleted_count`, `last_run_bytes_reclaimed`,
`last_error`. States: `disabled`, `idle`, `running`, `error`.

It is **inert**: it does not run the planner, query SQLite, stat a file, delete
anything, create lifecycle state, record an observation, start a worker, touch
the camera or run a migration, and it moves no counter. Proven by attaching
doubles that fail on any access, by trapping `sqlite3.connect`, the filesystem
seams, `plan_retention`, `run_once`, `dry_run` and `_unlink`, and by asserting a
snapshot is unchanged after 50 requests.

It returns HTTP 200 whenever the API is serving, including `disabled` and
`error`. No path or raw exception appears in the response.

**There is no `POST /retention/run`.** A test asserts that no route under
`/retention` accepts any method beyond `GET`/`HEAD`/`OPTIONS`, and that
`/retention/status` is the only `/retention` path in the OpenAPI document.

### Lifecycle wiring

Startup attaches the runtime state and the service. Construction is inert:
`run_once` and `dry_run` are replaced with traps during the real lifespan tests
and are never called. No retention asyncio task, timer, interval loop, scheduler,
startup deletion or shutdown deletion exists — asserted by inspecting live task
names and by proving a full start-and-stop cycle leaves the lifecycle table empty
and an eligible file on disk. No retention shutdown stage was required.

---

## 16. Files changed

**Added**

* `migrations/003_capture_media_lifecycle.sql`
* `src/mgo/retention/__init__.py`
* `src/mgo/retention/models.py`
* `src/mgo/retention/policy.py`
* `src/mgo/retention/repository.py`
* `src/mgo/retention/service.py`
* `tests/test_retention_config.py`
* `tests/test_retention_policy.py`
* `tests/test_retention_database.py`
* `tests/test_retention_service.py`
* `tests/test_retention_api.py`
* `docs/Retention.md`
* `docs/tasks/Task-014-Capture-Retention-Policy-Foundation.md`

**Modified**

* `src/mgo/core/config.py` — `RetentionConfig`, defaults, validation, parsing
* `src/mgo/core/database.py` — `CURRENT_SCHEMA_VERSION = 3`, version tables/columns
* `src/mgo/core/observations.py` — `build_observation`, `insert_observation`,
  `record_observation_in_transaction`; `record_observation` behaviour unchanged
* `src/mgo/api/app.py` — response model, state accessor, lifespan wiring,
  `GET /retention/status`
* `tests/mutation_register.py` — retention mutations
* `tests/test_app_routes.py` — retention lifespan wiring tests, config template
* `tests/test_database_migrations.py`, `tests/test_database.py`,
  `tests/test_captures.py` — version-3 expectations
* `config/mgo.toml`, `config/mgo.production.example.toml` — `[retention]`, disabled
* `README.md`, `docs/API.md`, `docs/Database.md`, `docs/Event-Capture.md`

No unexpected file was changed. No deployment script, systemd unit, sudoers or
gateway policy was touched. `pyproject.toml` and `uv.lock` are unchanged.

Existing assertions were updated only where migration 003 genuinely changed the
expected value (applied-version lists, recorded history rows, the renumbered
broken-migration fixture, and the unversioned-adoption cases). No assertion was
weakened, no test deleted and no failure converted into a skip. Historical Task
12 and Task 13 evidence records were not rewritten.

---

## 17. Validation

| Gate | Result |
| --- | --- |
| `uv sync --frozen` | Checked 36 packages |
| `uv run ruff check .` | All checks passed |
| `uv run mypy src` | Success: no issues found in 59 source files |
| Focused retention/database/config/API tests | See below |
| `uv run pytest` (complete suite) | See §17.2 |
| `uv run python scripts/dev/run-mutations.py` | See §17.3 |
| `git diff --check` | PASS |

### 17.1 Focused results

| Suite | Result |
| --- | --- |
| `tests/test_retention_config.py` | 20 passed |
| `tests/test_retention_policy.py` | 42 passed |
| `tests/test_retention_database.py` | 46 passed |
| `tests/test_retention_service.py` | 54 passed |
| `tests/test_retention_api.py` | 20 passed |
| `tests/test_database_migrations.py` | 25 passed |
| `tests/test_app_routes.py` | 54 passed |

Run together with the capture, observation and event-capture suites as one
focused selection: **377 passed, 0 failed**.

### 17.2 Complete suite

**2756 passed, 12 skipped, 0 failed.**

Skip reconciliation: the 12 skips are byte-identical to the baseline — the same
files and line numbers, all pre-existing Windows/POSIX capability skips (symlink
creation and POSIX mode bits in `test_operations_backup.py`,
`test_operations_events.py`, `test_operations_source_identity.py` and
`test_operations_support_bundle.py`). **No new skip was introduced.** An earlier
draft of the lifecycle-state test used a conditional skip; it was replaced with a
deterministic hand-edited-schema fixture precisely so it could not add one.

### 17.3 Mutation testing

**Total registered mutations: 222** (up from the established 182 — 40 added).

Result: **222/222 detected, 0 stale, 0 restoration failures, 0 unmatched
selectors.**

Coverage added spans origin protection, minimum-keep protection, the age
boundary, the byte boundary, byte-pressure minimality, the per-run batch bound,
remaining-work reporting, both ordering tie-breaks, byte-target satisfiability,
combined-reason classification, path containment, traversal rejection, filename
agreement, symlink refusal, absolute-path requirement, the regular-file check,
the size check, missing-media handling, capture-root validation, the disabled
gate, busy reporting, run-lock exclusivity, stop-on-first-failure, pending
recovery (existence, revalidation and unsafe-path handling), failed
finalisation, dry-run non-mutation, the three conditional lifecycle transitions,
the shared observation validation and `INSERT`, the runtime-state transitions and
the inertness of the status endpoint.

Six registered mutations initially survived. Each was a genuine gap and each was
closed by strengthening a test rather than by weakening the mutation:

1. *creation-time tie-break* — the fixture's ids happened to sort the same way as
   the creation times; the ids now sort the opposite way.
2. *traversal rejection* — the original case was also caught by containment; a
   new case uses a `..` path that normalises back **inside** the root, which only
   the syntactic rule refuses.
3. *absolute catalogue path* — the original case was also caught by containment;
   a new case sets the working directory inside the capture root so the relative
   path would otherwise resolve and pass every downstream check.
4. *directory target* — genuinely redundant with the regular-file rule, which
   already carries a detected mutation. The explicit check was kept and
   documented as deliberate defence in depth, and the duplicate register entry
   was removed rather than kept as a mutation nothing can detect.
5. *absolute capture root* — the original case relied on the relative root not
   existing; it now sets the working directory so the relative root really does
   resolve to the real capture directory.
6. *failed finalisation* — the existing test exercised the *raising* path; a new
   test exercises the *returns-False* path, where the conditional `UPDATE`
   matched nothing.

---

## 18. Limitations

* **No physical validation.** No deletion has been performed against the
  Raspberry Pi, the production database or the production media directory.
* **Retention is unproven at scale.** The planner and executor have been
  exercised only against temporary test catalogues and files.
* **No production values exist.** The retention period and storage budget have
  not been decided; both tracked configurations leave the destructive bounds
  commented out.
* **Task 13.2 was point-in-time physical validation only.** Long-term unattended
  event capture remains unproven.
* **Task 14.1 does not authorise production retention**, and by itself does not
  make the event-capture pipeline ready for permanent unattended enablement.
* Task 12 remains accepted at functional prototype scope; production camera
  hardware hardening remains deferred and is untouched by this task.

---

## 19. Explicitly deferred

Automatic retention scheduling; a service timer; a periodic deletion loop;
disk-pressure emergency deletion; manual-capture deletion policy;
unknown-origin deletion policy; image serving/download API; thumbnails; bird
detection; species classification; ROI/feeder masking; burst capture;
pre-roll/post-roll; video recording; visit/session grouping; notification
attachments; Telegram/email delivery; production enablement; physical retention
validation; long-duration unattended validation; camera hardware hardening.

Each requires its own task.

---

## 20. What this task did not do

* **NO RASPBERRY PI ACCESS.** No `ssh mgo-claude`, no `ssh mgo-core`, no reading
  or deleting of production captures, no inspection of
  `/var/lib/garden-observatory`, no change to `/etc/garden-observatory/mgo.toml`,
  no change to the approval file, no `mgo-validate`, no service restart, no
  reboot.
* **PRODUCTION UNCHANGED.** Production still runs `48bdaf3` with motion and event
  capture disabled and deployment approval withdrawn.
* **NO DEPLOYMENT PERFORMED.** No change to `scripts/deploy/`, systemd units,
  sudoers/gateway policy, deployment approval machinery, service identity,
  production directory ownership or backup behaviour.
* **NO EXISTING MGO MEDIA WAS DELETED.** All destructive tests operate only on
  temporary files the test itself created. The 17 Task 13.2 validation captures
  are untouched, no repository `data/` housekeeping was performed, and no
  production evidence was cleaned. There is still no authorised production
  deletion policy.
* **NO NEW DEPENDENCIES.** Python standard library and existing MGO
  infrastructure only.
* **NO PULL REQUEST. NO MERGE. TASK 14.2 NOT STARTED.**

A future physical validation is **not** recorded here as complete, because it has
not happened.
