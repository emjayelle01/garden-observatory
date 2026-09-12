# Species Recognition — Durable Job Foundation

**Status (Task 15.1): model-independent foundation only.** MGO has a durable
recognition job queue, catalogue-driven eligibility, atomic claims with leases,
bounded retries, a result schema and a single-job runner, all proven against a
deterministic fake adapter.

It has **no model, no classifier, no pixel decoding, no worker service, no loop,
no API, no configuration and no deployment.** Nothing in the running
application constructs any of it. Schema 4 is not deployed; see §14 before it
ever is.

Code: `src/mgo/recognition/`. Schema: `migrations/004_recognition_jobs.sql`.
Tests: `tests/test_recognition_schema.py`, `tests/test_recognition_eligibility.py`,
`tests/test_recognition_jobs.py`. Mutation register: the Task 15.1 section of
`tests/mutation_register.py`.

---

## 1. Vocabulary: a later *sighting*, not an *observation*

MGO already has an **observation**: an immutable row in the operational
timeline (`observations`, `GET /observations`) recording that something
*happened to the system* — an application start, a retention deletion, a
capture. Those rows are operational evidence and their meaning is fixed.

A recognised bird is a different kind of fact: a *conclusion about a picture*,
produced by a versioned pipeline, that may later be reviewed, superseded or
grouped into an encounter. Calling it an "observation" would put two unrelated
meanings behind one word and one endpoint. The biological record will
therefore be called a **sighting** when a later task introduces it. Task 15.1
introduces neither sightings nor any change to observations; recognition writes
no observation rows at all.

## 2. Where recognition begins

Recognition begins only **after a capture has been durably published** — after
its JPEG is verified on disk and its `captures` row is committed.

It observes that catalogue from outside. It is deliberately **not** a step in
`CaptureWorkflow`, `EventCaptureService`, `CaptureArchive`, the camera
transaction, retention or backup. So:

- recognition can never slow, fail, reorder or gate a capture;
- a capture that is never recognised is exactly as published as one that is;
- a recognition defect cannot turn into a capture defect.

## 3. Eligibility

### 3.1 The rules

Work enters the queue only through the **reconciler**, which reads committed
`captures` rows (joined to `capture_media_lifecycle`) and applies these rules
in order. A capture is eligible only if all hold:

| # | Rule | Refusal reported as |
|---|------|---------------------|
| 1 | `extra_metadata` decodes to a JSON **object** | `malformed_metadata` |
| 2 | `origin` is exactly `MANAGED_ORIGIN` (`"motion"`) — the constant retention manages and event capture writes | `not_motion_origin` |
| 3 | no `capture_media_lifecycle` row exists | `lifecycle_recorded` |
| 4 | `captured_at_utc` is a timezone-aware instant **at or after** the enrolment watermark | `before_watermark` / `invalid_record` |
| 5 | the media passes retention's safety boundary: absolute path; no `..`; filename agrees with the path; target is not a symlink; resolved parent inside the resolved capture root; target exists; regular file; on-disk size equals the catalogue | `unsafe_path`, `media_missing`, `size_mismatch`, `invalid_record`, `media_unreadable` |
| 6 | a real adapter opens the file with `O_NOFOLLOW` and re-checks type and size on the descriptor (`mgo.recognition.adapter.open_media`) | adapter error at run time |

Rule 5 **calls** `mgo.retention.service.validate_media_path` and
`validate_media_file`; it does not re-implement them. Recognition can therefore
never be weaker than the check that governs deletion, and a change to that
boundary changes both. Retention's code and behaviour are unchanged by Task
15.1. Two translations are applied on recognition's side only:

- retention's `not_regular_file` is reported as recognition's `unsafe_path`
  (a directory or device at a catalogued path means the path does not name the
  published file — the stored vocabulary is not widened for it);
- an `OSError`/`ValueError` raised by the host while validating (an unreadable
  file, a NUL byte) is a refusal: `media_unreadable` during reconciliation,
  `unexpected` at run time.

Refusal is **per row**. Retention fails a whole run closed on one undecodable
row because it deletes files; recognition only declines to look at one, so a
single malformed legacy row does not stop enrolment of every capture after it.
Reasons are reported in the reconcile report and logs, never stored.

### 3.2 Why directory scanning is forbidden

A file in the capture directory without a catalogue row is not a capture MGO
published: it may be a refused capture's leftover, an operator's copy, or
anything else. A directory listing is exactly the thing that must never be able
to nominate a file — for deletion (retention's rule) or for inference. Nothing
in `mgo.recognition` lists a directory; a test replaces every enumeration
primitive with one that fails and reconciliation still succeeds.

### 3.3 The enrolment watermark

The reconciler is **given** an aware UTC watermark; there is no default and no
configuration for it yet. Captures before it are not queued. The boundary is
inclusive, and it is compared as an instant (stored offsets are converted), not
as text.

**Approved in Task 15.1A.** Both the reconciler and the eligibility rules refuse
a naive watermark, and a test pins the *absence* of a default on both APIs: a
default — the epoch, the earliest capture, "now" — would silently enrol all of
history the first time reconciliation ran. Where the production value comes from
is a later worker/deployment decision, and this task defines no configuration
key for it.

This is what stops the first run from silently backfilling history — including
the protected Task 13.2 evidence (17 motion rows from before the watermark).
Enrolling earlier captures is a future, explicit operator decision made by
supplying an earlier watermark. Moving the watermark never enrols rows that fail
any other rule.

### 3.4 The legacy catalogue

The Task 15.0 production audit found 12 legacy catalogue rows: four historical
mock/pytest rows, four relocated-development rows (`/home/pi/Projects/…`) and
four legacy rows inside the capture directory. None carries `origin = "motion"`,
so none is ever eligible, whatever the watermark. The tests reproduce all three
shapes synthetically, and also prove that a legacy-shaped path outside the
capture root is refused by containment even if it did carry a motion origin.

## 4. Jobs and results

Work state and biological outcome are two tables.

**`recognition_jobs`** — what happened to the attempt to recognise one capture
under one pipeline version: `id`, `capture_id` (FK), `pipeline_version`,
`camera_id` (reserved, unused), `state`, `attempt_count`, `max_attempts`,
`next_attempt_at`, `lease_owner`, `lease_expires_at`, `created_at`,
`started_at`, `finished_at`, `error_category`.

**`recognition_results`** — what a *succeeded* attempt concluded: `id`,
`job_id` (FK, `UNIQUE`), `outcome` (`species`, `uncertain`, `unknown_species`,
`no_bird`, `person_present_only`), detector / classifier / label-set identity
and SHA-256, taxonomy identity and version, preprocessing and thresholds
versions, inference duration, peak RSS, CPU time, image width and height,
`created_at`.

Provenance is nullable field by field — the fake adapter and not-yet-existing
pipeline stages cannot honestly supply it — but identities and digests are
**paired** by `CHECK`, digests must be 64 lowercase hex characters, and the
schema can hold complete provenance later. "The model crashed" can never be
counted as "no bird", because a failure carries a category and no result.

The database enforces, rather than trusts: both vocabularies, the error
vocabulary, one job per `(capture_id, pipeline_version)`, **at most one** result
per job, foreign keys, a lease existing exactly while `running`, a finish time
exactly on terminal states, a pending job having a retry time and an attempt
left, success carrying no error, failure and skip always carrying one, integer
attempt counts within `max_attempts`, bounded identifier lengths, and a single
timestamp layout of one fixed 32-byte text shape.

Two limits of that enforcement are worth stating plainly rather than leaving to
be discovered:

- **"a result only for a succeeded job" is not a database property.** No
  `CHECK` can span two tables, and there is no trigger. What holds it is that
  `complete_success` is the only writer of `recognition_results`: it inserts the
  result and sets `succeeded` in one transaction, under an owned claim, so
  neither can exist without the other. Direct SQL could attach a result to a
  `failed` job; nothing in the application does. A later reprocessing campaign
  that marks a previously succeeded job `superseded` will legitimately leave a
  result behind a non-succeeded job, and that task must decide whether the
  result is re-pointed, kept as history, or removed.
- **the foreign keys hold only while `PRAGMA foreign_keys` is on.** Every
  recognition connection sets it (`connect_database`), so the application cannot
  orphan a job. The bare `sqlite3` CLI defaults it *off*: this database must not
  be hand-edited with the CLI, and `PRAGMA foreign_key_check` is the way to
  confirm nothing has been.

## 5. Pipeline-version idempotency

`UNIQUE (capture_id, pipeline_version)` is the final idempotency guarantee. The
reconciler's read skips captures that already have a job for the version, and
its insert uses `ON CONFLICT (capture_id, pipeline_version) DO NOTHING`, so
repeated, concurrent or interleaved reconcilers converge on exactly one job.

Reprocessing with a **different** `pipeline_version` creates a distinct job;
earlier jobs and results are retained untouched. A runner claims only jobs for
its adapter's `pipeline_version`, so a job is never answered by a pipeline it
was not queued for. `superseded` exists in the schema for a later reprocessing
campaign; nothing in Task 15.1 writes it.

## 6. State transitions

```text
                 claim                        result committed
  (enqueue) ──► pending ─────────► running ─────────────────────► succeeded
                   ▲                  │
                   │ retryable,       ├── media_missing ──────────► skipped
                   │ attempts left    │
                   └──────────────────┤── unsafe_path / size_mismatch /
                                      │   decode_error ───────────► failed
                   lease expired,     │
                   attempts left      ├── retryable, last attempt ─► failed
                   (reclaimed) ◄──────┤
                                      └── lease expired on last
                                          attempt (next claim) ───► failed (unexpected)
```

`succeeded`, `failed`, `skipped` and `superseded` are terminal.

## 7. Claims, leases, crash recovery and retry exhaustion

**Claim.** One short `BEGIN IMMEDIATE` transaction, scoped to one pipeline
version:

1. end every `running` job whose lease has expired **and** has no attempt left
   (`failed` / `unexpected` — its worker never reported why);
2. select the next due job — `pending` with `next_attempt_at <= now`, or
   `running` with `lease_expires_at <= now` and an attempt left — ordered by the
   time it became due, then `created_at`, then `id`;
3. move it to `running`, increment `attempt_count`, set `started_at`, and set a
   lease owned by `<worker_id>:<random token>` until `now + lease`;
4. commit.

`IMMEDIATE` takes SQLite's write reservation before the select, so no other
writer can interleave between select and update; this does not rely on
`RETURNING` or any recent SQLite feature. The update is additionally conditional
on the observed state and attempt count. A test interposes a competing claimer
between the select and the update and proves it is held off.

**Lease expiry is inclusive** on both sides: a lease is valid strictly before
`lease_expires_at` and recoverable at it.

**Ownership.** A claim is identified by its lease owner token *and* its attempt
number. Every later write — heartbeat, result, failure — matches both, and
writes nothing when they do not match. A worker whose lease expired and was
taken over is told `False`/`None`; it cannot complete, fail or renew the job,
and cannot add a result after another worker's success. A worker whose lease
expired but was *not* taken over may still finish, because no other result can
exist.

**Heartbeat.** `RecognitionRepository.renew_lease(job, lease)` extends a lease
only while it is still owned **and unexpired**. The runner hands it to the
adapter as `request.renew_lease()`. There is no worker-status table; broader
worker status and API reporting are reserved for later tasks.

**Crash recovery.** A worker that dies anywhere between claim and finish leaves
the job `running` with no result. When the lease expires, the next claim
recovers it as a new attempt. Because the attempt count increments at claim, a
job that kills its worker every time still exhausts `max_attempts` and ends
`failed`.

**Retries.** A retryable failure returns the job to `pending` with
`next_attempt_at = now + delay`, where the delay doubles from `base_delay`
(default 1 minute) up to `max_delay` (default 1 hour). The final permitted
attempt's retryable failure is terminal `failed` with that category. Defaults:
`max_attempts = 3` (bounded 1–100), lease 10 minutes.

## 8. The bounded error vocabulary

| Category | Meaning | Disposition |
|----------|---------|-------------|
| `media_missing` | the file is gone, or a lifecycle row now exists | **`skipped`**, terminal, never retried |
| `unsafe_path` | fails path safety, or is not a regular file | **`failed`**, terminal |
| `size_mismatch` | the file is not the catalogued one | **`failed`**, terminal |
| `decode_error` | the bytes do not decode | **`failed`**, terminal |
| `model_unavailable` | the pipeline cannot run now | retry until exhausted, then `failed` |
| `timeout` | the attempt exceeded its time | retry until exhausted, then `failed` |
| `resource_limit` | memory or CPU limit reached | retry until exhausted, then `failed` |
| `unexpected` | anything else, including an adapter exception | retry until exhausted, then `failed` |

`media_missing` is a skip because it is the expected consequence of retention,
not a fault: once retention has claimed or reclaimed media, it is not
recognition's to look at (see §10 for the one case where retention later changes
its mind). The other terminal categories describe media that exists but must
not or cannot be read; retrying reads the same bytes again. The retryable ones
describe the pipeline, where a later attempt can genuinely differ.

Only the category is stored. No exception message, traceback or path is written
to any recognition column — the column is `CHECK`-constrained to the vocabulary.
Exceptions are logged with `LOGGER.exception`, following the repository's
logging conventions. `RunReport` carries identifiers and vocabularies only.

## 9. Transaction boundaries

Every database operation opens its own connection, runs **one short**
transaction, and closes:

| Phase | Transaction |
|-------|-------------|
| reconcile read | autocommit `SELECT`, closed before any filesystem check |
| enqueue | `BEGIN IMMEDIATE`, at most 100 inserts, commit |
| claim | `BEGIN IMMEDIATE`, sweep + select + update, commit |
| run-time re-read of the capture | autocommit `SELECT` |
| **adapter execution** | **none** |
| heartbeat | `BEGIN IMMEDIATE`, one update, commit |
| finish | `BEGIN IMMEDIATE`: job → `succeeded` **and** result insert, commit — or one failure update |

No transaction is ever open while the adapter runs; a test opens a second
connection with a zero busy timeout inside the adapter and takes the write lock
immediately. The success update and the result insert share one transaction, so
no reader sees a succeeded job without its result, and no result is written for a
job that did not succeed — a guarantee of this one writer working under its owned
claim rather than a constraint the database could enforce across tables (§4). A
fault injected into the result insert leaves the job `running` and recoverable.

Every recognition connection has foreign keys on, WAL requested, the standard
bounded busy timeout, and a SQLite **authorizer that denies `INSERT`, `UPDATE`
and `DELETE` on every table except `recognition_jobs` and
`recognition_results`** (which also denies schema changes). The capture
catalogue, the lifecycle table, the observation timeline and the migration
history are read-only to recognition at the SQLite boundary.

## 10. Retention and missing media

| Situation | Behaviour |
|-----------|-----------|
| Retention removes media before inference | run-time validation finds it missing → `skipped` / `media_missing`; never retried |
| Lifecycle row exists before reconciliation | ineligible; no job is created |
| Lifecycle row committed during reconciliation | the insert re-checks the lifecycle table inside its transaction; no job |
| Lifecycle row appears after reconciliation, before the run | `skipped` / `media_missing`, even if the file is still present; the adapter is not called |
| Media disappears while the adapter works | no lock, no coupling: the adapter's own outcome is recorded transactionally (the fake adapter succeeds; a real one reading the file gets `media_missing` from `open_media`) |
| An existing result, after the media is removed | unchanged and still valid |
| Target outside the managed directory | never inferred; an existing job reaching validation ends `failed` / `unsafe_path` |
| File with no catalogue row | invisible to recognition |
| Catalogue row with no file | ineligible; `skipped` / `media_missing` if already queued |
| Protected Task 13.2 evidence | no special handling; eligible only through the watermark or a future explicit backfill |

**A cancelled deletion intent.** Retention removes a `pending_delete` row, and
the media stays on disk, when its revalidation or its unlink fails. If a job was
run while that intent existed it is already `skipped` / `media_missing`, and
because a job now exists for that `(capture_id, pipeline_version)`, the
reconciler will not offer the capture again under that pipeline version. This is
the behaviour the Task 15.0 contract specifies ("do not infer; finish as
skipped"), and it is deliberately kept: the media is still a retention candidate
under the policy that selected it, and racing retention for it is the thing the
rule forbids. Recognising such a capture later is an explicit action — a new
pipeline version today, or a future operator requeue — not an automatic retry.

**Approved in Task 15.1A.** A terminal job is never automatically reopened or
reset when retention cancels its deletion: terminal job history stays truthful
and immutable, and no requeue-on-cancellation mechanism belongs in this
foundation. A regression test pins it — the job row stays byte-for-byte
unchanged, reconciliation does not offer the capture again under that pipeline
version, and a different pipeline version still can.

Recognition takes **no** retention or backup lock, never deletes, renames,
edits or retains media, and is never a reason for retention to keep a file.
Retention does not know recognition exists.

## 11. No coupling

No recognition module directly imports `mgo.camera`, `mgo.captures`,
`mgo.event_capture`, `mgo.motion`, `mgo.operations`, `mgo.notifications` or
`mgo.api`, or any imaging, network, subprocess or file-locking module; a test
enforces this by parsing every module's imports. Its only dependency on another
subsystem is the deliberate reuse of retention's pure validators
(`mgo.retention.service`) and origin constant (`mgo.retention.policy`).
Importing the service module loads retention's own imports, including its lock
class, but recognition calls nothing in them except the two validators — and a
test replaces every `OperationLock` acquisition with a failure, runs
reconciliation and two jobs end to end, and finds no lock file afterwards.

## 12. The adapter boundary and the fake adapter

`RecognitionAdapter` is the whole contract between the queue and a pipeline:

```python
class RecognitionAdapter(Protocol):
    @property
    def pipeline_version(self) -> str: ...
    def recognise(self, request: RecognitionRequest) -> RecognitionResult: ...
```

An adapter returns a `RecognitionResult` or raises
`RecognitionAdapterError(category)`. Any other `Exception` is caught by the
runner — `Exception` only, never `BaseException` — logged with its traceback,
and recorded as `unexpected`. An interrupt still stops the worker and leaves a
recoverable claim. A real adapter must read media only through `open_media`.

`mgo.recognition.fake_adapter.FakeRecognitionAdapter` is for development and
tests only. It never opens the media, decodes nothing, imports no imaging or
model library and touches no network. Its outcome is a pure function of
`(pipeline_version, capture_id)`. Its provenance names `mgo-fake-*` artefacts
with digests of those names and no image dimensions. Tests configure it to
fail with any category, crash, or run a hook first (to remove media, add a
lifecycle row, probe for transactions, or interrupt). It is not exported from
`mgo.recognition`, not wired to configuration or the API, and a test proves no
other application module references it.

`run_one_job(repository, adapter, capture_directory=…, worker_id=…)` processes
**at most one** job and returns a `RunReport`. It is a callable, not a daemon.

## 13. What Task 15.1 deliberately does not implement

- any real detector, classifier, model file, weights, label set or taxonomy;
- pixel decoding, Pillow use for recognition, or any new dependency;
- a worker loop, thread, daemon, systemd unit, timer or CLI;
- configuration (`[recognition]`), including a configured watermark;
- any API route or dashboard field;
- detection, candidate, review, human-confirmation, encounter, sighting,
  first-seen, outbox or notification tables or behaviour;
- a worker-status table;
- automatic supersession or reprocessing campaigns;
- any change to capture, event capture, retention, backup or notifications;
- any deployment or production change.

## 14. Schema 4, rollback and deployment

Migration 004 moves `CURRENT_SCHEMA_VERSION` from 3 to 4. The consequence for
operations is the same one migration 003 had:

- **every schema-3 build refuses a schema-4 database** (`IncompatibleSchemaError`
  from the migration runner; `migration_status: ahead` from the health check),
  and leaves it unchanged — a test proves both against the real schema-3
  runner;
- once a production database has been migrated, rolling the code back to a
  schema-3 commit is not a rollback at all. The deployment gateway's
  schema-aware recovery refuses the automatic repository rollback when the
  schema has advanced, and recovery then needs a **fresh schema-3 recovery set
  taken immediately before deployment** and the existing schema-aware recovery
  procedure (`docs/Operations.md`);
- a schema-4 build's `mgo.operations.backup` verification refuses a recovery
  set whose manifest records `expected_schema_version` 3
  (`BACKUP_SCHEMA_INCOMPATIBLE`), so a schema-3 recovery set must be verified
  and restore-tested with the schema-3 build that produced it;
- the retention CLI refuses to run against a database that is not at the
  build's schema version, so after deploying a schema-4 build it runs only once
  the database has been migrated;
- **Task 15.1 authorises no deployment.** Taking that recovery set, deploying,
  and migrating production are separate decisions under separate authority.

## 15. Inherited design and Task 15.1 decisions

Inherited from the Task 15.0 architecture: the post-publication insertion
point; catalogue-driven eligibility with the exact motion origin, lifecycle
exclusion, enrolment watermark and retention-equivalent path safety; no
directory scanning; job/result separation; `UNIQUE (capture_id,
pipeline_version)`; the state, error and outcome vocabularies; leases and
bounded retries; short transactions; the missing-media contract; and no
coupling to camera, capture, retention or backup.

Decided in Task 15.1, within that contract (none changes it):

| Decision | Reason |
|----------|--------|
| `unsafe_path` is terminal **`failed`**, not `skipped` | an unsafe target is a fault needing a human, unlike reclaimed media |
| retention's `not_regular_file` maps to `unsafe_path` | keeps the stored vocabulary exactly as specified |
| `unexpected` is retryable, bounded by `max_attempts` | an unknown failure is as likely transient as permanent; the bound stops poison jobs |
| a lease that expires on the final attempt ends `failed` / `unexpected` at the next claim | otherwise a job that kills its worker every time stays `running` forever |
| attempts are counted at claim, not at failure | a crash still consumes an attempt |
| any lifecycle row at run time skips the job, even if the file exists | a pending deletion intent means retention owns that media |
| renewal requires an unexpired lease; completion requires only ownership | a late heartbeat must not reclaim a lapsed lease, but an untaken attempt's result is still the only one |
| the claimed lease owner is `<worker_id>:<random token>` per claim | the same worker reclaiming its own expired job is a different claim |
| a SQLite authorizer on every recognition connection | "never writes capture tables" is enforced, not only tested |
| one timestamp layout, enforced by `CHECK` on every recognition timestamp | leases and retry times are compared as text inside SQLite |
| legacy adoption verifies both recognition tables' safety-critical constraints (keys, `NOT NULL`, foreign keys, vocabularies, uniqueness, coherence rules, integer bounds, timestamp layout, provenance pairing and digest format — not identifier lengths or indexes) | follows the precedent migration 003 set for the lifecycle table |
| `open_media` also opens with `O_NONBLOCK` | a FIFO swapped in after validation fails the regular-file check instead of blocking the worker |
| result values are bounded to SQLite's integer range and refuse NUL characters | an unbindable integer raises a non-`sqlite3` error, and SQLite's `length()` cannot see past a NUL |
| heartbeat is a request callback; no worker-status table | the minimum the runner needs; status reporting is a later task |
| a `pending_delete` that retention later cancels leaves the job terminal — **approved in Task 15.1A** | terminal history is truthful and immutable; reprocessing is explicit or a new pipeline version, never an automatic requeue |
| the enrolment watermark has no default on either API and both refuse a naive value — **approved in Task 15.1A** | a default would silently enrol all history, including the protected Task 13.2 evidence |
