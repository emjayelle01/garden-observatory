# Task 12 — Physical camera acceptance and managed preview lifecycle

## Status

**Implementation and narrow Raspberry Pi validation complete.
Physical camera acceptance has not been performed.
Ready for final merge review.**

| Gate | Outcome |
| ---- | ------- |
| Task definition | Complete (this record, first commit) |
| Managed preview lifecycle implementation | Complete |
| Camera coordinator | Complete |
| Local static and automated validation | Passed |
| Mutation / negative verification | Passed — all 18 defects detected |
| Local runtime validation (simulator) | Passed |
| Auto-start failure runtime validation | Passed |
| Repository review | **Round 1 complete** — two blocking defects found and corrected |
| Repository re-review | **Round 2 complete** — one blocking classification defect found and corrected |
| Final repository review | **Round 3 complete** — two edge cases found and corrected |
| Final shutdown review | **Round 4 complete** — one monitor-drain defect found and corrected |
| Raspberry Pi validation of this branch | **Passed** — narrow ARM64 validation, 2026-07-31 |
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

The first of those steps has since happened. A separately authorised **narrow**
Raspberry Pi validation ran on 2026-07-31 against the reviewed SHA; it covered
the ARM64 build, the full suite and the managed preview lifecycle on the
simulator backend, and it deliberately did *not* touch production configuration,
the physical camera or the acceptance checklist. Its results are recorded under
[Raspberry Pi narrow validation](#raspberry-pi-narrow-validation--performed).
Every remaining step in the paragraph above is still outstanding.

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

## Repository-review correction — unexpected failure cleanup

Review round 1 found two blocking defects. Both were reproduced against the
unmodified branch before anything was changed. Neither affects the expected
failure paths, and neither changes an endpoint, a response field, a preview
state, a physical command or a production default.

### Defect 1 — an unexpected restoration fault left preview lying about itself

`CameraCoordinator._restore_preview_if_requested` caught an unexpected exception
and logged it, which correctly protected the capture's outcome. But
`PreviewService.start()` had no unexpected-exception handling of its own: if a
fault occurred *after* the process was launched and before startup validation
finished, the service was left in `STARTING`, still reporting
`owner: preview`, with `started_at: null`, `last_error: null` — and a live camera
process nothing would ever release.

Reproduced by injecting a `RuntimeError` at the production
`PreviewService._validate_startup` boundary during restoration, with the real
`PreviewService`, the real `CameraCoordinator` and the Task 11 simulator backend:

```text
capture result returned (success=True)   <- correct
preview state       : starting           <- wrong
preview owner       : preview            <- wrong
preview last_error  : None               <- wrong
simulator producers : 1                  <- leaked
```

The same result appeared with a failed capture: the original
`BackendCaptureError` still propagated correctly, while preview truth was wrong
in exactly the same way.

**Root cause.** The transaction protected the *capture's* truth and forgot the
*preview's*. `start()` settled a truthful state for every expected failure and
for success, but an unexpected exception simply escaped the middle of the
transaction, leaving the intermediate `STARTING` state and the launched process
as the observable result.

### Defect 2 — an unexpected auto-start fault escaped before cleanup existed

Lifespan preview auto-start ran *before* the `try/finally` that stops the
monitors and the camera. An unexpected exception therefore left the application
with no cleanup at all.

Reproduced with the real lifespan and the simulator backend:

```text
reached yield              : False
exception escaped lifespan : RuntimeError: ...
live monitor tasks         : ['mgo-camera-monitor(done=False)',
                              'mgo-database-monitor(done=False)']
simulator producers        : 1
preview state              : starting
```

**Root cause.** The cleanup scope was drawn around `yield` — around *serving* —
rather than around everything the lifespan had already created. Auto-start was
the first startup step placed after resource creation and before that scope, so
it was the first step whose failure could orphan them.

### Correction

**`PreviewService.start()` now resolves every invocation.** The whole startup
transaction (`_begin_start`, `_validate_startup`, `_finish_start`) is wrapped.
`PreviewError` is re-raised untouched, so the expected failure contract is
unchanged. An unexpected exception goes through `_settle_unexpected_start()`
first: the process this start launched is fully terminated, reaped and closed —
which also releases the startup-readiness reader thread by closing its pipe —
`_process` and `_started_at` are cleared, the state settles `FAILED`, and a
stable diagnostic is recorded. Only then is the exception logged, and only then
re-raised **unchanged**.

Ownership is proven before the state is rewritten: if a concurrent stop or
capture already superseded this start, the losing start still reaps its own
process (the camera must never stay held) but does not overwrite the later,
valid state.

**The diagnostic is safe by construction.** `last_error` becomes the constant
`Preview startup failed unexpectedly.` — never `str(exc)`, `repr(exc)`, the
exception's arguments or a traceback. An exception message is arbitrary
application data and may carry a path, a username, an environment value, a
secret, an address or control characters; none of that belongs in an API
response. The full exception and traceback go to `LOGGER.exception`, which is
where diagnosis belongs. This is the same boundary Task 11 established for
simulator producer failures.

The exception is deliberately **not** converted into a `PreviewError`: lifespan
auto-start distinguishes a programming defect from an expected hardware failure
by exception type, and collapsing the two would make a bug look like an absent
camera.

**The lifespan cleanup scope now opens before auto-start**, before motion-monitor
creation and before `yield`. `motion_task` is declared ahead of it so the
`finally` can always reason about it. An unexpected exception during either step
now gets the same shutdown a normal run gets — every monitor stop event is
signalled, every monitor task is awaited, `CameraCoordinator.shutdown()` runs
(so it also waits for any in-flight capture transaction) — and the original
exception is then re-raised.

### New tests

- **`tests/test_preview.py`** — unexpected startup faults settle `FAILED` and
  reap the process; the exception is re-raised unchanged (identity-checked, and
  asserted *not* to be a `PreviewError`); a hostile exception message reaches
  neither `last_error` nor any status field; the original exception and its
  traceback reach the log; a superseded start does not overwrite the winner's
  state but still releases the camera; the startup-readiness reader thread is
  released (the fault is injected *after* the real readiness wait, with a
  process whose pipe never produces a frame, so a reader is genuinely blocked at
  the moment of the fault); expected failures keep their own error.
- **`tests/test_camera_coordinator.py`** — the previous unexpected-restoration
  test overrode the whole `start()` method and asserted only capture preservation
  and logging, so it exercised none of the production startup transaction and
  proved nothing about preview truth. It is replaced by three tests that inject
  at `_validate_startup` and assert the full invariant — state, owner,
  `started_at`, `uptime_seconds`, the exact diagnostic, zero producer and reader
  threads, the original exception in the log — for a successful capture, for a
  failed capture, and for the rule that the restoration exception never reaches
  the caller.
- **`tests/test_app_routes.py`** — an unexpected auto-start fault propagates
  unchanged while every monitor is signalled and awaited, the coordinator
  shutdown runs, the motion monitor is never created, and no task, producer or
  reader survives (all inspected *inside* the running loop, before `asyncio.run`
  can hide a leak); the same for an unexpected fault during motion-monitor
  creation; expected auto-start failure still reaches `yield` and keeps serving;
  and motion loses frames while a capture owns the camera and gets them back from
  the restored preview generation.

### Acceptance-command hardening

Operator evidence commands in `docs/Camera-Acceptance.md` now fail closed. Two
helpers replace ad-hoc invocations:

```bash
mgo_get() { curl --noproxy '*' -fsS "http://127.0.0.1:8080$1"; }
mgo_preview_count() { n=$(pgrep -c -x rpicam-vid || true); n=${n:-0}; if [ "$n" -eq 1 ]; then echo "PASS exactly one rpicam-vid"; else echo "FAIL expected exactly 1 rpicam-vid, found $n"; return 1; fi; }
```

`curl -s localhost:8080/...` printed a 404 or 500 body and exited **0** — a
response body is not a passing endpoint check — and honoured a proxy variable
that could send a "local" check to another host. `pgrep -c` without an explicit
`-eq 1` treated two preview processes as a pass. Both are now errors. A test
asserts every runnable `curl` line in the guide carries `--noproxy '*'`, `-fsS`
and a literal `127.0.0.1`, that none names `localhost`, that the exactly-one
process gate exists, and that the only write is the capture the gate performs
deliberately.

### Validation

Ruff passed; mypy passed for 50 source files; the full suite passed with no new
skip and no thread or unraisable warning under escalation. Mutations A–E (state
settlement removed, process cleanup removed, cleanup scope moved back out,
`str(exc)` inserted into the diagnostic, bare `curl -s localhost` restored) were
each applied independently, each detected, and each reverted byte-for-byte.
Local runtime revalidation re-ran the simulator run on `127.0.0.1:8126` and added
motion recovery after capture restoration, plus in-process runs of unexpected
restoration (successful and failed capture) and unexpected auto-start cleanup.

### Unchanged by this correction

The Raspberry Pi was not accessed. The physical camera acceptance run has not
been performed. Every gate in `docs/acceptance/Initial-Camera-Acceptance.md`
remains `PENDING` / `NOT PERFORMED` / `NOT RECORDED`, and Matthew's sign-off
remains `NOT GIVEN`.

## Repository re-review correction 2 — complete startup fault classification

Round 1 established the unexpected-start contract for faults raised inside
`_validate_startup` and `_finish_start`. Re-review found the contract was not
applied consistently: one part of the same transaction still classified faults
the old way, and the readiness reader still rendered exception text.

### Defect 3 — a backend programming defect was classified as a camera failure

`_begin_start` caught *every* non-`PreviewError` from `PreviewBackend.start()`,
rendered `str(exc)` into `last_error`, and re-raised it as a `PreviewStartError`.
A programming defect in a backend was therefore indistinguishable from an absent
camera: lifespan auto-start caught it, the application served on, and the
arbitrary exception text was published through preview status.

Reproduced against the unmodified branch with a backend raising a hostile
`RuntimeError` through the real `PreviewService.start()` path:

```text
raised type                   : PreviewStartError   <- wrong
raised object is injected     : False               <- wrong
raised is a PreviewError      : True                <- wrong
state                         : failed
last_error contains 'MGO_SECRET' : True             <- leak
last_error contains 'hunter2'    : True             <- leak
last_error contains '0xDEADBEEF' : True             <- leak
```

and through the real lifespan with `auto_start = true`:

```text
lifespan reached yield      : True                  <- wrong
exception escaped lifespan  : None                  <- wrong
GET /health status          : 200
preview state               : failed
status leaks 'MatthewLewis' : True                  <- leak
status leaks 'MGO_SECRET'   : True                  <- leak
```

### Defect 4 — the readiness reader rendered exception text

`_await_first_frame` stored `repr(exc)` for *any* reader exception and turned it
into `stream error during startup: <repr>`. That both published arbitrary text
and collapsed two different things into one: an ordinary stream-read failure
(operational) and a defect in the reader path (a bug) produced the same expected
`PreviewStartError`.

### Root cause

The classification lived at the wrong altitude. Round 1 put the
expected/unexpected decision in `start()`, but two inner steps had already made
their own decision *before* the transaction handler could see the exception:
`_begin_start` decided every backend fault was operational, and the reader
decided every reader fault was operational. A boundary that a caller can bypass
is not a boundary.

### Correction

**One contract for the whole transaction.** `_begin_start` now catches
`PreviewError` only. Anything else is a violation of the backend contract and is
allowed to escape to `start()`, which settles `FAILED`, publishes
`UNEXPECTED_START_ERROR`, logs the traceback and re-raises the original object.
There is no second settlement in `_begin_start`.

**The backend adapter keeps its mapping responsibility**, unchanged:
`launch_preview_subprocess` maps `FileNotFoundError` to
`PreviewUnavailableError` and a launch `OSError` to `PreviewStartError`;
`NullPreviewBackend` raises `PreviewUnavailableError`; the simulator refuses an
unsafe configuration with a `PreviewError`. `PreviewService` deliberately does
*not* guess which arbitrary backend exceptions might be operational — that guess
is what produced the defect.

**The readiness reader now classifies rather than reports.** A typed
`_ReadinessOutcome` carries what the reader thread saw. An `OSError` or
`ValueError` becomes the stable operational reason
`stream read failed during startup` — no `str(exc)`, no `repr(exc)` — which
becomes the existing expected `PreviewStartError` and stays non-fatal during
auto-start; its detail goes to the log. Any other exception is carried back
untouched and re-raised **on the calling thread**, where `start()` applies the
unexpected-start contract. Either way the reader thread returns normally, so it
can never surface as an unhandled-thread warning.

**Lifespan cleanup is now resilient.** `await asyncio.gather(*monitor_tasks)`
and the coordinator shutdown were a flat sequence, so a monitor that raised on
its way out skipped the shutdown and stranded a live camera process behind an
unrelated-looking error. They are now nested `try/finally` steps: the monitor
exception still propagates — it is never swallowed — but only after the camera
has been released and the lifecycle records written.

### New and corrected tests

- `test_unexpected_backend_error_is_wrapped_as_start_error` locked in the
  defect and is replaced by `test_unexpected_backend_error_propagates_unchanged`,
  which asserts object identity, that the result is not a `PreviewError`, the
  full settled state, the exact constant diagnostic, and that no hostile
  fragment survives; a companion test proves the full exception still reaches
  the log.
- Expected-failure preservation: parametrised pass-through for
  `PreviewUnavailableError` and `PreviewStartError`; the real launcher mapping a
  missing command to `PreviewUnavailableError` and a launch `OSError` to
  `PreviewStartError`; the null backend staying an expected failure with its own
  message.
- Stream classification: an expected `OSError` and a closed-stream `ValueError`
  each produce the stable operational reason with no hostile fragment and a
  closed process; an unexpected `RuntimeError` propagates unchanged with the
  constant diagnostic; and the reader thread exits cleanly for all three.
- Lifespan: an unexpected backend fault and an unexpected stream fault are each
  fatal, propagate the original object, settle `FAILED` with the constant
  diagnostic (captured as cleanup begins, since shutdown normalises preview to
  `stopped`), leak nothing into `/camera/preview/status` or the `/health`
  preview projection, run every monitor to exit, never start the motion monitor,
  run coordinator shutdown and leave no task, producer or reader; an expected
  backend failure and an expected stream-I/O failure each keep the API serving
  with their own operational message and exactly one attempt.
- Cleanup resilience: `test_a_monitor_failure_cannot_strand_the_camera` proves
  preview was genuinely running, the monitor exception still propagates, the
  coordinator shutdown still ran and the producer count returned to zero.

The hostile message used throughout carries Windows, POSIX and UNC paths,
environment- and secret-looking values, a memory address, newlines, tabs, an
ANSI sequence and Unicode direction overrides.

### Validation

Ruff passed; mypy passed for 50 source files; the full suite passed with no new
skip and no thread or unraisable warning under escalation. Mutations A–E
(backend wrapping restored, `str(exc)` leaked into the backend diagnostic,
`repr(exc)` restored in the reader, an unexpected stream fault swallowed as an
ordinary failure, a monitor failure allowed to skip coordinator shutdown) were
each applied independently, each detected, and each reverted byte-for-byte.
Runtime revalidation re-ran the simulator managed-preview run (14 checks) and
added five in-process lifespan scenarios: expected stream-I/O failure, unexpected
stream-processing failure, unexpected backend fault, expected backend failure and
monitor failure during cleanup.

### Unchanged by this correction

No endpoint, response field, preview state, capture result, capture error
mapping, physical command array, simulator contract, motion behaviour, migration
or dependency changed. The Raspberry Pi was not accessed, and the physical
camera acceptance record remains entirely pending.

## Final review correction — precise stream classification and primary-exception preservation

Correction 2 established one classification for the whole startup transaction.
Final review found two edge cases where it still gave the wrong answer.

### Defect 5 — every `ValueError` was treated as an operational stream failure

`_await_first_frame` classified `(OSError, ValueError)` together. That is right
for the standard "read from a closed file" case, but a `ValueError` raised by a
**demonstrably open** stream is a defect in the read path, and it was being
handed to auto-start as an ordinary camera problem.

Reproduced against the unmodified branch with a stream that reports
`closed == False` and raises one specific `ValueError`:

```text
stream reported closed at raise : False
raised type                     : PreviewStartError   <- wrong
original ValueError propagated  : False               <- wrong
raised is a PreviewError        : True                <- wrong
last_error                      : Preview process stream read failed during startup.
```

and through the real lifespan with `auto_start = true`, `lifespan reached
yield: True` — the application started and served with a bug in the read path
presented as a camera failure.

### Defect 6 — a cleanup failure replaced the primary startup failure

The lifespan's nested `try/finally` guaranteed that every cleanup step ran, but
an exception raised *inside* a `finally` silently replaces the one already in
flight. With a startup programming defect active and a monitor failing on the
way out, the caller was handed the monitor's exception:

```text
lifespan reached yield  : False
escaped exception       : camera monitor failed during shutdown
is the STARTUP exception: False   <- wrong
is the MONITOR exception: True    <- wrong
```

An operator would have been given the symptom of the shutdown instead of the
cause of it.

### Root causes

**Defect 5** classified by exception *type* alone, when the type is genuinely
ambiguous: Python raises `ValueError` both for reading a closed file and for
ordinary programming mistakes. The disambiguator had to be the stream's own
state, and there was none.

**Defect 6** relied on `finally` for ordering and, implicitly, for exception
precedence. `finally` guarantees the first and silently inverts the second.

### Corrections

**The stream's state is the authority.** `_stream_is_closed(stream)` returns
`True` only when the stream can be *proven* closed. An `OSError` stays
operational; a `ValueError` is operational only when that helper says the stream
is closed; every other `ValueError` is an unexpected programming fault, carried
back and re-raised unchanged. The classification never inspects the exception's
message — a defect can produce text that reads exactly like the closed-file
error, and a real closed-file error is not obliged to contain it. A stream that
cannot answer (no attribute, or a property that raises) has not proven itself
closed, so the stricter classification stands; the exception raised while asking
is logged at debug level and never replaces the read's own error.

**Primary and cleanup exceptions are tracked explicitly.** The lifespan records
the startup/serving failure, runs every cleanup stage through
`_shutdown_lifespan`, and then decides: with a primary failure present the
cleanup failure is logged and the **original object** is re-raised
(`raise primary_error.with_traceback(primary_traceback)`, so identity and
traceback survive); with no primary failure the first cleanup failure is raised
after every stage has been attempted. No `ExceptionGroup` is introduced — the
caller always receives the exact original exception object.

**Every cleanup stage is an independent obligation.** `_shutdown_lifespan` runs
monitor gathering, camera shutdown, stop notification and stop-observation
persistence in sequence, each guarded. A failure in one is logged with its
traceback and never prevents the rest — a monitor that fails must not be able to
strand a camera process, and a failed notification must not lose the stop
record. The first failure is returned for the caller to weigh.

### New tests

- **Stream classification** (`tests/test_preview.py`): an `OSError` from an open
  stream is operational; a genuinely closed `io.BytesIO` produces Python's own
  `ValueError` and stays operational; a `ValueError` from an open stream
  propagates as the exact original object with the constant diagnostic; a
  `ValueError` whose message reads `I/O operation on closed file` but whose
  stream is open is still a defect (message is not the test); a stream whose
  `closed` property raises keeps the stricter classification and the read's own
  error; and the reader thread exits cleanly for all five cases.
- **Lifespan** (`tests/test_app_routes.py`): an open-stream `ValueError` is
  fatal to startup with full cleanup;
  `test_a_cleanup_failure_never_replaces_a_startup_failure` asserts object
  identity between the raised exception and the startup error while the monitor
  error appears only in the logs and every remaining stage still ran; a
  coordinator-shutdown failure propagates only after the notification and
  observation stages were attempted; a notification failure does not prevent
  observation persistence; an observation failure remains observable.

### Validation

Ruff passed; mypy passed for 50 source files; the full suite passed with no new
skip and no thread or unraisable warning under escalation. Mutations A–D (broad
`ValueError`, message-based closed detection, cleanup replacing the primary
exception, cleanup short-circuiting) were each applied independently, each
detected, and each reverted byte-for-byte. Runtime revalidation re-ran the
simulator managed-preview run (14 checks, including motion recovery) and eight
in-process lifespan scenarios, adding the closed-stream `ValueError`, the
open-stream `ValueError` and the simultaneous startup-and-cleanup failure.

### Unchanged by this correction

No endpoint, response field, preview state, capture result, capture error
mapping, physical command array, simulator contract, motion behaviour, migration
or dependency changed. The Raspberry Pi was not accessed, and the physical
camera acceptance record remains entirely pending.

## Final shutdown correction — drain every monitor task

The previous correction made every cleanup *stage* run. Final shutdown review
found that the first stage did not finish what it started.

### Defect 7 — cleanup advanced while a monitor was still running

`_shutdown_lifespan` awaited `asyncio.gather(*monitor_tasks)` with the default
`return_exceptions=False`. That propagates the first exception the instant it
happens and **stops awaiting the remaining tasks**. The tasks keep running — the
await is simply over. Cleanup therefore proceeded to camera shutdown, published
the stop event, recorded the stop observation and returned from the lifespan
while another application-created monitor was still alive.

Reproduced against `c94b46a` with a bounded two-monitor harness driving the real
`_shutdown_lifespan`. Monitor A raises on its stop event; monitor B observes the
stop event and then waits on a controlled release:

```text
monitor A failed                      : True
monitor B observed stop               : True
monitor B exited at camera shutdown   : False
cleanup advanced to camera shutdown   : True
monitor B task done at camera shutdown: False
_shutdown_lifespan returned           : True
cleanup error returned                : RuntimeError('monitor A failed during shutdown')
```

`monitor B task done at camera shutdown: False` is the decisive evidence. The
harness releases monitor B in its own `finally`, so the reproduction leaks
nothing of its own.

### Root cause

`asyncio.gather`'s default is *fail fast*, which is the right default for
concurrent work whose results you need and whose peers you would abandon. It is
the wrong default for a shutdown drain, where the point of the await is not the
results but the guarantee that nothing is left running. The code read as though
it waited for every monitor; it waited only for the first failure.

### Correction

The monitor stage now collects results instead of failing fast:

```python
results = await asyncio.gather(*monitor_tasks, return_exceptions=True)
for task, result in zip(monitor_tasks, results, strict=True):
    if isinstance(result, BaseException):
        _record(f"monitor-tasks[{task.get_name()}]", result)
```

Consequences, each deliberate:

- **Every task is terminal** — returned, raised, or already cancelled — before
  camera shutdown begins, so the lifespan cannot return with a monitor it
  created still running.
- **A failing monitor never cancels its peers.** The stop events are their
  cooperative shutdown mechanism; cutting a healthy monitor short would discard
  whatever it was still finishing. Cancellation is not a substitute for asking.
- **Every failure is logged**, named by its task, not just the one retained.
- **The retained failure is deterministic** — first in `monitor_tasks` order,
  not whichever happened to fail first in wall-clock terms.
- **A cancelled monitor is terminal but is not a clean shutdown**, so its
  `CancelledError` is recorded as a monitor-stage failure. `Task.uncancel()` is
  not called and cancellation is never hidden.

The primary-exception guarantee from the eighth commit is unchanged: when a
startup or serving failure is already active it remains the object raised, and
the monitor failure is logged beside it.

### New tests

Four drive the real `_shutdown_lifespan` directly, so the ordering they pin is
the production one:

- **terminal-before-shutdown** — a failing monitor plus one still draining;
  asserts camera shutdown had not begun while the second was pending, that the
  second was neither cancelled nor abandoned, that both tasks are done, and that
  the stage order is `camera-shutdown → stop-notification → stop-observation`;
- **deterministic selection** — two failing monitors where the *second* fails
  first in wall-clock terms; asserts the returned failure is the first in list
  order, and that both failures and both task names appear in the log;
- **cancellation** — a cancelled monitor is terminal and recorded, while the
  cooperative peer completes normally and is not cancelled;
- **slow-monitor lifespan tests** — one with an active startup failure (the
  startup exception is still the object raised, by identity) and one without
  (the monitor failure is the object raised, by identity); both assert the slow
  monitor reached terminal completion *before* camera shutdown and that no task,
  producer or readiness reader survives.

### Validation

Ruff passed; mypy passed for 50 source files; the full suite passed with no new
skip and no thread or unraisable warning under escalation. Mutations A–D
(default `gather`, cancelling the remaining monitors, inspecting only the first
failing result, starting camera shutdown before the drain completes) were each
applied independently, each detected, and each reverted byte-for-byte. Runtime
revalidation re-ran the simulator managed-preview run (14 checks), the eight
in-process classification and cleanup scenarios, and the restoration scenarios.

### Unchanged by this correction

No endpoint, response field, preview state, capture result, capture error
mapping, physical command array, simulator contract, motion behaviour, migration
or dependency changed. `src/mgo/camera/` was not touched. The Raspberry Pi was
not accessed, and the physical camera acceptance record remains entirely
pending.

## Raspberry Pi narrow validation — performed

A separately authorised narrow validation ran on the production Raspberry Pi
between **2026-07-31T16:12:44+02:00** and **2026-07-31T16:27:58+02:00**, against
SHA `591d4a3ef70c4014ed0d292b05d739f334e2bd41`.

Its purpose was to prove that the reviewed branch builds and behaves correctly in
the real ARM64 environment, without modifying or interrupting production. It is
**not** physical camera acceptance and makes no claim about what the camera can
see.

### Environment

| Fact | Value |
| ---- | ----- |
| Validation account | `claude` |
| Host | `mgo-core` |
| Architecture | `aarch64` |
| Operating system | Raspberry Pi OS (Debian), Linux 6.18.34+rpt-rpi-2712 |
| Approval SHA | Matched the validated SHA exactly |

### Production non-interference

Every production fact below was recorded before the validation began and re-read
after it finished. All were unchanged.

| Fact | Before and after |
| ---- | ---------------- |
| Production branch | `main` |
| Production SHA | `fc66e5193c272f9f7d8d3c101ee3d99cd193d0e4` |
| Production working tree | Clean |
| Service state | Active |
| `MainPID` | `42147` |
| `NRestarts` | `0` |
| Physical preview PID | `42175` |
| Physical preview `started_at` | Unchanged — the preview was never interrupted |
| Production configuration checksum | Unchanged |
| Production capture count | `8` |

The production service was not restarted, its configuration was not edited, the
physical preview was not stopped, no production capture was taken and no
production image content was read.

### Static and automated results

| Check | Result |
| ----- | ------ |
| Dependency sync | Passed, `uv.lock` unchanged |
| Ruff | Passed |
| mypy | Passed — 50 source files |
| Full Pi suite | **1680 passed, 0 skipped** |
| Windows-only skips | All 12 executed and passed on the Pi |
| Configuration suite | 41 passed |
| Preview suite | 50 passed |
| Coordinator suite | 26 passed |
| Lifespan suite | 35 passed |
| Focused monitor-drain selection | 9 passed |
| API suites | 28 passed |
| Camera capture suite | 30 passed |
| Simulator suite | 204 passed |
| Acceptance-document suite | 28 passed |
| Warning-escalated Task 12 suites | 442 passed |

The Windows suite reports 1668 passed with 12 skips, all of them POSIX-only
environment guards in the operations suites. On the Pi those 12 ran and passed,
reconciling exactly to 1680.

### Managed preview lifecycle behaviour

Exercised against an isolated runtime bound only to `127.0.0.1:8126`, using the
simulator backend, its own configuration file, its own database and its own
capture directory — entirely separate from production.

| Behaviour | Result |
| --------- | ------ |
| Preview auto-started before the first request | Passed |
| MJPEG stream produced balanced, decodable live JPEG frames | Passed |
| Motion progressed beyond `waiting_for_frames` | Passed |
| Capture and archive persistence | Passed |
| Preview restoration after capture, with a new `started_at` | Passed |
| Motion recovered after restoration | Passed |
| Capture while stopped did not start preview | Passed |
| Preview start and stop were idempotent | Passed |
| Two clients consumed the shared stream | Passed |
| Isolated runtime shut down cleanly | Passed |
| Temporary runtime and worktree material removed | Passed |
| Production non-interference | Passed |

### Validation deviations

1. **The Task 12 remote-tracking ref and objects were fetched into the production
   repository metadata.** Validating a remote SHA on the Pi requires its objects
   locally. `main`, `HEAD` and the production working tree were unchanged.
2. **The full Pi suite was run twice.** The second run existed only to capture
   explicit post-test working-tree cleanliness evidence, which the first run had
   not recorded. Both runs gave 1680 passed with the lockfile unchanged.
3. **The warning-escalated run covered 442 tests**, a superset of the equivalent
   Windows run.
4. **Repository-wide `ruff format --check` is not clean.** It is not currently a
   project gate — the standard is `ruff check .`, `mypy src` and `pytest` — and
   the condition was not introduced by Task 12.

### Boundaries still standing

- The validation used the **simulator backend only**.
- Physical `rpicam` / `libcamera` behaviour was not exercised by the validation
  runtime.
- Production geometry and physical restoration latency remain unvalidated.
- Physical acceptance remains **entirely pending**.
- Matthew's visual sign-off remains **NOT GIVEN**.
- The 24-hour gate remains **NOT STARTED**.
- The 48-hour gate remains **NOT STARTED**.
- Task 12 has **not** been deployed and **not** been merged.

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
7. **A sixth commit exists.** The original plan specified exactly five. Review
   round 1 separately authorised one correction commit,
   `Harden managed preview failure cleanup`, so the branch carries six. No
   existing commit was amended, rebased, squashed or force-pushed.
8. **The motion-recovery test slows the simulator capture.** It patches
   `SimulatorCaptureBackend.capture` (at test level; the Task 11 source is
   untouched) to take ~0.8 s, so the window in which no preview producer exists
   is longer than a motion analysis interval. Without it the test would be
   racing the sampler rather than proving recovery, and a real `rpicam-still`
   capture takes far longer than 0.8 s anyway.
9. **A seventh commit exists.** Re-review round 2 separately authorised one
   further correction commit, `Preserve preview startup fault boundaries`. No
   existing commit was amended, rebased, squashed or force-pushed.
10. **An eighth commit exists.** Final review round 3 separately authorised one
    further correction commit, `Complete Task 12 startup failure handling`. No
    existing commit was amended, rebased, squashed or force-pushed.
11. **A ninth commit exists.** Final shutdown review round 4 separately
    authorised one further correction commit,
    `Drain all monitor tasks during shutdown`. No existing commit was amended,
    rebased, squashed or force-pushed.
12. **The drain ordering tests use one bounded window.** The direction that
    *must not* happen — camera shutdown beginning while a monitor is pending —
    is checked after a 0.25 s window. With the drain in place that ordering is
    structurally impossible whatever the timing; without it the executor hop is
    orders of magnitude shorter than the window, so the mutation is caught
    reliably. The direction that must happen is always driven by an explicit
    event, never a sleep.

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
- An *unexpected* preview startup failure publishes one constant diagnostic.
  Distinguishing *kinds* of programming defect requires the application log; the
  status response deliberately carries no exception detail at all.
- The lifespan cleanup scope opens immediately before auto-start. An unexpected
  fault in an earlier startup step — for example the initial camera readiness
  check — would still escape with the health and database monitors running.
  Widening the scope further would mean restructuring startup beyond this
  correction, and no such fault is known.
- Stream-failure classification uses the stream's closed state, which is the
  only evidence available at that point. A `ValueError` raised while the stream
  genuinely *is* closed is therefore recorded as operational even in the
  unlikely case that a defect produced it — the observable facts are identical,
  and the alternative (guessing from the message) is what this correction
  removed.
- `PreviewService.shutdown()` normalises a failed preview to `STOPPED`, so after
  application shutdown the last failure is visible only in the log. That is the
  pre-existing shutdown contract and was not changed here.
- The monitor drain waits for every monitor task without a timeout. A monitor
  that never honoured its stop event would hold shutdown open indefinitely.
  That is deliberate for now: every monitor in the application is a bounded
  cooperative loop, and a bounded wait here would reintroduce exactly the defect
  this correction removed — returning while a task is still running — only with
  the truth hidden behind a timeout instead of an exception.
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
