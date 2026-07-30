# Task 12 — Physical camera acceptance and managed preview lifecycle

## Status

**Implementation complete and locally validated; physical camera acceptance not
performed.**

| Gate | Outcome |
| ---- | ------- |
| Task definition | Complete (this record, first commit) |
| Managed preview lifecycle implementation | Complete |
| Camera coordinator | Complete |
| Local static and automated validation | Passed |
| Mutation / negative verification | Passed — all 18 defects detected |
| Local runtime validation (simulator) | Passed |
| Auto-start failure runtime validation | Passed |
| Repository review | Not started |
| Raspberry Pi validation of this branch | **Not performed** — the Pi was not accessed |
| Physical camera acceptance run | **Not performed** — requires separate authorisation |
| Matthew's visual sign-off | **Not given** |

Nothing in this task claims that the physical camera has been accepted. Task 12
builds the *gate*; passing the gate is a separate, authorised hardware activity.

The software rows above were written as `In progress` / `Pending` in the first
commit and updated at closeout. The three physical rows have never changed and
must not be changed by anything other than an authorised hardware run.

## Purpose

Task 12 has two halves that serve one goal — making the camera phase provable
rather than assumed.

**Part A — managed preview lifecycle.** Give the deployment an explicit, opt-in
way to keep the camera pipeline live: preview may start automatically when the
application starts, and may be restored automatically after a still capture has
taken the camera. Both policies default to *off*, so every configuration written
before this task behaves exactly as it did in Task 11.

**Part B — the acceptance procedure and record.** Turn the project plan's
one-line "acceptance record" intent into an exact, repeatable, evidence-based
checklist, plus a structured record that starts out honest (everything pending)
and is completed only by an authorised hardware run.

The two halves are related: several acceptance gates (application restart,
reboot recovery, the 24-hour minimum and the 48-hour soak) are only meaningful
if the camera pipeline can survive a restart without a human pressing a button.

## Original project-plan intent

The project plan defined Task 12 as:

```text
Acceptance record — document the exact camera bring-up checklist.
```

The wider camera acceptance requirements the plan attaches to the camera phase
are: correct physical installation; camera detection; reliable still capture; a
useful field of view; feeder coverage; bird-sized subject scale; autofocus
reliability at the feeder plane; acceptable exposure; manageable window
reflections; privacy-conscious framing; mechanical stability; preview and
snapshots surviving restart and reboot; a 24-hour minimum camera acceptance run;
at least a 48-hour soak before declaring the camera pipeline stable; and
Matthew's practical garden acceptance.

Task 12 reconciles that intent with what the repository actually is today. It
does not treat automated tests as a substitute for Matthew's visual assessment:
software checks and human checks are recorded separately and neither implies the
other.

## Current repository reality

Merged `main` (`fc66e519`) already provides, and Task 12 does **not** rebuild:

- physical-camera readiness through `rpicam-hello --list-cameras`
  (`mgo.core.camera_detection`);
- real Camera Module 3 detection and a background readiness monitor;
- full-resolution still capture through `rpicam-still`
  (`mgo.camera.backend.RPiCamBackend`, `mgo.camera.capture.CaptureService`);
- live MJPEG preview through `rpicam-vid`
  (`mgo.camera.preview_backend.RPiCamPreviewBackend`);
- one supervised preview process with first-frame startup validation, bounded
  shutdown and truthful state reconciliation (`mgo.camera.preview`);
- browser streaming with one producer and many viewers (`mgo.camera.streaming`);
- still-capture archiving (`mgo.captures`);
- a deterministic, hardware-free simulator backend (`mgo.camera.simulator`,
  Task 11);
- real frame-difference motion analysis sharing the preview stream
  (`mgo.motion`);
- service restart policy, database health and backup (Task 10);
- dashboard and API status contracts;
- the production `mgo` service identity.

### Physical-camera facts already known

Recorded after the Task 11 deployment, on the production host:

```text
host: mgo-core
architecture: aarch64
camera backend: rpicam
sensor: Sony IMX708 / Camera Module 3 Standard
still resolution requested by MGO: 4608 × 2592
preview backend: rpicam-vid
preview resolution: 1280 × 720
preview frame rate: 15 fps
```

These are software-side facts (what MGO detects and requests). They say nothing
about focus, exposure, framing, feeder coverage or reflections — which is
precisely what the acceptance procedure exists to establish.

## Lifecycle gaps

Two lifecycle contracts in merged `main` prevent the camera phase from
satisfying an always-on acceptance gate.

### Gap 1 — preview does not start after an application restart

The lifespan constructs `PreviewService` in `STOPPED`. Preview only starts when
an operator calls `POST /camera/preview/start`. A service restart or a Pi reboot
therefore leaves the camera pipeline with no frames — and motion detection
truthfully stuck in `waiting_for_frames` — until a person intervenes. A 24-hour
or 48-hour unattended run cannot begin from that state.

### Gap 2 — a still capture leaves preview stopped

`POST /camera/capture` releases an active preview before capturing
(`PreviewService.release_for_capture`) and deliberately leaves it stopped. That
guarantees exclusive camera ownership — which is correct and is preserved — but
it means the continuous camera stream ends after every still capture until an
operator restarts it.

Task 12 closes both gaps with an explicit, opt-in managed-preview policy. It does
not change the default behaviour of any existing deployment.

## Selected architecture

A single **camera coordinator** (`src/mgo/camera/coordinator.py`) owns the
ordering of every camera-mutating operation, and two new booleans in the
`[preview]` configuration section select the managed policies.

The coordinator was chosen over the alternatives for these reasons:

- *Policy inside `PreviewService`* was rejected: the service supervises one
  process and knows nothing about capture. Teaching it to restore itself after a
  capture would make preview depend on capture and would put the restoration
  decision inside the object whose truthfulness it must not distort.
- *Policy inside the API layer* was rejected: the route functions would then
  carry the serialisation guarantee, and the guarantee would be absent from every
  non-HTTP caller (lifespan auto-start, application shutdown). Races between a
  lifespan auto-start and an early request would be unguarded.
- *A coordinator* keeps one place that answers "who may touch the camera now" for
  all four mutating operations — preview start, preview stop, still capture and
  application shutdown — and leaves `PreviewService` and `CaptureService`
  unchanged in behaviour and in their existing contracts.

The coordinator depends only on `CaptureService` and `PreviewService`. It knows
nothing about FastAPI, HTTP status codes, the database, the capture archive,
systemd, concrete physical backends or simulator internals.

## New configuration contract

`PreviewConfig` gains exactly two booleans:

```python
auto_start: bool
restore_after_capture: bool
```

```toml
[preview]
enabled = true
auto_start = false
restore_after_capture = false
width = 1280
height = 720
fps = 15
startup_timeout_seconds = 5.0
shutdown_timeout_seconds = 5.0
```

### `auto_start`

> Attempt to start preview once during application lifespan startup.

When `false` (the default) behaviour is unchanged: the application starts with
preview stopped and no camera preview process is launched automatically.

When `true`, preview is started during application startup through the real
`PreviewService.start()` path, so the same first-frame startup validation
applies and no duplicate camera process can be launched. Motion monitoring starts
only after the auto-start attempt has resolved. There is exactly **one** attempt:
no retry loop.

### `restore_after_capture`

> If preview was running when a capture transaction began, attempt to restore it
> after the capture attempt finishes.

When `false` (the default) behaviour is unchanged: capture releases preview and
leaves it stopped.

When `true`, preview is restored **only** when it was running at transaction
entry. A previously stopped preview stays stopped; a previously failed preview
stays failed unless explicitly started. Restoration is attempted after a
successful capture *and* after a failed capture, and uses the real
`PreviewService.start()` path, so first-frame startup validation still applies.

### Defaults are load-bearing

Both values default to `false`, in the dataclass and in the configuration parser.
Every configuration written before Task 12 loads with behaviour identical to
Task 11. Neither tracked configuration file (`config/mgo.toml`,
`config/mgo.production.example.toml`) enables either policy; enabling managed
mode is an explicit, deliberate edit to the *external* production configuration
at `/etc/garden-observatory/mgo.toml`.

### Cross-configuration validation

Rejected combinations, because they request a managed policy for a subsystem that
can never run:

- `preview.enabled = false` with `preview.auto_start = true`;
- `preview.enabled = false` with `preview.restore_after_capture = true`;
- `camera.enabled = false` with either managed policy true.

Allowed, because the policies are independent:

- `auto_start = false`, `restore_after_capture = true` — a manually started
  preview that should survive captures;
- `auto_start = true`, `restore_after_capture = false` — preview starts with the
  process but capture keeps the Task 11 "capture stops preview" behaviour.

Validation errors name the conflicting settings and nothing else: no
configuration path, no unrelated value.

## Camera-operation coordination

`CameraCoordinator` exposes four operations:

```python
start_preview()  -> PreviewStatus
stop_preview()   -> PreviewStatus
capture_image()  -> CaptureResult
shutdown()       -> None
```

One bounded in-process mutex serialises all four. It prevents:

- a preview start beginning while a still capture owns the camera;
- a preview stop interleaving with a restoration;
- two captures owning the camera simultaneously;
- application shutdown interleaving with a capture restoration;
- a manual start launching a second process during a capture.

Status reads (`GET /camera/preview/status`, `GET /health`) and frame-stream reads
(`GET /camera/preview/stream`) deliberately do **not** pass through the mutation
lock: an operator must be able to see what the camera is doing while a capture is
in progress. Database archive persistence stays outside the lock, in the API
layer, after the coordinated hardware operation returns.

### Capture transaction

1. acquire the coordinator operation lock;
2. reconcile and record whether preview is actually `RUNNING`;
3. release an active preview through the existing `release_for_capture()`;
4. execute the real `CaptureService.capture_image()`;
5. remember the original capture result or exception;
6. if configured *and* preview was previously running, attempt restoration;
7. release the lock;
8. return the original capture result, or raise the original capture exception.

### Failure semantics

The capture outcome and the preview-restoration outcome are two separate truths
and are never merged into an invented combined flag. No new capture response
field is added.

- **Capture succeeded, restoration failed** — the successful capture result is
  returned, preview is left truthfully in `FAILED` with its `last_error`, the
  restoration failure is logged, nothing is retried and the captured JPEG is not
  deleted. This matters: a caller told "capture failed" would retry, and the
  archive would gain duplicate evidence for a capture that actually succeeded.
- **Capture failed, restoration also failed** — the original capture exception is
  raised, preview is left truthfully in `FAILED`, the restoration failure is
  logged, and a preview failure never replaces a capture failure.
- **Preview was not running at entry** — release is a no-op, no restoration is
  attempted, and capture never starts preview merely because
  `restore_after_capture` is true.

## Lifespan auto-start and ordering

Application startup ordering:

1. load validated configuration;
2. apply database migrations;
3. establish database health;
4. attach the notification manager;
5. attach health and database monitors;
6. attach camera readiness state;
7. attach capture and preview services;
8. attach the streaming broker;
9. attach the coordinator;
10. perform the initial camera readiness check;
11. if `preview.auto_start` is true, attempt a preview start through the
    coordinator;
12. initialise and start motion monitoring;
13. begin serving.

The auto-start attempt resolves *before* motion monitoring begins, so motion does
not open in a misleading waiting state when auto-start was explicitly requested.
Camera readiness `available` is deliberately **not** a second gate inside the
coordinator: the physical backend itself remains authoritative about whether it
can start.

### Expected auto-start failure behaviour

Expected physical failures — camera absent, camera busy, camera tool absent,
encoder startup failure, no first frame, permission denial, process exit during
startup — already map to preview-domain exceptions. During lifespan auto-start
such a failure is caught and logged; the API keeps serving, preview is left in
`FAILED` with its truthful `last_error`, and camera readiness and the background
monitor stay truthful. The API is never crashed for an expected camera failure,
`RUNNING` is never reported, preview is not reset to `STOPPED`, nothing retries
in a loop and `last_error` is never suppressed or cleared. Unexpected programming
errors are logged with their traceback rather than being disguised as ordinary
hardware absence. Database failures remain fatal to startup, exactly as before.

## Application shutdown

Shutdown routes through `CameraCoordinator.shutdown()` rather than bypassing it,
so it waits for an active mutation transaction to finish before stopping preview.
It leaves no preview process, is idempotent, and preserves the existing graceful
termination and forced-kill behaviour.

## API compatibility

Mutating operations route through the coordinator; read-only operations do not.

| Endpoint | Route |
| -------- | ----- |
| `POST /camera/preview/start` | coordinator |
| `POST /camera/preview/stop` | coordinator |
| `POST /camera/capture` | coordinator, then archive persistence |
| `GET /camera/preview/status` | preview service (read-only) |
| `GET /camera/preview/stream` | preview service + broker (read-only) |
| `GET /health` | preview service (read-only) |

No endpoint path changes. No response field changes. No status vocabulary
changes. No new endpoint. Every existing capture success field (`success`,
`filename`, `absolute_path`, `timestamp`, `width`, `height`, `filesize_bytes`,
`backend`, `capture_id`) and every existing error mapping (503 unavailable, 504
timeout, 502 backend, 500 write, 500 other camera-domain, 500 archive) is
preserved.

The browser preview page and the dashboard remain inert: opening `/preview` or
`/dashboard` is never an auto-start trigger. Preview starts only from
configuration-requested lifespan auto-start or an explicit
`POST /camera/preview/start`.

Preview restoration truth is visible only through the existing preview status
endpoint. The capture response gains no restoration field.

## Concurrency boundary

At most one mutating camera transaction is active at any instant. During a
capture a second capture waits, a preview start waits, a preview stop waits and
shutdown waits. After a capture and its restoration, a queued explicit stop may
stop the restored preview, a queued explicit start is idempotent, no duplicate
preview process exists, no operation is lost and no deadlock occurs.

## Acceptance procedure

The full procedure is `docs/Camera-Acceptance.md`. It separates four categories
and never lets one imply another:

1. automated software checks (hardware-free, run in CI and on the Pi);
2. operator-observed physical checks (a person at the machine);
3. Matthew's final visual acceptance (field of view, framing, privacy, focus,
   exposure, reflections, usefulness);
4. the time-based gates.

### Objective and human gates

Objective gates can be evidenced by command output, process state, file metadata
and journal history: camera detection, capture resolution, file integrity,
archive consistency, one preview process, restart recovery, service uptime,
`NRestarts`, temperature, memory and disk. Human gates cannot: framing, feeder
coverage, subject scale usefulness, focus quality at the feeder plane, exposure
acceptability, reflection tolerability, mechanical stability under ordinary
disturbance, and privacy. Claude may collect and describe evidence for the human
gates; only Matthew decides them.

### 24-hour and 48-hour distinction

- **24 hours continuous** is the camera bring-up minimum. Passing it may be
  recorded as `CAMERA BRING-UP PASSED`.
- **48 hours continuous** is the stability gate. Only passing it may be recorded
  as `CAMERA PIPELINE STABLE`.

A 24-hour result must never be presented as, or extrapolated to, the 48-hour
result.

## Evidence-record design

The structured record is `docs/acceptance/Initial-Camera-Acceptance.md`. It
starts entirely `PENDING` / `NOT PERFORMED` / `NOT RECORDED`; only facts already
supported by the repository and the completed Task 11 deployment are
pre-populated. No result is pre-filled as a pass.

The committed record may carry capture UUIDs, filenames, UTC and SAST
timestamps, dimensions, file sizes, backend names, SHA-256 digests, camera index,
sensor identity, metadata values, service and PID state, test outcomes and
Matthew's written decisions. It must not carry JPEG bytes, thumbnails,
screenshots, raw metadata with unnecessary paths, neighbouring private imagery,
production database copies, configuration contents, credentials or support
bundles. Captures are referred to by archive ID and filename, not by absolute
filesystem path.

## Privacy boundary

Acceptance requires a full-resolution image and a live preview review confirming
that the feeders and the intended garden area are the subject, that neighbouring
windows and private indoor areas are excluded where practical, that public
pavement and neighbour activity are not unnecessarily framed, and that the camera
points nowhere inconsistent with a bird-observation purpose. The decision is
Matthew's — `PASS`, `FAIL` or `CONDITIONAL PASS` — and cannot be auto-approved.
The acceptance image itself is never committed.

## Test strategy

All Task 12 automated tests are hardware-free. They cover:

- **configuration** — old files load; both defaults are false; explicit values
  load; the three rejected combinations are rejected; the two policies are
  independent; existing preview defaults and the backend vocabulary are
  unchanged; both tracked configuration files keep managed mode off;
- **coordinator** — delegation, idempotence, the full capture transaction matrix
  (running/stopped × success/failure × restore on/off), first-result
  preservation, restoration-failure isolation, and serialisation of every pair of
  mutating operations, driven by events and barriers rather than sleeps;
- **lifespan** — no auto-start by default; a single auto-start when enabled;
  first-frame validation still applied; expected failure is non-fatal and leaves
  `FAILED` with a visible `last_error`; motion starts after the attempt;
  shutdown routes through the coordinator and leaves no producer;
- **API** — unchanged contracts for every camera endpoint, coordinator routing
  for the three mutations, read-only status, and the two restoration-failure
  response-authority cases;
- **simulator integration** — a real application with `auto_start = true` reaches
  a running preview, motion consumes frames, capture restores preview, one
  producer exists, shutdown leaves none, and no physical camera command runs;
- **physical command preservation** — the `rpicam-still`, `libcamera-still`,
  `rpicam-vid` and `libcamera-vid` argument arrays are unchanged. No autofocus,
  exposure, AWB, ROI or lens-position argument is added in this task: the
  acceptance procedure evaluates the *current defaults* first.

Mutation verification independently applies each of the 18 defects listed in the
task brief, confirms that a test or check fails, and reverts each byte-for-byte.

## Raspberry Pi validation boundary

The Raspberry Pi is **not** accessed during Task 12 implementation: no SSH, no
checkout update, no approval-file change, no production configuration edit, no
managed-preview enablement, no service restart, no reboot, no preview stop, no
physical capture, no image inspection, no hardware disconnection, and no
24-hour or 48-hour run.

After repository review, a separate authorised instruction will install the
reviewed SHA, validate the branch on ARM64, update the external production
configuration in a controlled manner, test auto-start and capture restoration,
execute the physical acceptance checklist, begin the 24-hour and 48-hour records,
collect Matthew's decisions, and update the acceptance record with actual
evidence.

## Rollback

**Before merge.** Switch back to `main`. No production behaviour changes; the
physical acceptance record remains pending.

**After a future merge, before production enablement.** Revert the five Task 12
commits and redeploy `main`. No production configuration rollback is needed
because both managed-preview defaults are `false`.

**After production managed mode is enabled.** Set both keys back to `false` in
the external production configuration and restart `mgo.service`.

No automatic production rollback script is written. No database rollback, no
migration rollback and no media deletion are required.

## Deviations

Recorded for the reviewer. None changes production behaviour.

1. **A second new test file exists.** The plan expected one new test file
   (`tests/test_camera_coordinator.py`). `tests/test_camera_acceptance_docs.py`
   was added because two of the required mutations — marking the pending
   acceptance record as passed, and claiming the 48-hour gate from a 24-hour
   result — are defects in a *document*, and no test existed that could detect
   them. It also carries the evidence-handling checks (no image bytes, no
   credentials, no production filesystem paths in the committed record).
2. **`tests/test_camera_capture.py` was modified.** It is not on the expected
   list, but it is where the still-capture command lives, and the physical
   command-preservation requirement needs an *exact* argument-array assertion for
   `rpicam-still` and `libcamera-still`. The existing tests only checked
   membership, so an added autofocus flag would have passed them.
3. **The managed-preview keys landed in the second commit, not the fourth.**
   `config/mgo.toml` and `config/mgo.production.example.toml` are part of the
   configuration *contract*, so they are committed with the contract rather than
   with the prose documentation.
4. **The two new `PreviewConfig` fields carry dataclass defaults of `False`.**
   Every other field in that dataclass is required. Defaults were chosen here
   because "off unless explicitly asked for" is the load-bearing guarantee, and a
   default keeps every existing construction — including eight in the test suite
   — valid and unchanged. Both default sites (the dataclass and the parser's
   `_PREVIEW_DEFAULTS`) are pinned by tests, so neither can drift alone.
5. **Mutations 8 and 9 share one mutation site.** "Restoration failure replaces
   a successful capture" and "…replaces the original capture error" are both
   prevented by the same guard in `_restore_preview_if_requested`. The mutation
   was applied twice, once verified against the successful-capture tests and once
   against the failed-capture tests, and each run was reverted byte-for-byte.
6. **This record's status table was updated in the fifth commit.** The first
   commit defined the task with the software gates marked in progress; leaving
   them that way after the work was finished would have made the record untrue.
   The physical gates were not touched.

## Known limitations

- Task 12 delivers the acceptance *gate*, not the acceptance *result*. Nothing
  here proves focus, exposure, framing, feeder coverage, reflections or
  mechanical stability.
- Automated tests prove software lifecycle behaviour only. A passing suite says
  nothing about what the camera can actually see.
- Auto-start makes exactly one attempt. A camera that becomes available later is
  not picked up automatically; an operator starts preview explicitly.
- Restoration reproduces "preview was running" only. It does not remember or
  restore any other camera state, because there is no other camera state to
  restore.
- The coordinator's mutex is in-process. It serialises operations within one
  application process, which is the correct scope: the deployment runs a single
  `mgo.service` instance and camera ownership is already exclusive at the device
  level.
- Preview restoration failure is visible through preview status and the
  application log, not in the capture response. A client that only reads the
  capture response will not learn that preview did not come back.
- No numerical subject-scale threshold is asserted. Measurements are recorded so
  a later model-selection task can set thresholds from evidence rather than
  guesswork.

## Explicit non-goals

Task 12 does not implement: an event lifecycle; motion-triggered capture; regions
of interest in application code; pre-event or post-event buffers; media
retention; bird detection; species identification; bounding boxes; frame-quality
ranking; sharpest-frame selection; thumbnails; video clips; new database tables;
a database migration; notification transports; Telegram; email; audio; BirdNET;
second-camera support; PTZ control; automatic Matt's Viewings publication; camera
settings through the web UI; autofocus, exposure or manual lens-position
configuration fields; image-processing libraries (OpenCV, TensorFlow, PyTorch,
YOLO); changes to physical camera resolution or frame rate; changes to the
current motion threshold; or automatic 24-hour/48-hour background jobs.

It adds no dependency, changes no migration, changes no database schema, changes
no systemd unit and does not modify `uv.lock`.

## Boundary with event capture

Task 12 ends at a camera pipeline that can stay up unattended and an acceptance
gate that says, with evidence, whether the physical installation is good enough
to build on. The event-capture phase — motion-triggered capture, event lifecycle,
pre/post buffers, retention and detection — has **not** started and is not begun
here. It should begin only once the 48-hour stability gate and Matthew's sign-off
are recorded as passed.
