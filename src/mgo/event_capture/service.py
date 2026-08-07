"""The motion-triggered still-capture service and its single worker.

This is the whole of the feature's behaviour: admit a material
``motion_detected`` transition, hand it to one background worker through a
bounded queue, and let that worker run the shared
:class:`~mgo.captures.workflow.CaptureWorkflow` once.

The shape is chosen for a garden, not for a benchmark. The production camera
watches four feeders on a tree in the open: wind, leaves, shadows and birds all
produce motion, and a still capture temporarily takes the camera away from
preview. An unbounded queue -- or a task per transition -- would let a windy
afternoon build a backlog of captures for movement that finished minutes ago,
each one stealing the camera again. So:

* **One worker.** Never two captures at once, and never a second camera owner.
* **One pending slot.** One capture may execute while at most one further
  trigger waits; anything beyond that is dropped and counted. The next real
  motion transition is the next capture opportunity.
* **A non-blocking submission.** :meth:`EventCaptureService.submit` runs on the
  motion monitor's own cycle. It performs no camera work, no database work and
  no waiting of any kind -- it copies four values, tries one bounded enqueue and
  returns.
* **No retry.** A failed attempt is recorded truthfully and abandoned. Motion is
  a renewable trigger; retrying a capture of a moment that has passed produces
  evidence of nothing.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from pathlib import Path

from mgo.camera.exceptions import (
    BackendCaptureError,
    CameraUnavailableError,
    CaptureTimeoutError,
    CaptureWriteError,
)
from mgo.captures.archive import CaptureArchiveError
from mgo.captures.models import Capture
from mgo.captures.workflow import CaptureWorkflow
from mgo.core.observations import Observation, record_observation
from mgo.event_capture.models import (
    EventCaptureErrorCategory,
    EventCaptureRuntimeState,
    EventCaptureState,
    EventCaptureStatus,
    MotionTrigger,
    safe_error_message,
)
from mgo.motion.models import MotionResult, MotionStatus

LOGGER = logging.getLogger(__name__)

ObservationRecorder = Callable[..., Observation]

#: The observation vocabulary this feature owns. One kind, one source, two
#: statuses -- reusing the existing immutable timeline rather than inventing an
#: event table for it.
OBSERVATION_KIND = "event_capture"
OBSERVATION_SOURCE = "mgo-event-capture"
SUCCESS_STATUS = "captured"
SUCCESS_SUMMARY = "Motion-triggered still captured"
FAILURE_STATUS = "failed"
FAILURE_SUMMARY = "Motion-triggered still capture failed"

#: The worker's asyncio task name, in the same ``mgo-`` family as every other
#: application-owned task so shutdown auditing can see it.
WORKER_TASK_NAME = "mgo-event-capture-worker"

#: Exactly one trigger may wait while one is executing. Not configurable: see
#: the module docstring -- a larger queue would be a backlog of stale moments,
#: and a smaller one would drop the trigger that arrives during a capture.
QUEUE_CAPACITY = 1

#: Ordered because the hierarchy is: every entry below is a
#: :class:`CameraCaptureError`, so the specific classes must be tested before
#: any base class would swallow them.
_CAMERA_ERROR_CATEGORIES: tuple[
    tuple[type[BaseException], EventCaptureErrorCategory], ...
] = (
    (CameraUnavailableError, EventCaptureErrorCategory.CAMERA_UNAVAILABLE),
    (CaptureTimeoutError, EventCaptureErrorCategory.CAPTURE_TIMEOUT),
    (BackendCaptureError, EventCaptureErrorCategory.BACKEND_FAILURE),
    (CaptureWriteError, EventCaptureErrorCategory.WRITE_FAILURE),
    (CaptureArchiveError, EventCaptureErrorCategory.ARCHIVE_FAILURE),
)


class _Stop:
    """The shutdown sentinel placed on the queue to retire the worker.

    A sentinel rather than a second wait target: the worker then has exactly one
    thing to await, so "the worker woke up" and "the worker has work" cannot
    disagree, and a shutdown cannot be missed while a capture is in flight.
    """


def classify_failure(error: BaseException) -> EventCaptureErrorCategory:
    """Map a capture-attempt failure to one of the public categories.

    Anything outside the known camera and archive domains -- including a
    camera-domain error that is not one of the four specific kinds -- is
    ``UNEXPECTED``. That is the honest answer: the five named categories each
    describe a *recognised* operational condition, and calling something a
    backend failure because it was raised nearby would put a guess into an
    operator's status endpoint.
    """
    for error_type, category in _CAMERA_ERROR_CATEGORIES:
        if isinstance(error, error_type):
            return category
    return EventCaptureErrorCategory.UNEXPECTED


class EventCaptureService:
    """Owns the trigger queue, the single worker and the runtime state.

    The service is created only when the feature is enabled. Everything it
    touches is injected -- the shared capture workflow, the runtime-state
    holder, the database path and the observation recorder -- so it can be
    exercised completely without a camera, a Raspberry Pi or a real database.
    """

    def __init__(
        self,
        workflow: CaptureWorkflow,
        state: EventCaptureRuntimeState,
        database_path: Path,
        *,
        recorder: ObservationRecorder = record_observation,
    ) -> None:
        self._workflow = workflow
        self._state = state
        self._database_path = database_path
        self._recorder = recorder
        self._queue: asyncio.Queue[MotionTrigger | _Stop] = asyncio.Queue(
            maxsize=QUEUE_CAPACITY
        )
        self._accepting = False
        self._task: asyncio.Task[None] | None = None

    # -- public API --------------------------------------------------------

    def status(self) -> EventCaptureStatus:
        """Return an immutable snapshot of the runtime state."""
        return self._state.snapshot()

    def start(self) -> None:
        """Create the single worker task and begin accepting triggers.

        Called from the application lifespan *before* the motion monitor, so a
        trigger can never arrive before something is there to receive it.
        Starting the worker performs no camera work and captures nothing: it
        only parks the worker on an empty queue.
        """
        if self._task is not None:
            raise RuntimeError("The event-capture worker is already running")
        self._accepting = True
        self._state.state = EventCaptureState.IDLE
        self._task = asyncio.create_task(self._run(), name=WORKER_TASK_NAME)
        LOGGER.info("Motion-triggered capture started; awaiting triggers")

    def submit(self, result: MotionResult) -> bool:
        """Offer a material motion transition to the capture queue.

        Returns whether the trigger was queued. Never blocks, never raises,
        never touches the camera, the archive or the database, and never creates
        a task: this runs inside the motion monitor's analysis cycle, and the
        motion subsystem's timing is not this feature's to spend.

        Only :attr:`~mgo.motion.models.MotionStatus.MOTION_DETECTED` is
        admitted. Every other status -- ``disabled``, ``waiting_for_frames``,
        ``establishing_baseline``, ``no_motion``, ``error`` -- is ignored and is
        *not* counted as a received trigger, because it never was one. The
        upstream motion monitor's material-transition and cooldown rules decide
        which transitions reach here at all; this service adds no second
        cooldown of its own.
        """
        if result.status is not MotionStatus.MOTION_DETECTED:
            return False
        if not self._accepting:
            # Shutdown has begun (or the worker was never started). Refusing is
            # the whole point: no new automatic capture may start from here on.
            LOGGER.info(
                "Motion trigger ignored; event capture is no longer accepting "
                "work"
            )
            return False

        trigger = MotionTrigger.from_motion_result(result)
        self._state.total_triggers_received += 1
        self._state.last_trigger_at = trigger.evaluated_at
        try:
            self._queue.put_nowait(trigger)
        except asyncio.QueueFull:
            # One capture is executing and one is already waiting. Dropping is
            # correct, not a degradation: the waiting trigger will capture the
            # same continuing activity, and the next transition is the next
            # opportunity. Deliberately no observation -- a windy tree must not
            # be able to flood the timeline with rows about work not done.
            self._state.total_triggers_dropped += 1
            LOGGER.warning(
                "Motion trigger dropped; a motion-triggered capture is already "
                "running with one queued (dropped=%d)",
                self._state.total_triggers_dropped,
            )
            return False

        self._state.pending_triggers = self._queue.qsize()
        return True

    async def shutdown(self) -> None:
        """Stop accepting work, discard the queue and await the worker.

        The order is the guarantee. Submission is closed first, so nothing can
        be added behind the drain; the queue is then emptied, so a trigger that
        had not started never starts; and only then is the sentinel queued and
        the worker awaited. A capture already in flight is allowed to finish --
        it owns the camera, and abandoning it mid-transaction would leave a
        partial file and an unrestored preview.

        Idempotent, and safe to call when the worker was never started.
        """
        self._accepting = False
        if self._task is None:
            return

        discarded = 0
        while True:
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            discarded += 1
        self._state.pending_triggers = 0
        if discarded:
            LOGGER.info(
                "Discarded %d queued motion trigger(s) that had not started",
                discarded,
            )

        # The queue was just emptied and nothing may add to it, so this cannot
        # block: the sentinel always fits.
        await self._queue.put(_Stop())
        try:
            await self._task
        finally:
            self._task = None
        LOGGER.info("Motion-triggered capture stopped")

    # -- the worker --------------------------------------------------------

    async def _run(self) -> None:
        """Process one trigger at a time until the stop sentinel arrives.

        Waits on the queue rather than polling, so an idle worker costs nothing.
        Every per-trigger failure is contained by :meth:`_execute`, so the loop
        cannot be ended by a bad capture -- only by shutdown.
        """
        while True:
            item = await self._queue.get()
            self._state.pending_triggers = self._queue.qsize()
            if isinstance(item, _Stop):
                return
            await self._execute(item)

    async def _execute(self, trigger: MotionTrigger) -> None:
        """Run one automatic capture attempt. Never raises.

        The blocking capture-and-archive workflow runs in a worker thread, so
        the event loop -- and therefore the motion monitor, the API and every
        other monitor -- keeps running while the camera is busy.
        """
        self._state.state = EventCaptureState.CAPTURING
        try:
            capture = await asyncio.to_thread(
                self._workflow.capture,
                extra_metadata=trigger.capture_metadata(),
            )
        except Exception as error:
            self._handle_failure(trigger, error)
            return
        self._handle_success(trigger, capture)

    def _handle_success(self, trigger: MotionTrigger, capture: Capture) -> None:
        """Record a successful automatic capture and return to idle."""
        self._state.total_captures_succeeded += 1
        self._state.last_capture_id = str(capture.id)
        self._state.last_capture_at = capture.captured_at_utc
        self._state.last_error = None
        self._state.state = EventCaptureState.IDLE
        LOGGER.info(
            "Motion-triggered capture %s archived as %s",
            capture.filename,
            capture.id,
        )

        payload = trigger.observation_payload()
        payload["capture_id"] = str(capture.id)
        payload["filename"] = capture.filename
        try:
            self._recorder(
                self._database_path,
                kind=OBSERVATION_KIND,
                source=OBSERVATION_SOURCE,
                status=SUCCESS_STATUS,
                summary=SUCCESS_SUMMARY,
                payload=payload,
                # The capture UUID is what ties this timeline entry to the
                # catalogue record, so it is the correlation identifier and is
                # only ever the id of a capture that really was persisted.
                correlation_id=str(capture.id),
            )
        except Exception:
            # The capture happened and is catalogued; that is the durable
            # record. A timeline write that fails afterwards is a telemetry
            # failure, not a capture failure, so it is logged in full and the
            # truthful capture outcome is left standing.
            LOGGER.exception(
                "Motion-triggered capture %s was archived but its observation "
                "could not be recorded",
                capture.id,
            )

    def _handle_failure(
        self, trigger: MotionTrigger, error: BaseException
    ) -> None:
        """Record a failed automatic capture attempt, safely."""
        category = classify_failure(error)
        message = safe_error_message(category)
        self._state.total_captures_failed += 1
        self._state.last_error = message
        self._state.state = EventCaptureState.ERROR
        # The raw exception is logged with its traceback here and *only* here.
        # It is arbitrary application data -- it may carry a path, a command
        # line, a username or an environment value -- so nothing derived from it
        # reaches the status endpoint or the observation below.
        LOGGER.error(
            "Motion-triggered capture failed (category=%s)",
            category.value,
            exc_info=error,
        )

        payload = trigger.observation_payload()
        payload["error_category"] = category.value
        try:
            self._recorder(
                self._database_path,
                kind=OBSERVATION_KIND,
                source=OBSERVATION_SOURCE,
                status=FAILURE_STATUS,
                summary=FAILURE_SUMMARY,
                payload=payload,
                # Deliberately no correlation id: nothing was archived, and
                # inventing one would attach this failure to a capture that does
                # not exist.
            )
        except Exception:
            # Logged and dropped. There is deliberately no second attempt and no
            # failure-observation-for-the-failure-observation: a database that
            # cannot take this row will not take that one either, and the
            # capture failure already recorded in the runtime state must not be
            # overwritten by a database error message.
            LOGGER.exception(
                "The event-capture failure observation could not be recorded"
            )


__all__ = [
    "FAILURE_STATUS",
    "FAILURE_SUMMARY",
    "OBSERVATION_KIND",
    "OBSERVATION_SOURCE",
    "QUEUE_CAPACITY",
    "SUCCESS_STATUS",
    "SUCCESS_SUMMARY",
    "WORKER_TASK_NAME",
    "EventCaptureService",
    "classify_failure",
]
