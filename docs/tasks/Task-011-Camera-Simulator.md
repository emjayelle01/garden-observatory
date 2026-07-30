# Task 11 — Deterministic camera simulator

## Status

**Implementation corrected and locally revalidated; Raspberry Pi validation not
performed. Awaiting repository re-review.**

| Gate | Outcome |
| ---- | ------- |
| Task definition | Complete (this record, first commit) |
| Implementation | Complete |
| Local static and automated validation | Passed |
| Mutation / negative verification | Passed — all 18 defects detected, plus 1 correction mutation |
| Local runtime validation | Passed |
| Repository review | **Round 1 complete** — one blocking defect found and corrected (see below) |
| Repository re-review | Not started |
| Raspberry Pi validation | **Not performed** — the Pi was not accessed |

### Repository-review correction — truthful producer failure handling

Review round 1 found one blocking defect. The producer thread could die from an
unexpected frame-generation exception without updating the simulated process
state, so `poll()` kept returning `None`, `read_error()` stayed empty, the frame
mailbox was never ended, a blocked reader stayed blocked for ever, and
`PreviewService` went on reporting `RUNNING`. The failure was never reconciled to
`FAILED`, and no test exercised it.

This was reproduced first, against the unmodified branch, with a frame sequence
that returns one valid frame and then raises: the producer died, `producer_alive`
became false, `poll()` returned `None`, `read_error()` returned `""`, the blocked
reader was never released and `status()` still reported `running` with
`owner: preview` and `last_error: null`.

The correction (fifth commit, `src/mgo/camera/simulator.py`) makes an unexpected
producer exit observable, bounded and truthful. See *Preview-process design*
below and `docs/Camera-Simulator.md` for the full contract. In summary:

- an unexpected exit settles exit code **`1`** — distinct from a requested
  `terminate` (`0`) and `kill` (`-9`);
- `read_error()` publishes a bounded single-line diagnostic built only from the
  exception's class and message; the traceback goes to the application log;
- the mailbox is ended and cleared, so blocked readers see ordinary end-of-file;
- `poll()` reconciles a producer thread that is no longer alive even if it left no
  exit state, so truthfulness does not depend on the exception handler alone;
- the **first** settled exit state always wins, so later cleanup cannot mask a
  failure and a requested stop is never relabelled a failure;
- `PreviewService` needed no change: its existing unexpected-exit reconciliation
  settles the service into the existing `FAILED` state.

The misleading test `test_unexpected_exit_reconciliation_stays_truthful` — which
killed an *unrelated* simulator process and then confirmed the service's own
process was healthy — was renamed to
`test_an_unrelated_simulator_process_does_not_affect_the_service` with an accurate
docstring, and the explicit-kill test was renamed
`test_a_killed_process_is_reported_as_failed` so neither is presented as proof of
unexpected producer failure.

### Delivered

| File | Change |
| ---- | ------ |
| `src/mgo/camera/simulator.py` | **new** — frame generation, `SimulatorCaptureBackend`, `SimulatorPreviewBackend`, `SimulatorPreviewProcess`, `SimulatorMjpegStream` |
| `src/mgo/core/camera_detection.py` | `SimulatorCameraDetector`, `SIMULATOR_READINESS_DETAIL`, one `build_detector` branch |
| `src/mgo/core/config.py` | `simulator` added to `SUPPORTED_CAMERA_BACKENDS` |
| `src/mgo/camera/backend.py` | one `build_capture_backend` branch |
| `src/mgo/camera/preview_backend.py` | one `build_preview_backend` branch |
| `src/mgo/camera/__init__.py` | re-exports |
| `tests/test_camera_simulator.py` | **new** — 187 tests (170, plus 17 for the review correction) |
| `docs/Camera-Simulator.md` | **new** — full reference |
| `README.md`, `config/mgo.toml`, `config/mgo.production.example.toml` | documentation and comments only |

`src/mgo/api/app.py` needed **no change**: the existing wiring already calls
`build_capture_backend`, `build_preview_backend` and `build_detector`, so the
simulator is selected purely by configuration.

Validation on the Windows development workstation:

```text
Ruff:   passed
mypy:   passed — 49 source files (48 baseline + simulator.py)
pytest: 1514 passed, 12 skipped  (baseline 1327 + 187 added; same 12 skips)
```

No dependency was added; `pyproject.toml` and `uv.lock` are unchanged. No API
route, response-field meaning, status vocabulary, migration, schema, systemd
unit, deployment script or production configuration *value* was touched.

## Purpose

Give Matt's Garden Observatory a supported, explicitly configured runtime camera
backend that produces deterministic generated imagery, so the **real** MGO
pipeline — readiness, still capture, preview lifecycle, MJPEG streaming, browser
delivery, motion-frame consumption and status/health reporting — can be
exercised end to end on a development workstation with:

- no Raspberry Pi camera hardware;
- no `rpicam-*` tooling;
- no `libcamera-*` tooling;
- no video device;
- no subprocess;
- no external image or video file;
- no network access.

The simulator is **not** another test double. It is a normal backend selected
through normal configuration:

```toml
[camera]
enabled = true
backend = "simulator"
```

## Current repository context

Task 11 starts from merged `main` at:

```text
6f5f70395b004ce62b3689491fc45e80f8070574
```

Windows validation baseline at that commit:

```text
Ruff: passed
mypy: passed — 48 source files
pytest: 1327 passed, 12 skipped
```

Raspberry Pi validation baseline from merged Task 10:

```text
pytest: 1339 passed, 0 failed, 0 skipped
```

Every capability delivered through Task 10 is preserved. Task 12 has not
started.

## Architectural decision

The existing architecture already provides every extension boundary Task 11
needs, so the simulator **extends** those boundaries and adds no parallel
pipeline:

| Existing boundary | Simulator participation |
| ----------------- | ----------------------- |
| `CameraDetector` (`mgo.core.camera_detection`) | `SimulatorCameraDetector`, selected by `build_detector` |
| `CaptureBackend` (`mgo.camera.backend`) | `SimulatorCaptureBackend`, selected by `build_capture_backend` |
| `PreviewBackend` / `PreviewProcess` (`mgo.camera.preview_backend`) | `SimulatorPreviewBackend` / `SimulatorPreviewProcess`, selected by `build_preview_backend` |
| `PreviewProcessFrameSource` (`mgo.camera.streaming`) | reads the simulator's MJPEG stream unchanged |
| `MjpegBroker` (`mgo.camera.streaming`) | fans simulator frames out to viewers unchanged |
| `BrokerFrameSource` (`mgo.motion.frame_source`) | feeds simulator frames to motion unchanged |
| `CaptureService` / `PreviewService` | orchestrate the simulator unchanged |

Deliberately **not** created: a second camera pipeline, a simulator-only API, a
simulator-only preview page, a simulator-only motion monitor, a second broker, a
separate application, a shell process pretending to be a camera, or any
test-only code path inside the API routes.

The normal production wiring in `mgo.api.app` already calls
`build_capture_backend(config.camera)`, `build_preview_backend(config.camera.backend)`
and `build_detector(config.camera.backend)`, so selecting the simulator requires
**no change to the application wiring** beyond factory support.

### Relationship to the existing mocks

`MockBackend`, `MockPreviewBackend`, `MockPreviewProcess` and `MockFrameSource`
remain exactly what they are: test doubles, configured from test code, not
reachable from configuration, and in the case of `MockBackend` not even
producing a decodable image. They stay, unrenamed, and Task 11 does not pretend
they satisfy this task. The simulator differs on every axis that matters:

| | Existing mocks | Task 11 simulator |
| --- | --- | --- |
| Selected by | test code | `camera.backend` configuration |
| Image output | fixed non-decodable byte pattern (`MockBackend`) | valid, decodable JPEG |
| Preview frames | supplied by the test | generated by a real bounded producer |
| Motion usable | only with hand-written frames | yes, by design |
| Runs under `uvicorn` | no | yes |

## Supported runtime contract

Supported camera backend vocabulary after Task 11:

```text
rpicam      physical Raspberry Pi camera through rpicam-*
libcamera   physical camera through legacy libcamera-*
simulator   deterministic generated imagery, no physical camera
null/none   deliberately unavailable
```

Existing meanings are unchanged. Defaults are unchanged. The tracked
development configuration and the production example keep their existing
`backend` values; only their comments and the documentation mention
`simulator`.

## Simulator truthfulness rules

The simulator must never claim a physical camera is connected. It must not
report a device index, an IMX708, an enumerated camera, a video device, a camera
serial number, an `rpicam` command, or any hardware-discovery evidence.

One stable sentence is used across implementation, tests and documentation:

```text
Deterministic camera simulator is active; no physical camera is in use.
```

Every capture and every status response identifies the source through the
existing `backend` field, whose value is `simulator`. No new response field and
no new status value are introduced.

## Deterministic frame design

Frames are generated at runtime with Pillow (already a production dependency).
No image fixture is committed. Generation is independent of wall-clock time,
hostname, process identifier, random-number generation and operating system, and
embeds no EXIF or private metadata.

Every frame carries the static visible marker:

```text
MGO CAMERA SIMULATOR
```

drawn with Pillow's bundled bitmap font (`ImageFont.load_default_imagefont()`)
— no external font file and no FreeType antialiasing, so the marker is
bit-identical on every platform.

The scene is simple and bounded: a neutral background, fixed feeder-like shapes,
the static simulator label, and one clearly contrasting test object that is
either absent or at one of two positions.

The logical sequence is eight frames long and repeats:

```text
frame 0: quiet scene A
frame 1: identical quiet scene A
frame 2: object present at position B
frame 3: identical object at position B
frame 4: object moved to position C
frame 5: identical object at position C
frame 6: quiet scene A
frame 7: identical quiet scene A
repeat
```

The identical pairs are load-bearing: they let motion detection settle to
`no_motion`, while the 1→2, 3→4 and 5→6 transitions produce deterministic
`motion_detected`. Nothing time-varying — no frame counter, no timestamp — is
drawn into the image, so the simulator never claims continuous motion merely
because a counter advanced.

JPEG output for the same dimensions and sequence index is byte-identical across
repeated calls in one environment, and the pixel content is semantically
deterministic across Windows and Linux.

## Readiness behaviour

With `camera.enabled = true` and `camera.backend = "simulator"`, monitored
readiness reports:

```text
backend:   simulator
status:    available
available: true
detail:    Deterministic camera simulator is active; no physical camera is in use.
```

With `camera.enabled = false` the existing `disabled` behaviour wins:
selecting the simulator never overrides the top-level enabled gate.

The simulator detector performs no subprocess call, no filesystem probe and no
network call, returns deterministic evidence, and never raises during ordinary
detection.

## Capture behaviour

`SimulatorCaptureBackend` implements the existing `CaptureBackend` protocol:

- `name` is exactly `simulator`;
- `capture(destination)` writes one valid, decodable JPEG at 1280 × 720 (the
  deterministic simulator capture resolution — the full 4608 × 2592 physical
  sensor frame is deliberately **not** imitated);
- reported dimensions are exactly the dimensions of the generated JPEG;
- the file is non-empty and visibly carries the simulator marker;
- no EXIF, timestamp, hostname or path metadata is embedded;
- repeated captures are deterministic;
- no subprocess is launched and no hardware probe occurs;
- expected filesystem failures map to the existing camera-domain exceptions;
  raw `OSError` never escapes.

Capture flows through the real filename generation, destination selection,
directory creation, output verification, metadata construction, capture archive
and `POST /camera/capture` contract. Capture metadata reports `backend:
simulator`. No new response field is added.

## Preview-process design

`SimulatorPreviewBackend` implements `PreviewBackend`; `SimulatorPreviewProcess`
implements the existing `PreviewProcess` protocol with truthful in-process
equivalents:

```text
pid:        None            (there is no operating-system process)
poll:       None while active, 0 after normal termination, -9 after a forced kill,
            1 after an unexpected producer failure
read_error: empty during normal operation; a bounded diagnostic after a failure
```

`terminate`, `kill`, `wait` and `close` are idempotent, and the **first** settled
exit state wins.

A single bounded producer thread per preview process generates frames at the
configured preview frame rate into a bounded mailbox (at most two frames). When
the consumer is slow the stale frame is discarded rather than queued, so memory
stays bounded. The producer does not busy-spin, exits promptly on terminate,
kill or close, is a daemon so it can never keep the interpreter alive, and is
started **only** by the normal preview start path — never merely because the
application started.

An **unexpected** producer exit is a distinct, truthful case (added by the
repository-review correction). An explicit `terminate()`/`kill()` and an
unexpected failure are never conflated:

```text
producer raises  -> log the traceback; settle exit code 1 with a bounded
                    read_error diagnostic; set the stop event; end and clear the
                    mailbox; release every blocked reader at end-of-file; the
                    thread exits
poll()           -> never reports None for a producer thread that is not alive:
                    a thread that vanished without settling is reconciled to the
                    same failure state (thread-safe and idempotent)
first exit wins  -> failure then close stays 1; terminate then kill stays 0;
                    kill then close stays -9; a normal stop acquires no message
```

`KeyboardInterrupt`, `SystemExit` and `GeneratorExit` are deliberately not caught.
The exit-state lock is never held while the mailbox is ended and the calling
thread is never joined against itself, so no lifecycle combination can deadlock.
`PreviewService` then reconciles to the existing `FAILED` state with
`owner: null`, `uptime_seconds: null` and a `last_error` naming code `1` and the
bounded diagnostic; when the failure precedes the first frame, `start()` raises
`PreviewStartError` instead and the state is never `RUNNING`.

No `subprocess`, `multiprocessing`, shell, local HTTP server or temporary video
file is used.

## Streaming behaviour

`frame_stream()` returns a blocking binary stream of concatenated complete JPEG
frames — the same raw MJPEG assumption `rpicam-vid` satisfies — so the existing
`parse_mjpeg_frames` demultiplexer consumes it unchanged. The streaming parser
is **not** modified for the simulator.

The first complete JPEG arrives within the existing preview startup timeout,
because `PreviewService` validates startup by reading the first complete frame.
After that first frame is consumed, later readers continue to receive subsequent
frames from the same simulator process. A blocked `read()` unblocks when the
process is stopped or closed.

Delivery to browsers uses the existing `PreviewProcessFrameSource`,
`MjpegBroker` and `encode_multipart_frame` with no simulator-specific path.

## Motion compatibility

Generated frames are consumed by the existing `FrameDifferenceDetector` through
the existing `BrokerFrameSource` and `MjpegBroker`. The motion algorithm and its
thresholds are not modified. The deterministic sequence demonstrates:

```text
identical pair      -> no_motion
scene transition    -> motion_detected
next identical pair -> no_motion
```

The simulator creates no motion detector, bypasses no JPEG decoding, publishes
no fabricated `MotionResult`, writes no motion observation and never manipulates
motion state.

## Configuration changes

Only one configuration change is made: `simulator` is added to
`SUPPORTED_CAMERA_BACKENDS`. No new section and no new setting are introduced.
Simulator preview uses the existing `[preview]` width, height, fps, startup
timeout and shutdown timeout. Configuration files that omit any Task 11 setting
load exactly as before, because there is no Task 11 setting to omit.

## Safety boundaries

Explicit backend-specific bounds keep the simulator from becoming an unbounded
memory or CPU generator:

```text
maximum simulator preview width:  1920
maximum simulator preview height: 1080
maximum simulator preview fps:    30
frames retained in the mailbox:   2
```

An unsafe simulator preview request is **rejected**, not silently clamped, and
maps to the existing preview-domain failure model with a concise message that
exposes no private path or environment data.

The simulator is opt-in. The default configuration remains hardware-safe and
unchanged; the production example remains configured for the real Raspberry Pi
backend. An operator must deliberately set `backend = "simulator"` before any
simulated imagery can appear.

## Test strategy

New focused coverage lives in `tests/test_camera_simulator.py`; existing test
files are touched only where a genuinely shared contract changed (the supported
backend vocabulary). Tests require no Raspberry Pi, no camera hardware, no
camera tooling, no network, no live external `uvicorn` service, no production
database, no production capture directory and no production configuration, and
use no sleeps long enough to make the suite slow or flaky.

Coverage areas: configuration vocabulary and compatibility; readiness truth;
frame generation (validity, dimensions, determinism, identical pairs,
meaningful transitions, absent EXIF, marker presence, refused dimensions);
capture backend and its service/archive/API integration; preview-process
lifecycle including reader unblocking, terminate, kill, wait, close idempotence
and thread-leak freedom; streaming integration through the real parser, broker
and multipart encoder; motion integration through the real detector; API
integration through real in-process ASGI dispatch; and a negative hardware
boundary proving no subprocess or `rpicam`/`libcamera` command is ever invoked.

Independent mutations are applied and reverted to prove the new tests actually
fail when the corresponding defect is introduced — **all 18 were detected**, and
every mutated file was verified byte-identical afterwards by SHA-256 digest. The
repository-review correction adds a nineteenth: **removing the unexpected-producer-failure
settlement** (both the producer's exception handler and the `poll()`
reconciliation) fails 13 of the new tests and raises 12 unhandled-thread-exception
teardown errors; the file was verified byte-identical after reverting.
`tests/test_camera_simulator.py`
holds **187** tests, and no existing test file needed a change: the shared
contracts they cover (backend vocabulary, detector selection, the physical and
null branches) are asserted from the new file, so nothing existing was weakened
or rewritten.

Every read in the producer-failure tests is **bounded** — performed on a helper
thread with a timeout — so a stream that fails to reach end-of-file makes a test
fail rather than hang it. The failure tests also carry
`filterwarnings("error::pytest.PytestUnhandledThreadExceptionWarning")`, so an
exception escaping the producer thread is a test failure rather than a warning.

### Delivered coverage

| Area | What is proven |
| ---- | -------------- |
| Configuration | `simulator` accepted; every pre-existing value still accepted; unsupported values still rejected; trimming and case-folding preserved; the default is still `rpicam`; no tracked file selects the simulator |
| Readiness | `available` + `backend: simulator` + the exact sentence; the disabled gate still wins; no command runner, no subprocess, no filesystem probe; repeated checks materially identical |
| Frame generation | every frame a decodable JPEG at the exact size; no EXIF; the marker present before *and* after encoding; deterministic; clock-independent; the four identical pairs identical at pixel level; the three transitions meaningfully different at pixel level; only three distinct images ever; out-of-bounds geometry refused |
| Capture | factory selection; `name == "simulator"`; truthful dimensions and file size; deterministic; write failure mapped to `CaptureWriteError` (not an `OSError`); no subprocess; the real `CaptureService`; the disabled gate; archive insertion in a temporary database; capture confined to the temporary directory |
| Preview | factory selection; nothing starts until preview starts; truthful `pid`/`poll`/`read_error`; first frame prompt; `RUNNING` via the real service; idempotent start; unsafe width/height/fps refused and never clamped; message leaks no environment detail; terminate, kill, wait codes; blocked reads unblocked by both close and terminate; full idempotence; an unrelated process's death does not disturb the service; an explicit kill surfaces as `FAILED` with `-9`; no thread survives stop or repeated cycles; producer is a daemon |
| Producer failure | a fault inside frame generation settles exit code `1`, never `0`/`-9`/`None`; `read_error()` is the bounded one-line diagnostic, is empty while healthy, after a normal terminate and after an explicit kill, and is bounded to 200 characters for a pathological 5 000-character message; the traceback reaches the log but never the diagnostic; the diagnostic contains no path, environment fragment or `Traceback`; a failure survives later terminate/kill/close; a kill *before* a failing frame stays a kill; repeated `poll`/`wait`/`close` stay safe; a blocked reader is released at end-of-file; the stream reaches EOF; `poll()` reconciles a producer that returned without settling; startup failure raises `PreviewStartError` with code `1` and the diagnostic and never reports `RUNNING`; post-start failure reconciles the real service to `FAILED` with `owner: null`, `uptime_seconds: null` and a truthful `last_error`; shutdown stays idempotent; no producer thread survives |
| Streaming | the existing parser consumes the generated MJPEG; broker delivery; three viewers share one producer and one pump; a slow viewer's mailbox holds one frame; last-viewer disconnect leaves preview running; stop ends the stream; a new generation is not ended by the old one; the existing multipart encoder |
| Motion | quiet pair scores exactly `0.0`; each transition clears the threshold; all scores finite and in `[0, 1]`; the threshold is untouched; hand-written frames behave exactly as before; the real monitor + `BrokerFrameSource` + `MjpegBroker` + `FrameDifferenceDetector` produce `establishing_baseline` → `no_motion` → `motion_detected` with no `error` |
| API | real in-process ASGI dispatch of `/camera/status`, the whole preview lifecycle including a real multipart frame, `/camera/capture`, `/captures`, `/captures/{id}`, `/health`, `/dashboard`, `/preview`; the 409 stream gate; capture releases preview with no auto-restart; neither page starts a producer; the documented route set is intact |
| Hardware boundary | behavioural spies make `subprocess.run`, `subprocess.Popen`, `run_subprocess` and `launch_preview_subprocess` fatal across the whole pipeline; AST checks prove the module imports no process/network facility and names no camera command in executable code |

## Measured motion behaviour

Measured through the real `FrameDifferenceDetector` with the production defaults
(analysis 160 × 90, per-pixel threshold 20, changed-pixel ratio threshold 0.08):

| Transition | Changed-pixel ratio | Result |
| ---------- | ------------------- | ------ |
| identical pair (`0→1`, `2→3`, `4→5`, `6→7`) | `0.000` | `no_motion` |
| object appears (`1→2`) | ≈ `0.117` | `motion_detected` |
| object moves (`3→4`) | ≈ `0.236` | `motion_detected` |
| object leaves (`5→6`) | ≈ `0.119` | `motion_detected` |

Every identical pair scores exactly zero and every transition clears the
threshold with margin, so the demonstration is not marginal.

## Local validation procedure

Static and automated validation on the Windows development workstation:

```powershell
uv sync
uv run ruff check .
uv run mypy src
uv run pytest
git diff --check
```

A real local runtime validation then runs the application under `uvicorn` with a
**temporary** configuration and temporary storage outside the repository,
camera and preview and motion enabled, notifications disabled, bound only to
`127.0.0.1` on a non-production port, and exercises readiness, preview start,
the MJPEG stream, motion progression, still capture, the capture archive,
preview stop and clean shutdown. It is never run against `config/mgo.toml`, the
repository development database, `/etc/garden-observatory/mgo.toml`,
`/var/lib/garden-observatory`, or the Raspberry Pi. No temporary configuration,
capture, database or log is committed.

### Local runtime validation result

**Passed — 24 of 24 checks.** The real application was served by `uvicorn` from
the production `mgo.api.app:app` object against a temporary configuration and
temporary storage in the system temp directory, bound to `127.0.0.1:8125`, with
`camera.enabled = true`, `camera.backend = "simulator"`, `preview.enabled = true`,
`motion.enabled = true` and `notifications.enabled = false`. Every request went
over real HTTP.

Confirmed: startup with no camera tooling; `/camera/status` reporting `available`,
`simulator` and the exact truthful detail; preview initially `stopped`; start
succeeding; `running` with `backend: simulator`; the stream delivering complete
decodable 1280 × 720 JPEGs as `multipart/x-mixed-replace`; the marker visibly
present (corner luminance 19–247); several distinct frames; motion reporting
**both** `no_motion` and `motion_detected` with no `error`, every score in
`[0, 1]` and the threshold still `0.08`; capture succeeding with
`backend: simulator` at 1280 × 720; the file decoding; the capture landing only
under the temporary directory; the archive listing it; preview stopping; a clean
shutdown with every monitor stopped and `Application shutdown complete`; the
`application_stop` observation recorded in the temporary database; no producer
process surviving; and no `rpicam-*` or `libcamera-*` command named anywhere in
the application log, which instead carried the simulator's own
"no physical camera is in use" lines.

Two harness details are worth recording so a future reader does not mistake them
for defects. A Ctrl+Break-initiated stop on Windows exits with the control-event
code (3), so the *log* is the evidence of graceful shutdown, not the exit status.
And `uvicorn`'s default logging configures only its own loggers, so the harness
configures root logging itself in order to capture the application's INFO output.

The temporary configuration, database, captures and logs were deleted afterwards
and none were committed. `git status --porcelain` showed no unexpected change to
any tracked file.

This runtime validation was **re-run unchanged after the repository-review
correction** and passed identically (24 of 24), confirming the correction did not
disturb the normal runtime path. Producer failure cannot be provoked through the
API, so it is validated by a separate controlled in-process run that injects a
fault into frame generation and confirms the failure code, bounded diagnostic,
released reader, `FAILED` reconciliation and absence of any surviving producer
thread. The temporary configuration and the production application API were not
modified for it.

## Raspberry Pi validation status

**Not performed.** The Raspberry Pi is not accessed during Task 11
implementation: no SSH, no checkout update, no approval-file change, no service
restart, no production configuration edit, no simulator run on the Pi, no
capture, no preview start or stop, and no change to Task 10 operations
components.

After repository review, a separate narrow Pi authorisation may validate
simulator mode using isolated temporary configuration without replacing the
production camera configuration. That decision belongs to Matt.

## Rollback

Before merge: switch back to `main`. The simulator branch has no production
effect — no migration, no schema change, no dependency change, no configuration
value change and no deployment change.

After a future merge: revert the five Task 11 commits, re-run the test suite,
and redeploy `main` if required.

No database rollback and no migration rollback are required. No media cleanup is
required unless an operator deliberately ran a simulator capture with a
non-temporary capture directory. No automatic rollback script is written.

## Deviations

Two deviations from the prescribed plans, recorded for the reviewer.

**1. A fifth commit exists, adding the repository-review correction.** The
original Task 11 plan specified exactly four commits. The review correction was
separately authorised as exactly one additional commit
(`Handle simulator producer failures truthfully`), so the branch now carries five.
No existing commit was amended, rebased, squashed or force-pushed.

**2. Test-robustness hardening landed in the fourth commit rather than the third.**
Mutation verification (removing the producer's first-frame output) exposed a
latent weakness in two of the new tests: they read from the frame stream on the
calling thread, so against a simulator that produces *nothing* they would block
for ever rather than fail. That is a real defect in the tests — a broken
implementation must make a test fail, never hang a suite.

The fix adds two bounded helpers (`_read_frames` and `_first_frame`) that read on
a helper thread with a join timeout, plus a `queue.Empty` guard in the
end-of-stream test. It belongs logically in the third commit ("Integrate and test
camera simulator"), but that commit was already made and the commit plan forbids
amending it and forbids a fifth commit — so it is carried in the fourth commit
alongside the documentation. No assertion was weakened; the bounded reads made
the suite *faster* (the simulator file's targeted run dropped from ~23 s to
~8 s) because a satisfied read no longer waits for a frame it does not need.

The same lesson recurred in the correction: the first draft of the
producer-failure tests read from the stream on the calling thread, and the
correction's own mutation run *hung* instead of failing. Those reads now go
through a bounded `_read_bounded` helper, so the mutation run fails cleanly.

## Known limitations

- Simulator readiness proves the *software* path only; it says nothing about a
  physical camera being present, focused or correctly exposed.
- Simulated captures are not wildlife evidence and must never be entered into
  Matt's Viewings.
- Simulator preview does not validate focus, exposure, field of view or window
  reflections.
- Simulator output is unsuitable as a bird-identification training dataset.
- JPEG bytes may differ between platforms if the platform's JPEG encoder library
  differs; the *pixel content* is semantically deterministic, and
  byte-determinism is guaranteed within one environment.
- Task 12 physical camera acceptance remains required and is not replaced by
  this task.
- An unexpected producer failure is reported through one failure code (`1`) and
  one bounded message; the simulator does not classify *kinds* of internal fault,
  because a real preview process does not either.

## Explicit non-goals

Task 11 does not add or change: an API endpoint or path, a response field's
meaning, a camera-readiness status, a preview status, a motion status, a
database migration, the database schema, a systemd unit, a deployment script, a
dependency, or `uv.lock`. It does not start preview automatically, trigger
capture automatically, implement motion-triggered capture, implement an event
lifecycle, add regions of interest, add pre/post-event windows, add media
retention, add bird detection, add species identification, add bounding boxes,
add any AI or machine-learning framework, add Telegram or email, add an external
asset, commit a binary media file, or fetch anything from the internet.

## Task 12 boundary

Task 11 ends at a deterministic, hardware-free camera backend and the proof that
the existing pipeline consumes it. Task 12 — physical camera acceptance on the
Raspberry Pi — has **not** started and is not begun by this task. Future
event-capture and detection work may reuse the simulator as a deterministic
frame source, but no part of that work is delivered here.
