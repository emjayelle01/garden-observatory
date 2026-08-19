# Capture Media Retention

**Status: software foundation. Disabled by default. Not authorised for
production. Never physically validated.**

Task 14.1 builds the machinery MGO needs to reclaim captured media safely. It
does not turn that machinery on, does not schedule it, does not expose it
destructively over HTTP, and has never deleted a real capture on the Raspberry
Pi.

---

## 1. Why this exists

Task 13 proved MGO can perform the whole automatic-capture transaction against
the physical camera:

scene change → motion transition → automatic trigger → exclusive camera handover
→ full-resolution still → catalogue entry → correlated immutable observation →
preview restoration.

That pipeline is still **disabled in production**, and the blocker is not the
camera. It is that captured media has no lifecycle.

During the bounded Task 13.2 physical validation, a 341-second enabled window
produced **17 captures totalling 42,660,151 bytes** of JPEG, from ambient garden
motion alone. That measurement is a point-in-time observation of one short
window, **not** a long-term rate prediction — but it is already enough to show
that an unattended capture pipeline cannot be enabled responsibly while nothing
reclaims what it produces.

Task 14.1 answers "how would media be reclaimed *safely*". It does not answer
"should retention be switched on, with what values, invoked by what" — those are
separate decisions that this task deliberately does not pre-empt.

---

## 2. What Task 14.1 is not

* **There is no automatic retention scheduler.** No interval, no timer, no
  systemd timer, no periodic loop, no startup deletion, no shutdown deletion.
  Application startup constructs the objects and invokes nothing.
* **There is no public destructive endpoint.** `GET /retention/status` is
  read-only and inert. There is no `POST /retention/run`. Unauthenticated
  destructive media deletion over HTTP is a decision for a later, controlled
  task.
* **There is no disk-pressure policy.** Retention never reads
  `health.disk_warning_percent`, `health.disk_critical_percent`, free-space
  percentage, filesystem fullness or SD-card utilisation. Emergency behaviour
  under disk pressure is a separate architectural decision and is not invented
  here.
* **There is no manual-capture retention policy** and no unknown-origin policy.
* **Task 14.1 does not authorise production retention**, and by itself it does
  **not** make the event-capture pipeline ready for permanent unattended
  enablement. Event capture remains disabled in production; Task 13.2 was
  point-in-time physical validation only, and long-term unattended behaviour
  remains unproven.

---

## 3. Configuration

```toml
[retention]
enabled = false
# max_age_days = 30
# max_managed_bytes = 2147483648
minimum_keep_count = 100
max_deletions_per_run = 25
```

| Setting | Meaning |
| --- | --- |
| `enabled` | Gates every destructive action. Default `false`. |
| `max_age_days` | Optional. Managed captures at or beyond this age are eligible. Must be `> 0`. |
| `max_managed_bytes` | Optional. Cap on the total catalogued size of PRESENT managed captures. Must be `> 0`. |
| `minimum_keep_count` | The newest N managed captures are preserved unconditionally. Must be `>= 1`. Default `100`. |
| `max_deletions_per_run` | Hard ceiling on captures one run may *newly* select. Must be `>= 1`. Default `25`. |

### Safe defaults and backwards compatibility

A configuration file written before this section existed loads **unchanged**,
with retention off. An absent `[retention]` section and an explicitly empty one
produce an identical configuration. No retention read, planner execution or
filesystem mutation begins because the section is absent.

### Validation

When `enabled = true`, **at least one of `max_age_days` and `max_managed_bytes`
is required**. An enabled policy with no bound is refused at load time: it is a
deletion subsystem with nothing telling it when to stop, and accepting it would
leave an operator believing retention was managing their media while nothing was
ever selected.

Retention does **not** require `camera.enabled` and does **not** require
`event_capture.enabled`. It is a storage-lifecycle concern, and it must be able
to reclaim media that a now-disabled capture feature already produced.

Configuration error messages name only the setting at fault. No configuration
path, capture directory, database location or unrelated value appears in one.

---

## 4. Managed captures

Automatic retention manages **only** captures whose stored `extra_metadata`
contains exactly:

```json
{"origin": "motion"}
```

These are the automatic motion-triggered captures created by Task 13.

Everything else is **PROTECTED** and can never be selected:

* manual captures (`POST /camera/capture` writes no `origin`);
* captures with no `origin` key;
* captures with an unknown origin;
* captures from any future origin not exactly equal to `"motion"`.

Matching is exact equality — `"Motion"`, `"MOTION"`, `" motion"` and
`"motion_burst"` are all different origins and all protected. A non-string
`origin` (a number, a list, `null`) is treated as absent rather than coerced:
`str(["motion"])` is not `"motion"`, and a value that is not the exact managed
origin must land in the protected set, not near it.

This is a safety contract, not a convenience. A future task may introduce an
explicit manual-capture retention policy if that is wanted; Task 14.1 does not.

---

## 5. Policy semantics

The planner (`mgo.retention.policy`) is a **pure function** of the catalogue,
the configuration and one instant. It opens no file, stats no path and mutates
nothing, which is what makes a dry run and a destructive run provably the same
decision.

Planning uses **catalogue** timestamps and **catalogue** byte sizes. A planner
that sized files on disk would be making destructive decisions from a directory
listing, and a directory listing must never be able to nominate a file for
deletion. The filesystem gets a vote later, in the executor, and it is only ever
a veto.

### 5.1 Ordering

Managed captures are ordered oldest first by:

1. `captured_at_utc`
2. `created_at_utc`
3. capture `id`

Three levels, because SQLite's incidental row order is not a tie-break — it is
the absence of one. Falling through to the primary key makes the order total, so
the same catalogue always produces the same plan.

### 5.2 The preservation floor

The newest `minimum_keep_count` managed PRESENT captures are protected
regardless of age or byte pressure. The planner can never select one of them.

### 5.3 Age policy

When `max_age_days` is configured, a non-protected managed capture is
age-expired when:

```
captured_at_utc <= now_utc - max_age_days
```

The boundary is **inclusive** and uses timezone-aware UTC throughout: a capture
taken exactly `max_age_days` ago is expired.

### 5.4 Managed-byte policy

`max_managed_bytes` refers **only** to the total catalogue `filesize_bytes` of
PRESENT managed motion captures. It deliberately does **not** include manual
captures, unknown-origin captures, the database, logs, filesystem overhead or
already-deleted captures.

The naming is deliberate: the policy controls the media generated by the
automatic capture feature. It does not pretend to control total filesystem
utilisation, and a manual capture ten times the byte budget must not make an
automatic capture eligible.

When the managed PRESENT total exceeds the limit, the oldest non-protected
managed captures are selected until the projected total reaches or falls below
it. The limit is inclusive on the permitted side: a total exactly at the limit
selects nothing.

**If the preserved set alone exceeds the byte limit, the target cannot be met.**
The plan reports `byte_target_satisfiable = false` rather than breaking the
floor. `minimum_keep_count` is never violated to satisfy a byte limit.

### 5.5 Combined policy

When both rules are configured, age-expired captures are selected first; if the
projected managed total is still above `max_managed_bytes`, further oldest
eligible captures are added. Because both rules act on the same oldest-first
ordering, each selects a prefix of the eligible list, so the union is the longer
prefix and no capture is selected twice.

The recorded reason comes from a **fixed three-word vocabulary**, enforced by a
database `CHECK` constraint:

| Reason | Meaning |
| --- | --- |
| `age` | Age expiry alone reached it. |
| `managed_bytes` | Byte pressure alone reached it. |
| `age_and_managed_bytes` | Both rules independently reached it. |

Free-form operator prose is never stored as a policy reason.

### 5.6 The per-run destructive bound

No run may **newly** select more than `max_deletions_per_run` captures. This is
a hard destructive safety bound. When further candidates remain, the plan and
the run result report `more_work_remains = true`. Truncation is never silent and
the cap is never exceeded.

Pending-deletion recovery is separate from this bound — see §8.

---

## 6. The lifecycle table

Migration `003_capture_media_lifecycle.sql` adds:

```sql
CREATE TABLE capture_media_lifecycle (
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
```

The current schema version is now **3**.

| Lifecycle state | Meaning |
| --- | --- |
| *(no row)* | **PRESENT** — the JPEG is expected on disk. |
| `pending_delete` | Retention deletion intended, not yet finalised. |
| `deleted` | Media reclaimed; the observation recording it committed with the transition. |

Migration 003 uses a plain `CREATE TABLE`, **not** `CREATE TABLE IF NOT
EXISTS`. The migration runner already guarantees, from the `schema_migrations`
history, that the file executes only when version 3 is genuinely pending, so
`IF NOT EXISTS` could never make a legitimate re-run succeed — it could only let
a *pre-existing* table of some other shape silently satisfy the statement while
the runner went on to record version 3. A database claiming version 3 with a
table the migration never created is precisely the state that must fail closed,
so a name collision aborts the migration and rolls the database back to
version 2. Migration idempotency comes from the recorded history, not from the
DDL.

### Adopting an unversioned version-3 database

Legacy adoption writes history rows and then trusts the tables forever
afterwards, so for the lifecycle table an exact **column set is not enough**.
Two tables can carry the same five column names while one enforces a primary
key, a foreign key and three `CHECK` constraints and the other enforces nothing
at all. Before an unversioned database is adopted at version 3, MGO verifies
that `capture_media_lifecycle` genuinely has:

* `capture_id` as the primary key;
* `NOT NULL` on `state`, `requested_at_utc` and `reason`, and a nullable
  `deleted_at_utc`;
* a foreign key from `capture_id` to `captures(id)`;
* the `state` vocabulary `CHECK`;
* the `reason` vocabulary `CHECK`;
* the pending/deleted timestamp-coherence `CHECK`.

Primary keys and foreign keys come from `PRAGMA table_info` and
`PRAGMA foreign_key_list`. `CHECK` constraints are exposed by no pragma, so the
stored `CREATE TABLE` text is compared with comments stripped, whitespace
removed and case folded — which ignores layout but not meaning. Anything that
does not match is refused with `IncompatibleSchemaError`, and the database is
left exactly as it was found with no fabricated migration history.

Version-1 and version-2 adoption is deliberately unchanged and remains a
columns-only check: strengthening it would change whether real deployed
databases can still be adopted, which is not this task's decision to make.

### Why a separate table

The `captures` table is the historical catalogue of captures that **happened**.
A capture record is evidence, and reclaiming the JPEG it points at does not
un-happen the capture. Those are two different facts, so they live in two
places.

**Historical `captures` rows are retained permanently.** Retention never deletes
a capture row, and never repurposes `extra_metadata` as a hidden retention
database.

Keeping lifecycle state out of `captures` also preserves the **exact shape of
the version-2 capture table**. The repository's legacy-schema adoption compares
an exact column set, so adding a retention column to `captures` would have made
every existing unversioned version-2 database unrecognisable. An unversioned
version-2 database is still adopted at version 2 and then migrated to version 3.

---

## 7. The deletion state machine

Filesystem deletion and SQLite cannot be one atomic transaction, so retention
uses a **durable, recoverable intent**.

### Stage A — durable intent

In one short transaction, conditional on there being no lifecycle row for the
capture:

* persist `pending_delete`;
* persist `requested_at_utc`;
* persist the fixed policy reason.

**Commit.** No database transaction is held while the filesystem is touched. The
conditional insert is what stops two overlapping executions claiming the same
capture: one inserts, the other is refused and leaves the capture alone.

### Stage B — filesystem operation

After Stage A commits, the full safety validation runs **again**, as close to
the deletion as the code can get it. Then exactly one file is unlinked. No
database lock is held.

Validation also runs *before* Stage A, so a candidate whose media is already
missing or whose path is unsafe never acquires a deletion intent at all.

### Stage C — durable completion

In one new short transaction, committed together:

1. `pending_delete` → `deleted`, conditional on the row still being pending;
2. `deleted_at_utc` is set;
3. the immutable success observation is created.

If the conditional `UPDATE` matches nothing, **no observation is written** —
that is what guarantees one deletion produces exactly one success observation.

If Stage C fails *after* the file has been deleted, the lifecycle row remains
`pending_delete`. Nothing fabricates a successful completion in memory; the
durable pending intent is what makes recovery possible.

### Deletion failure

If Stage B fails while the file still exists, one transaction commits both:

1. the pending intent is removed, returning the media logically to PRESENT;
2. an immutable failure observation.

If that recovery transaction itself fails, the durable `pending_delete` row is
left intact. The next run can recover it; a lifecycle state invented in memory
cannot be recovered from.

There is **one attempt per capture per run**. Nothing retries a filesystem
deletion inside the same run.

---

## 8. Pending-deletion recovery

A run inspects pending lifecycle entries **before** selecting new candidates.

| Pending state | Behaviour |
| --- | --- |
| File still safely exists | Revalidate fully, then complete the deletion and finalise. |
| File is absent | Finalise as `deleted` with a success observation carrying `recovered_pending: true`. The durable intent proves MGO had already authorised removal of that specific capture — this is a crash between unlink and finalisation. |
| Unsafe path / directory / symlink / size mismatch / otherwise unverifiable | **Do not touch it.** Report the fixed error category and stop the destructive run, leaving the intent standing for a human. |

Recovery is **not** governed by the current `minimum_keep_count`: it represents
an earlier durable destructive intent that may already have been carried out.
Re-deciding it now could only leave an already-deleted file reported as an
unexplained inconsistency forever.

**When `retention.enabled = false`, no recovery mutation occurs.** Disabled means
no destructive retention mutation of any kind. An interrupted deletion stays
interrupted until someone turns retention on, and the durable intent is exactly
what makes waiting safe.

### PRESENT + missing is not recovery

A capture in PRESENT state whose JPEG is already missing is **not** proof that
retention removed it. It is never silently marked deleted and never given a
fabricated success observation. It is reported as `media_missing` and stops the
destructive run.

The distinction is load-bearing:

* `PRESENT` + missing = unexplained inconsistency;
* `pending_delete` + missing = recoverable interrupted retention operation.

---

## 9. Filesystem safety boundary

Retention may delete only the exact media file represented by an eligible
catalogue record. **The database catalogue is the authority for candidate
identity.** Nothing scans a directory and decides that old-looking JPEGs are
disposable.

Before unlinking, **all** of the following must hold:

1. the catalogue path is absolute;
2. the path contains no `..` component (rejected syntactically, before any
   normalisation could erase the evidence);
3. the catalogue filename equals the path's final component;
4. the target is not itself a symlink (checked *before* resolution, since
   resolving a link is how a link gets followed out of the root);
5. the resolved parent directory is the configured capture root or lies beneath
   it;
6. the configured capture root itself resolves to an absolute existing
   directory;
7. the target is a regular file, not a directory;
8. its current on-disk size equals the catalogue `filesize_bytes`.

If any condition fails: **do not delete, fail closed**, and report a fixed
failure category.

Retention never deletes recursively, never calls `rmtree`, never removes a
directory, and never deletes an untracked file merely because it sits in the
capture folder.

---

## 10. Dry run

`RetentionService.dry_run()` reads the catalogue, evaluates the policy and
reports the candidates, the projected byte recovery and whether the byte target
can be satisfied.

A dry run **must not and does not**: create or modify a lifecycle row, delete a
file, record an observation, alter a capture record, or move a runtime counter.
It is a pure preview of the current policy decision, and it is available whether
or not retention is enabled — previewing a policy is how an operator decides
whether to enable it.

---

## 11. Disabled behaviour

With `retention.enabled = false`, a destructive `run_once()` performs **no
filesystem mutation and no database mutation**, and returns a truthful result
with `executed = false`, `enabled = false` and no error. No startup code
executes retention.

The read-only planner can still be exercised independently.

---

## 12. Concurrent runs

Two retention runs in one application process cannot execute together. A
process-level mutual exclusion holds for the duration of a run; a second
overlapping destructive run returns `BUSY` deterministically rather than
queueing. Nothing waits, because a caller that waited would delete against a
plan computed before the run it waited for changed the catalogue underneath it.

The database transitions are conditional as well, so duplicate finalisation
cannot silently occur even across processes. **No capture can receive two
successful retention observations for one deletion.**

---

## 13. Failure philosophy

A destructive retention run is conservative. On any safety-relevant failure it
**stops**:

* no further candidates are attempted;
* already-completed successful deletions stand and are reported;
* unattempted candidates are left entirely untouched;
* the **first** fixed failure category is reported.

An **unexpected** exception is not an exception to any of that. Ordinary
`Exception`s escaping the run are converted at an execution boundary into a
result with category `unexpected`: the run stops, the process lock is released,
the runtime state moves to `error` (never left stranded in `running`), and the
counters for any deletion that had **already completed** are preserved — because
stopping does not un-delete what was already reclaimed. If the exception
occurred after the unlink but before finalisation, the `pending_delete` row is
left durable and the next run recovers it.

`KeyboardInterrupt`, `SystemExit` and the rest of `BaseException` are
deliberately **not** converted. They are process-control signals, not retention
failures, and catching them to make a status endpoint look tidy would suppress a
shutdown.

Detailed exception information is logged internally and only there. No raw
exception, traceback, database path or capture-root path appears in the public
status contract.

---

## 14. Observations

Retention reuses the existing immutable observation timeline. It creates no
parallel event log, and never mutates or deletes an existing observation.

| Field | Value |
| --- | --- |
| `kind` | `capture_retention` |
| `source` | `mgo-retention` |
| success `status` | `reclaimed` |
| success `summary` | `Capture media removed by retention policy` |
| failure `status` | `failed` |
| failure `summary` | `Capture media retention failed` |
| `correlation_id` | the capture id |

Success payload: `capture_id`, `filename`, `filesize_bytes`, `policy_reason`,
`captured_at`, `recovered_pending`.

Failure payload: `capture_id`, `error_category`, `policy_reason`.

**A failure payload deliberately carries no `filename`**, and the asymmetry with
the success payload is the point. A success observation is only ever written
after the full path/filename safety boundary has passed, so its filename is a
value retention has already verified. A *failure* is frequently the boundary
refusing that very value — a damaged catalogue filename may itself be an
absolute path, a traversal or a database location — and persisting it would
write exactly the string the privacy contract exists to keep out of the
immutable timeline. A sanitised path or a basename is not offered in its place
either: the capture id identifies the record completely, and anything derived
from the rejected value would still be derived from it.

Never included anywhere: `absolute_path`, the capture directory, the database
path, command lines, tracebacks, raw exception strings.

### Fixed error categories

The public failure vocabulary is bounded and closed:

`unsafe_path`, `media_missing`, `not_regular_file`, `size_mismatch`,
`filesystem_delete_failed`, `database_transition_failed`, `finalization_failed`,
`catalogue_invalid`, `busy`, `unexpected`.

Each has one fixed public sentence. No exception message, path, filename or
configuration value can ride out on one.

### Failing closed on a bad catalogue

Every column of the retention projection is **validated, never coerced**, and
any column that cannot be decoded stops the run with `catalogue_invalid`:

| Column | Rule |
| --- | --- |
| `id`, `filename`, `absolute_path`, `extra_metadata` | must be a non-empty string |
| `filesize_bytes` | must be an integer (not `bool`), and must be `> 0` |
| `captured_at_utc`, `created_at_utc`, `requested_at_utc`, `deleted_at_utc` | must parse as a timezone-aware ISO-8601 instant |
| `extra_metadata` | must parse as a JSON object |
| `state`, `reason` | must be in the stored vocabulary |

Two of these are worth stating plainly, because SQLite does not enforce them.
Column affinity is a *conversion preference*, not a constraint — the `captures`
table is not `STRICT` — so a damaged or hand-repaired row can hold text, a real,
a blob or `NULL` in any column:

* `str()` is never used to coerce a required text column. `str(None)` is the
  four-character filename `"None"`: a value that looks real, would be compared
  against a real path, and was never in the catalogue. The filename and the
  absolute path are the two columns that decide *which file is about to be
  removed*, so neither may be manufactured.
* `filesize_bytes` must be **positive**. The capture service only ever
  catalogues a verified non-empty JPEG, so zero or negative is not a small file
  — it is a corrupt record, and it must not be allowed to present itself as an
  ordinary size mismatch against a real file on disk.

**No deletion occurs after catalogue parsing has become untrustworthy**, and no
decoding failure escapes as a raw Python conversion error. The origin is the
single field standing between an automatic capture and a manual one; reading it
out of a document that will not parse is how a manual capture gets deleted.

---

## 15. Status endpoint

```
GET /retention/status
```

```json
{
  "enabled": false,
  "state": "disabled",
  "total_runs": 0,
  "total_captures_deleted": 0,
  "total_bytes_reclaimed": 0,
  "last_run_at": null,
  "last_run_candidate_count": 0,
  "last_run_deleted_count": 0,
  "last_run_bytes_reclaimed": 0,
  "last_error": null
}
```

**The endpoint is inert.** The request does not run the planner, scan the
captures table, stat any file, delete anything, create lifecycle state, record
an observation, start a worker, touch the camera or run a migration. It reads
one application-managed holder and nothing else, and it moves no counter —
polling it a thousand times leaves the numbers exactly where they were.

It returns HTTP 200 whenever the API is serving, including in the `disabled` and
`error` states. States: `disabled`, `idle`, `running`, `error`.

Counters are **process-lifetime** and reset on restart. They are never
persisted; the durable history is the lifecycle table and the observation
timeline.

No filesystem path and no raw exception appears in the response.

---

## 16. Capture API compatibility

`GET /captures` and `GET /captures/{capture_id}` are unchanged. No field was
removed or renamed, and neither call is destructive.

Captures whose media has been reclaimed are **still listed**. The catalogue is a
history of captures that happened; the lifecycle table is the authority on
whether their media was subsequently reclaimed. Task 14.1 adds no image-serving
or download behaviour.

---

## 17. Event capture is unchanged

Task 14.1 alters no motion-transition semantics, event-capture queue semantics,
pending-trigger or in-flight-capture bound, drop/coalesce behaviour, retry
policy, `CaptureWorkflow` behaviour, `CameraCoordinator` behaviour, preview
restoration, event-capture observation or event-capture counter.

No retention logic belongs in the camera transaction. Retention never acquires
the camera coordinator, never starts or stops preview, never invokes the capture
service or workflow, and never touches a camera backend. A JPEG is never deleted
immediately after capture merely because the current policy would eventually
make it eligible — retention is a separate lifecycle operation.

A capture that is currently in progress is not yet catalogued and therefore
cannot be a retention candidate.

---

## 18. Deferred

Not implemented, and each requiring its own task: automatic retention
scheduling, a service timer, a periodic deletion loop, disk-pressure emergency
deletion, manual-capture and unknown-origin deletion policies, an image
serving/download API, thumbnails, bird detection, species classification,
ROI/feeder masking, burst capture, pre-roll/post-roll, video recording,
visit/session grouping, notification attachments, Telegram/email delivery,
production enablement, physical retention validation, long-duration unattended
validation and camera hardware hardening.
