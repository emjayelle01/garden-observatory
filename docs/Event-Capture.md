# Event Capture — motion-triggered still capture

Matt's Garden Observatory (MGO) can optionally take **one full-resolution still
image** when the existing motion subsystem records a material transition into
`motion_detected`, and catalogue it with the motion facts that caused it.

That is the entire feature. It connects two subsystems that already existed and
were deliberately kept apart — the motion monitor, which reports frame-to-frame
scene change, and the camera coordinator, which owns every camera mutation — and
adds nothing else.

It is **disabled by default**.

## Purpose

Motion detection has been able to say "the scene changed" since Task 4, and the
camera has been able to produce a verified, catalogued still since Task 2B. Until
now nothing joined them: an operator had to be watching, and had to ask. Event
capture makes the observatory able to record *evidence of the moment* without a
person in the loop, which is the first thing a garden observatory has to be able
to do before anything cleverer is worth building.

## Scope

**In scope (implemented here):**

- one optional automatic still capture per material `motion_detected` transition;
- a bounded, non-blocking handoff from the motion callback to a background worker;
- one background worker, using the existing camera coordinator and capture
  archive through one shared capture workflow;
- motion attribution stored on the capture's catalogue record;
- one correlated immutable observation per successful automatic capture;
- one sanitised observation per failed attempt;
- a read-only `GET /event-capture/status`.

**Out of scope (not implemented, and not partially implemented):**

- bird detection, species identification, object detection, bounding boxes;
- any AI/ML framework — no TensorFlow, PyTorch, YOLO or OpenCV;
- confidence thresholds or region-of-interest masking;
- pre-event or post-event frame buffers, burst capture, sharpest-frame selection;
- video, visit/session grouping, incident modelling, multi-image events;
- retention or deletion policy, thumbnails, media download or image serving;
- notification image attachments or any new notification transport;
- a manual event-trigger endpoint.

## `motion_detected` is not a bird

This is the single most important thing to understand about this feature.

The motion subsystem compares each analysed frame with the **previous analysed
frame** and reports whether enough pixels changed. It measures **activity**, not
presence, and it does not know what changed. In the production scene — four bird
feeders on a tree in an open garden — wind, moving leaves, shifting shadows,
swaying feeders and changing daylight all legitimately produce `motion_detected`.

So:

- an automatic capture means *the scene changed and the camera took a picture*;
- it does **not** mean a bird was there;
- a bird that lands and holds still stops producing motion, and will not trigger
  a capture, even though it is in frame;
- `no_motion` does not mean nothing is present.

Event capture is simply another consumer of that signal. It adds no
interpretation of its own. See [`docs/Motion-Detection.md`](Motion-Detection.md).

## Architecture

```text
motion monitor  ──material transition──▶  application composition layer
                                                │            │
                                    notification manager   EventCaptureService.submit()
                                                             │ (bounded, non-blocking)
                                                        [queue: 1 pending]
                                                             │
                                                    one background worker
                                                             │
                                                      CaptureWorkflow
                                                       │            │
                                            CameraCoordinator   CaptureArchive
                                                       │            │
                                              CaptureService    observations
```

| Module                            | Responsibility                                                     |
| --------------------------------- | ------------------------------------------------------------------ |
| `mgo.event_capture.models`        | `MotionTrigger`, the state vocabulary, the runtime-state holder and the fixed safe error messages. |
| `mgo.event_capture.service`       | The bounded queue, the single worker, the observation recording.    |
| `mgo.captures.workflow`           | `CaptureWorkflow` — the one capture-and-catalogue path.             |

Three boundaries are deliberate:

- **`mgo.motion` does not import `mgo.event_capture`.** The motion subsystem
  stays transport- and capture-agnostic; the application lifespan is the only
  place the two are connected. A test audits the motion package's source for
  this.
- **Event capture does not touch a camera backend.** Every camera operation goes
  through `CameraCoordinator`, exactly like the manual endpoint. There is no
  second `rpicam` process, no second `libcamera` process and no second preview.
- **`CaptureWorkflow` knows nothing about HTTP.** It composes the coordinator and
  the archive and nothing else, so the manual route and the worker cannot drift
  apart.

## Trigger semantics

A trigger is submitted **only** for a material transition whose status is
`motion_detected`. Every other status — `disabled`, `waiting_for_frames`,
`establishing_baseline`, `no_motion`, `error` — is ignored, and is not counted as
a received trigger, because it never was one.

**The motion monitor is the upstream authority on which transitions exist at
all.** Its existing material-transition rule and its existing `cooldown_seconds`
decide when a transition is recorded and therefore when the callback fires. Event
capture adds **no second cooldown**, no rate limit of its own and no
re-interpretation of motion scores. It does not analyse frames, does not
subscribe to the preview broker, and does not see image data of any kind.

A `MotionTrigger` carries exactly four values, copied at submission time:

```text
status   score   threshold   evaluated_at
```

It never holds JPEG bytes, preview frames, detector buffers, filesystem paths,
configuration objects or exceptions, and it is immutable once created.

## The motion callback stays non-blocking

Submission runs inside the motion monitor's own analysis cycle. It therefore:

- performs **no** camera work;
- performs **no** database work;
- waits for **no** capture, and for no preview restart;
- **never** blocks waiting for queue capacity;
- creates **no** task.

It copies four values, attempts one bounded enqueue, updates counters and
returns. If it did anything more, a slow capture would slow down the very signal
the capture depends on.

## Bounded queue, and what gets dropped

There is one in-memory queue with a **pending capacity of exactly one**:

- one trigger may be executing;
- at most one further trigger may wait;
- any trigger arriving while that pending slot is occupied is **dropped**.

A dropped trigger increments `total_triggers_dropped`, logs a concise warning and
returns immediately. It is **not** written to the observation timeline: a windy
tree must not be able to flood the SQLite timeline with rows about work that was
not done. The live counter is the truthful record.

Dropping is correct rather than a degradation. The waiting trigger will capture
the same continuing activity a moment later, and the next material transition is
the next capture opportunity. The alternative — an unbounded queue, or a task per
transition — would let a gusty afternoon build a backlog of captures of moments
that finished minutes ago, each one taking the camera away from preview again.

The capacity is **not configurable**. A knob here would advertise a behaviour the
feature does not have.

## One worker, one camera owner

When the feature is enabled the application owns exactly **one** background
worker task (`mgo-event-capture-worker`). It:

- waits efficiently on the queue and never busy-loops;
- processes one trigger at a time;
- runs the blocking capture-and-archive workflow in a worker thread, so the event
  loop — and therefore motion, the API and every monitor — keeps running;
- survives both expected and unexpected per-trigger failures;
- keeps accepting later valid triggers after a failure;
- terminates deterministically during shutdown.

**There is no retry.** A failed attempt is recorded truthfully and abandoned;
motion is a renewable trigger, and re-capturing a moment that has passed produces
evidence of nothing.

Camera ownership is unchanged in every respect. The coordinator releases an
active preview, the capture owns the camera exclusively, and preview is restored
afterwards under the **existing** `preview.restore_after_capture` contract — the
same transaction a manual `POST /camera/capture` runs.

## The shared capture workflow

`CaptureWorkflow.capture(extra_metadata=...)`:

1. invokes the coordinator exactly once;
2. receives one verified `CaptureResult`;
3. archives it exactly once, with a defensive copy of the supplied metadata;
4. returns the persisted `Capture`.

If the **capture** fails, nothing is archived and the camera domain's own
exception propagates. If the **archive** fails, the successfully captured JPEG is
**not deleted** — the capture is still valid, so it stays on disk for a later
reconciliation and the archive's own error propagates.

The camera-operation lock is deliberately **not** held across the database write.
The coordinator's transaction closes — including any preview restoration — before
the archive is touched, so SQLite can never stall the camera.

## Capture metadata

A successful automatic capture is catalogued with:

```json
{
  "origin": "motion",
  "motion_status": "motion_detected",
  "motion_score": 0.42,
  "motion_threshold": 0.08,
  "motion_evaluated_at": "2026-08-07T09:30:00+00:00"
}
```

`origin` is what distinguishes an automatic capture from a manual one. A manual
capture supplies no metadata at all and never acquires a motion origin.

The catalogue record already carries the capture's path, so the path is
deliberately **not** duplicated here. Image bytes, base64, preview frames,
detector buffers, exception details, credentials, configuration contents and
command lines are never stored.

## Success observations

After — and only after — the archive write succeeds, exactly one immutable
observation is recorded in the existing timeline:

| Field            | Value                                  |
| ---------------- | -------------------------------------- |
| `kind`           | `event_capture`                        |
| `source`         | `mgo-event-capture`                    |
| `status`         | `captured`                             |
| `summary`        | `Motion-triggered still captured`      |
| `correlation_id` | the persisted capture's UUID           |

Payload: `capture_id`, `motion_score`, `motion_threshold`,
`motion_evaluated_at`, `filename`.

The payload never contains `absolute_path`, `capture_directory`, a database
path, a configuration path or raw exception data. The correlation identifier is
only ever the id of a capture that really was persisted.

If the timeline write itself fails after a successful capture, that is logged in
full and the capture is still reported as the success it was: the catalogue row
is the durable record, and a telemetry failure is not a capture failure.

## Failure observations

When an attempt enters the worker and fails, one observation is attempted:

| Field            | Value                                        |
| ---------------- | -------------------------------------------- |
| `kind`           | `event_capture`                              |
| `source`         | `mgo-event-capture`                          |
| `status`         | `failed`                                     |
| `summary`        | `Motion-triggered still capture failed`      |
| `correlation_id` | **absent** — no capture exists to correlate to |

Payload: `motion_score`, `motion_threshold`, `motion_evaluated_at`,
`error_category`. It never contains a raw exception message, a `repr`, a
traceback, a filesystem path, a subprocess command, configuration content or an
environment value.

If **that** write also fails it is logged with its traceback, the worker stays
alive, and the original event-capture failure remains what the runtime state
reports. There is deliberately no recursive second attempt: a database that
cannot take the first row will not take the second, and a database error message
must not replace the capture failure an operator has to diagnose.

## Safe error handling

Every failure maps to one of six categories, each with one fixed public sentence:

| Category             | Public message                                            |
| -------------------- | --------------------------------------------------------- |
| `camera_unavailable` | `Camera unavailable for motion-triggered capture.`        |
| `capture_timeout`    | `Motion-triggered capture timed out.`                     |
| `backend_failure`    | `Motion-triggered camera backend failed.`                 |
| `write_failure`      | `Motion-triggered capture could not be written.`          |
| `archive_failure`    | `Capture completed but metadata could not be archived.`   |
| `unexpected`         | `Motion-triggered capture failed unexpectedly.`           |

The first five come from the existing camera and archive domain exceptions. A
camera-domain error outside those four kinds is reported as `unexpected` rather
than relabelled as one of them: the named categories describe *recognised*
operational conditions, and a guess does not belong in an operator's status.

The raw exception is logged **once**, with its traceback, and appears nowhere
else. It never reaches `/event-capture/status`, an event-capture observation or
any notification payload.

## Error recovery

A per-trigger failure never terminates the worker, the motion monitor, the FastAPI
application, or preview (beyond whatever the preview subsystem itself reports).

After a failure the state is `error` and `last_error` holds the category message.
A later valid trigger is still accepted; when it succeeds the state returns to
`idle` and `last_error` clears.

## `GET /event-capture/status`

Read-only in the strongest sense available: it reads one application-managed
holder and nothing else. A request **does not** touch the camera, start or stop
preview, submit a trigger, invoke the capture workflow, open the database, run a
migration, wait for the worker or alter a counter.

It returns HTTP 200 whenever the application is serving.

```json
{
  "enabled": true,
  "state": "idle",
  "pending_triggers": 0,
  "total_triggers_received": 12,
  "total_captures_succeeded": 11,
  "total_captures_failed": 1,
  "total_triggers_dropped": 3,
  "last_trigger_at": "2026-08-07T09:30:00+00:00",
  "last_capture_id": "0f0a2b1c-...",
  "last_capture_at": "2026-08-07T09:30:01+00:00",
  "last_error": null
}
```

### States

| State       | Meaning                                                              |
| ----------- | -------------------------------------------------------------------- |
| `disabled`  | Off by configuration. No worker exists.                              |
| `idle`      | Enabled, worker alive, no capture executing.                         |
| `capturing` | One motion-triggered still capture is executing now.                 |
| `error`     | The most recent attempt failed. The worker is alive and still accepting triggers. |

### Counter semantics

| Counter                    | Counts                                                                 |
| -------------------------- | ---------------------------------------------------------------------- |
| `total_triggers_received`  | Every valid `motion_detected` submission received while accepting work — **including** one subsequently dropped. |
| `total_triggers_dropped`   | Valid submissions not queued because the pending slot was already full. |
| `total_captures_succeeded` | Automatic captures that were captured **and** archived.                 |
| `total_captures_failed`    | Attempts that entered the worker and failed at the capture or archive stage. |

Ignored non-motion statuses are **not** counted as received triggers. Every
counter is **process-lifetime only** and is never persisted; a restart resets
them. The durable record remains the capture catalogue and the observation
timeline.

No filesystem path is ever exposed by this endpoint.

## Configuration

```toml
[event_capture]
enabled = false
```

That is the whole section. Enabling it **requires** all of:

| Setting                          | Why                                                              |
| -------------------------------- | ---------------------------------------------------------------- |
| `camera.enabled = true`          | An automatic capture is a camera operation.                      |
| `preview.enabled = true`         | Motion analyses preview frames; with preview off no trigger could ever be produced. |
| `preview.auto_start = true`      | Unattended operation must survive a service restart or a reboot. |
| `preview.restore_after_capture = true` | A capture takes the camera from preview; without restoration the first automatic capture would permanently remove the frame source that produces triggers. |
| `motion.enabled = true`          | There is no trigger without the motion monitor.                  |

Any missing dependency is rejected at load time with a message naming exactly the
two conflicting settings — no path, no unrelated value, no file content:

```text
event_capture.enabled = true requires motion.enabled = true
```

The section is **optional**. A configuration file written before Task 13.1 loads
unchanged with `event_capture.enabled == false`, and a disabled feature creates no
worker, no queue and no camera activity.

There is deliberately no configuration for queue length, retries, burst size,
pre-roll, post-roll, event duration, retention, AI confidence, species or
notification transports.

## Startup

1. migrations and database setup;
2. existing services and state (archive, notifications, monitors);
3. camera readiness;
4. managed preview auto-start attempt;
5. **event-capture worker startup**;
6. motion monitor startup;
7. the application serves.

The worker is started **before** the motion monitor, so a trigger can never
arrive before something exists to receive it. Starting the worker captures
nothing — it only parks the worker on an empty queue.

With the feature disabled, none of this happens and startup is exactly as it was.

## Shutdown

Ordering is load-bearing:

1. the motion producer is stopped and drained, so no new transition can be
   submitted;
2. the service stops accepting triggers;
3. any queued trigger that had **not started** is discarded;
4. one already **in-flight** capture is allowed to finish — it owns the camera,
   and abandoning it mid-transaction would leave a partial file and an unrestored
   preview;
5. the worker is awaited to a terminal state;
6. the remaining monitors are drained;
7. **only then** is `CameraCoordinator.shutdown()` called;
8. the existing stop notification and stop observation are performed.

No automatic capture may **begin** after shutdown has started. Every cleanup
stage is still attempted if an earlier one fails, and a cleanup failure never
replaces the original startup or serving exception.

## Off-Pi / Windows behaviour

The whole feature is hardware-safe. With the shipped defaults it is disabled, so
no camera command runs, no worker exists and no Raspberry Pi dependency is
introduced. The automated tests use fakes and the deterministic Task 11
simulator; none of them requires `/dev/video*`, Raspberry Pi hardware,
`rpicam-*`, `libcamera-*`, systemd, sudo or a Linux-only path.

## Database

No new database, no new table and **no migration**. Event capture reuses:

- the existing `captures` table and its `extra_metadata` column;
- the existing `observations` table and its `correlation_id` column.

## Dependencies

None added. Standard library, the existing FastAPI/Pydantic stack and existing
MGO modules only.

## Limitations

- It captures on **scene change**, not on a subject. Wind, leaves and shadows
  produce captures; a still bird does not.
- One transition produces **one** still image. There is no burst, no pre-roll and
  no post-roll, so the captured instant is shortly *after* the change that
  triggered it — the subject may already have moved or left.
- With the camera busy, further triggers are **dropped**, not queued. During
  sustained activity the record is a sample, not a complete sequence.
- There is **no retry**: a failed attempt is lost, not re-attempted.
- Counters are process-lifetime and reset on restart.
- Captured images accumulate on disk. **There is no retention or deletion policy
  in this task**, so an enabled deployment needs disk headroom and manual
  housekeeping until one exists.
- No hardware validation of this feature has been performed on the Raspberry Pi.

## Future work

Explicitly *not* started here, and each its own task: bird detection and species
identification; regions of interest and feeder masking; pre/post-event frame
buffers, burst capture and sharpest-frame selection; visit/session grouping and
incident modelling; media retention and deletion policy; thumbnails and an image
serving or download API; notification image attachments and real transports.
