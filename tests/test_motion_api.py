"""Tests for the read-only ``GET /motion/status`` endpoint.

These call the route function directly with a lightweight fake request (the same
pattern as ``test_camera_api``/``test_preview_stream_api``), attaching a motion
state to ``app.state``. No Raspberry Pi hardware, broker or detector is required:
the endpoint only reads application-managed state and never runs analysis.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from mgo.api.app import MotionStatusResponse, motion_status
from mgo.core.config import MotionConfig
from mgo.motion.models import MotionResult, MotionStatus, default_motion_result

_NOW = datetime(2026, 7, 25, 12, 0, 0, tzinfo=UTC)


def _config(*, enabled: bool) -> MotionConfig:
    return MotionConfig(
        enabled=enabled,
        analysis_interval_seconds=1.0,
        analysis_width=160,
        analysis_height=90,
        pixel_difference_threshold=20,
        changed_pixel_ratio_threshold=0.08,
        cooldown_seconds=5.0,
    )


def _request(result: MotionResult | None) -> SimpleNamespace:
    """Build a fake request whose app.state holds the given motion result."""
    state = SimpleNamespace()
    if result is not None:
        holder = SimpleNamespace(get=lambda: result)
        state.motion_state = holder
    return SimpleNamespace(app=SimpleNamespace(state=state))


def _result(
    status: MotionStatus,
    *,
    detected: bool = False,
    score: float = 0.0,
    frames_available: bool = True,
) -> MotionResult:
    return MotionResult(
        status=status,
        detected=detected,
        score=score,
        threshold=0.02,
        frames_available=frames_available,
        detail="detail",
        evaluated_at=_NOW,
    )


def test_status_defaults_to_disabled_without_state() -> None:
    """With no motion state attached the endpoint reports the disabled default."""
    response = motion_status(_request(None))

    assert isinstance(response, MotionStatusResponse)
    assert response.enabled is False
    assert response.status == "disabled"
    assert response.detected is False


def test_status_reports_disabled_result() -> None:
    response = motion_status(_request(default_motion_result(_config(enabled=False))))

    assert response.enabled is False
    assert response.status == "disabled"
    assert response.frames_available is False


def test_status_reports_waiting_when_enabled_without_frames() -> None:
    response = motion_status(_request(default_motion_result(_config(enabled=True))))

    assert response.enabled is True
    assert response.status == "waiting_for_frames"
    assert response.detected is False
    assert response.frames_available is False


def test_status_reports_establishing_baseline() -> None:
    response = motion_status(
        _request(_result(MotionStatus.ESTABLISHING_BASELINE))
    )

    assert response.enabled is True
    assert response.status == "establishing_baseline"
    assert response.detected is False


def test_status_reports_no_motion() -> None:
    response = motion_status(
        _request(_result(MotionStatus.NO_MOTION, score=0.001))
    )

    assert response.enabled is True
    assert response.status == "no_motion"
    assert response.detected is False
    assert response.score == 0.001
    assert response.threshold == 0.02


def test_status_reports_motion_detected() -> None:
    response = motion_status(
        _request(_result(MotionStatus.MOTION_DETECTED, detected=True, score=0.5))
    )

    assert response.enabled is True
    assert response.status == "motion_detected"
    assert response.detected is True
    assert response.score == 0.5


def test_status_reports_error() -> None:
    response = motion_status(_request(_result(MotionStatus.ERROR)))

    assert response.enabled is True
    assert response.status == "error"
    assert response.detected is False


def test_status_response_includes_evaluation_timestamp() -> None:
    response = motion_status(_request(_result(MotionStatus.NO_MOTION)))

    assert response.evaluated_at == _NOW.isoformat()
