# Task 14.2 — Controlled Retention Operator Interface

**Status: implementation complete, validated off-Pi, awaiting review.**

**Not merged. Not deployed. No Raspberry Pi access. No production change. No
physical retention validation. Retention remains disabled in production.**

---

## 1. Scope

Task 14.1 built the retention machinery. Task 14.2 makes it *invocable* — safely,
manually, and only by someone who has said what they mean three separate times.

It does two things:

1. makes retention **planning genuinely read-only at the SQLite/filesystem
   boundary**, which it was documented to be and was not;
2. adds a small deterministic local command, `mgo-retention`, with exactly two
   subcommands: preview a plan, or execute one bounded run.

It does **not** deploy, access the Raspberry Pi, enable production retention,
choose a retention period or storage budget, schedule anything, add a timer, add
a destructive HTTP endpoint, delete production media, physically validate
retention, or enable event capture. Physical deployment and deletion validation
remain a later controlled task.

---

## 2. Baseline

| Item | Value |
| --- | --- |
| Repository | `C:\AI\garden-observatory` (`emjayelle01/garden-observatory`) |
| Starting `main` | `f39c94f5644438c639d683aac0fed5c57e28c48b` (merged Task 14.1) |
| Branch | `task-014-2-controlled-retention-operator-interface`, created from that exact commit |

Preflight: clean working tree, empty stash, one normal worktree, `main` the only
local and remote branch, no Git operation in progress.

Baseline validation on the clean tip, before any implementation:

| Gate | Result |
| --- | --- |
| `uv sync --frozen` | Checked 36 packages |
| `uv run ruff check .` | All checks passed |
| `uv run mypy src` | Success: no issues found in 59 source files |
| `uv run pytest` | **2824 passed, 12 skipped, 0 failed** |

The 12 skips reconcile exactly to the established Windows/POSIX capability skips.

---

## 3. The read-only defect, reproduced

Task 14.1 documented `RetentionService.dry_run()` as read-only and mutating
nothing. At merged `main` it read the catalogue through
`RetentionRepository.list_lifecycle_records()`, which uses
`database_connection(...)` — the ordinary **read-write** path.

The problem is not the `SELECT`. It is what *opening* that connection does. All
three of the following were reproduced against `f39c94f5` before any change:

| Reproduction | Result at merged main |
| --- | --- |
| `dry_run` against a **missing database** | The database file was **created**, then the preview reported it could not read the `captures` table it had just finished not creating. |
| `dry_run` where the **parent directory did not exist** | The whole directory tree was **created**. |
| `dry_run` against a database using **DELETE journalling** | `journal_mode` was **changed from `delete` to `wal`** — a persistent change to a database the operator only asked to look at. |

An operator told a command was safe could therefore leave three separate marks
on a system by previewing it.

After the correction, the same script reports: database absent, parent absent,
`journal_mode` unchanged at `delete`.

---

## 4. The read-only projection

`RetentionRepository.read_lifecycle_records()` opens the database through the
repository's existing `connect_readonly(...)` boundary, which uses SQLite's
`mode=ro` URI. It never creates a database, never creates a directory, never
requests or changes a journal mode, never creates `schema_migrations`, never
applies a migration, never creates the lifecycle table and cannot modify
anything.

It reuses the **same** `_PROJECTION_SQL` and the **same** `_record_from_row`
decoder as the destructive path, deliberately. A second interpretation of origin,
lifecycle state, reason, timestamps or filesize is exactly the divergence that
would let a preview disagree with the run it is previewing — and a test asserts
the two paths return equal projections.

`list_lifecycle_records()` keeps the read-write path and keeps its caller: that
is the read a *destructive* run performs, and that run opens read-write
transactions immediately afterwards anyway.

### One honest limitation

A WAL database cannot be read **at all** — even read-only, even with `mode=ro` —
without SQLite's `-shm` shared-memory index, so SQLite may create or use that
sidecar. That is SQLite's documented mechanism for reading WAL, not a change this
code makes.

The precise claim is therefore **no MGO-managed state changes**, not "no byte on
the filesystem moves". `immutable=1` would avoid the sidecar and is deliberately
not used: it asserts the file cannot change, which is untrue of a live database
and would licence SQLite to ignore concurrent WAL state. Reading a live database
through semantics that may miss committed data is the worse trade.

The sidecar test pins a DELETE-journal database, where nothing whatsoever should
appear; a separate test asserts the WAL case directly, proving journal mode
unchanged and every table byte-identical.

---

## 5. The operator command

Registered as a `[project.scripts]` console entry point:

```toml
mgo-retention = "mgo.retention.cli:main"
```

`uv.lock` is unchanged and no dependency was added — `argparse` from the standard
library covers two subcommands, and a CLI framework would be a dependency bought
for nothing.

### `plan` — read-only

Evaluates the configured policy and prints what it would select. Creates no
lifecycle row, deletes no file, records no observation, applies no migration and
moves no counter. Works whether or not retention is enabled, because previewing a
policy is how an operator decides whether to enable it.

It performs **no filesystem validation**. The filesystem is only ever a veto
during destructive execution; stat-ing during a preview would make `plan` report
a different set from the one the policy chose. A test replaces the filesystem
seams with doubles that fail on any call and asserts a capture whose media is
missing still appears in the plan.

Output is JSON: the `RetentionPlan.as_dict()` contract plus `retention_enabled`.

### `run-once --execute` — potentially destructive

Three gates, in this order:

| Gate | Requirement | When | Why |
| --- | --- | --- | --- |
| A | The exact `--execute` flag | Before anything is opened | No `-y`, `--yes`, `--force`, `--really` or `--override`. One spelling is easier to audit. |
| B | `MGO_CONFIG_PATH` set and **absolute** | Before anything is opened | Configuration identity decides *which* media is deleted. Without it, an operator standing in the repository could resolve the tracked **development** configuration and point a deletion at whatever database and capture directory it names. |
| C | `retention.enabled = true` | After the configuration is read | Establishing it requires reading the file the value lives in. Nothing destructive happens in between. |

Then the schema gate, then **exactly one** `RetentionService.run_once()`, then
exit. It never repeats because `more_work_remains` is true — a test counts the
calls into the service and asserts exactly one.

---

## 6. The schema gate

Neither subcommand applies migrations. Schema migration belongs to application
startup, where it is transactional, logged and part of a reviewed deployment. An
operator asking to inspect or execute retention must never silently upgrade a
database as a side effect of asking, and an *older* database is precisely where a
silent upgrade would be most tempting and least safe.

Both commands require the database to already record exactly
`CURRENT_SCHEMA_VERSION == 3`, read through the existing read-only
`read_schema_version` helper. Lower, higher, unversioned, missing and unreadable
are all refused identically with exit `3`, and the database is left untouched —
a test asserts a version-2 database is still version 2 afterwards and still has no
`capture_media_lifecycle` table.

One defect found and fixed during implementation: `read_schema_version` raises
`sqlite3.OperationalError` for a missing database, which the first draft did not
catch, so the most ordinary operator mistake — pointing the command at the wrong
path — was reported as an unexpected internal failure (exit `5`) rather than the
refusal it is (exit `3`).

---

## 7. Output and exit codes

Both commands emit one JSON document on stdout. Refusals go to stderr as one
fixed sentence.

Nothing published anywhere contains an absolute path, a capture root, a database
location, a configuration path, a filename in a failure result, a raw exception
or a traceback. A traceback is the one part of the output that could carry a
capture path into a log an operator forwards elsewhere, so an unexpected
exception is reduced to one fixed sentence and exit `5`.

| Code | Meaning |
| --- | --- |
| `0` | Completed successfully |
| `2` | Operator/configuration refusal |
| `3` | Database/schema precondition refused |
| `4` | Bounded retention error category |
| `5` | Unexpected failure, reduced to a fixed sentence |

`0` and `2` carry the meanings they already carry in the backup and
support-bundle commands, so an operator reading exit codes across MGO's tools is
not learning two systems. `1` is deliberately unused. No capture id, filename or
policy reason is encoded into an exit code.

---

## 8. What the command refuses to be

* **No policy on the command line.** No `--max-age-days`, `--max-managed-bytes`,
  `--minimum-keep-count`, `--max-deletions`, `--origin`, `--capture-id`,
  `--before` or `--after`. Unknown arguments are refused, not ignored. An
  operator who could retune the policy at the prompt could turn a reviewed
  retention policy into an unreviewed deletion at the moment of deletion.
* **No single-capture delete.** No `delete`, `delete-file`, `delete-capture`,
  `purge` or `rm`. Task 14.2 exposes the existing policy engine and nothing else.
* **No configuration path argument.** No `--config`, `--database` or
  `--capture-root`. Configuration identity stays governed by the existing MGO
  contract.
* **No scheduling.** No daemon, interval, loop or retry.
* **No HTTP.** The command calls the domain service directly and adds no route.

---

## 9. Concurrency, stated as a limitation

Task 14.2 introduces no scheduler and no cross-process lock. The Task 14.1
process-local mutex protects two runs inside one process; two *separately
invoked* CLI processes are not serialised by it.

The final safety boundary across processes remains the database's conditional
lifecycle transitions: an intent can be claimed once, a finalisation can fire
once, and one deletion produces exactly one success observation. That is a real
limitation rather than a guarantee, and inventing a broad cross-process locking
subsystem was deliberately out of scope.

---

## 10. Files changed

**Added**

* `src/mgo/retention/cli.py`
* `tests/test_retention_readonly.py`
* `tests/test_retention_cli.py`
* `docs/tasks/Task-014-2-Controlled-Retention-Operator-Interface.md`

**Modified**

* `src/mgo/retention/repository.py` — `read_lifecycle_records()`
* `src/mgo/retention/service.py` — `dry_run()` uses the read-only path
* `pyproject.toml` — `[project.scripts]` entry point only
* `tests/mutation_register.py` — Task 14.2 mutations; one Task 14.1 anchor
  updated for the renamed call
* `docs/Retention.md`, `README.md`

Unchanged and deliberately untouched: migrations (none added, 003 unmodified),
`uv.lock`, the deployment gateway, sudoers, systemd units, Task 12/13 evidence,
the camera/preview/event-capture stack, the HTTP API, and both tracked
configurations — retention stays disabled and `event_capture` stays disabled in
the production example. **No production retention values were chosen.**

---

## 11. Validation

| Gate | Result |
| --- | --- |
| `uv sync --frozen` | Checked 36 packages |
| `uv run ruff check .` | All checks passed |
| `uv run mypy src` | Success: no issues found in 60 source files |
| Focused Task 14.2 suites | 345 passed, 0 failed |
| `uv run pytest` | **2894 passed, 12 skipped, 0 failed** |
| `uv run python scripts/dev/run-mutations.py` | **246/246 detected**, 0 stale, 0 restoration failures, 0 unmatched selectors |
| `git diff --check` | PASS |

The 12 skips are byte-identical to the baseline: the same files and line
numbers, all pre-existing Windows/POSIX capability skips. No new skip was
introduced. The register was run strictly after the complete suite finished,
with nothing overlapping it.

Ten mutations were added — one per safety property the command introduces —
taking the register from 236 to **246**. One existing Task 14.1 mutation went
stale because `dry_run`'s call was renamed; its anchor was updated rather than
dropped.

Two mutations initially failed to be detected and both revealed real weaknesses
rather than register noise:

1. the blank-`MGO_CONFIG_PATH` gate was redundant with the configuration
   loader's own validation *at the exit-code level*, so the test could not tell
   the two refusals apart. The gate genuinely matters — it names the variable to
   set, whereas the loader can only say the configuration would not load — so the
   test now asserts the message, which also proves the gate fires first;
2. three structural tests used naive substring searches that matched the module's
   own prose (`"delete"` in help text describing what `run-once` can do,
   `"sched"` inside "schedule", `"time"` inside `RetentionRuntimeState`). They
   now read the parser's actual subcommand choices and exact import forms.

A third defect was found the same way: `_downgrade_to_version_two` in the CLI
tests produced an *unversioned* database rather than a version-2 one, because the
migration files create tables while the runner writes the history rows. The
schema test was passing for the wrong reason until the history rows were written
explicitly.

---

## 12. Limitations

* **No physical validation.** Nothing here has run against the Raspberry Pi, the
  production database or the production media directory.
* **No production policy.** No retention period or storage budget was chosen;
  both bounds stay commented out in the tracked production example.
* **No cross-process serialisation** — see §9.
* **A WAL database still gets a `-shm` when read** — see §4.
* Task 13.2 was point-in-time physical validation only; long-term unattended
  event capture remains unproven, and event capture stays disabled in production.

---

## 13. Explicitly deferred

Automatic retention scheduling; a service timer; a periodic deletion loop;
disk-pressure emergency deletion; manual-capture and unknown-origin deletion
policies; a destructive HTTP endpoint; an authentication subsystem; image
serving, download or thumbnails; production enablement; physical retention
validation; long-duration unattended validation; camera hardware hardening.

A later controlled task can deploy the merged code with retention disabled, prove
schema-v3 startup on the Pi, verify `mgo-retention plan` read-only against
production, choose a deliberately bounded validation policy, obtain explicit
operator approval, temporarily enable retention, reclaim a tightly controlled
number of known motion-origin captures, prove catalogue preservation and disk
recovery, restore the configuration, and only then decide whether automatic
scheduling is justified. **Task 14.2 pre-empts none of those decisions.**

---

## 14. What this task did not do

* **NO RASPBERRY PI ACCESS.** No SSH, no `mgo-validate`, no approval file, no
  service restart, no reboot, no inspection of production media.
* **PRODUCTION UNCHANGED.** No deployment, no configuration change, retention not
  enabled, motion and event capture not enabled.
* **NO PRODUCTION MEDIA DELETION.** Every destructive test operates on temporary
  databases and temporary capture directories the test itself created. The 17
  Task 13.2 validation captures are untouched.

The local development database at `data/mgo.db` was **read but not modified**
during implementation. It contains historical test rows with naive timestamps, so
`mgo-retention plan` correctly refuses it under the Task 14.1 fail-closed
decoder — an unplanned but useful demonstration of that contract against real
data.

* **NO NEW DEPENDENCIES.** Standard library and existing MGO infrastructure only.
* **NO PULL REQUEST. NO MERGE.**


---

## 15. Correction round 1 — operator safety

Independent review accepted the design and found three operator-boundary defects
and one documentation precision issue. All four were corrected in a second normal
commit on this branch; `2da0c17b` was not amended.

### 15.1 The plan published a raw catalogue filename

`RetentionPlan.as_dict()` includes each candidate's `filename`, and the operator
output passed it straight through. That value has been validated only as *a
non-empty string* — it has **not** passed the path/filename safety boundary,
because that boundary belongs to destructive execution.

**Reproduced.** With three hostile filenames in the catalogue, `plan` published
all three:

```
'/var/lib/garden-observatory/db/mgo.db' in stdout: True
'../../secret.jpg'                      in stdout: True
'C:\sensitive\secret.jpg'               in stdout: True (JSON-escaped, fully recoverable)
```

The Windows one is worth noting: a naive substring search of the rendered
document misses it, because JSON escapes the backslashes — `json.loads` returns
the exact original string. The tests check the decoded structure, not just the
text.

**Corrected** by a narrow CLI serialisation helper, `_operator_plan()`, which
omits `filename`. `plan` gained **no** filesystem validation — that would have
made a preview disagree with the pure policy selection it exists to report — and
the Task 14.1 domain model is untouched. The filename is **omitted**, not
sanitised and not reduced to a basename: anything derived from an unverified
value is still derived from it, and the capture id identifies the record
completely.

A control test proves the hostile capture is *still selected* by the pure policy
and still reports `capture_id`, `captured_at`, `filesize_bytes` and
`policy_reason` — otherwise "the string is absent" would prove nothing about
output privacy. Another proves destructive execution still validates the filename
and refuses `unsafe_path` before any unlink.

### 15.2 Configuration-shape errors were reported as internal failures

`_load_configuration()` mapped only `OSError`, `ValueError` and `KeyError` to the
refusal boundary. The existing loader can also raise, for **syntactically valid**
TOML:

* `TypeError` — `float()` handed a list, or a string subscripted as a table;
* `AttributeError` — a section that is a scalar, so `.get()` does not exist.

**Reproduced.** Both exited `5` with "The command failed unexpectedly":

```
top-level `health = "not-a-table"`   -> AttributeError -> exit 5
`temperature_warning_celsius = [1,2]` -> TypeError      -> exit 5
```

That is the wrong answer twice over: it points the operator at MGO instead of at
their own file, and it spends the exit code reserved for genuine defects on an
ordinary mistake.

**Corrected** by adding both to the refusal boundary, after reading
`parse_config_text()` to establish the set it can legitimately raise rather than
guessing. `Exception` is deliberately **not** caught wholesale — that would hide
real bugs behind a message blaming the operator — and a control test proves an
injected `RuntimeError` still exits `5`.

### 15.3 The destructive gate accepted a relative configuration path

The gate accepted any non-blank `MGO_CONFIG_PATH`, and the application's rules
resolve a relative value against the current working directory. That is correct
for configuration generally and too weak for a gate whose whole purpose is that
the operator has identified *one deployment*: `config/mgo.toml` names a different
file after a `cd`.

**Reproduced.** With `MGO_CONFIG_PATH=mgo.toml` and the working directory set to
a temporary deployment, `run-once --execute` **succeeded and deleted the media**
(`deleted_count: 1`).

**Corrected**: the destructive gate now requires an absolute path. A relative
value is refused with a fixed sentence and is **not** resolved on the operator's
behalf — silently making it absolute would produce exactly the outcome the gate
prevents while looking checked. The supplied value is never echoed back.

This is CLI-only and destructive-only. `resolve_config_path()` is unchanged, and
`plan` still accepts a relative path because it is read-only; a test asserts both.

### 15.4 Documentation precision

Two passages overclaimed:

* "mutates nothing" did not distinguish MGO-managed state from SQLite's own WAL
  `-shm` mechanism. The claim is now **no MGO-managed state changes**, with the
  sidecar behaviour stated plainly and `immutable=1` explicitly rejected — it
  asserts a live database cannot change, which would licence SQLite to ignore
  concurrent WAL state;
* the three gates were described as checked "before anything is opened or read",
  but gate C is `retention.enabled`, which can only be established *after*
  reading the configuration. Gates A and B are now documented as pre-open, gate C
  as post-read, with the note that nothing destructive happens in between.

### 15.5 Correction-round validation

| Gate | Result |
| --- | --- |
| `uv sync --frozen` | Checked 36 packages |
| `uv run ruff check .` | All checks passed |
| `uv run mypy src` | Success: no issues found in 60 source files |
| Focused Task 14.2 suites | 436 passed, 0 failed |
| `uv run pytest` | **2944 passed, 12 skipped, 0 failed** |
| `uv run python scripts/dev/run-mutations.py` | **250/250 detected**, 0 stale, 0 restoration failures, 0 unmatched selectors |
| `git diff --check` | PASS |

The 12 skips are byte-identical to the baseline: the same files and line
numbers, all pre-existing Windows/POSIX capability skips. No new skip. The
register was run strictly after the complete suite finished, with nothing
overlapping it.

Four mutations were added — one per corrected property, with `TypeError` and
`AttributeError` registered separately because dropping either alone reopens a
different half of the boundary — taking the register from 246 to **250**.

One pre-existing test expectation was updated: `test_plan_output_has_a_deterministic_shape`
asserted `filename` was present in candidate output, which is exactly the
contract this round changed.

### 15.6 What the correction round did not change

Two subcommands, no aliases, no scheduler, no loop, no retry, no timer, no
destructive HTTP endpoint, no arbitrary named-capture deletion, no policy on the
command line, no migration from the CLI, the exact schema-version-3 gate and its
refusals, the read-only SQLite connection for `plan`, every Task 14.1 lifecycle,
recovery, protected-origin, keep-count, per-run-bound, observation and
capture-preservation semantic, one service call per invocation, no looping on
`more_work_remains`, and the fixed destructive result fields.

No new dependency, no migration, retention still disabled and `event_capture`
still disabled in tracked configuration, and no production policy chosen.
