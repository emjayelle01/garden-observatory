"""Integration tests: monitors invoke their notification listeners.

These exercise the wiring seams the application uses -- the camera monitor's
``on_material_change`` callback and the motion observer's
``transition_listener`` -- with fake recorders and detectors, so no database,
hardware or real provider transport is involved.
"""

from __future__ import annotations

import asyncio
import dataclasses
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mgo.core.camera import (
    CameraReadiness,
    CameraState,
    DetectionEvidence,
    DetectionOutcome,
)
from mgo.core.camera_monitor import perform_camera_check
from mgo.core.config import CameraConfig, MGOConfig, load_config
from mgo.motion.models import MotionResult, MotionStatus
from mgo.motion.monitor import _MotionObserver

_NOW = datetime(2026, 7, 26, 12, 0, 0, tzinfo=UTC)


class _Recorder:
    """A fake observation recorder capturing every persisted observation."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, database_path: Path, **kwargs: Any) -> None:
        self.calls.append(kwargs)


class _Detector:
    """A camera detector returning scripted evidence per call."""

    def __init__(self, *evidence: DetectionEvidence) -> None:
        self._evidence = list(evidence)

    def detect(self, config: CameraConfig) -> DetectionEvidence:
        if len(self._evidence) > 1:
            return self._evidence.pop(0)
        return self._evidence[0]


def _camera_config() -> MGOConfig:
    base = load_config()
    return dataclasses.replace(
        base,
        camera=dataclasses.replace(base.camera, enabled=True, backend="null"),
    )


_DETECTED = DetectionEvidence(DetectionOutcome.DETECTED, "imx708")
_NOT_DETECTED = DetectionEvidence(DetectionOutcome.NOT_DETECTED, "no camera")


# --- camera monitor listener ------------------------------------------------


def test_camera_listener_fires_on_material_change() -> None:
    """Each material readiness change invokes the listener with the result."""
    config = _camera_config()
    state = CameraState()
    recorder = _Recorder()
    seen: list[CameraReadiness] = []

    async def _run() -> None:
        detector = _Detector(_NOT_DETECTED, _DETECTED)
        for _ in range(2):
            await perform_camera_check(
                config,
                state,
                detector=detector,
                recorder=recorder,
                on_material_change=seen.append,
            )

    asyncio.run(_run())

    assert [readiness.available for readiness in seen] == [False, True]
    assert len(recorder.calls) == 2


def test_camera_listener_not_fired_without_material_change() -> None:
    """Repeated identical readiness never invokes the listener again."""
    config = _camera_config()
    state = CameraState()
    seen: list[CameraReadiness] = []

    async def _run() -> None:
        detector = _Detector(_DETECTED)
        for _ in range(3):
            await perform_camera_check(
                config,
                state,
                detector=detector,
                recorder=_Recorder(),
                on_material_change=seen.append,
            )

    asyncio.run(_run())

    assert len(seen) == 1


def test_camera_listener_failure_never_breaks_the_check() -> None:
    """A raising listener is isolated: the check completes and persists."""
    config = _camera_config()
    state = CameraState()
    recorder = _Recorder()

    def _explode(readiness: CameraReadiness) -> None:
        raise RuntimeError("listener boom")

    readiness = asyncio.run(
        perform_camera_check(
            config,
            state,
            detector=_Detector(_DETECTED),
            recorder=recorder,
            on_material_change=_explode,
        )
    )

    assert readiness.available is True
    assert state.get() == readiness
    assert len(recorder.calls) == 1


# --- motion observer listener -----------------------------------------------


def _motion_result(status: MotionStatus, at: datetime = _NOW) -> MotionResult:
    return MotionResult(
        status=status,
        detected=status is MotionStatus.MOTION_DETECTED,
        score=0.0,
        threshold=0.08,
        frames_available=True,
        detail="detail",
        evaluated_at=at,
    )


def _motion_observer(
    recorder: _Recorder,
    listener: Any,
) -> _MotionObserver:
    base = load_config()
    config = dataclasses.replace(
        base,
        motion=dataclasses.replace(base.motion, enabled=True, cooldown_seconds=0.0),
    )
    return _MotionObserver(
        config, recorder=recorder, transition_listener=listener
    )


def test_motion_listener_fires_on_material_transition() -> None:
    """Each persisted transition invokes the listener with the new result."""
    seen: list[MotionResult] = []
    observer = _motion_observer(_Recorder(), seen.append)

    observer.record(_motion_result(MotionStatus.NO_MOTION), _NOW)
    observer.record(_motion_result(MotionStatus.MOTION_DETECTED), _NOW)

    assert [result.status for result in seen] == [
        MotionStatus.NO_MOTION,
        MotionStatus.MOTION_DETECTED,
    ]


def test_motion_listener_not_fired_without_transition() -> None:
    """A repeated identical status neither persists nor notifies."""
    seen: list[MotionResult] = []
    recorder = _Recorder()
    observer = _motion_observer(recorder, seen.append)

    observer.record(_motion_result(MotionStatus.NO_MOTION), _NOW)
    observer.record(_motion_result(MotionStatus.NO_MOTION), _NOW)

    assert len(seen) == 1
    assert len(recorder.calls) == 1


def test_motion_listener_failure_never_breaks_recording() -> None:
    """A raising listener is isolated: the observation is still persisted."""
    recorder = _Recorder()

    def _explode(result: MotionResult) -> None:
        raise RuntimeError("listener boom")

    observer = _motion_observer(recorder, _explode)
    persisted = observer.record(_motion_result(MotionStatus.NO_MOTION), _NOW)

    assert persisted is True
    assert len(recorder.calls) == 1
