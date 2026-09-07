# Task 14.5 — Automatic capture safety and retention scheduling

**Status: design record and implementation contract. Repository work only.
Nothing in this task touches the Raspberry Pi, production configuration, the
production database, production media, backups, the deployment gateway, sudo
policy or any installed systemd unit.**

This task does not implement species recognition, first-seen detection, a
notification transport, or any model, runtime, weight or label set. Task 14.4
established that none of those exist at `c416b6bf…`; they remain future work.

---

## 1. Why this exists

Task 14.4 commissioned the live capture foundation on the deployed build
(`c416b6bf7ddcbbedcf8ebcc8af2cdba8b7e1425d`) and found three blockers to safe
continuous operation:

1. **No hard ceiling on automatic capture.** Motion-triggered capture had a
   cooldown and a one-slot queue, but nothing bounded how many stills a windy
   afternoon could produce. Task 13.2 measured about three captures a minute
   with nothing to reclaim them.
2. **Whole-frame changes read as motion.** Two `motion_detected` transitions
   during the Task 14.4 window had changed-pixel ratios of about `0.986` and
   `0.708` and settled to exactly `0.0` on the next frame. That is an exposure
   or lighting step, not a subject in the scene.
3. **Retention had no scheduled execution contract.** `mgo-retention run-once`
   exists and is safe, but nothing could run it unattended without a design
   for locks, backup overlap and a fail-closed default.

The starting evidence this task reconciles is the Task 14.4 completion report:
catalogue 29 rows / 57,786,186 bytes, 26 media files / 59,307,483 bytes,
protected evidence 17/17 at 42,660,151 bytes, recovery set
`mgo-20260906T172226Z` (`.db` `5c3d9ea2…fdc3`), and the scheduled set
`mgo-20260907T005128Z` whose configuration snapshot is the temporary Task 14.4
candidate (`9558c3ed…1cb31`). That last set is not touched, not repaired and
not referenced anywhere in permanent documentation; the general lesson it
teaches is written into `docs/Operations.md` as a recovery warning.

---

## 2. Source audit (what exists at `c416b6bf…`)

| Concern | Where | Finding |
| --- | --- | --- |
| Configuration | `src/mgo/core/config.py` | Frozen dataclasses per section; per-section `_validate_*`; cross-section `_validate_event_capture_policy`; absent sections load with disabled defaults. |
| Capture workflow | `src/mgo/captures/workflow.py` | One camera transaction then one archive write; a JPEG is never deleted because cataloguing failed. |
| Manual capture | `POST /camera/capture` in `src/mgo/api/app.py` | Uses the shared workflow with no `extra_metadata`. |
| Motion analysis | `src/mgo/motion/detector.py`, `monitor.py` | Per-pixel luminance differencing at 160×90; rolling reference; observer persists on status change with cooldown suppression of repeated `motion_detected`. |
| Event capture | `src/mgo/event_capture/service.py` | One worker task, `QUEUE_CAPACITY = 1`, no retry; `submit()` admits only `MOTION_DETECTED`; `_execute()` calls the workflow and then records an observation. |
| Provenance | `MotionTrigger.capture_metadata()` | `extra_metadata = {"origin": "motion", "motion_status", "motion_score", "motion_threshold", "motion_evaluated_at"}` stored as JSON text in `captures.extra_metadata`. |
| Capture repository | `src/mgo/captures/archive.py` | `captures(id, filename, absolute_path UNIQUE, captured_at_utc, …, extra_metadata)` with `idx_captures_captured_at_utc`. Timestamps are `isoformat()` of UTC-aware datetimes. |
| Disk checks | `src/mgo/core/health.py` | `shutil.disk_usage(Path("/"))` — the root filesystem, not the media filesystem. Unsuitable as an admission guard. |
| Retention planning/execution | `src/mgo/retention/{policy,service,repository,cli}.py` | `plan_retention` manages only `origin == "motion"` exactly; execution is three-stage with a durable `pending_delete` intent; `run_once` holds a process-level `threading.Lock`; the CLI gates `--execute`, absolute `MGO_CONFIG_PATH`, `retention.enabled` and the schema version. |
| Backup locking | `src/mgo/operations/locking.py`, `backup.py` | `OperationLock`: `O_CREAT|O_EXCL` file, non-blocking, age-based stale reclamation (6 h); the backup holds `<backup dir>/.mgo-backup.lock`. |
| Runtime paths | `src/mgo/core/config.py` | `SYSTEM_DATABASE_DIRECTORY`, `SYSTEM_CAPTURE_DIRECTORY`, `SYSTEM_BACKUP_DIRECTORY`, `SYSTEM_RUNTIME_STATE_DIRECTORY`. |
| systemd conventions | `scripts/deploy/mgo-backup.service.template`, `mgo-backup.timer` | Oneshot, `User=mgo`, full hardening set, `ReadWritePaths` naming exactly the writable locations, `Persistent=true`, stamp seeded before enabling. |
| Installer conventions | `scripts/deploy/install-service-identity.sh` | Strict bash, `--dry-run`, `sed` templating, `install_managed_file` (skip identical, back up replaced), explicit enable with verification. Enabling and installing are one step there; this task separates them. |
| Migration machinery | `src/mgo/core/database.py` | Numbered SQL files, `CURRENT_SCHEMA_VERSION = 3`, forward-only. |
| Mutation register | `tests/mutation_register.py`, `scripts/dev/run-mutations.py` | Single-line `old` anchors, `suite` per entry, restoration by digest. |

### Design questions answered from source

1. **`origin=motion` representation.** `extra_metadata` JSON text; the managed
   set is `json_extract(extra_metadata, '$.origin') = 'motion'` with exact
   equality (`mgo.retention.policy.MANAGED_ORIGIN`). SQLite's `json_extract`
   errors on malformed JSON, so a quota query guards with `json_valid` and
   counts an unparseable row as automatic (see §4.3).
2. **Durable counting.** Every successful automatic capture is a `captures`
   row with that origin and a UTC `captured_at_utc`. Rows are the durable
   count; the `captured_at_utc` index makes a window query cheap. No new
   table is needed.
3. **Two workers racing.** Impossible today: `EventCaptureService.start()`
   raises if a worker exists, and the queue has one consumer. The admission
   controller nevertheless holds a `threading.Lock` across the count-and-
   reserve step, so a second admission path added later cannot double-admit.
4. **Manual and automatic publishing concurrently.** The camera transaction is
   serialised by `CameraCoordinator`; archive writes use independent bounded
   connections. Manual captures never consume automatic quota.
5. **Rejection point.** Inside the worker's `_execute`, before
   `CaptureWorkflow.capture()` — before preview release, before `rpicam-still`,
   before any file. A refused trigger produces no image, no row and no
   lifecycle row.
6. **Reserved capture size.** `rpicam-still` at 4608×2592: Task 13.2 averaged
   about 2.5 MB across 17 captures; Task 14.4's dark capture was 1,033,160
   bytes. The reservation is configurable (`maximum_capture_bytes`, default
   16 MiB) and enforced at the publication boundary.
7. **Free-space probe failure.** `shutil.disk_usage` on the capture directory
   raising `OSError` (or the directory being absent) refuses admission with
   `storage_reserve`. Never treated as room.
8. **Time.** Every module injects `clock: Callable[[], datetime]` defaulting
   to `datetime.now(UTC)`; the controller does the same.
9. **Status models.** Pydantic `BaseModel` classes in `app.py`; fields are
   added with defaults so the existing shape is a subset of the new one.
10. **Backup lock.** `<backup dir>/.mgo-backup.lock` via `OperationLock`.
11. **Retention lock.** A new `OperationLock` at `<database dir>/
    .mgo-retention.lock`, held by `RetentionService.run_once` in addition to
    the process-level lock. The database directory is the one location every
    retention execution — CLI or in-process — already needs writable.
12. **Detecting a running backup without privilege.** The backup directory is
    `mgo:mgo 0750`; the runtime account can `stat` the backup lock. A lock
    younger than the stale threshold means a backup is running and retention
    skips with `backup_in_progress`. The deployment lock (`/run/lock/
    mgo-deployment.lock`, root 0600) is not readable by the runtime account
    and is not consulted; a restore test never touches media, so it needs no
    exclusion.
13. **Timer ownership.** A dedicated `scripts/deploy/install-retention-timer.sh`.
    The identity installer installs *and enables* the backup timer in one
    step; retention must not be enabled implicitly, and the deployment gateway
    installer owns a different authority boundary.
14. **Migration.** Not required. Existing provenance is unambiguous, indexed
    and already the retention subsystem's authority. Migration 004 is left
    free for Task 15.

---

## 3. Contract

### 3.1 Configuration

`[event_capture]` when `enabled = true` requires all of:

| Key | Type | Bounds | Meaning |
| --- | --- | --- | --- |
| `max_captures_per_hour` | integer | 1 … 3600 | Automatic captures admitted in any rolling 3600 s window. |
| `max_captures_per_day` | integer | 1 … 86400 | Automatic captures admitted in the current UTC calendar day. |
| `minimum_free_bytes` | integer | ≥ 1 | Free space that must remain on the media filesystem *after* one capture. |
| `maximum_capture_bytes` | integer, default 16777216 | ≥ 1 | Size reserved for one capture and the ceiling an automatic capture may publish. |

`minimum_free_bytes + maximum_capture_bytes` must not exceed `2^63 − 1`.
An enabled section missing any mandatory key fails at load time. An absent
section, or `enabled = false`, loads exactly as before with every limit `None`.

`[motion]` gains `global_change_ratio_threshold` (float, default `0.5`) with
`0 < changed_pixel_ratio_threshold < global_change_ratio_threshold <= 1`.

### 3.2 Admission order (automatic capture only)

1. feature enabled — the worker only exists when it is;
2. worker not busy — the one-slot queue and single worker;
3. cooldown — newest automatic capture older than `motion.cooldown_seconds`;
4. rolling-hour quota;
5. UTC-day quota;
6. storage reserve — `free − maximum_capture_bytes ≥ minimum_free_bytes` on
   the filesystem containing `camera.capture_directory`;
7. camera prerequisite — the camera readiness holder reports available;
8. reservation — one in-flight slot taken under the controller lock.

Step 3 is checked here even though the motion observer already suppresses
re-entry within the cooldown, because the admission gate must not depend on
an upstream component's behaviour to be safe.

### 3.3 Quota semantics

* **Counted:** rows in `captures` whose `extra_metadata.origin == "motion"`,
  plus rows whose metadata is not valid JSON (counted conservatively), plus
  in-flight reservations.
* **Rolling hour:** `captured_at_utc ≥ now − 3600 s`. **UTC day:**
  `captured_at_utc ≥ today 00:00:00 UTC`. Both cutoffs are UTC; the row
  timestamp is re-parsed in Python and compared as a datetime, so ordering
  never depends on string layout.
* **Failed captures** produce no row and do not count; cooldown still spaces
  attempts.
* **In-flight** captures count as one until the worker releases them
  (success or failure).
* **Restart:** state is rebuilt from rows on the first evaluation; nothing is
  process-local except the reservation, which cannot outlive the process
  because the capture cannot either.
* **Clock reversal:** rows stamped later than "now" still satisfy `≥ cutoff`
  and count. A backwards clock therefore over-counts, never under-counts.
* **Malformed rows:** unparseable metadata or timestamp inside the window
  counts; a database error refuses admission with `admission_error`.

### 3.4 Storage guard

Probe `shutil.disk_usage(camera.capture_directory)`. Refuse unless
`free − maximum_capture_bytes ≥ minimum_free_bytes`. A probe error, a missing
directory or a non-directory refuses. At the publication boundary an automatic
capture whose file exceeds `maximum_capture_bytes` is removed (it is the file
this attempt just created, never pre-existing media) and reported as
`oversize_capture`; the floor is therefore never crossed by an admitted
capture.

Manual capture keeps its public behaviour except one shared fail-safe: when a
floor is configured and the media filesystem is already below it, the route
returns HTTP 507 and captures nothing.

### 3.5 Suppression reason codes

`cooldown`, `hourly_limit`, `daily_limit`, `storage_reserve`, `capture_busy`,
`global_scene_change`, `camera_unavailable`, `admission_error`,
`oversize_capture`. Suppression updates counters and the status endpoint on
every trigger; an observation is written only when the reason *changes*, so
a wind-driven afternoon cannot flood the timeline.

### 3.6 Global-change filter

The detector compares luminance after subtracting the *median* per-pixel
brightness shift between the two frames (uniform exposure steps cancel; a
bright subject over part of the frame leaves the median at zero, which is why
the mean was rejected during implementation), then applies the existing
changed-pixel ratio. If the larger of the raw and compensated ratios exceeds
`global_change_ratio_threshold` the status is `global_change`: not motion,
never enqueued, the reference is advanced to the new frame, and the next
stable frame settles to `no_motion`. Localised motion remains detectable.
Defaults are not field-calibrated; daylight commissioning is a later task.
See `docs/Capture-Safety.md` §6.

### 3.7 Retention scheduling

* `mgo-retention scheduled-run --execute [--backup-directory PATH]`: the same
  gates as `run-once`, but `retention_disabled`, `busy` and
  `backup_in_progress` are structured *skips* with exit 0. Errors keep their
  non-zero codes.
* `RetentionService.run_once` acquires `<db dir>/.mgo-retention.lock` after
  its process lock and checks the backup lock before touching anything.
* Units: `mgo-retention.service` (oneshot, `User=mgo`, hardened, writable
  paths = database directory and capture directory only) and
  `mgo-retention.timer` (`OnCalendar=*-*-* 04:00:00`, `RandomizedDelaySec=15m`,
  `AccuracySec=1m`, `Persistent=true`, `After=mgo-backup.service`).
* Installer: `scripts/deploy/install-retention-timer.sh` — dry-run, root
  required to install, validation, atomic publication with rollback of the
  pair, no `enable`/`start` without `--enable`, idempotent.

With the current production configuration (`[retention]` absent) an installed
timer performs no deletion: the run skips with `retention_disabled`.

---

## 4. Task 14.5A — independent review corrections (2026-09-07)

An adversarial review of PR #16 at `cd10340` re-derived every safety claim
above from the code and found four defects, each reproduced deterministically
before it was fixed. The fixes are additive commits on the same branch; the
original five commits are untouched. Two §2 decisions are revised below.

| # | Finding | Severity | Correction |
| --- | --- | --- | --- |
| A | The in-flight reservation was a process-local counter. A crash after `rpicam-still` wrote the JPEG and before the catalogue row was committed left an uncounted file, and the next process admitted again under a limit of one (reproduced: `attempt 2 admitted: True`). §3.3 "failed captures do not count" made an archive-failure loop bounded only by the storage floor. | Critical | **Durable reservation ledger**: a marker file beside the database, `O_EXCL`, flushed before the camera is touched, removed only when the archive has committed. Rows plus unreleased markers are the count; failed attempts count for the window; markers older than every window are swept. Ten crash boundaries are an executable table. |
| B | Suppression observations were written on every *change* of reason, so reasons that alternate wrote one row per trigger (reproduced: 200 triggers, 200 rows). | High | A 60-second monotonic minimum between suppression rows, on top of the reason-change rule. Counters are unchanged; identical repeats still write nothing. |
| C | Backup exclusion read the backup lock's *age* before and after taking the retention lock. A backup starting after the second check acquired its lock and ran alongside the deletion (reproduced). | Critical | The retention run now **holds the backup's own `O_EXCL` lock** for its duration. Mutual, whichever starts first; stale reclamation, unreadable metadata, process death and separate processes are all executable. `run-once` gained `--backup-directory`; a service with no backup location, or an absent backup directory, declines. The unit's `ReadWritePaths` gains the backup directory for that one file. |
| D | The installer compared `--unit-directory` as a string, so `/etc/systemd/system/`, `/etc/systemd/./system` and a symlink to the real directory were "developer" directories: no root requirement, no ownership, and the `--fail-after-first-publish` seam armed. `--user`/`--group` were unvalidated and a backslash-`n` in any path rendered as a real newline and a second directive (reproduced: `ExecStartPre=/bin/evil` in the rendered unit). | High | Canonical comparison via `realpath -m`; symlink refusal on every component as supplied; account names validated; `|`, `&`, backslash and whitespace refused in paths; rollback restores the previous mode. |

Lesser corrections: the oversize cleanup now refuses to unlink anything that
is not a regular file and surfaces a cleanup failure on the refusal; the two
"mean luminance" docstrings say median; `GET /retention/status` reports
`scheduled_lock_state`; the lock refusal names the holder.

**§2 decision 12 is withdrawn.** Reading a lock's age is not exclusion.
**§2 decision 14 stands, for a different reason**: no migration, because the
reservation must be durable *independently of the catalogue* — it must be
writable while the database is failing and must survive a database restore
— which a table in that database cannot be. Migration 004 remains free.

**Residual limitation, decided not to block merge.** Retention cannot yield
to a deployment or a §6.2 recovery: the deployment lock is a root-only
`flock` with no observable state, and coupling the privileged gateway to the
retention lock was judged out of scope for this change. The contract is an
operator gate (`docs/Operations.md` §4.7, §5.7, Stage D), enforceable today
because the timer is not installed or enabled in production; the task that
enables the timer must carry that gate, and a later gateway change may
automate it.

**Mutation register.** The two installer gaps reported as unmutatable are
closed: the placeholder check and the structural validation are mutated and
detected by tests that copy the installer beside a deliberately malformed
template (`tests/test_retention_timer_hostile_input.py`). Twelve entries
were added and four re-anchored; the CRLF check is provable only on Linux
because the MSYS `grep` normalises carriage returns, and is a Pi gate.
