"""Bounded suppression telemetry and reservation outcomes (Task 14.5A, brief §12).

The worker is driven with a scripted gate whose decisions alternate reasons,
and an injected monotonic clock, so the number of persisted observations is a
function of *time*, never of how many triggers arrived or how the reasons
alternated. The reservation outcome the worker reports on release is checked
for every ending: a committed row is the only success.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from mgo.captures.models import Capture
from mgo.captures.workflow import CapturePublicationRefused
from mgo.event_capture import (
    SUCCESS_STATUS,
    SUPPRESSED_STATUS,
    SUPPRESSION_RECORD_INTERVAL_SECONDS,
    AdmissionDecision,
    EventCaptureRuntimeState,
    EventCaptureService,
    SuppressionReason,
)
from mgo.motion.models import MotionResult, MotionStatus

DATABASE_PATH = Path("unused-by-these-tests.db")
_T0 = datetime(2026, 9, 7, 8, 0, tzinfo=UTC)


class _Monotonic:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _motion(seconds: int = 0) -> MotionResult:
    return MotionResult(
        status=MotionStatus.MOTION_DETECTED,
        detected=True,
        score=0.3,
        threshold=0.08,
        frames_available=True,
        detail="scripted",
        evaluated_at=_T0 + timedelta(seconds=seconds),
    )


def _decision(
    admitted: bool, reason: SuppressionReason | None = None
) -> AdmissionDecision:
    return AdmissionDecision(
        admitted=admitted,
        reason=reason,
        evaluated_at=_T0,
        hourly_count=3,
        hourly_limit=3,
        daily_count=3,
        daily_limit=10,
        storage_free_bytes=0,
        storage_reserve_ok=False,
        minimum_free_bytes=1_000,
        maximum_capture_bytes=2_000,
    )


class _Gate:
    """Returns the scripted decisions in order, then repeats the last."""

    def __init__(self, *decisions: AdmissionDecision) -> None:
        self._script = list(decisions)
        self.maximum_capture_bytes = 2_000
        self.minimum_free_bytes = 1_000
        self.outcomes: list[bool] = []

    def evaluate(self) -> AdmissionDecision:
        return _decision(True)

    def admit(self) -> AdmissionDecision:
        return self._script[0] if len(self._script) == 1 else self._script.pop(0)

    def release(self, *, succeeded: bool) -> None:
        self.outcomes.append(succeeded)


class _Workflow:
    def __init__(
        self, *, error: BaseException | None = None, filesize: int = 1_000
    ) -> None:
        self._error = error
        self._filesize = filesize

    def capture(
        self, *, extra_metadata: Any = None, publication_guard: Any = None
    ) -> Capture:
        if self._error is not None:
            raise self._error
        if publication_guard is not None:
            from types import SimpleNamespace

            publication_guard(SimpleNamespace(filesize_bytes=self._filesize))
        return Capture(
            id=uuid.uuid4(),
            filename="still.jpg",
            absolute_path="/captures/still.jpg",
            captured_at_utc=_T0,
            width=4608,
            height=2592,
            filesize_bytes=1_000,
            camera_backend="simulator",
            created_at_utc=_T0,
        )


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, database_path: Path, **kwargs: Any) -> None:
        self.calls.append(kwargs)

    def by_status(self, status: str) -> list[dict[str, Any]]:
        return [call for call in self.calls if call.get("status") == status]


async def _until(predicate: Any, *, message: str, timeout: float = 10.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        assert loop.time() < deadline, message
        await asyncio.sleep(0.005)


def _service(
    gate: _Gate, workflow: _Workflow, monotonic: _Monotonic
) -> tuple[EventCaptureService, EventCaptureRuntimeState, _Recorder]:
    state = EventCaptureRuntimeState(enabled=True)
    recorder = _Recorder()
    service = EventCaptureService(
        workflow,  # type: ignore[arg-type]
        state,
        DATABASE_PATH,
        admission=gate,  # type: ignore[arg-type]
        recorder=recorder,
        monotonic=monotonic,
    )
    return service, state, recorder


async def _suppress_n(
    service: EventCaptureService,
    state: EventCaptureRuntimeState,
    monotonic: _Monotonic,
    *,
    count: int,
    seconds_between: float,
) -> None:
    for index in range(count):
        assert service.submit(_motion(seconds=index))
        await _until(
            lambda index=index: state.total_triggers_suppressed == index + 1,
            message="not suppressed",
        )
        monotonic.now += seconds_between


ALTERNATING = (
    _decision(False, SuppressionReason.HOURLY_LIMIT),
    _decision(False, SuppressionReason.STORAGE_RESERVE),
)


def _alternating_gate(rounds: int) -> _Gate:
    return _Gate(*(ALTERNATING * rounds))


# --- rate bounding --------------------------------------------------------------------


def test_alternating_reasons_are_persisted_at_a_bounded_rate() -> None:
    """Forty triggers, twenty reason changes, one second apart: ONE row.

    The counters see all forty; the timeline sees the first, and nothing more
    until the interval has passed.
    """

    async def _main() -> tuple[EventCaptureRuntimeState, _Recorder]:
        monotonic = _Monotonic()
        service, state, recorder = _service(
            _alternating_gate(20), _Workflow(), monotonic
        )
        service.start()
        try:
            await _suppress_n(service, state, monotonic, count=40, seconds_between=1.0)
        finally:
            await service.shutdown()
        return state, recorder

    state, recorder = _run(_main())

    assert state.total_triggers_suppressed == 40
    assert state.last_suppression_reason is SuppressionReason.STORAGE_RESERVE
    assert len(recorder.by_status(SUPPRESSED_STATUS)) == 1


def test_the_bound_is_one_row_per_interval_however_reasons_alternate() -> None:
    """Over ten intervals of alternation: at most one row per interval."""

    async def _main() -> tuple[EventCaptureRuntimeState, _Recorder]:
        monotonic = _Monotonic()
        service, state, recorder = _service(
            _alternating_gate(100), _Workflow(), monotonic
        )
        service.start()
        try:
            # 200 triggers, spaced so the run spans exactly ten intervals.
            spacing = (SUPPRESSION_RECORD_INTERVAL_SECONDS * 10) / 200
            await _suppress_n(
                service, state, monotonic, count=200, seconds_between=spacing
            )
        finally:
            await service.shutdown()
        return state, recorder

    state, recorder = _run(_main())

    rows = recorder.by_status(SUPPRESSED_STATUS)
    assert state.total_triggers_suppressed == 200
    assert 1 <= len(rows) <= 11
    reasons = [row["payload"]["reason"] for row in rows]
    assert set(reasons) <= {"hourly_limit", "storage_reserve"}


def test_a_change_of_reason_after_the_interval_is_persisted() -> None:
    async def _main() -> _Recorder:
        monotonic = _Monotonic()
        gate = _Gate(
            _decision(False, SuppressionReason.HOURLY_LIMIT),
            _decision(False, SuppressionReason.STORAGE_RESERVE),
            _decision(False, SuppressionReason.STORAGE_RESERVE),
        )
        service, state, recorder = _service(gate, _Workflow(), monotonic)
        service.start()
        try:
            await _suppress_n(
                service,
                state,
                monotonic,
                count=3,
                seconds_between=SUPPRESSION_RECORD_INTERVAL_SECONDS,
            )
        finally:
            await service.shutdown()
        return recorder

    recorder = _run(_main())

    reasons = [r["payload"]["reason"] for r in recorder.by_status(SUPPRESSED_STATUS)]
    assert reasons == ["hourly_limit", "storage_reserve"]


def test_identical_reasons_write_nothing_even_after_the_interval() -> None:
    async def _main() -> _Recorder:
        monotonic = _Monotonic()
        gate = _Gate(_decision(False, SuppressionReason.HOURLY_LIMIT))
        service, state, recorder = _service(gate, _Workflow(), monotonic)
        service.start()
        try:
            await _suppress_n(
                service,
                state,
                monotonic,
                count=5,
                seconds_between=SUPPRESSION_RECORD_INTERVAL_SECONDS * 2,
            )
        finally:
            await service.shutdown()
        return recorder

    recorder = _run(_main())

    assert len(recorder.by_status(SUPPRESSED_STATUS)) == 1


def test_recovery_to_an_admitted_capture_is_observable() -> None:
    """The captured observation is the recovery signal; the interval does not
    delay it, and a later refusal is news again once the interval allows."""

    async def _main() -> tuple[_Recorder, EventCaptureRuntimeState]:
        monotonic = _Monotonic()
        gate = _Gate(
            _decision(False, SuppressionReason.HOURLY_LIMIT),
            _decision(True),
            _decision(False, SuppressionReason.HOURLY_LIMIT),
            _decision(False, SuppressionReason.HOURLY_LIMIT),
        )
        service, state, recorder = _service(gate, _Workflow(), monotonic)
        service.start()
        try:
            assert service.submit(_motion(0))
            await _until(lambda: state.total_triggers_suppressed == 1, message="1")
            monotonic.now += 5
            assert service.submit(_motion(1))
            await _until(lambda: state.total_captures_succeeded == 1, message="2")
            monotonic.now += 5
            assert service.submit(_motion(2))  # same reason as before; 10 s later
            await _until(lambda: state.total_triggers_suppressed == 2, message="3")
            monotonic.now += SUPPRESSION_RECORD_INTERVAL_SECONDS
            assert service.submit(_motion(3))
            await _until(lambda: state.total_triggers_suppressed == 3, message="4")
        finally:
            await service.shutdown()
        return recorder, state

    recorder, state = _run(_main())

    assert len(recorder.by_status(SUCCESS_STATUS)) == 1
    # First refusal recorded; the post-capture refusal inside the interval was
    # not; the one after the interval was (an admitted capture reset the
    # memory, so the same reason is news again).
    assert len(recorder.by_status(SUPPRESSED_STATUS)) == 2
    assert state.snapshot().admission_state.value == "suppressed"


# --- reservation outcomes -------------------------------------------------------------


def test_a_failed_attempt_keeps_its_reservation() -> None:
    async def _main() -> _Gate:
        gate = _Gate(_decision(True))
        service, state, _ = _service(
            gate, _Workflow(error=RuntimeError("camera")), _Monotonic()
        )
        service.start()
        try:
            assert service.submit(_motion())
            await _until(lambda: state.total_captures_failed == 1, message="no failure")
        finally:
            await service.shutdown()
        return gate

    assert _run(_main()).outcomes == [False]


def test_an_oversize_still_keeps_its_reservation() -> None:
    async def _main() -> _Gate:
        gate = _Gate(_decision(True))
        service, state, _ = _service(gate, _Workflow(filesize=2_001), _Monotonic())
        service.start()
        try:
            assert service.submit(_motion())
            await _until(lambda: state.total_captures_failed == 1, message="no failure")
        finally:
            await service.shutdown()
        return gate

    gate = _run(_main())
    assert gate.outcomes == [False]


def test_only_a_catalogued_capture_releases_with_success() -> None:
    async def _main() -> _Gate:
        gate = _Gate(_decision(True))
        service, state, _ = _service(gate, _Workflow(), _Monotonic())
        service.start()
        try:
            assert service.submit(_motion())
            await _until(
                lambda: state.total_captures_succeeded == 1, message="no capture"
            )
        finally:
            await service.shutdown()
        return gate

    assert _run(_main()).outcomes == [True]


def test_a_refused_publication_is_the_oversize_category() -> None:
    """Sanity: the guard's refusal is classified, not swallowed."""
    caught: list[BaseException] = []

    class _Guarded(_Workflow):
        def capture(
            self, *, extra_metadata: Any = None, publication_guard: Any = None
        ) -> Capture:
            try:
                return super().capture(
                    extra_metadata=extra_metadata, publication_guard=publication_guard
                )
            except CapturePublicationRefused as error:
                caught.append(error)
                raise

    async def _main() -> EventCaptureRuntimeState:
        gate = _Gate(_decision(True))
        service, state, _ = _service(gate, _Guarded(filesize=5_000), _Monotonic())
        service.start()
        try:
            assert service.submit(_motion())
            await _until(lambda: state.total_captures_failed == 1, message="no failure")
        finally:
            await service.shutdown()
        return state

    state = _run(_main())
    assert len(caught) == 1
    assert state.last_suppression_reason is SuppressionReason.OVERSIZE_CAPTURE


def _run(coroutine: Any) -> Any:
    return asyncio.run(coroutine)
