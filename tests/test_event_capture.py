"""Tests for motion-triggered still capture.

These cover the whole of the feature's runtime behaviour without a camera, a
Raspberry Pi or `rpicam-*`: trigger admission, the bounded queue, the single
worker, success and failure paths, the safety of what is published, and
shutdown.

Timing is never the mechanism. Where a test needs "a capture is in progress
right now" it uses an explicit :class:`threading.Event` barrier inside a fake
workflow, so the assertion holds regardless of how the machine schedules.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path
from threading import Event as ThreadEvent
from typing import Any

import pytest

from mgo.camera.exceptions import (
    BackendCaptureError,
    CameraUnavailableError,
    CaptureTimeoutError,
    CaptureWriteError,
    InvalidCameraConfigurationError,
)
from mgo.captures.archive import CaptureArchiveError
from mgo.captures.models import Capture
from mgo.event_capture import (
    FAILURE_STATUS,
    FAILURE_SUMMARY,
    OBSERVATION_KIND,
    OBSERVATION_SOURCE,
    QUEUE_CAPACITY,
    SUCCESS_STATUS,
    SUCCESS_SUMMARY,
    WORKER_TASK_NAME,
    EventCaptureErrorCategory,
    EventCaptureRuntimeState,
    EventCaptureService,
    EventCaptureState,
    MotionTrigger,
    classify_failure,
    safe_error_message,
)
from mgo.motion.models import MotionResult, MotionStatus

DATABASE_PATH = Path("unused-by-these-tests.db")

_EVALUATED_AT = datetime(2026, 8, 7, 9, 30, 15, 250000, tzinfo=UTC)


def _motion(
    status: MotionStatus = MotionStatus.MOTION_DETECTED,
    *,
    score: float = 0.37,
    threshold: float = 0.08,
    evaluated_at: datetime = _EVALUATED_AT,
) -> MotionResult:
    """Build a motion result of the requested status."""
    return MotionResult(
        status=status,
        detected=status is MotionStatus.MOTION_DETECTED,
        score=score,
        threshold=threshold,
        frames_available=status
        not in (MotionStatus.DISABLED, MotionStatus.WAITING_FOR_FRAMES),
        detail=f"Motion result for {status.value}.",
        evaluated_at=evaluated_at,
    )


def _capture(filename: str = "2026-08-07T09-30-15.250000Z.jpg") -> Capture:
    """Build a catalogue record that no camera produced."""
    return Capture(
        id=uuid.uuid4(),
        filename=filename,
        absolute_path=f"/var/lib/garden-observatory/media/captures/{filename}",
        captured_at_utc=datetime(2026, 8, 7, 9, 30, 16, tzinfo=UTC),
        width=4608,
        height=2592,
        filesize_bytes=2048,
        camera_backend="simulator",
        created_at_utc=datetime(2026, 8, 7, 9, 30, 17, tzinfo=UTC),
    )


class _FakeWorkflow:
    """A capture workflow double with an optional in-capture barrier."""

    def __init__(
        self,
        *,
        capture: Capture | None = None,
        errors: list[BaseException | None] | None = None,
        entered: ThreadEvent | None = None,
        release: ThreadEvent | None = None,
    ) -> None:
        self._capture = capture if capture is not None else _capture()
        self._errors = errors or []
        self._entered = entered
        self._release = release
        self.calls = 0
        self.metadata: list[dict[str, Any] | None] = []

    def capture(
        self, *, extra_metadata: dict[str, Any] | None = None
    ) -> Capture:
        self.calls += 1
        self.metadata.append(extra_metadata)
        if self._entered is not None:
            self._entered.set()
        if self._release is not None:
            # Bounded so a defect fails the test instead of hanging the suite.
            assert self._release.wait(timeout=10.0), "capture was never released"
        index = self.calls - 1
        if index < len(self._errors):
            error = self._errors[index]
            if error is not None:
                raise error
        return self._capture


class _Recorder:
    """An observation recorder double that records, and can fail."""

    def __init__(self, *, error: BaseException | None = None) -> None:
        self._error = error
        self.calls: list[dict[str, Any]] = []

    def __call__(self, database_path: Path, **kwargs: Any) -> Any:
        self.calls.append({"database_path": database_path, **kwargs})
        if self._error is not None:
            raise self._error
        return None

    def by_status(self, status: str) -> list[dict[str, Any]]:
        return [call for call in self.calls if call.get("status") == status]


def _service(
    workflow: Any,
    *,
    recorder: Any = None,
    state: EventCaptureRuntimeState | None = None,
) -> tuple[EventCaptureService, EventCaptureRuntimeState, _Recorder]:
    """Build an enabled service over doubles."""
    runtime_state = state or EventCaptureRuntimeState(enabled=True)
    observation_recorder = recorder if recorder is not None else _Recorder()
    service = EventCaptureService(
        workflow,
        runtime_state,
        DATABASE_PATH,
        recorder=observation_recorder,
    )
    return service, runtime_state, observation_recorder


async def _settle(cycles: int = 40) -> None:
    """Give the worker task every scheduling opportunity it could need."""
    for _ in range(cycles):
        await asyncio.sleep(0)


async def _drain(service: EventCaptureService) -> None:
    """Let the worker finish whatever it is doing, bounded."""
    for _ in range(200):
        await asyncio.sleep(0.005)
        if service.status().state is not EventCaptureState.CAPTURING:
            return
    raise AssertionError("the worker never left the capturing state")


# --- trigger admission -------------------------------------------------------


@pytest.mark.parametrize(
    "status",
    [
        MotionStatus.DISABLED,
        MotionStatus.WAITING_FOR_FRAMES,
        MotionStatus.ESTABLISHING_BASELINE,
        MotionStatus.NO_MOTION,
        MotionStatus.ERROR,
    ],
)
def test_only_motion_detected_is_admitted(status: MotionStatus) -> None:
    """Every other motion status is ignored, and is not a received trigger."""

    async def _main() -> None:
        workflow = _FakeWorkflow()
        service, state, recorder = _service(workflow)
        service.start()
        try:
            assert service.submit(_motion(status)) is False
            await _settle()
        finally:
            await service.shutdown()

        assert workflow.calls == 0
        assert state.total_triggers_received == 0
        assert state.total_triggers_dropped == 0
        assert state.last_trigger_at is None
        assert recorder.calls == []

    asyncio.run(_main())


def test_motion_detected_is_admitted() -> None:
    """A material motion transition is accepted and counted."""

    async def _main() -> None:
        service, state, _ = _service(_FakeWorkflow())
        service.start()
        try:
            assert service.submit(_motion()) is True
            assert state.total_triggers_received == 1
            assert state.last_trigger_at == _EVALUATED_AT
            await _settle()
        finally:
            await service.shutdown()

    asyncio.run(_main())


def test_a_trigger_is_refused_before_the_worker_starts() -> None:
    """Nothing is queued for a worker that does not exist."""

    async def _main() -> None:
        workflow = _FakeWorkflow()
        service, state, _ = _service(workflow)

        assert service.submit(_motion()) is False
        await _settle()

        assert workflow.calls == 0
        assert state.total_triggers_received == 0

    asyncio.run(_main())


def test_submission_performs_no_capture_or_database_work() -> None:
    """The motion callback must return before anything expensive happens.

    ``submit`` is called on the motion monitor's own analysis cycle. If it did
    camera or SQLite work there, a slow capture would slow motion detection --
    the very signal the capture depends on.
    """

    async def _main() -> None:
        workflow = _FakeWorkflow()
        service, _, recorder = _service(workflow)
        service.start()
        try:
            service.submit(_motion())
            # Deliberately *before* yielding to the loop: this is the state of
            # the world at the instant the callback returned.
            assert workflow.calls == 0
            assert recorder.calls == []
            await _settle()
        finally:
            await service.shutdown()

    asyncio.run(_main())


def test_no_task_is_created_per_transition() -> None:
    """There is one worker, not one task per motion event."""

    async def _main() -> None:
        release = ThreadEvent()
        entered = ThreadEvent()
        workflow = _FakeWorkflow(entered=entered, release=release)
        service, _, _ = _service(workflow)
        service.start()
        try:
            before = _worker_tasks()
            for _ in range(5):
                service.submit(_motion())
                await _settle(5)
            assert _worker_tasks() == before == 1
        finally:
            release.set()
            await service.shutdown()

    asyncio.run(_main())


def _worker_tasks() -> int:
    """Count live event-capture worker tasks on the running loop."""
    return sum(
        1
        for task in asyncio.all_tasks()
        if task.get_name() == WORKER_TASK_NAME and not task.done()
    )


# --- the bounded queue -------------------------------------------------------


def test_the_queue_capacity_is_exactly_one() -> None:
    """The pending capacity is a fixed one -- not configurable, not larger."""
    assert QUEUE_CAPACITY == 1


def test_one_trigger_may_wait_while_another_is_captured() -> None:
    """A trigger arriving during a capture is queued rather than lost."""

    async def _main() -> None:
        entered = ThreadEvent()
        release = ThreadEvent()
        workflow = _FakeWorkflow(entered=entered, release=release)
        service, state, _ = _service(workflow)
        service.start()
        try:
            service.submit(_motion())
            await _await_entered(entered)

            # One is executing; exactly one more fits.
            assert service.submit(_motion()) is True
            assert state.total_triggers_dropped == 0
            assert state.total_triggers_received == 2
        finally:
            release.set()
            await service.shutdown()

    asyncio.run(_main())


def test_a_third_trigger_is_dropped_not_queued() -> None:
    """A windy tree cannot build a backlog behind a busy camera."""

    async def _main() -> None:
        entered = ThreadEvent()
        release = ThreadEvent()
        workflow = _FakeWorkflow(entered=entered, release=release)
        service, state, recorder = _service(workflow)
        service.start()
        try:
            service.submit(_motion())
            await _await_entered(entered)
            service.submit(_motion())

            assert service.submit(_motion()) is False
            assert service.submit(_motion()) is False

            assert state.total_triggers_dropped == 2
            # A dropped trigger is still a trigger that was received.
            assert state.total_triggers_received == 4
            # And it is emphatically not a row in the observation timeline.
            assert recorder.calls == []
        finally:
            release.set()
            await service.shutdown()

    asyncio.run(_main())


def test_a_dropped_trigger_never_blocks_the_caller() -> None:
    """Submission returns immediately even with the queue full."""

    async def _main() -> None:
        entered = ThreadEvent()
        release = ThreadEvent()
        workflow = _FakeWorkflow(entered=entered, release=release)
        service, _, _ = _service(workflow)
        service.start()
        try:
            service.submit(_motion())
            await _await_entered(entered)
            service.submit(_motion())

            loop = asyncio.get_running_loop()
            started = loop.time()
            for _ in range(50):
                service.submit(_motion())
            elapsed = loop.time() - started

            # Fifty refused submissions are bookkeeping, not waiting. The bound
            # is generous; a blocking implementation would sit on the barrier
            # above until the ten-second capture timeout.
            assert elapsed < 1.0
        finally:
            release.set()
            await service.shutdown()

    asyncio.run(_main())


def test_the_queue_accepts_work_again_after_a_capture_finishes() -> None:
    """A full queue is a moment, not a state the feature gets stuck in."""

    async def _main() -> None:
        entered = ThreadEvent()
        release = ThreadEvent()
        workflow = _FakeWorkflow(entered=entered, release=release)
        service, state, _ = _service(workflow)
        service.start()
        try:
            service.submit(_motion())
            await _await_entered(entered)
            service.submit(_motion())
            assert service.submit(_motion()) is False

            release.set()
            await _drain(service)
            await _settle()

            assert service.submit(_motion()) is True
            assert state.total_triggers_dropped == 1
        finally:
            release.set()
            await service.shutdown()

    asyncio.run(_main())


async def _await_entered(entered: ThreadEvent, timeout: float = 10.0) -> None:
    """Wait until the fake workflow reports it is inside a capture."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not entered.is_set():
        assert loop.time() < deadline, "the worker never started a capture"
        await asyncio.sleep(0.005)


# --- a successful automatic capture -----------------------------------------


def test_a_successful_automatic_capture_records_everything() -> None:
    """One trigger, one capture, one archive record, one observation."""

    async def _main() -> dict[str, Any]:
        record = _capture()
        workflow = _FakeWorkflow(capture=record)
        service, state, recorder = _service(workflow)
        service.start()
        try:
            service.submit(_motion())
            await _drain(service)
            await _settle()
        finally:
            await service.shutdown()
        return {
            "workflow": workflow,
            "state": state,
            "recorder": recorder,
            "record": record,
        }

    observed = asyncio.run(_main())
    workflow: _FakeWorkflow = observed["workflow"]
    state: EventCaptureRuntimeState = observed["state"]
    recorder: _Recorder = observed["recorder"]
    record: Capture = observed["record"]

    # The capture ran exactly once, through the shared workflow.
    assert workflow.calls == 1
    assert workflow.metadata == [
        {
            "origin": "motion",
            "motion_status": "motion_detected",
            "motion_score": 0.37,
            "motion_threshold": 0.08,
            "motion_evaluated_at": _EVALUATED_AT.isoformat(),
        }
    ]

    # Exactly one observation, and it is the success one.
    assert len(recorder.calls) == 1
    observation = recorder.calls[0]
    assert observation["kind"] == OBSERVATION_KIND == "event_capture"
    assert observation["source"] == OBSERVATION_SOURCE == "mgo-event-capture"
    assert observation["status"] == SUCCESS_STATUS == "captured"
    assert observation["summary"] == SUCCESS_SUMMARY
    assert observation["summary"] == "Motion-triggered still captured"
    # The correlation identifier is the capture that really was persisted.
    assert observation["correlation_id"] == str(record.id)
    payload = observation["payload"]
    assert payload["capture_id"] == str(record.id)
    assert payload["motion_score"] == 0.37
    assert payload["motion_threshold"] == 0.08
    assert payload["motion_evaluated_at"] == _EVALUATED_AT.isoformat()
    assert payload["filename"] == record.filename

    # Counters and state settled truthfully.
    assert state.total_captures_succeeded == 1
    assert state.total_captures_failed == 0
    assert state.last_capture_id == str(record.id)
    assert state.last_capture_at == record.captured_at_utc
    assert state.state is EventCaptureState.IDLE
    assert state.last_error is None


def test_a_success_observation_exposes_no_path() -> None:
    """The timeline records what happened, never where the application lives."""

    async def _main() -> _Recorder:
        record = _capture()
        service, _, recorder = _service(_FakeWorkflow(capture=record))
        service.start()
        try:
            service.submit(_motion())
            await _drain(service)
            await _settle()
        finally:
            await service.shutdown()
        return recorder

    recorder = asyncio.run(_main())
    payload = recorder.calls[0]["payload"]

    assert "absolute_path" not in payload
    assert "capture_directory" not in payload
    assert "database_path" not in payload
    rendered = repr(payload)
    assert "/var/lib" not in rendered
    assert "captures/" not in rendered


def test_a_success_observation_failure_does_not_rewrite_the_outcome() -> None:
    """A capture that happened is not reported as one that did not."""

    async def _main() -> EventCaptureRuntimeState:
        recorder = _Recorder(error=RuntimeError("timeline write failed"))
        service, state, _ = _service(_FakeWorkflow(), recorder=recorder)
        service.start()
        try:
            service.submit(_motion())
            await _drain(service)
            await _settle()
        finally:
            await service.shutdown()
        return state

    state = asyncio.run(_main())

    assert state.total_captures_succeeded == 1
    assert state.total_captures_failed == 0
    assert state.state is EventCaptureState.IDLE
    assert state.last_error is None


# --- failure categories ------------------------------------------------------


_FAILURES = [
    (
        CameraUnavailableError("no cameras available"),
        "Camera unavailable for motion-triggered capture.",
        "camera_unavailable",
    ),
    (
        CaptureTimeoutError("rpicam-still timed out after 30s"),
        "Motion-triggered capture timed out.",
        "capture_timeout",
    ),
    (
        BackendCaptureError("rpicam-still exited with code 1"),
        "Motion-triggered camera backend failed.",
        "backend_failure",
    ),
    (
        CaptureWriteError("could not create /var/lib/garden-observatory/media"),
        "Motion-triggered capture could not be written.",
        "write_failure",
    ),
    (
        CaptureArchiveError("database is locked at /var/lib/x/db/mgo.db"),
        "Capture completed but metadata could not be archived.",
        "archive_failure",
    ),
    (
        ZeroDivisionError("division by zero in some defect"),
        "Motion-triggered capture failed unexpectedly.",
        "unexpected",
    ),
]


@pytest.mark.parametrize(("error", "message", "category"), _FAILURES)
def test_a_failed_capture_is_recorded_safely(
    error: BaseException, message: str, category: str
) -> None:
    """Each failure category reports its fixed public sentence, and only that."""

    async def _main() -> dict[str, Any]:
        workflow = _FakeWorkflow(errors=[error])
        service, state, recorder = _service(workflow)
        service.start()
        try:
            service.submit(_motion())
            await _drain(service)
            await _settle()
        finally:
            await service.shutdown()
        return {"state": state, "recorder": recorder}

    observed = asyncio.run(_main())
    state: EventCaptureRuntimeState = observed["state"]
    recorder: _Recorder = observed["recorder"]

    assert state.total_captures_failed == 1
    assert state.total_captures_succeeded == 0
    assert state.state is EventCaptureState.ERROR
    assert state.last_error == message
    assert state.last_capture_id is None

    # The raw exception is nowhere in what the application publishes.
    raw = str(error)
    snapshot = repr(state.snapshot().as_dict())
    assert raw not in snapshot
    assert repr(error) not in snapshot
    assert "Traceback" not in snapshot

    assert len(recorder.calls) == 1
    observation = recorder.calls[0]
    assert observation["kind"] == OBSERVATION_KIND
    assert observation["source"] == OBSERVATION_SOURCE
    assert observation["status"] == FAILURE_STATUS == "failed"
    assert observation["summary"] == FAILURE_SUMMARY
    assert observation["summary"] == "Motion-triggered still capture failed"
    # No capture exists, so nothing is correlated to one.
    assert observation.get("correlation_id") is None

    payload = observation["payload"]
    assert payload["error_category"] == category
    assert payload["motion_score"] == 0.37
    assert payload["motion_threshold"] == 0.08
    assert payload["motion_evaluated_at"] == _EVALUATED_AT.isoformat()
    rendered = repr(payload)
    assert raw not in rendered
    assert repr(error) not in rendered
    assert "Traceback" not in rendered
    assert "/var/lib" not in rendered
    assert "rpicam" not in rendered


def test_an_archive_failure_emits_no_success_observation() -> None:
    """A capture that could not be catalogued is not a captured observation."""

    async def _main() -> _Recorder:
        workflow = _FakeWorkflow(errors=[CaptureArchiveError("disk full")])
        service, _, recorder = _service(workflow)
        service.start()
        try:
            service.submit(_motion())
            await _drain(service)
            await _settle()
        finally:
            await service.shutdown()
        return recorder

    recorder = asyncio.run(_main())

    assert recorder.by_status(SUCCESS_STATUS) == []
    assert len(recorder.by_status(FAILURE_STATUS)) == 1


def test_the_unclassified_camera_error_is_unexpected_not_a_guess() -> None:
    """A camera-domain error outside the four named kinds is not relabelled."""
    assert classify_failure(InvalidCameraConfigurationError("bad")) is (
        EventCaptureErrorCategory.UNEXPECTED
    )


def test_every_category_has_a_bounded_safe_message() -> None:
    """No category can publish an unbounded string."""
    for category in EventCaptureErrorCategory:
        message = safe_error_message(category)
        assert message
        assert len(message) <= 200
        assert "\n" not in message


# --- recovery ----------------------------------------------------------------


def test_the_worker_survives_a_failure_and_captures_again() -> None:
    """One bad capture never ends automatic capture."""

    async def _main() -> dict[str, Any]:
        workflow = _FakeWorkflow(
            errors=[BackendCaptureError("rpicam-still exited with code 1")]
        )
        service, state, recorder = _service(workflow)
        service.start()
        try:
            service.submit(_motion())
            await _drain(service)
            await _settle()
            first = state.snapshot()

            service.submit(_motion())
            await _drain(service)
            await _settle()
        finally:
            await service.shutdown()
        return {
            "first": first,
            "state": state,
            "workflow": workflow,
            "recorder": recorder,
        }

    observed = asyncio.run(_main())
    first = observed["first"]
    state: EventCaptureRuntimeState = observed["state"]
    workflow: _FakeWorkflow = observed["workflow"]

    assert first.state is EventCaptureState.ERROR
    assert first.last_error == "Motion-triggered camera backend failed."

    # The second attempt really ran, and cleared the error.
    assert workflow.calls == 2
    assert state.total_captures_failed == 1
    assert state.total_captures_succeeded == 1
    assert state.state is EventCaptureState.IDLE
    assert state.last_error is None
    assert state.last_capture_id is not None


def test_there_is_no_retry(caplog: pytest.LogCaptureFixture) -> None:
    """A failed attempt is abandoned; the next transition is the next chance."""

    async def _main() -> _FakeWorkflow:
        workflow = _FakeWorkflow(errors=[CaptureTimeoutError("timed out")])
        service, _, _ = _service(workflow)
        service.start()
        try:
            service.submit(_motion())
            await _drain(service)
            # Generous: any retry loop would have several chances here.
            for _ in range(20):
                await asyncio.sleep(0.01)
        finally:
            await service.shutdown()
        return workflow

    with caplog.at_level(logging.ERROR):
        workflow = asyncio.run(_main())

    assert workflow.calls == 1


# --- the failure observation failing -----------------------------------------


def test_a_failing_failure_observation_keeps_the_original_truth(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A database that cannot record the failure does not become the failure."""

    async def _main() -> dict[str, Any]:
        recorder = _Recorder(error=RuntimeError("observations table is gone"))
        workflow = _FakeWorkflow(
            errors=[CameraUnavailableError("camera is disabled"), None]
        )
        service, state, _ = _service(workflow, recorder=recorder)
        service.start()
        try:
            service.submit(_motion())
            await _drain(service)
            await _settle()
            after_failure = state.snapshot()

            # A later valid trigger still works.
            service.submit(_motion())
            await _drain(service)
            await _settle()
        finally:
            await service.shutdown()
        return {
            "after_failure": after_failure,
            "state": state,
            "recorder": recorder,
            "workflow": workflow,
        }

    with caplog.at_level(logging.ERROR):
        observed = asyncio.run(_main())

    after_failure = observed["after_failure"]
    state: EventCaptureRuntimeState = observed["state"]
    recorder: _Recorder = observed["recorder"]

    # The event-capture failure -- not the database error -- is what is reported.
    assert after_failure.state is EventCaptureState.ERROR
    assert after_failure.last_error == (
        "Camera unavailable for motion-triggered capture."
    )
    assert "observations table is gone" not in repr(after_failure.as_dict())

    # Exactly two recorder attempts: one failure observation, one success
    # observation. No recursive "the failure observation failed" attempt.
    assert len(recorder.calls) == 2
    assert [call["status"] for call in recorder.calls] == [
        FAILURE_STATUS,
        SUCCESS_STATUS,
    ]

    # The worker lived, and the later capture succeeded.
    assert state.total_captures_failed == 1
    assert state.total_captures_succeeded == 1

    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "failure observation could not be recorded" in logged


# --- runtime state and counters ---------------------------------------------


def test_a_disabled_holder_reports_a_disabled_feature() -> None:
    """No worker exists, and every lifetime field is empty."""
    snapshot = EventCaptureRuntimeState(enabled=False).snapshot()

    assert snapshot.enabled is False
    assert snapshot.state is EventCaptureState.DISABLED
    assert snapshot.pending_triggers == 0
    assert snapshot.total_triggers_received == 0
    assert snapshot.total_captures_succeeded == 0
    assert snapshot.total_captures_failed == 0
    assert snapshot.total_triggers_dropped == 0
    assert snapshot.last_trigger_at is None
    assert snapshot.last_capture_id is None
    assert snapshot.last_capture_at is None
    assert snapshot.last_error is None


def test_an_enabled_holder_opens_idle() -> None:
    """Enabled and doing nothing is ``idle``, never ``disabled``."""
    snapshot = EventCaptureRuntimeState(enabled=True).snapshot()

    assert snapshot.enabled is True
    assert snapshot.state is EventCaptureState.IDLE


def test_the_capturing_state_is_visible_while_a_capture_runs() -> None:
    """An operator can see that the camera is busy on motion's behalf."""

    async def _main() -> dict[str, Any]:
        entered = ThreadEvent()
        release = ThreadEvent()
        workflow = _FakeWorkflow(entered=entered, release=release)
        service, _, _ = _service(workflow)
        service.start()
        try:
            service.submit(_motion())
            await _await_entered(entered)
            during = service.status()
        finally:
            release.set()
            await service.shutdown()
        return {"during": during}

    during = asyncio.run(_main())["during"]

    assert during.state is EventCaptureState.CAPTURING
    assert during.enabled is True


def test_a_snapshot_is_an_independent_copy() -> None:
    """A held snapshot never changes under the caller."""
    state = EventCaptureRuntimeState(enabled=True)
    snapshot = state.snapshot()

    state.total_captures_succeeded = 7
    state.state = EventCaptureState.ERROR

    assert snapshot.total_captures_succeeded == 0
    assert snapshot.state is EventCaptureState.IDLE


def test_the_status_dictionary_renders_utc_iso_timestamps() -> None:
    """Timestamps match the convention every other MGO endpoint uses."""
    state = EventCaptureRuntimeState(enabled=True)
    state.last_trigger_at = _EVALUATED_AT
    state.last_capture_at = _EVALUATED_AT

    rendered = state.snapshot().as_dict()

    assert rendered["last_trigger_at"] == "2026-08-07T09:30:15.250000+00:00"
    assert rendered["last_capture_at"] == "2026-08-07T09:30:15.250000+00:00"


# --- the trigger value object ------------------------------------------------


def test_a_trigger_copies_the_facts_and_holds_nothing_else() -> None:
    """The trigger carries attribution, never buffers or paths."""
    result = _motion()
    trigger = MotionTrigger.from_motion_result(result)

    assert trigger.status is MotionStatus.MOTION_DETECTED
    assert trigger.score == result.score
    assert trigger.threshold == result.threshold
    assert trigger.evaluated_at == result.evaluated_at

    # Exactly four fields, all of them plain values.
    fields = set(vars(trigger))
    assert fields == {"status", "score", "threshold", "evaluated_at"}
    for value in vars(trigger).values():
        assert isinstance(value, str | float | int | datetime)


def test_a_trigger_is_immutable() -> None:
    """A queued trigger cannot be rewritten while it waits."""
    trigger = MotionTrigger.from_motion_result(_motion())

    with pytest.raises(AttributeError):
        trigger.score = 0.99  # type: ignore[misc]


def test_capture_metadata_does_not_duplicate_the_capture_path() -> None:
    """The catalogue record already carries the path; metadata does not."""
    metadata = MotionTrigger.from_motion_result(_motion()).capture_metadata()

    assert set(metadata) == {
        "origin",
        "motion_status",
        "motion_score",
        "motion_threshold",
        "motion_evaluated_at",
    }
    assert metadata["origin"] == "motion"


# --- shutdown ----------------------------------------------------------------


def test_shutdown_refuses_new_triggers() -> None:
    """Once shutdown has begun, no new automatic capture may be requested."""

    async def _main() -> dict[str, Any]:
        workflow = _FakeWorkflow()
        service, state, _ = _service(workflow)
        service.start()
        await service.shutdown()

        accepted = service.submit(_motion())
        await _settle()
        return {"accepted": accepted, "workflow": workflow, "state": state}

    observed = asyncio.run(_main())

    assert observed["accepted"] is False
    assert observed["workflow"].calls == 0
    assert observed["state"].total_triggers_received == 0


def test_a_pending_trigger_is_discarded_and_never_starts() -> None:
    """Queued-but-unstarted work does not begin during shutdown."""

    async def _main() -> dict[str, Any]:
        entered = ThreadEvent()
        release = ThreadEvent()
        workflow = _FakeWorkflow(entered=entered, release=release)
        service, state, _ = _service(workflow)
        service.start()

        service.submit(_motion())
        await _await_entered(entered)
        # The second one is queued behind the capture in flight.
        assert service.submit(_motion()) is True

        shutdown = asyncio.ensure_future(service.shutdown())
        await _settle()
        # Only now is the in-flight capture allowed to complete.
        release.set()
        await shutdown

        return {"workflow": workflow, "state": state}

    observed = asyncio.run(_main())

    # One capture ran: the one already in flight. The pending one never started.
    assert observed["workflow"].calls == 1
    assert observed["state"].pending_triggers == 0


def test_an_in_flight_capture_is_allowed_to_finish() -> None:
    """A capture that owns the camera is never abandoned mid-transaction."""

    async def _main() -> dict[str, Any]:
        entered = ThreadEvent()
        release = ThreadEvent()
        workflow = _FakeWorkflow(entered=entered, release=release)
        service, state, recorder = _service(workflow)
        service.start()

        service.submit(_motion())
        await _await_entered(entered)

        shutdown = asyncio.ensure_future(service.shutdown())
        await _settle()
        # Shutdown is genuinely waiting for the capture, not past it.
        assert shutdown.done() is False

        release.set()
        await shutdown
        return {"state": state, "recorder": recorder}

    observed = asyncio.run(_main())
    state: EventCaptureRuntimeState = observed["state"]
    recorder: _Recorder = observed["recorder"]

    # The capture completed normally: counted, archived, observed.
    assert state.total_captures_succeeded == 1
    assert state.state is EventCaptureState.IDLE
    assert len(recorder.by_status(SUCCESS_STATUS)) == 1


def test_the_worker_reaches_a_terminal_state() -> None:
    """No event-capture task survives shutdown."""

    async def _main() -> dict[str, Any]:
        service, _, _ = _service(_FakeWorkflow())
        service.start()
        assert _worker_tasks() == 1
        await service.shutdown()
        return {"live": _worker_tasks()}

    assert asyncio.run(_main())["live"] == 0


def test_shutdown_is_idempotent_and_safe_before_start() -> None:
    """Cleanup never depends on how far startup got."""

    async def _main() -> None:
        service, _, _ = _service(_FakeWorkflow())
        # Never started.
        await service.shutdown()
        service.start()
        await service.shutdown()
        await service.shutdown()

    asyncio.run(_main())


def test_starting_twice_is_refused() -> None:
    """There is one worker, and the application owns it."""

    async def _main() -> None:
        service, _, _ = _service(_FakeWorkflow())
        service.start()
        try:
            with pytest.raises(RuntimeError):
                service.start()
        finally:
            await service.shutdown()

    asyncio.run(_main())


def test_starting_the_worker_captures_nothing() -> None:
    """A worker coming up must never take a picture by itself."""

    async def _main() -> _FakeWorkflow:
        workflow = _FakeWorkflow()
        service, _, _ = _service(workflow)
        service.start()
        await _settle()
        await service.shutdown()
        return workflow

    assert asyncio.run(_main()).calls == 0
