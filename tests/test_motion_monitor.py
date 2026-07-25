"""Tests for the motion monitor: baseline logic, persistence and the run loop.

No Raspberry Pi hardware is required. A deterministic fake detector and scripted
in-memory frame sources drive every case, and observation persistence is
captured by a fake recorder so no database is touched.
"""

from __future__ import annotations

import asyncio
import dataclasses
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from mgo.core.config import MGOConfig, MotionConfig, load_config
from mgo.motion.detector import AnalysisFrame, FrameDecodeError
from mgo.motion.frame_source import MockMotionFrameSource
from mgo.motion.models import MotionResult, MotionStatus
from mgo.motion.monitor import (
    MotionState,
    _MotionEvaluator,
    _MotionObserver,
    run_motion_monitor,
)

_T0 = datetime(2026, 7, 25, 12, 0, 0, tzinfo=UTC)


# --- fakes ----------------------------------------------------------------


class _FakeDetector:
    """A deterministic detector keyed off the raw frame bytes.

    ``b"BAD"`` raises a decode error and ``b"BOOM"`` an unexpected error;
    otherwise a frame decodes to a 4-byte luma (its first 4 bytes, zero-padded).
    The score is the fraction of the four luma bytes that differ, and motion is
    a score strictly above ``motion_threshold``.
    """

    def __init__(self, motion_threshold: float = 0.5) -> None:
        self._threshold = motion_threshold

    def decode(self, frame: bytes) -> AnalysisFrame:
        if frame == b"BAD":
            raise FrameDecodeError("bad frame")
        if frame == b"BOOM":
            raise RuntimeError("unexpected detector fault")
        luma = (frame + b"\x00\x00\x00\x00")[:4]
        return AnalysisFrame(width=2, height=2, luma=luma)

    def score(self, baseline: AnalysisFrame, current: AnalysisFrame) -> float:
        changed = sum(
            1 for a, b in zip(baseline.luma, current.luma, strict=True) if a != b
        )
        return changed / 4

    def is_motion(self, score: float) -> bool:
        return score > self._threshold


class _Recorder:
    """A fake observation recorder capturing every persisted observation."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self,
        database_path: Path,
        *,
        kind: str,
        source: str,
        status: str,
        summary: str,
        payload: dict[str, Any] | None = None,
        **_: Any,
    ) -> None:
        self.calls.append(
            {
                "kind": kind,
                "source": source,
                "status": status,
                "summary": summary,
                "payload": payload,
            }
        )

    @property
    def statuses(self) -> list[str]:
        return [call["status"] for call in self.calls]


def _motion_config(
    *,
    enabled: bool = True,
    interval: float = 0.01,
    cooldown: float = 0.0,
    refresh: float = 10_000.0,
    threshold: float = 0.02,
) -> MotionConfig:
    return MotionConfig(
        enabled=enabled,
        analysis_interval_seconds=interval,
        analysis_width=2,
        analysis_height=2,
        pixel_difference_threshold=20,
        changed_pixel_ratio_threshold=threshold,
        baseline_refresh_seconds=refresh,
        cooldown_seconds=cooldown,
    )


def _mgo_config(motion: MotionConfig) -> MGOConfig:
    """Return a full config with a customised motion section."""
    return dataclasses.replace(load_config(), motion=motion)


# --- evaluator ------------------------------------------------------------


def test_evaluator_first_frame_establishes_baseline() -> None:
    evaluator = _MotionEvaluator(_motion_config(), _FakeDetector())

    result = evaluator.evaluate(b"AAAA", _T0)

    assert result.status is MotionStatus.ESTABLISHING_BASELINE
    assert result.detected is False
    assert result.frames_available is True
    assert result.score == 0.0


def test_evaluator_identical_frame_reports_no_motion() -> None:
    evaluator = _MotionEvaluator(_motion_config(), _FakeDetector())
    evaluator.evaluate(b"AAAA", _T0)

    result = evaluator.evaluate(b"AAAA", _T0 + timedelta(seconds=1))

    assert result.status is MotionStatus.NO_MOTION
    assert result.detected is False
    assert result.score == 0.0


def test_evaluator_changed_frame_reports_motion() -> None:
    evaluator = _MotionEvaluator(_motion_config(), _FakeDetector())
    evaluator.evaluate(b"AAAA", _T0)

    result = evaluator.evaluate(b"ZZZZ", _T0 + timedelta(seconds=1))

    assert result.status is MotionStatus.MOTION_DETECTED
    assert result.detected is True
    assert result.score == 1.0


def test_evaluator_no_frame_resets_baseline_to_waiting() -> None:
    evaluator = _MotionEvaluator(_motion_config(), _FakeDetector())
    evaluator.evaluate(b"AAAA", _T0)

    waiting = evaluator.on_no_frame(_T0 + timedelta(seconds=1))
    # After a reset the next frame re-establishes the baseline (not no_motion).
    reestablished = evaluator.evaluate(b"AAAA", _T0 + timedelta(seconds=2))

    assert waiting.status is MotionStatus.WAITING_FOR_FRAMES
    assert waiting.frames_available is False
    assert reestablished.status is MotionStatus.ESTABLISHING_BASELINE


def test_evaluator_decode_error_reports_error_and_keeps_baseline() -> None:
    evaluator = _MotionEvaluator(_motion_config(), _FakeDetector())
    evaluator.evaluate(b"AAAA", _T0)

    error = evaluator.evaluate(b"BAD", _T0 + timedelta(seconds=1))
    # The baseline survives a bad frame, so a following good identical frame is
    # a normal comparison (no_motion), not a fresh baseline.
    recovered = evaluator.evaluate(b"AAAA", _T0 + timedelta(seconds=2))

    assert error.status is MotionStatus.ERROR
    assert error.detected is False
    assert error.frames_available is True
    assert recovered.status is MotionStatus.NO_MOTION


def test_evaluator_unexpected_detector_fault_reports_error() -> None:
    evaluator = _MotionEvaluator(_motion_config(), _FakeDetector())
    evaluator.evaluate(b"AAAA", _T0)

    result = evaluator.evaluate(b"BOOM", _T0 + timedelta(seconds=1))

    assert result.status is MotionStatus.ERROR


def test_evaluator_refreshes_stale_quiet_baseline() -> None:
    """A quiet scene that drifts slowly refreshes the baseline over time."""
    evaluator = _MotionEvaluator(
        _motion_config(refresh=10.0), _FakeDetector(motion_threshold=0.5)
    )
    evaluator.evaluate(b"AAAA", _T0)
    # A 2-of-4 change (score 0.5) is not motion; after the refresh window it is
    # adopted as the new baseline.
    evaluator.evaluate(b"AABB", _T0 + timedelta(seconds=20))
    # Compared with the refreshed baseline (AABB) this is a 2-of-4 change again
    # (no motion); against the original baseline (AAAA) it would be a full
    # change and read as motion.
    result = evaluator.evaluate(b"BBBB", _T0 + timedelta(seconds=20))

    assert result.status is MotionStatus.NO_MOTION


def test_evaluator_does_not_refresh_baseline_before_window() -> None:
    """Without enough elapsed time the baseline is not refreshed."""
    evaluator = _MotionEvaluator(
        _motion_config(refresh=10_000.0), _FakeDetector(motion_threshold=0.5)
    )
    evaluator.evaluate(b"AAAA", _T0)
    evaluator.evaluate(b"AABB", _T0 + timedelta(seconds=1))
    # Baseline is still AAAA, so a full change reads as motion.
    result = evaluator.evaluate(b"BBBB", _T0 + timedelta(seconds=2))

    assert result.status is MotionStatus.MOTION_DETECTED


# --- observer (persistence + cooldown) ------------------------------------


def _result(status: MotionStatus, now: datetime) -> MotionResult:
    return MotionResult(
        status=status,
        detected=status is MotionStatus.MOTION_DETECTED,
        score=1.0 if status is MotionStatus.MOTION_DETECTED else 0.0,
        threshold=0.02,
        frames_available=status
        not in (MotionStatus.DISABLED, MotionStatus.WAITING_FOR_FRAMES),
        detail="detail",
        evaluated_at=now,
    )


def test_observer_persists_only_material_transitions() -> None:
    recorder = _Recorder()
    observer = _MotionObserver(
        _mgo_config(_motion_config(cooldown=0.0)), recorder=recorder
    )

    assert observer.record(_result(MotionStatus.NO_MOTION, _T0), _T0) is True
    # Same status again is not a transition.
    assert (
        observer.record(
            _result(MotionStatus.NO_MOTION, _T0 + timedelta(seconds=1)),
            _T0 + timedelta(seconds=1),
        )
        is False
    )
    assert (
        observer.record(
            _result(MotionStatus.MOTION_DETECTED, _T0 + timedelta(seconds=2)),
            _T0 + timedelta(seconds=2),
        )
        is True
    )
    assert recorder.statuses == ["no_motion", "motion_detected"]


def test_observer_suppresses_continuous_motion() -> None:
    recorder = _Recorder()
    observer = _MotionObserver(
        _mgo_config(_motion_config(cooldown=0.0)), recorder=recorder
    )

    for offset in range(5):
        now = _T0 + timedelta(seconds=offset)
        observer.record(_result(MotionStatus.MOTION_DETECTED, now), now)

    assert recorder.statuses == ["motion_detected"]


def test_observer_cooldown_suppresses_repeat_but_not_return_to_no_motion() -> None:
    recorder = _Recorder()
    observer = _MotionObserver(
        _mgo_config(_motion_config(cooldown=5.0)), recorder=recorder
    )

    def rec(status: MotionStatus, offset: float) -> None:
        now = _T0 + timedelta(seconds=offset)
        observer.record(_result(status, now), now)

    rec(MotionStatus.MOTION_DETECTED, 0)  # recorded
    rec(MotionStatus.NO_MOTION, 1)  # return to no-motion is always recorded
    rec(MotionStatus.MOTION_DETECTED, 2)  # within cooldown of t0 -> suppressed
    rec(MotionStatus.NO_MOTION, 3)  # no unrecorded motion to close -> suppressed
    rec(MotionStatus.MOTION_DETECTED, 10)  # cooldown elapsed -> recorded

    assert recorder.statuses == [
        "motion_detected",
        "no_motion",
        "motion_detected",
    ]


def test_observer_payload_is_bounded_and_has_no_frame_bytes() -> None:
    recorder = _Recorder()
    observer = _MotionObserver(
        _mgo_config(_motion_config()), recorder=recorder
    )

    observer.record(_result(MotionStatus.MOTION_DETECTED, _T0), _T0)

    call = recorder.calls[0]
    assert call["kind"] == "motion_status"
    assert call["source"] == "mgo-motion"
    payload = call["payload"]
    assert set(payload) == {
        "status",
        "detected",
        "score",
        "threshold",
        "frames_available",
        "detail",
        "evaluated_at",
    }
    assert not any(isinstance(value, bytes) for value in payload.values())


# --- run loop -------------------------------------------------------------


async def _drive(
    frames: list[bytes | None],
    *,
    exhausted: bytes | None,
    config: MGOConfig,
    detector: _FakeDetector,
    recorder: _Recorder,
) -> tuple[MotionState, MockMotionFrameSource]:
    """Run the monitor over a scripted source until every frame is consumed.

    ``exhausted`` is returned by the source after the scripted frames run out; it
    keeps the monitor in a stable final status so extra cycles neither change the
    outcome nor add observations, making assertions deterministic.
    """
    stop = asyncio.Event()
    source = MockMotionFrameSource(frames, exhausted=exhausted)
    state = MotionState()
    task = asyncio.create_task(
        run_motion_monitor(
            config, state, source, detector, stop, recorder=recorder
        )
    )
    while source.read_count < len(frames):
        await asyncio.sleep(0.005)
    # Let the final scripted frame's analysis settle before stopping.
    await asyncio.sleep(0.03)
    stop.set()
    await asyncio.wait_for(task, timeout=2.0)
    return state, source


def test_disabled_monitor_never_consumes_frames() -> None:
    recorder = _Recorder()
    source = MockMotionFrameSource([b"AAAA"], exhausted=b"AAAA")
    state = MotionState()
    config = _mgo_config(_motion_config(enabled=False))

    asyncio.run(
        run_motion_monitor(
            config, state, source, _FakeDetector(), asyncio.Event(),
            recorder=recorder,
        )
    )

    assert source.read_count == 0
    assert recorder.calls == []
    latest = state.get()
    assert latest is not None
    assert latest.status is MotionStatus.DISABLED


def test_monitor_establishes_baseline_then_reports_motion() -> None:
    recorder = _Recorder()
    config = _mgo_config(_motion_config())

    state, source = asyncio.run(
        _drive(
            [b"AAAA", b"AAAA", b"ZZZZ"],
            exhausted=b"ZZZZ",
            config=config,
            detector=_FakeDetector(),
            recorder=recorder,
        )
    )

    latest = state.get()
    assert latest is not None
    assert latest.status is MotionStatus.MOTION_DETECTED
    assert recorder.statuses == [
        "establishing_baseline",
        "no_motion",
        "motion_detected",
    ]
    assert source.closed is True


def test_monitor_continuous_motion_does_not_flood_observations() -> None:
    recorder = _Recorder()
    config = _mgo_config(_motion_config())

    asyncio.run(
        _drive(
            [b"AAAA", b"ZZZZ", b"ZZZZ", b"ZZZZ"],
            exhausted=b"ZZZZ",
            config=config,
            detector=_FakeDetector(),
            recorder=recorder,
        )
    )

    assert recorder.statuses.count("motion_detected") == 1


def test_monitor_recovers_after_detector_failure() -> None:
    recorder = _Recorder()
    config = _mgo_config(_motion_config())

    state, _ = asyncio.run(
        _drive(
            [b"AAAA", b"BAD", b"AAAA"],
            exhausted=b"AAAA",
            config=config,
            detector=_FakeDetector(),
            recorder=recorder,
        )
    )

    latest = state.get()
    assert latest is not None
    # The bad frame surfaced an error, then a valid frame recovered the monitor.
    assert "error" in recorder.statuses
    assert latest.status is MotionStatus.NO_MOTION


def test_monitor_waits_truthfully_when_no_frames() -> None:
    recorder = _Recorder()
    config = _mgo_config(_motion_config())

    state, source = asyncio.run(
        _drive(
            [None, None],
            exhausted=None,
            config=config,
            detector=_FakeDetector(),
            recorder=recorder,
        )
    )

    latest = state.get()
    assert latest is not None
    assert latest.status is MotionStatus.WAITING_FOR_FRAMES
    assert source.closed is True


def test_monitor_shuts_down_cleanly_while_running() -> None:
    recorder = _Recorder()
    config = _mgo_config(_motion_config())

    async def _run() -> bool:
        stop = asyncio.Event()
        # An endless source of identical frames keeps the monitor busy.
        source = MockMotionFrameSource([], exhausted=b"AAAA")
        state = MotionState()
        task = asyncio.create_task(
            run_motion_monitor(
                config, state, source, _FakeDetector(), stop, recorder=recorder
            )
        )
        await asyncio.sleep(0.05)
        stop.set()
        await asyncio.wait_for(task, timeout=2.0)
        return source.closed

    assert asyncio.run(_run()) is True
