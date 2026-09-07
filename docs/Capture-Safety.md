# Automatic Capture Safety (Task 14.5)

**Scope.** This document describes the hard limits that bound *automatic*
(motion-origin) still capture, the storage guard shared with manual capture,
the whole-frame change filter in the motion detector, and how all of it is
observed. It is the contract Task 14.4 found missing: the capture foundation
worked on the deployed build, but nothing bounded it.

**What this is not.** It is not species recognition, bird detection,
first-seen detection or a notification transport. None of those exist in the
application, and none is introduced here. It is also not a production
enablement: every setting described below ships **disabled** and the
production configuration is unchanged by this work. Daylight commissioning on
the Raspberry Pi is a separate, later task.

---

## 1. The safety model in one paragraph

A motion trigger may reach the camera only if an **admission gate** admits it.
The gate is asked inside the event-capture worker, *before* preview is
released, *before* `rpicam-still` runs and *before* any file exists. It counts
durable catalogue rows and durable reservation markers, so neither a restart
nor a crash at any point in an attempt can reset it; it probes the filesystem
the next JPEG would land on, so a full SD card stops it; and it fails closed
on every doubt, so a broken probe, an unreadable catalogue or a reservation
that cannot be written refuses rather than admits. A refused trigger creates
no image, no catalogue row and no lifecycle row. An admitted capture that
turns out larger than the reserved size is withdrawn before it is catalogued.
Separately, the motion detector no longer mistakes an exposure step for a
subject.

---

## 2. Configuration

`[event_capture]` when `enabled = true` **requires** three limits and accepts a
fourth with a default. An enabled section missing any mandatory limit is
refused at load time, before anything runs. An absent section, or
`enabled = false`, loads exactly as before with every limit `None`.

| Key | Type | Bounds | Meaning |
| --- | --- | --- | --- |
| `max_captures_per_hour` | integer | 1 … 3600 | Automatic captures admitted in any rolling 3600-second window. Mandatory. |
| `max_captures_per_day` | integer | 1 … 86400 | Automatic captures admitted in the current UTC calendar day. Mandatory. |
| `minimum_free_bytes` | integer | ≥ 1 | Free space that must remain on the media filesystem *after* one more capture. Mandatory. |
| `maximum_capture_bytes` | integer | ≥ 1 | Size reserved for one capture before it is admitted, and the largest file an automatic capture may publish. Default 16 MiB (16,777,216). |

`minimum_free_bytes + maximum_capture_bytes` must not exceed 2⁶³ − 1. Supplied
values are validated whether or not the feature is enabled, so a typo is
caught when it is written. Error messages name only the setting at fault.

The tracked examples (`config/mgo.toml`, `config/mgo.production.example.toml`)
carry illustrative values with the feature disabled: 20 per hour, 120 per
day, a 2 GiB floor and the 16 MiB reservation. They are not an accepted
operating policy.

`[motion]` gains `global_change_ratio_threshold` (float, default 0.5), which
must be strictly greater than `changed_pixel_ratio_threshold` and at most 1.
See §6.

### Migrating an existing configuration

A configuration that predates Task 14.5 keeps loading unchanged. To enable
automatic capture later, an operator adds the three mandatory keys to
`[event_capture]` alongside `enabled = true`; nothing is inserted into a live
file by the application, ever.

---

## 3. Admission order

The gate evaluates, in this order, and stops at the first refusal:

1. **feature enabled** — the worker only exists when `[event_capture]` is
   enabled; disabled means no gate, no queue and no camera activity;
2. **worker not busy** — the one-slot queue and single worker are unchanged; a
   trigger that arrives while one capture runs and one waits is dropped and
   counted as it always was;
3. **cooldown** — the newest automatic capture must be older than
   `motion.cooldown_seconds`. The motion observer already suppresses repeated
   `motion_detected` inside the cooldown; the gate checks it again because
   the gate must be safe on its own, not because the observer might fail;
4. **rolling-hour quota**;
5. **UTC-day quota**;
6. **storage reserve** — `free − maximum_capture_bytes ≥ minimum_free_bytes`
   on the filesystem containing `camera.capture_directory`;
7. **camera prerequisite** — the camera readiness holder reports available;
8. **reservation** — one durable reservation marker is written beside the
   database, under the gate's lock, before the camera is touched (see
   *Durable reservations* below). A marker that cannot be written refuses.

Steps 3 to 8 run under one lock, so two admission attempts cannot both see the
same counts and both pass. The single event-capture worker never contends for
it; the guarantee does not depend on that.

---

## 4. Quota semantics

**What counts.** A row in `captures` whose `extra_metadata.origin` is exactly
`"motion"` — the same rule retention uses to decide what it manages. Two
conservative additions: a row whose metadata is not a JSON object (malformed,
or valid JSON that is not an object) counts, because the archive only ever
writes objects and a row this code cannot vouch for must not create room; and
every unreleased reservation marker counts as the attempt it represents.

**Windows.** The rolling hour is `captured_at_utc ≥ now − 3600 s`; the day is
`captured_at_utc ≥ 00:00:00 UTC today`. Both cutoffs are UTC. The stored
timestamp is re-parsed and compared as a datetime, so the decision never
depends on how two strings collate. A stored timestamp that cannot be parsed,
or is naive, counts.

**Failed captures count.** An admitted attempt keeps its reservation marker
unless it ended as a committed catalogue row, so a camera failure, an oversize
refusal, an archive failure that keeps the JPEG, a crash or a restart all
count against the hourly and daily quotas for the full window — exactly as a
row would. A camera that fails on every attempt therefore cannot be hammered,
and a JPEG the archive could not catalogue is still counted. The bytes of any
orphaned file are counted by the free-space probe for as long as the file
exists. Cooldown is spaced by the newest row *or* marker.

**Restart.** Nothing about the quota lives only in memory. A new process
reconstructs the counts from rows and markers on its first evaluation, which
the application performs once at startup so the status endpoint reports real
numbers rather than zeros.

### Durable reservations (Task 14.5A)

The independent review of Task 14.5 found that an in-memory reservation is
not a reservation across a crash: a process that died after `rpicam-still`
wrote the JPEG and before the catalogue row was committed left a file on disk
that nothing counted, and the next process admitted again. The reservation is
therefore a **file**: `<database directory>/.mgo-capture-reservations/
<UTC stamp>-<token>.reservation`, created with `O_CREAT|O_EXCL` and flushed
before the camera is touched, and *removed only* when the workflow has
returned — that is, only when the archive has committed the row that
supersedes it. Every other ending keeps the marker.

The consequences are deliberate:

* the quota window counts rows **plus** unreleased markers, both durable, so
  a restarted process computes exactly what the crashed one would have;
* a marker and its row may both exist for the instant between commit and
  release — or for good, if the process dies in that instant. That counts
  twice, which is the conservative direction; markers older than every
  window (48 hours) are swept on the next evaluation;
* a marker whose name cannot be parsed counts by its modification time, and
  one whose age cannot be read at all counts unconditionally and is never
  swept blind; a directory that cannot be listed refuses admission;
* the markers live beside the database rather than inside it, so a database
  restored from a backup does not silently forget the attempts made since
  that backup, and a reservation can be written while the catalogue is
  failing — which is one of the boundaries it exists to cover.

The crash-boundary table (before reservation; after reservation; after the
still exists; after size validation; after publication; before the row;
after commit but before release; termination; restart; archive failure with
the JPEG kept) is executable: `tests/test_capture_admission_durability.py`.
No migration was needed, and none was taken: the reservation must be
independent of the catalogue's own durability, which a table cannot be.

**Clock reversal.** A row stamped later than "now" still satisfies
`≥ cutoff` and counts; it also lies inside any positive cooldown. A backwards
clock therefore over-counts and waits — it never under-counts.

**Database error.** Admission is refused with `admission_error`. Nothing is
captured while the catalogue cannot be read.

---

## 5. Storage reserve and the publication ceiling

The probe is `shutil.disk_usage` on `camera.capture_directory` itself — or,
when that directory does not exist yet (the capture service creates it on
first use), on its nearest existing ancestor, which is the filesystem a new
directory would inherit. It is deliberately not the root filesystem: the media
may live elsewhere, and the free space that matters is where the JPEG lands.

The gate admits only when `free − maximum_capture_bytes ≥ minimum_free_bytes`.
A probe error, a path that is not a directory, a chain with no existing
ancestor, or a probe that returns nonsense all refuse with `storage_reserve`
and report `storage_reserve_ok = false` with no number.

**The ceiling.** An admitted capture may still produce a still larger than
the reservation. The capture workflow therefore runs a *publication guard*
between the camera transaction and the archive write: if the file exceeds
`maximum_capture_bytes` it is refused, the file this attempt just created is
removed, nothing is catalogued, and the attempt is reported as
`oversize_capture`. This is the single exception to "a successful JPEG is
never deleted", and it is narrow on purpose: the file is milliseconds old,
has no catalogue row, and was never admitted at that size. Pre-existing media
is never touched: the path is the one the capture service built from the
configured capture directory and a fixed-format timestamp, and the removal
(Task 14.5A) first checks that the object there is still a plain regular file
— never a symlink, never a directory — and otherwise leaves it alone. A
removal that fails is logged at error level and surfaced on the refusal; the
orphan stays counted by the durable reservation for the window and by the
free-space probe for as long as it exists, and it is visible on disk for
reconciliation. Together the reserve and the ceiling mean an admitted
capture can never take the filesystem below the configured floor.

**Manual capture.** `POST /camera/capture` applies no quota, no cooldown and
no reservation. It shares one fail-safe: when a `minimum_free_bytes` floor is
configured and the media filesystem is already below it — or its free space
cannot be established — the route answers HTTP 507 and captures nothing. With
no floor configured (the section absent, or the limits omitted from a
disabled section) the route behaves exactly as it always has.

---

## 6. Whole-frame change filtering

Task 14.4 recorded two `motion_detected` transitions with changed-pixel ratios
of about 0.99 and 0.71 that settled to exactly 0.0 on the next frame: an
exposure or lighting step, not a subject. Two changes address that.

**Compensation.** The detector now subtracts the *median* per-pixel luminance
difference between the reference and the current frame before applying the
noise floor. A uniform brightening moves every pixel by about the same
amount, so the median equals the step and the compensated change is nothing.
A localised subject moves only some pixels, so the median stays at zero and
the subject survives. The median, not the mean, is load-bearing: a bright bird
over a sixth of the frame shifts the *mean* by more than the noise floor and
would make every unchanged background pixel read as changed. The result of
the comparison is reported as `score` (compensated), `raw_score`
(uncompensated) and `luminance_shift` (the median that was subtracted).

**The ceiling.** If the larger of the raw and compensated ratios exceeds
`global_change_ratio_threshold`, the frame is reported as **`global_change`**:
not motion, `detected = false`, never enqueued for capture, counted on the
event-capture status as a suppression with reason `global_scene_change`, and
the rolling reference is advanced to the new frame so the next stable frame
settles to `no_motion`. The raw ratio is tested as well as the compensated
one so a uniform exposure step, which compensates to almost nothing, is still
surfaced as the whole-frame event it is rather than silently absorbed.

**Ordering.** `0 < changed_pixel_ratio_threshold < global_change_ratio_threshold ≤ 1`
is enforced at load time; the three bands (no motion, motion, global change)
are contiguous.

**Defaults.** 0.5 — "a majority of the frame" — derived from the Task 14.4
measurements. It is **not** field-calibrated. A subject filling more than half
the analysis frame (a person at the lens) is a global change by this rule,
which for a feeder camera is the intended reading. Daylight commissioning may
tune it; that is a later task.

**The trade the ceiling makes (Task 14.5A).** The ceiling is a maximum-ratio
rule and nothing more. It trades false negatives for false positives: a
subject that changes more than the ceiling's share of the analysis frame — a
close bird filling most of the image — is reported as `global_change` and is
**not** captured. Above roughly half the frame the median shift *is* the
subject's shift, so compensation makes the background read as changed and
the raw ratio is already over half; the rule cannot tell that from a lighting
step, and it does not try. The edge is executable, not asserted:
`tests/test_motion_subject_sizes.py` shows subjects of 10 %, 25 %, 40 % and
49 % of the frame as motion and 51 % and 70 % as global change at the default,
and the 70 % subject recovered at a ceiling of 0.8. Raising the ceiling is
the operator's trade, made in daylight commissioning.

**Cost.** One extra pass over the 160×90 analysis frame and a 511-bin
histogram; comfortably inside the one-second analysis cycle on the Pi.

---

## 7. Suppression reason codes

Fixed vocabulary; nothing derived from a path, an exception or a
configuration value can appear in one.

| Reason | Meaning |
| --- | --- |
| `cooldown` | The newest automatic capture is younger than `motion.cooldown_seconds`. |
| `hourly_limit` | The rolling-hour count (including any reservation) has reached `max_captures_per_hour`. |
| `daily_limit` | The UTC-day count has reached `max_captures_per_day`. |
| `storage_reserve` | The reserve test failed, or free space could not be established. |
| `capture_busy` | Reserved for a future admission path; the current worker reports a busy pipeline through `total_triggers_dropped`. |
| `global_scene_change` | A whole-frame change was reported by the detector; not a trigger. |
| `camera_unavailable` | The camera readiness holder reported the camera unavailable. |
| `admission_error` | The gate could not evaluate (catalogue unreadable, unexpected fault). |
| `oversize_capture` | An admitted still exceeded `maximum_capture_bytes` and was withdrawn before cataloguing. |

**Persistence.** Every suppression moves the counters on
`GET /event-capture/status`. An observation (`kind = event_capture`,
`status = suppressed`) is written only when the *reason changes* from the
last one recorded **and** at least 60 seconds have passed since the last
suppression row (Task 14.5A). An identical repeat writes nothing; reasons
that alternate on every trigger — `hourly_limit`, `storage_reserve`,
`hourly_limit` — write at most one row a minute, so a blockade can never make
telemetry the dominant writer whatever the reasons do. A windy afternoon under
quota therefore produces one timeline row, not hundreds. An admitted capture
resets the reason memory (its own `captured` row is the recovery signal) but
not the interval. `global_scene_change` writes nothing: the motion observer
already persisted the transition. The interval is measured on the monotonic
clock, so a wall-clock step cannot open or close it.

---

## 8. Observability

`GET /event-capture/status` gains, additively and with defaults:

`admission_state` (`disabled`, `unknown`, `open`, `suppressed`),
`total_triggers_suppressed`, `last_suppression_reason`, `last_suppressed_at`,
`hourly_count`, `hourly_limit`, `hourly_remaining`, `daily_count`,
`daily_limit`, `daily_remaining`, `storage_reserve_ok`, `storage_free_bytes`,
`minimum_free_bytes`, `maximum_capture_bytes`, `last_admitted_at`,
`worker_busy`, `total_global_scene_changes`.

Every value is the outcome of the *last admission evaluation*. The endpoint
never probes the filesystem or the catalogue itself, so it stays as cheap and
as side-effect free as it was; the application refreshes the facts once at
startup and after every admitted capture. `storage_free_bytes` is a number; no
path, directory or configuration location appears anywhere in the response.

`GET /motion/status` gains `raw_score`, `luminance_shift` and
`global_change_threshold`, and `status` may now be `global_change`. The
dashboard renders that status as a warning.

---

## 9. What this does not bound

* Manual captures, beyond the fail-safe floor.
* The camera's own behaviour: an unavailable camera still fails a capture in
  the usual way; the gate merely declines to start one it can see will fail.
* Disk consumed by anything other than automatic captures — logs, the
  database, backups. The floor is measured, not budgeted.
* Long-term unattended behaviour, which remains unproven until daylight
  commissioning.

---

## 10. Later Pi validation

Before automatic capture is enabled unattended, a daylight commissioning task
must, at minimum: enable the feature with reviewed limits through the same
root-controlled configuration checkpoint Task 14.4 used; observe at least one
admitted capture and at least one refusal of each of `hourly_limit` and
`storage_reserve` (the latter with an artificially high floor); confirm
`global_change` on a deliberate exposure step and `motion_detected` on a
deliberate localised subject; confirm the status counts survive a gateway
restart; and restore the disabled configuration byte-for-byte afterwards.
