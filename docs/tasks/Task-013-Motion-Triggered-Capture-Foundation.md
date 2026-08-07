# Task 13.1 — Motion-Triggered Capture Foundation

**Status: implemented and validated in the repository. Not reviewed, not merged,
not deployed. No Raspberry Pi access was used and no hardware validation was
performed.**

Repository starting SHA: `9634e84a19b31411b6d741d4c5a13f8c0aec508a`
(`main`, and `origin/main`, at the time this branch was created).

Branch: `task-013-motion-triggered-capture-foundation`.

## What this task implements

MGO can now **optionally** take and catalogue one full-resolution still image
when the existing motion subsystem records a material transition into
`motion_detected`:

```text
motion transition -> bounded trigger handoff -> background worker
    -> CameraCoordinator -> CaptureService -> CaptureArchive
    -> correlated immutable observation
```

The feature is **disabled by default**. With the shipped configuration nothing
about the application's behaviour changes: no worker is created, no queue exists,
and no camera activity happens that did not happen before.

The behaviour, contracts, states, counters, error categories and limitations are
documented in [`docs/Event-Capture.md`](../Event-Capture.md). This record covers
scope, decisions and validation.

## What this task does **not** implement

Bird detection. Species identification. Object detection, bounding boxes or
confidence thresholds. Any AI/ML framework (no TensorFlow, PyTorch, YOLO,
OpenCV). Region-of-interest masking. Pre-event or post-event frame buffers.
Burst capture or sharpest-frame selection. Video. Visit or session grouping.
Incident modelling. Multi-image events. Retention or deletion policy. Thumbnails.
A media download or image-serving API. Notification image attachments or any new
notification transport. A manual event-trigger endpoint.

`motion_detected` continues to mean **the scene changed**, never *a bird is
present*. Event capture is a consumer of that signal and adds no interpretation
of its own.

## Architectural decisions

**One shared capture workflow.** The manual `POST /camera/capture` route
previously performed two steps inline: a coordinator capture and an archive
write. Adding a second caller would have created two copies of the same
transaction, which drift. `mgo.captures.workflow.CaptureWorkflow` is now the one
place that composition lives; the route and the worker both run through it. The
workflow knows nothing about FastAPI, HTTP, motion, notifications, systemd or any
concrete camera backend, and a test audits its AST to keep it that way.

**The camera coordinator remains the single camera authority.** Automatic
capture calls `CameraCoordinator.capture_image()` exactly as the manual endpoint
does. No second camera process, no second preview process, no direct backend
access, and preview restoration is governed entirely by the existing
`preview.restore_after_capture` contract.

**The camera lock is not widened.** The coordinator's transaction closes —
including any preview restoration — before the archive is touched, so SQLite work
can never hold or stall the camera. A test drives a real coordinator call from
inside `record_capture` in a bounded thread: if the lock were still held, that
call would deadlock rather than raise.

**Motion stays capture-agnostic.** `mgo.motion` does not import
`mgo.event_capture`, `mgo.captures` or `CameraCoordinator`; the application
lifespan is the only place the two are connected, and a test audits the motion
package's source for it. The existing transition-listener hook is reused — there
is no second preview-frame subscription and no second detector.

**The existing cooldown is the only cooldown.** The motion monitor's material
transition rule and `cooldown_seconds` decide which transitions reach the
callback. Event capture adds no rate limiting, no second cooldown and no
re-interpretation of motion scores.

**A bounded queue of exactly one pending trigger.** One capture may execute while
at most one further trigger waits; anything beyond that is dropped and counted.
This is not configurable. The production scene is four feeders on a tree in an
open garden, where wind alone produces motion for minutes at a time; an unbounded
queue — or a task per transition — would build a backlog of captures of moments
that had already finished, each one taking the camera away from preview again.

**A dropped trigger writes no observation.** It increments a live counter and
logs. Persisting a row per dropped trigger would let a windy afternoon flood the
SQLite timeline with records of work that was not done.

**No retry.** A failed attempt is recorded truthfully and abandoned. Motion is a
renewable trigger; re-capturing a moment that has passed produces evidence of
nothing.

**Fixed public error messages.** Six categories, each with one constant sentence.
The raw exception is logged once with its traceback and appears nowhere else —
not in the status endpoint, not in an observation, not in a notification. An
exception message is arbitrary application data and may carry a path, a command
line, a username or an environment value.

**Enablement is validated, not silently ignored.** `event_capture.enabled = true`
requires `camera.enabled`, `preview.enabled`, `preview.auto_start`,
`preview.restore_after_capture` and `motion.enabled`. Each is a real dependency
(see `docs/Event-Capture.md`); in particular, without `restore_after_capture` the
*first* automatic capture would permanently remove the preview stream that
produces triggers. Silently accepting such a configuration would leave an
operator believing captures were being taken when nothing would ever take one.

**Shutdown ordering is explicit.** `_shutdown_lifespan` now drains the motion
producer first, then retires the event-capture worker, then drains the remaining
monitors, and only then shuts the camera coordinator down. Queued-but-unstarted
work is discarded; one in-flight capture is allowed to finish. Every existing
cleanup guarantee is preserved: each stage is attempted even if an earlier one
failed, and a cleanup failure never replaces the original startup or serving
exception.

## Database

**No migration.** No new database and no new table. The feature reuses the
existing `captures` table with its `extra_metadata` column and the existing
`observations` table with its `correlation_id` column. Nothing in the required
behaviour needed schema that did not already exist.

## Dependencies

**None added.** Standard library, the existing FastAPI/Pydantic stack and
existing MGO modules only. `pyproject.toml` and `uv.lock` are unchanged.

## Public API

`GET /event-capture/status` is **added**. It is read-only, typed, always HTTP 200
while the application serves, and exposes no filesystem path.

`POST /camera/capture` is **unchanged**: same path, method, response fields,
`capture_id`, filename semantics, `absolute_path`, timestamp representation,
width, height, filesize, backend and failure mappings (503 / 504 / 502 / 500 /
500 / 500). It now runs through the shared workflow and supplies no
`extra_metadata`, so a manual capture never acquires `origin = motion`.

No other endpoint, field, type, unit or status value changed.

## Validation performed

All of the following were run on Windows against this branch, with no Raspberry
Pi involved:

| Check | Result |
| ----- | ------ |
| `uv sync --frozen` | Pass — 36 packages checked, no dependency change |
| `uv run ruff check .` | Pass |
| `uv run mypy src` | Pass — 54 source files |
| Focused event-capture tests | Pass — 44 event-capture, 27 workflow, 21 config, 10 status-endpoint |
| Full suite `uv run pytest` | **2559 passed, 12 skipped, 3 failed** — see below |
| `uv run python scripts/dev/run-mutations.py` | **180/180 detected**, 0 stale, 0 restoration failures, 0 unmatched selectors |
| `git diff --check` | Pass |

The 12 skips are the established POSIX-only ones (symlink creation and mode
bits) and are unchanged from `main`. No new skip was introduced.

The mutation register went from **173** entries to **180**: seven new Task 13.1
entries, no existing entry added, removed or edited.

### Mutation-runner changes

`scripts/dev/run-mutations.py` needed two changes, both reported here because
the file is outside the expected scope list:

1. **A per-mutation suite.** `Mutation` gained a `suite` field defaulting to
   `tests/test_deployment_gateway.py`, and the runner uses it. Without this the
   register could only ever defend shell assets, because the suite was hard-coded.
   Every pre-existing entry keeps the default, so none of them changed.
2. **A bounded retry on every asset write.** On this Windows machine a virus
   scanner or the search indexer intermittently holds a handle to a just-written
   file, and `write_bytes` fails with `PermissionError`. That aborted two runs —
   and, worse, the bare write in the restoring `finally` failed the same way, so
   a run ended with `scripts/deploy/install-mgo-validate.sh` and
   `scripts/deploy/mgo-validate` **left mutated in the working tree**. Both were
   restored from `HEAD` and verified byte-identical before this commit. A run
   that dies is acceptable; a run that dies holding a mutated deployment gateway
   is precisely the silent failure this register exists to prevent, so both the
   mutation write and the restore now retry a transient lock (12 attempts,
   linear backoff) and a genuine permission failure still raises.

Automated tests cover: configuration defaults, backwards compatibility and every
enablement rejection; the shared workflow's call counts, metadata copying,
failure paths and lock behaviour; manual API regression including every HTTP
mapping; trigger admission for all six motion statuses; the non-blocking
callback; queue capacity, coalescing and recovery; a successful automatic capture
end to end against the simulator, with its catalogue metadata and its correlated
observation; every failure category and its sanitisation; a failing failure
observation; the status endpoint's fields and its inertness; motion callback
composition in both failure directions; and shutdown ordering, in-flight
completion, pending discard and cleanup-stage precedence.

### The three full-suite failures

One is a load flake and two pre-date this branch.

`tests/test_operations_deployment.py::test_a_root_caller_validates_a_good_policy`
timed out after 60 seconds inside a `bash` subprocess while the machine was also
running the mutation register. Re-run on its own the module passes in full
(**305 passed in 157 s**). It is a machine-load timeout, not a behaviour change,
and nothing in Task 13.1 touches that code path.

The other two are pre-existing:

- `test_the_task_record_states_the_installation_without_overclaiming`
- `test_the_power_failure_not_the_installation_explains_the_baseline_change`

**They fail identically on a pristine checkout of `9634e84`**, verified in a
separate temporary worktree. Commit `9634e84` ("Clarify Task 12 historical camera
status") rewrote two phrases in
`docs/tasks/Task-012-Physical-Camera-Acceptance.md` that these two tests still
assert verbatim:

- `Physical camera acceptance remains pending` — replaced by dated wording;
- `**Preview is currently stopped because of a power failure, not because of
  the\ninstallation.**` — replaced by
  `**After the 2026-08-04 power failure, preview remained stopped because
  managed ...**`.

That commit's own message records that only targeted tests were run for it.

Task 13.1 **did not** change these tests, that document, or anything they read,
and deliberately does not fix them: they belong to the Task 12 record and
correcting them here would mix an unrelated documentation change into this
branch. They are reported for a separate decision.

## Not performed

- **No Raspberry Pi access.** Nothing was run on, copied to, or read from the
  production host.
- **No production configuration change.** `/etc/garden-observatory/mgo.toml` was
  not read or written by this task.
- **No deployment.** `mgo.service` was not restarted; the deployment gateway,
  its installer and the sudo policy are untouched.
- **No hardware validation of automatic capture.** The feature has never run
  against a physical camera. Enabling it in production is a separate,
  hardware-validated step and must not be inferred from this record.
- **Task 13.2 has not started.**

## Recommended next steps

1. Repository review of this branch.
2. Merge to `main` after review.
3. A separate, explicitly authorised Raspberry Pi validation before
   `event_capture.enabled` is ever set to `true` in production — including the
   disk-headroom question, since **no retention policy exists yet**.
