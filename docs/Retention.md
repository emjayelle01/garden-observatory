# Capture Media Retention

**Status: software foundation plus a manual operator command. Disabled by
default. Not authorised for production. Never physically validated.**

Task 14.1 built the machinery MGO needs to reclaim captured media safely.
Task 14.2 added the first supported way for an operator to invoke it by hand.
Neither turns the machinery on, schedules it, or exposes it destructively over
HTTP, and nothing here has ever deleted a real capture on the Raspberry Pi.

| Task | What it provides |
| --- | --- |
| 14.1 | The safe retention machinery: policy, lifecycle, executor, status. |
| 14.2 | A manual operator command, `mgo-retention`, and a genuinely read-only preview. |
| Not yet implemented or authorised | Automatic scheduling; production enablement; physical retention validation; permanent event-capture enablement. |

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
    capture_id TEXT NOT NULL PRIMARY KEY REFERENCES captures(id),
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

### Capture identity

**Every lifecycle row belongs to exactly one real capture.** `capture_id` is the
primary key **and** is explicitly `NOT NULL`, and it references `captures(id)`.
`NULL` is not a valid lifecycle capture identity.

The explicit `NOT NULL` is load-bearing rather than decorative. In SQLite a
`PRIMARY KEY` column that is not `INTEGER PRIMARY KEY` remains **nullable** — a
documented legacy quirk — and a `NULL` foreign key is never checked, because
`NULL` means there is no referenced value to check. Without it the table would
admit lifecycle rows bound to no capture at all, and would admit *more than one*
of them, since the primary-key index treats `NULL`s as distinct.

The two constraints close different halves of the same invariant: `NOT NULL`
refuses "no capture at all", and the foreign key refuses "a capture that does not
exist". Legacy version-3 adoption verifies the `NOT NULL` requirement alongside
the primary key, the foreign key and the three `CHECK` constraints.

Retention deliberately has **no behaviour** for an unbound lifecycle row. The
database prevents the invalid state rather than the application learning to
interpret it — the failure belongs at the schema boundary.

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
* `NOT NULL` on `capture_id`, `state`, `requested_at_utc` and `reason`, and a
  nullable `deleted_at_utc`;
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
It issues no SQL write of any kind, creates no missing database or parent
directory, and changes no journal mode. It is a pure preview of the current
policy decision, and it is available whether or not retention is enabled —
previewing a policy is how an operator decides whether to enable it.

The precise claim is **no MGO-managed state changes**. That is deliberately not
the same as "no byte on the filesystem moves": reading a live WAL database
requires SQLite to use its own `-shm` shared-memory index, and that is SQLite's
documented read mechanism rather than anything this code chooses. See below.

### Read-only at the SQLite boundary

"Read-only" here means read-only *at the connection*, not merely "this method
only issues a `SELECT`". Task 14.1 read the catalogue through the ordinary
read-write helper, which — as a side effect of **opening**, not of the statement
— will create a missing parent directory, bring a missing database file into
existence, and request WAL journalling on a database that was not using it. All
three were reachable from an operation documented as mutating nothing.

The preview now reads through SQLite's `mode=ro` URI instead. A missing database
fails cleanly rather than being created, no directory is made, no journal mode is
requested, no migration runs, and no statement can modify anything. It reuses the
**same** projection SQL and the **same** fail-closed decoder as the destructive
path — a second interpretation of origin, lifecycle state, reason, timestamps or
filesize is exactly the divergence that would let a preview disagree with the run
it is previewing.

One honest limitation, stated precisely rather than hidden behind "mutates
nothing": a database in WAL mode cannot be read *at all* — even read-only, even
with `mode=ro` — without SQLite's `-shm` shared-memory index, so SQLite may
create or use that sidecar. That is SQLite's documented mechanism for reading
WAL, not a change this code makes.

`immutable=1` would avoid it and is **deliberately not used**: it tells SQLite
the file cannot change, which is untrue of a live database and would licence
SQLite to ignore concurrent WAL state. Reading a live database through semantics
that may miss committed data is a worse trade than a sidecar.

What is asserted directly, on both journal modes: no SQL write, no database or
directory created, journal mode unchanged, and every table byte-identical
afterwards.

---

## 10a. The operator command (Task 14.2)

`mgo-retention` is the first supported way to invoke retention by hand. It is a
thin boundary: it parses arguments, checks preconditions, calls the retention
domain service, prints one JSON document and maps the outcome to an exit status.
Every policy decision and safety refusal still happens behind it in the Task 14.1
engine.

```bash
mgo-retention plan
mgo-retention run-once --execute
```

There are exactly two subcommands and no aliases.

### `plan` — read-only

Evaluates the configured policy and prints what it would select, using the
read-only path above. It creates no lifecycle row, deletes no file, records no
observation, applies no migration and moves no counter. It works whether or not
retention is enabled.

`plan` deliberately performs **no filesystem validation**. The filesystem is only
ever a veto during destructive execution; stat-ing candidates during a preview
would make `plan` report a different set from the one the policy actually chose.
A capture whose media is already missing therefore still appears in the plan.

Output is JSON on stdout: the fields of `RetentionPlan.as_dict()` plus
`retention_enabled`. Candidates carry `capture_id`, `captured_at`,
`filesize_bytes` and `policy_reason` — never an absolute path, capture root,
database location or configuration path.

**The catalogue `filename` is deliberately omitted.** It has been validated only
as a non-empty string; it has *not* passed the path/filename safety boundary,
because that boundary belongs to destructive execution and running it during a
preview would make `plan` disagree with the pure policy selection it exists to
report. A damaged or hand-edited catalogue can therefore hold a "filename" that
is really a path, and publishing it would put that string into output the
contract says carries none. It is omitted rather than sanitised or reduced to a
basename: anything derived from an unverified value is still derived from it, and
the capture id identifies the record completely. Destructive execution still
uses the filename, and still validates it before any unlink.

### `run-once --execute` — potentially destructive

Executes **exactly one** retention run and exits. Three gates must all pass, in
this order:

| Gate | Requirement | When it is checked |
| --- | --- | --- |
| A | The exact `--execute` flag — and *exact* is enforced, not merely intended. There is no `-y`, `--yes`, `--force`, `--really` or `--override`, and no abbreviation: one spelling is easier to audit. | Before anything is opened or read. |
| B | `MGO_CONFIG_PATH` set, and **absolute as supplied**, naming the configuration this run acts on. | Before anything is opened or read. |
| C | `retention.enabled = true` in that configuration. | After the configuration file is read — necessarily, since that is where the value lives. |

Only once all three pass does the schema gate run and the database get opened.

Gates A and B are the ones that hold *before any file is touched*; gate C cannot
be, because establishing it means reading the configuration. Nothing destructive
happens in between: reading the operator's own configuration file is the only
step, and the database and capture directory are still untouched when gate C is
evaluated.

Gate A needed help from the parser to mean what it says. `argparse` accepts
unambiguous prefixes of long options by default, so `--exe`, `--exec`, `--execut`
and even `--e` were all accepted as `--execute` and all reached deletion — one
deliberate authorisation spelling had quietly become a family of them, and a
typo was a destructive consent. Abbreviation is now disabled on every parser in
the tree, subparsers included: setting it on the top-level parser alone is not
enough, because `add_subparsers` builds each subparser with argparse's own
defaults.

Gate B exists because configuration identity decides *which* media is deleted.
Without it, an operator standing in the repository could run
`run-once --execute` and have it resolve the tracked **development**
configuration — pointing a deletion at whichever database and capture directory
that file happens to name.

It must be **absolute as supplied**. The application's general rules resolve a
relative `MGO_CONFIG_PATH` against the current working directory, which is fine
for configuration at large and too weak here: `config/mgo.toml` names a different
file after a `cd`, so the same environment value would select a different
deployment to delete from.

`~` is not expanded before that decision, and that distinction is the whole gate.
`~/mgo.toml` is not an absolute path — it is an instruction to look in *the
executing account's* home directory, so the same string names a different
configuration under a different account. Expanding it first made it look
absolute and let it through, which is the same context-dependence the gate
exists to remove, merely a different context from the working directory. An
already-expanded absolute path is of course fine: a home directory is not the
problem, the *deferred interpretation* of one is.

A relative value — `~` forms included — is refused rather than resolved on the
operator's behalf. Silently making it absolute would produce exactly the outcome
the gate prevents while looking like it had been checked. This is a CLI-only
destructive rule; it changes nothing about `resolve_config_path()`, which still
expands `~` and still resolves relative values, and nothing about `plan`, which
is read-only and keeps the ordinary rules.

The command **never repeats**. When the result reports `more_work_remains`, that
is a fact for the operator, not a trigger: a second run is a second decision.
There is no loop, no retry, no daemon and no interval.

Output is a bounded JSON result: `executed`, `enabled`, `candidate_count`,
`deleted_count`, `bytes_reclaimed`, `recovered_count`, `more_work_remains`,
`error_category` and `error_message`. No filename, path, raw exception or
traceback appears in it.

### A malformed configuration is the operator's, not MGO's

A configuration the loader cannot turn into an `MGOConfig` is an operator
refusal (exit `2`), never an internal failure (exit `5`). Exit `5` is reserved
for genuine defects, and spending it on an ordinary mistake in the operator's
own file points them at the wrong thing entirely.

The boundary catches the exceptions the *existing* loader can legitimately
raise, established by reading it rather than guessing: `OSError`, `ValueError`
(which covers `TOMLDecodeError`), `KeyError`, `TypeError`, `AttributeError` and
`OverflowError`. The last is the least obvious and the reason it is listed: TOML
has a literal `inf`, so `collection_interval_seconds = inf` is *syntactically
valid*, parses to a floating-point infinity and reaches `int(...)`, which raises
`OverflowError` — and that is not a `ValueError` subclass, so it escaped
entirely.

`Exception` is deliberately **not** caught wholesale. Turning every programmer
defect inside the loader into "bad configuration" would hide real bugs behind a
message blaming the operator, so a genuine internal defect still exits `5`.

### The schema gate

Neither subcommand applies migrations. Schema migration belongs to application
startup, where it is transactional, logged and part of a reviewed deployment. An
operator asking to inspect or execute retention must never silently upgrade a
database as a side effect of asking — and an *older* database is precisely where
a silent upgrade would be most tempting and least safe.

Both commands therefore require the database to already record exactly schema
version **3**. Every other case is refused identically and the database is left
untouched: lower, higher, unversioned, missing and unreadable.

### An invalid invocation is refused without repeating it

`argparse`'s own `error()` writes a usage dump *plus the offending argument text*
straight to `sys.stderr`, and only then raises `SystemExit`. Catching
`SystemExit` afterwards is too late: the text has already been written, it names
whatever the operator typed — `--config /etc/garden-observatory/mgo.toml` rode
out verbatim — and it goes to the process's stderr rather than to the stream the
caller asked for, so a test capturing the injected stream would have seen an
innocent-looking empty refusal.

Invalid arguments are therefore refused through this command's own boundary: one
fixed sentence, on the caller's stream, exit `2`, no usage dump, and nothing
operator-supplied in it. The offending text is discarded rather than truncated —
a truncated path is still a path.

`--help` is untouched. It exits through `parser.exit()` rather than `error()`,
still succeeds, and still prints the ordinary help; its text is static, so there
is nothing in it to bound.

### No policy on the command line

There is no `--max-age-days`, `--max-managed-bytes`, `--minimum-keep-count`,
`--max-deletions`, `--origin`, `--capture-id`, `--before` or `--after`, and
unknown arguments are refused rather than ignored. The command executes the
**configured, reviewed** policy or it executes nothing. An operator who could
retune the policy at the prompt could turn a reviewed retention policy into an
unreviewed deletion at the moment of deletion.

There is likewise no `delete`, `purge` or `rm` subcommand. Task 14.2 exposes the
existing policy engine and nothing else; deleting an arbitrary named file remains
out of scope.

### Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Completed successfully. |
| `2` | Operator or configuration refusal — missing or abbreviated `--execute`, retention disabled, `MGO_CONFIG_PATH` unset or not absolute as supplied (`~` included), a malformed configuration file, invalid arguments. |
| `3` | Database or schema precondition refused. |
| `4` | The run completed but stopped on a bounded retention error category. |
| `5` | Unexpected failure, reduced to one fixed sentence. |

`0` and `2` carry the same meaning they already carry in the backup and
support-bundle commands. `1` is deliberately unused. No capture id, filename or
policy reason is ever encoded into an exit code, and no traceback is ever
printed — it is the one part of the output that could carry a capture path or a
database location into a log an operator forwards elsewhere.

### Concurrency, stated plainly

Task 14.2 introduces no scheduler and no cross-process lock. The Task 14.1
process-local mutex protects two runs inside one process; two *separately
invoked* CLI processes are not serialised by it. The final safety boundary
across processes remains the database's conditional lifecycle transitions: a
deletion intent can be claimed once, a finalisation can fire once, and one
deletion produces exactly one success observation. That is a real limitation,
not a guarantee, and inventing a broad cross-process locking subsystem was
deliberately out of scope here.

### What Task 14.2 does not do

It performs **no production deletion**. It does not deploy, does not access the
Raspberry Pi, does not enable retention, does not choose a retention period or
storage budget, does not schedule anything, and does not physically validate
deletion. Those remain a later controlled task.

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

Not implemented, and each requiring its own task: production enablement of
the retention timer (the assets exist, see §19), a periodic deletion loop
inside the API process, disk-pressure emergency
deletion, manual-capture and unknown-origin deletion policies, an image
serving/download API, thumbnails, bird detection, species classification,
ROI/feeder masking, burst capture, pre-roll/post-roll, video recording,
visit/session grouping, notification attachments, Telegram/email delivery,
production enablement, physical retention validation, long-duration unattended
validation and camera hardware hardening.

---

## 19. Scheduled execution (Task 14.5)

Retention can now be run unattended, and the contract is fail-closed at every
step. Nothing in this section changes what §1–§17 promise: the policy, the
managed set, the safety boundary and the deletion state machine are exactly
as reviewed.

### 19.1 `scheduled-run`

`mgo-retention scheduled-run --execute [--backup-directory PATH]` is the
subcommand the timer invokes. It keeps every gate `run-once` has — the
explicit `--execute` flag, an absolute `MGO_CONFIG_PATH` as supplied, and the
schema gate — and makes the same single call into the service. What differs
is how a run that *correctly declined to start* is reported:

| Condition | `outcome` | `reason` | Exit |
| --- | --- | --- | --- |
| `retention.enabled = false` | `skipped` | `retention_disabled` | 0 |
| another retention process holds the lock | `skipped` | `lock_unavailable` or `retention_busy` | 0 |
| a database backup is in progress | `skipped` | `backup_in_progress` | 0 |
| the run started | `executed` | — | 0, or 4 on a safety refusal |
| a gate refused (`--execute` missing, relative configuration, wrong schema) | — | — | 2 or 3 |

A skip is not silent: the structured document names the reason and the
journal carries it. It is merely not a failed unit. **With the production
configuration as deployed (`[retention]` absent), every scheduled run skips
as `retention_disabled` and deletes nothing.**

`--backup-directory` must be absolute as supplied, for the same reason the
configuration path must be, and the directory must already exist: a run never
creates it. Both `scheduled-run` and `run-once` accept it (Task 14.5A) and
both default to the canonical production backup location, so an operator's
manual run holds the same lock the nightly backup does.

### 19.2 Locks

Two locks now guard a run, taken in this order and released in reverse:

1. the **process lock** (§12), unchanged;
2. the **cross-process file lock** `<database directory>/.mgo-retention.lock`,
   an `O_CREAT|O_EXCL` file with the same age-based stale reclamation as the
   backup lock. The timer and an operator's shell are different processes,
   and the process lock cannot see across them. The database directory is
   chosen because every retention execution already needs it writable. A lock
   that cannot be taken or created declines the run with `lock_unavailable`;
   a service constructed with no lock location declines every run.

Then, still before anything is read, the run **takes the backup's own lock**
(`<backup directory>/.mgo-backup.lock`) and holds it until the run ends
(Task 14.5A). That file is the backup's `O_CREAT|O_EXCL` primitive, acquired
here with the same call and the same age-based stale reclamation, so a
backup and a retention run can never both hold it whichever starts first:

| Interleaving | Outcome |
| --- | --- |
| retention starts, then a backup starts at any instant during the run | the backup's acquisition fails (`Another retention operation is already running`) and it exits without publishing; retention completes |
| a backup starts, then retention starts | retention skips with `backup_in_progress` and deletes nothing |
| both start in the same instant | exactly one creates the file; the other declines |
| a stale backup lock (older than the threshold) | reclaimed, as the backup itself would |
| a backup lock whose content or age cannot be read | treated as held; retention skips |
| a process dies holding either lock | the file survives; the next run respects it until it is stale, then reclaims it |
| timer catch-up after a reboot | `After=mgo-backup.service` orders the pair; the lock decides if ordering is not enough |
| no backup lock location, or the backup directory absent | retention declines with `lock_unavailable` |

The earlier design *read the backup lock's age* before and after taking the
retention lock. The Task 14.5A review showed that to be check-then-act: a
backup starting a moment after the second check ran alongside the deletion.
The interleavings above are executable, in-process and across real
processes: `tests/test_retention_mutual_exclusion.py`.

What is **not** excluded, and why: the deployment lock
(`/run/lock/mgo-deployment.lock`) is a root-owned `0600` `flock` whose
holder cannot be observed by the runtime account — its modification time
never changes — so retention cannot yield to a deployment, and the gateway
does not consult the retention lock. A restore test never touches media.
The contract for those is therefore an operator gate, stated in
`docs/Operations.md` §4.7: before a deployment, a recovery, a restore test
or manual database maintenance, the retention timer must be inactive or
proven idle, using `GET /retention/status` → `scheduled_lock_state`, which
reports the retention lock as `idle`, `busy` or `unknown`.

### 19.3 Units and timer

`scripts/deploy/mgo-retention.service.template` renders to a `Type=oneshot`
unit running as the `mgo` account with the backup unit's full hardening set,
no network, no devices, and `ReadWritePaths` limited to the database
directory, the capture directory and the backup directory. The last is there
for exactly one file — the backup lock the run holds (§19.2); the retention
code has no path that names a recovery set. It carries no `[Install]`
section; only the timer is ever enabled.

`scripts/deploy/mgo-retention.timer` fires at 04:00 local time with a
15-minute randomised delay, `AccuracySec=1m` and `Persistent=true`, and is
ordered `After=mgo-backup.service`. The backup window (02:30 plus up to 30
minutes, 15-minute ceiling) is finished by 03:15; the margin is a courtesy,
the lock is the guarantee.

### 19.4 Installing and enabling are two decisions

`scripts/deploy/install-retention-timer.sh` renders, validates and publishes
the pair atomically (a same-filesystem temporary and `mv -T`), verifies bytes,
type and mode afterwards, rolls the first unit back if the second cannot be
published, is idempotent, and supports an exact non-root `--dry-run`. **It
never enables or starts the timer unless `--enable` is passed**, and then it
seeds the persistence stamp first so enabling cannot trigger a catch-up run.
Root is required to install into the real unit directory. Full commissioning
steps are in `docs/Operations.md` §4.5.

### 19.5 Still not done

Physical validation of a deletion on the Pi, an operating policy (the bounds
remain commented out in every tracked configuration), and enabling the timer
in production are each separate decisions and separate tasks.
