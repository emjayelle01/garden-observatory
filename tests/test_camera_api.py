"""Tests for the camera-related API endpoints.

These call the route functions directly with a lightweight fake request so no
HTTP client dependency is required. They verify that the endpoints reflect the
canonical monitored state and never trigger hardware detection themselves.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from mgo.api.app import camera_status, health
from mgo.core.camera import CameraReadiness, CameraState, CameraStatus


def _request(state: CameraState | None) -> SimpleNamespace:
    """Build a fake request exposing ``app.state.camera_state``."""
    if state is None:
        app_state = SimpleNamespace()
    else:
        app_state = SimpleNamespace(camera_state=state)
    return SimpleNamespace(app=SimpleNamespace(state=app_state))


def _available_readiness() -> CameraReadiness:
    """A readiness value distinguishable from any default on Windows/CI."""
    return CameraReadiness(
        enabled=True,
        backend="rpicam",
        status=CameraStatus.AVAILABLE,
        available=True,
        detail="Detected camera device(s): 0 : imx708",
        checked_at=datetime(2026, 7, 23, 12, 0, tzinfo=UTC),
    )


def test_camera_status_returns_latest_state() -> None:
    """/camera/status returns exactly the stored readiness result."""
    state = CameraState()
    readiness = _available_readiness()
    state.set(readiness)

    result = camera_status(_request(state))

    assert result == readiness.as_dict()
    # The stored state must be untouched: the endpoint reads, never detects.
    assert state.get() == readiness


def test_camera_status_is_safe_before_first_check() -> None:
    """/camera/status returns a safe default when state is empty."""
    result = camera_status(_request(CameraState()))

    # Default configuration has the camera disabled.
    assert result["status"] == CameraStatus.DISABLED.value
    assert result["available"] is False


def test_camera_status_safe_without_state_attribute() -> None:
    """/camera/status is safe even if state was never attached."""
    result = camera_status(_request(None))

    assert result["available"] is False


def test_health_uses_canonical_camera_state() -> None:
    """/health composes its camera section from the canonical state."""
    state = CameraState()
    readiness = _available_readiness()
    state.set(readiness)

    result = health(_request(state))

    assert result["camera"] == readiness.as_dict()
    # Core system-health fields remain present and compatible.
    assert result["status"] in {"healthy", "warning", "critical"}
    assert "memory" in result
    assert "disk" in result
