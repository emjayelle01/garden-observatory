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
    threshold: float = 0.08,
) -> MotionConfig:
    return MotionConfig(
        enabled=enabled,
        analysis_interval_seconds=interval,
        analysis_width=2,
        analysis_height=2,
        pixel_difference_threshold=20,
        changed_pixel_ratio_threshold=threshold,
        cooldown_seconds=cooldown,
    )


def _mgo_config(motion: MotionConfig) -> MGOConfig:
    """Return a full config with a customised motion section."""
    return dataclasses.replace(load_config(), motion=motion)


# --- evaluator (rolling reference) ----------------------------------------


def _seq(evaluator: _MotionEvaluator, frames: list[bytes]) -> list[MotionStatus]:
    """Evaluate ``frames`` one second apart and return their statuses."""
    return [
        evaluator.evaluate(frame, _T0 + timedelta(seconds=i)).status
        for i, frame in enumerate(frames)
    ]


def test_evaluator_first_frame_establishes_baseline() -> None:
    """A. The first valid frame becomes the rolling reference (no comparison)."""
    evaluator = _MotionEvaluator(_motion_config(), _FakeDetector())

    result = evaluator.evaluate(b"AAAA", _T0)

    assert result.status is MotionStatus.ESTABLISHING_BASELINE
    assert result.detected is False
    assert result.frames_available is True
    assert result.score == 0.0


def test_evaluator_identical_frames_report_no_motion() -> None:
    """B. Identical successive frames stay at no_motion (stable scene)."""
    evaluator = _MotionEvaluator(_motion_config(), _FakeDetector())

    statuses = _seq(evaluator, [b"AAAA", b"AAAA", b"AAAA"])

    assert statuses == [
        MotionStatus.ESTABLISHING_BASELINE,
        MotionStatus.NO_MOTION,
        MotionStatus.NO_MOTION,
    ]


def test_evaluator_changed_frame_reports_motion() -> None:
    evaluator = _MotionEvaluator(_motion_config(), _FakeDetector())
    evaluator.evaluate(b"AAAA", _T0)

    result = evaluator.evaluate(b"ZZZZ", _T0 + timedelta(seconds=1))

    assert result.status is MotionStatus.MOTION_DETECTED
    assert result.detected is True
    assert result.score == 1.0


def test_evaluator_arrival_then_settling() -> None:
    """C. Empty → object appears → object stays: establishing, motion, no_motion.

    Critical acceptance test: a large change is detected on arrival, and the
    same scene repeated immediately settles to no_motion because the arrived
    frame became the reference.
    """
    evaluator = _MotionEvaluator(_motion_config(), _FakeDetector())

    statuses = _seq(evaluator, [b"AAAA", b"ZZZZ", b"ZZZZ"])

    assert statuses == [
        MotionStatus.ESTABLISHING_BASELINE,
        MotionStatus.MOTION_DETECTED,
        MotionStatus.NO_MOTION,
    ]


def test_evaluator_continued_activity_stays_motion() -> None:
    """D. A region that keeps changing each frame keeps reading as motion."""
    evaluator = _MotionEvaluator(_motion_config(), _FakeDetector())

    statuses = _seq(evaluator, [b"AAAA", b"BBBB", b"CCCC", b"DDDD"])

    assert statuses == [
        MotionStatus.ESTABLISHING_BASELINE,
        MotionStatus.MOTION_DETECTED,
        MotionStatus.MOTION_DETECTED,
        MotionStatus.MOTION_DETECTED,
    ]


def test_evaluator_departure_then_settling() -> None:
    """E. Object present → removed → empty repeats: motion on removal, then settles."""
    evaluator = _MotionEvaluator(_motion_config(), _FakeDetector())

    statuses = _seq(evaluator, [b"ZZZZ", b"AAAA", b"AAAA"])

    assert statuses == [
        MotionStatus.ESTABLISHING_BASELINE,
        MotionStatus.MOTION_DETECTED,
        MotionStatus.NO_MOTION,
    ]


def test_evaluator_lasting_change_does_not_latch() -> None:
    """F. A lasting change settles to no_motion; it does not latch forever.

    Under a frozen baseline the persistently-changed scene would read as motion
    indefinitely (it never matches the original empty frame again). Under the
    rolling reference it settles after the change stops moving.
    """
    evaluator = _MotionEvaluator(_motion_config(), _FakeDetector())

    statuses = _seq(evaluator, [b"AAAA", b"ZZZZ", b"ZZZZ", b"ZZZZ", b"ZZZZ"])

    assert statuses[1] is MotionStatus.MOTION_DETECTED
    # It does not stay latched: every later unchanged frame is no_motion.
    assert all(s is MotionStatus.NO_MOTION for s in statuses[2:])


def test_evaluator_reference_advances_after_no_motion() -> None:
    """The current frame becomes the reference even after a no_motion result."""
    evaluator = _MotionEvaluator(
        _motion_config(), _FakeDetector(motion_threshold=0.5)
    )
    evaluator.evaluate(b"AAAA", _T0)
    # 2-of-4 change (score 0.5) is not motion; AABB becomes the new reference.
    mid = evaluator.evaluate(b"AABB", _T0 + timedelta(seconds=1))
    # Compared with AABB this is another 2-of-4 change (no motion); against the
    # original AAAA it would be a full change and read as motion.
    result = evaluator.evaluate(b"BBBB", _T0 + timedelta(seconds=2))

    assert mid.status is MotionStatus.NO_MOTION
    assert result.status is MotionStatus.NO_MOTION


def test_evaluator_reference_advances_after_motion() -> None:
    """G. A motion-detected frame becomes the reference for the next comparison."""
    evaluator = _MotionEvaluator(_motion_config(), _FakeDetector())

    evaluator.evaluate(b"AAAA", _T0)
    motion = evaluator.evaluate(b"ZZZZ", _T0 + timedelta(seconds=1))
    # ZZZZ is now the reference: an identical ZZZZ is no_motion...
    settled = evaluator.evaluate(b"ZZZZ", _T0 + timedelta(seconds=2))
    # ...and returning to the original AAAA is a fresh change (motion), proving
    # the reference had advanced to ZZZZ rather than staying at AAAA.
    returned = evaluator.evaluate(b"AAAA", _T0 + timedelta(seconds=3))

    assert motion.status is MotionStatus.MOTION_DETECTED
    assert settled.status is MotionStatus.NO_MOTION
    assert returned.status is MotionStatus.MOTION_DETECTED


def test_evaluator_no_frame_resets_reference_to_waiting() -> None:
    """H. Frame loss resets the reference; the next valid frame re-establishes it."""
    evaluator = _MotionEvaluator(_motion_config(), _FakeDetector())
    evaluator.evaluate(b"AAAA", _T0)

    waiting = evaluator.on_no_frame(_T0 + timedelta(seconds=1))
    # After a reset the next frame re-establishes the reference (not no_motion).
    reestablished = evaluator.evaluate(b"AAAA", _T0 + timedelta(seconds=2))

    assert waiting.status is MotionStatus.WAITING_FOR_FRAMES
    assert waiting.frames_available is False
    assert reestablished.status is MotionStatus.ESTABLISHING_BASELINE


def test_evaluator_decode_error_reports_error_and_keeps_reference() -> None:
    """I. A bad frame reports error without corrupting the rolling reference."""
    evaluator = _MotionEvaluator(_motion_config(), _FakeDetector())
    evaluator.evaluate(b"AAAA", _T0)

    error = evaluator.evaluate(b"BAD", _T0 + timedelta(seconds=1))
    # The reference survives a bad frame, so a following good identical frame is
    # a normal comparison (no_motion), not a fresh reference.
    recovered = evaluator.evaluate(b"AAAA", _T0 + timedelta(seconds=2))

    assert error.status is MotionStatus.ERROR
    assert error.detected is False
    assert error.frames_available is True
    assert recovered.status is MotionStatus.NO_MOTION


def test_evaluator_unexpected_fault_reports_error_and_keeps_reference() -> None:
    evaluator = _MotionEvaluator(_motion_config(), _FakeDetector())
    evaluator.evaluate(b"AAAA", _T0)

    error = evaluator.evaluate(b"BOOM", _T0 + timedelta(seconds=1))
    # The reference is preserved across an unexpected fault, so a following good
    # identical frame recovers a normal comparison.
    recovered = evaluator.evaluate(b"AAAA", _T0 + timedelta(seconds=2))

    assert error.status is MotionStatus.ERROR
    assert recovered.status is MotionStatus.NO_MOTION


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


def test_monitor_reports_arrival_then_settles() -> None:
    """Arrival is recorded as motion; the now-static scene settles to no_motion.

    The exhausted frame repeats the arrived scene, and under the rolling
    reference an unchanged repeat is no_motion — so a lasting change does not
    latch at the monitor level either.
    """
    recorder = _Recorder()
    config = _mgo_config(_motion_config())

    state, source = asyncio.run(
        _drive(
            [b"AAAA", b"ZZZZ", b"ZZZZ"],
            exhausted=b"ZZZZ",
            config=config,
            detector=_FakeDetector(),
            recorder=recorder,
        )
    )

    latest = state.get()
    assert latest is not None
    assert latest.status is MotionStatus.NO_MOTION
    assert recorder.statuses == [
        "establishing_baseline",
        "motion_detected",
        "no_motion",
    ]
    assert source.closed is True


def test_monitor_continuous_activity_does_not_flood_observations() -> None:
    """A scene that keeps changing every frame records motion once, not per frame."""
    recorder = _Recorder()
    config = _mgo_config(_motion_config())

    asyncio.run(
        _drive(
            [b"AAAA", b"BBBB", b"CCCC", b"DDDD"],
            exhausted=b"DDDD",
            config=config,
            detector=_FakeDetector(),
            recorder=recorder,
        )
    )

    assert recorder.statuses.count("motion_detected") == 1


def test_monitor_departure_then_settles() -> None:
    """Removing the subject records motion; the empty scene then settles."""
    recorder = _Recorder()
    config = _mgo_config(_motion_config())

    asyncio.run(
        _drive(
            [b"ZZZZ", b"AAAA", b"AAAA"],
            exhausted=b"AAAA",
            config=config,
            detector=_FakeDetector(),
            recorder=recorder,
        )
    )

    assert recorder.statuses == [
        "establishing_baseline",
        "motion_detected",
        "no_motion",
    ]


def test_monitor_repeated_stable_frames_do_not_flood_observations() -> None:
    """A persistently unchanged scene records one no_motion, not one per frame."""
    recorder = _Recorder()
    config = _mgo_config(_motion_config())

    asyncio.run(
        _drive(
            [b"AAAA", b"AAAA", b"AAAA"],
            exhausted=b"AAAA",
            config=config,
            detector=_FakeDetector(),
            recorder=recorder,
        )
    )

    assert recorder.statuses.count("no_motion") == 1
    assert "motion_detected" not in recorder.statuses


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
