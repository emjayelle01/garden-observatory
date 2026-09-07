"""Integration tests for admission inside the event-capture worker (Task 14.5).

The worker is driven with a scripted admission gate and a fake workflow, so
every assertion is about *sequencing and publication*: the gate is asked before
the camera, a refused trigger never reaches the workflow, a reservation is
released whatever the outcome, an oversize still is withdrawn, identical
suppressions are counted but not persisted, and a whole-frame change is never a
trigger. Timing is never the mechanism; barriers and bounded polls are.
"""

from __future__ import annotations

import asyncio
import threading
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from mgo.captures.models import Capture
from mgo.captures.workflow import CapturePublicationRefused
from mgo.event_capture import (
    SUPPRESSED_STATUS,
    SUPPRESSED_SUMMARY,
    AdmissionDecision,
    AdmissionState,
    EventCaptureErrorCategory,
    EventCaptureRuntimeState,
    EventCaptureService,
    EventCaptureState,
    SuppressionReason,
    safe_error_message,
)
from mgo.motion.models import MotionResult, MotionStatus

DATABASE_PATH = Path("unused-by-these-tests.db")
_T0 = datetime(2026, 9, 7, 8, 0, tzinfo=UTC)


def _motion(
    status: MotionStatus = MotionStatus.MOTION_DETECTED, *, seconds: int = 0
) -> MotionResult:
    return MotionResult(
        status=status,
        detected=status is MotionStatus.MOTION_DETECTED,
        score=0.3,
        threshold=0.08,
        frames_available=True,
        detail="scripted",
        evaluated_at=_T0 + timedelta(seconds=seconds),
    )


def _capture() -> Capture:
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


def _decision(
    admitted: bool,
    reason: SuppressionReason | None = None,
    *,
    hourly: int = 0,
) -> AdmissionDecision:
    return AdmissionDecision(
        admitted=admitted,
        reason=reason,
        evaluated_at=_T0,
        hourly_count=hourly,
        hourly_limit=3,
        daily_count=hourly,
        daily_limit=10,
        storage_free_bytes=5_000,
        storage_reserve_ok=True,
        minimum_free_bytes=1_000,
        maximum_capture_bytes=2_000,
    )


class _ScriptedGate:
    """Admission double: returns decisions in order, then repeats the last."""

    def __init__(self, *decisions: AdmissionDecision | Exception) -> None:
        self._script = list(decisions)
        self.maximum_capture_bytes = 2_000
        self.minimum_free_bytes = 1_000
        self.in_flight = 0
        self.admit_calls = 0
        self.evaluate_calls = 0
        self.releases = 0
        self.events: list[str] = []

    def _next(self) -> AdmissionDecision:
        item = self._script[0] if len(self._script) == 1 else self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def evaluate(self) -> AdmissionDecision:
        self.evaluate_calls += 1
        return _decision(True)

    def admit(self) -> AdmissionDecision:
        self.admit_calls += 1
        self.events.append("admit")
        decision = self._next()
        if decision.admitted:
            self.in_flight += 1
        return decision

    def release(self) -> None:
        self.releases += 1
        self.in_flight -= 1
        self.events.append("release")


class _Workflow:
    def __init__(
        self,
        *,
        gate: _ScriptedGate | None = None,
        error: BaseException | None = None,
        filesize: int = 1_000,
    ) -> None:
        self.calls = 0
        self.guards: list[Any] = []
        self._gate = gate
        self._error = error
        self._filesize = filesize

    def capture(
        self, *, extra_metadata: Any = None, publication_guard: Any = None
    ) -> Capture:
        self.calls += 1
        self.guards.append(publication_guard)
        if self._gate is not None:
            self._gate.events.append("capture")
        if self._error is not None:
            raise self._error
        capture = _capture()
        if publication_guard is not None:
            # Behave like the real workflow: the guard sees the result's size.
            from types import SimpleNamespace

            publication_guard(SimpleNamespace(filesize_bytes=self._filesize))
        return capture


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
    gate: _ScriptedGate, workflow: _Workflow
) -> tuple[EventCaptureService, EventCaptureRuntimeState, _Recorder]:
    state = EventCaptureRuntimeState(enabled=True)
    recorder = _Recorder()
    service = EventCaptureService(
        workflow, state, DATABASE_PATH, admission=gate, recorder=recorder
    )
    return service, state, recorder


def _run(coroutine: Any) -> Any:
    return asyncio.run(coroutine)


# --- sequencing -------------------------------------------------------------------


def test_admission_is_asked_before_the_camera_and_released_after() -> None:
    async def _main() -> _ScriptedGate:
        gate = _ScriptedGate(_decision(True))
        workflow = _Workflow(gate=gate)
        service, state, _ = _service(gate, workflow)
        service.start()
        try:
            assert service.submit(_motion())
            await _until(
                lambda: state.total_captures_succeeded == 1, message="no capture"
            )
        finally:
            await service.shutdown()
        return gate

    gate = _run(_main())

    assert gate.events == ["admit", "capture", "release"]
    assert gate.in_flight == 0


def test_a_refused_trigger_never_reaches_the_workflow() -> None:
    async def _main() -> tuple[_Workflow, EventCaptureRuntimeState, _Recorder]:
        gate = _ScriptedGate(_decision(False, SuppressionReason.HOURLY_LIMIT, hourly=3))
        workflow = _Workflow(gate=gate)
        service, state, recorder = _service(gate, workflow)
        service.start()
        try:
            assert service.submit(_motion())
            await _until(
                lambda: state.total_triggers_suppressed == 1, message="not suppressed"
            )
        finally:
            await service.shutdown()
        return workflow, state, recorder

    workflow, state, _ = _run(_main())

    assert workflow.calls == 0
    assert state.state is EventCaptureState.IDLE
    assert state.total_captures_failed == 0
    assert state.last_suppression_reason is SuppressionReason.HOURLY_LIMIT
    snapshot = state.snapshot()
    assert snapshot.admission_state is AdmissionState.SUPPRESSED
    assert snapshot.hourly_count == 3
    assert snapshot.hourly_remaining == 0
    assert snapshot.worker_busy is False


def test_the_reservation_is_released_when_the_capture_fails() -> None:
    async def _main() -> tuple[_ScriptedGate, EventCaptureRuntimeState]:
        gate = _ScriptedGate(_decision(True))
        workflow = _Workflow(gate=gate, error=RuntimeError("camera exploded"))
        service, state, _ = _service(gate, workflow)
        service.start()
        try:
            assert service.submit(_motion())
            await _until(lambda: state.total_captures_failed == 1, message="no failure")
        finally:
            await service.shutdown()
        return gate, state

    gate, state = _run(_main())

    assert gate.events == ["admit", "capture", "release"]
    assert gate.in_flight == 0
    assert state.state is EventCaptureState.ERROR


def test_an_admission_error_suppresses_and_captures_nothing() -> None:
    async def _main() -> tuple[_Workflow, EventCaptureRuntimeState, _Recorder]:
        gate = _ScriptedGate(RuntimeError("catalogue unavailable"))
        workflow = _Workflow(gate=gate)
        service, state, recorder = _service(gate, workflow)
        service.start()
        try:
            assert service.submit(_motion())
            await _until(
                lambda: state.total_triggers_suppressed == 1, message="not suppressed"
            )
        finally:
            await service.shutdown()
        return workflow, state, recorder

    workflow, state, recorder = _run(_main())

    assert workflow.calls == 0
    assert state.last_suppression_reason is SuppressionReason.ADMISSION_ERROR
    assert recorder.by_status(SUPPRESSED_STATUS)[0]["payload"]["reason"] == (
        "admission_error"
    )


# --- oversize publication ------------------------------------------------------------


def test_an_oversize_still_is_a_failure_and_a_suppression() -> None:
    async def _main() -> tuple[EventCaptureRuntimeState, _Recorder, _ScriptedGate]:
        gate = _ScriptedGate(_decision(True))
        workflow = _Workflow(gate=gate, filesize=2_001)
        service, state, recorder = _service(gate, workflow)
        service.start()
        try:
            assert service.submit(_motion())
            await _until(lambda: state.total_captures_failed == 1, message="no failure")
        finally:
            await service.shutdown()
        return state, recorder, gate

    state, recorder, gate = _run(_main())

    assert state.last_error == safe_error_message(
        EventCaptureErrorCategory.OVERSIZE_CAPTURE
    )
    assert state.last_suppression_reason is SuppressionReason.OVERSIZE_CAPTURE
    assert state.total_captures_succeeded == 0
    failure = recorder.by_status("failed")[0]
    assert failure["payload"]["error_category"] == "oversize_capture"
    assert gate.in_flight == 0


def test_a_still_exactly_at_the_reservation_is_published() -> None:
    async def _main() -> EventCaptureRuntimeState:
        gate = _ScriptedGate(_decision(True))
        workflow = _Workflow(gate=gate, filesize=2_000)
        service, state, _ = _service(gate, workflow)
        service.start()
        try:
            assert service.submit(_motion())
            await _until(
                lambda: state.total_captures_succeeded == 1, message="no capture"
            )
        finally:
            await service.shutdown()
        return state

    state = _run(_main())

    assert state.total_captures_failed == 0
    assert state.last_admitted_at == _T0


def test_the_guard_refuses_only_above_the_reservation() -> None:
    gate = _ScriptedGate(_decision(True))
    service, _, _ = _service(gate, _Workflow())
    from types import SimpleNamespace

    service._refuse_oversize(SimpleNamespace(filesize_bytes=2_000))
    try:
        service._refuse_oversize(SimpleNamespace(filesize_bytes=2_001))
    except CapturePublicationRefused:
        pass
    else:  # pragma: no cover
        raise AssertionError("an oversize still must be refused")


# --- suppression persistence ---------------------------------------------------------


def test_identical_suppressions_are_counted_but_recorded_once() -> None:
    async def _main() -> tuple[EventCaptureRuntimeState, _Recorder]:
        gate = _ScriptedGate(_decision(False, SuppressionReason.STORAGE_RESERVE))
        service, state, recorder = _service(gate, _Workflow(gate=gate))
        service.start()
        try:
            for index in range(4):
                assert service.submit(_motion(seconds=index * 10))
                await _until(
                    lambda index=index: state.total_triggers_suppressed == index + 1,
                    message="not suppressed",
                )
        finally:
            await service.shutdown()
        return state, recorder

    state, recorder = _run(_main())

    assert state.total_triggers_suppressed == 4
    suppressed = recorder.by_status(SUPPRESSED_STATUS)
    assert len(suppressed) == 1
    assert suppressed[0]["summary"] == SUPPRESSED_SUMMARY
    assert suppressed[0]["payload"]["reason"] == "storage_reserve"
    assert "storage_reserve_ok" in suppressed[0]["payload"]
    assert "correlation_id" not in suppressed[0]


def test_a_change_of_reason_is_recorded_again() -> None:
    async def _main() -> _Recorder:
        gate = _ScriptedGate(
            _decision(False, SuppressionReason.HOURLY_LIMIT),
            _decision(False, SuppressionReason.HOURLY_LIMIT),
            _decision(False, SuppressionReason.STORAGE_RESERVE),
        )
        service, state, recorder = _service(gate, _Workflow(gate=gate))
        service.start()
        try:
            for index in range(3):
                assert service.submit(_motion(seconds=index * 10))
                await _until(
                    lambda index=index: state.total_triggers_suppressed == index + 1,
                    message="not suppressed",
                )
        finally:
            await service.shutdown()
        return recorder

    recorder = _run(_main())

    reasons = [c["payload"]["reason"] for c in recorder.by_status(SUPPRESSED_STATUS)]
    assert reasons == ["hourly_limit", "storage_reserve"]


def test_an_admitted_capture_resets_the_suppression_run() -> None:
    async def _main() -> _Recorder:
        gate = _ScriptedGate(
            _decision(False, SuppressionReason.HOURLY_LIMIT),
            _decision(True),
            _decision(False, SuppressionReason.HOURLY_LIMIT),
        )
        service, state, recorder = _service(gate, _Workflow(gate=gate))
        service.start()
        try:
            assert service.submit(_motion(seconds=0))
            await _until(lambda: state.total_triggers_suppressed == 1, message="1")
            assert service.submit(_motion(seconds=10))
            await _until(lambda: state.total_captures_succeeded == 1, message="2")
            assert service.submit(_motion(seconds=20))
            await _until(lambda: state.total_triggers_suppressed == 2, message="3")
        finally:
            await service.shutdown()
        return recorder

    recorder = _run(_main())

    assert len(recorder.by_status(SUPPRESSED_STATUS)) == 2


# --- whole-frame changes -------------------------------------------------------------


def test_a_global_change_is_not_a_trigger() -> None:
    gate = _ScriptedGate(_decision(True))
    workflow = _Workflow(gate=gate)
    service, state, recorder = _service(gate, workflow)

    async def _main() -> None:
        service.start()
        try:
            assert service.submit(_motion(MotionStatus.GLOBAL_CHANGE)) is False
            await asyncio.sleep(0.05)
        finally:
            await service.shutdown()

    _run(_main())

    assert workflow.calls == 0
    assert gate.admit_calls == 0
    assert state.total_triggers_received == 0
    assert state.total_global_scene_changes == 1
    assert state.total_triggers_suppressed == 1
    assert state.last_suppression_reason is SuppressionReason.GLOBAL_SCENE_CHANGE
    assert recorder.calls == []  # no database write for a whole-frame change


# --- status facts -------------------------------------------------------------


def test_refresh_admission_publishes_without_reserving() -> None:
    gate = _ScriptedGate(_decision(True))
    service, state, _ = _service(gate, _Workflow())

    decision = service.refresh_admission()

    assert decision.admitted is True
    assert gate.evaluate_calls == 1
    assert gate.admit_calls == 0
    assert state.snapshot().admission_state is AdmissionState.OPEN


def test_the_snapshot_is_unknown_until_the_gate_has_spoken() -> None:
    state = EventCaptureRuntimeState(enabled=True)
    assert state.snapshot().admission_state is AdmissionState.UNKNOWN
    assert state.snapshot().hourly_limit is None


def test_a_disabled_holder_reports_disabled_admission() -> None:
    snapshot = EventCaptureRuntimeState(enabled=False).snapshot()
    assert snapshot.admission_state is AdmissionState.DISABLED
    assert snapshot.total_triggers_suppressed == 0
    assert snapshot.as_dict()["admission_state"] == "disabled"


def test_worker_busy_is_derived_from_the_capturing_state() -> None:
    entered = threading.Event()
    release = threading.Event()

    class _Blocking(_Workflow):
        def capture(
            self, *, extra_metadata: Any = None, publication_guard: Any = None
        ) -> Capture:
            entered.set()
            assert release.wait(timeout=10.0)
            return super().capture(
                extra_metadata=extra_metadata, publication_guard=publication_guard
            )

    async def _main() -> tuple[bool, bool]:
        gate = _ScriptedGate(_decision(True))
        service, state, _ = _service(gate, _Blocking())
        service.start()
        try:
            assert service.submit(_motion())
            await asyncio.to_thread(entered.wait, 10.0)
            busy = state.snapshot().worker_busy
            release.set()
            await _until(lambda: state.total_captures_succeeded == 1, message="done")
            idle = state.snapshot().worker_busy
        finally:
            release.set()
            await service.shutdown()
        return busy, idle

    busy, idle = _run(_main())

    assert busy is True
    assert idle is False
