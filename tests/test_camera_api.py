"""Tests for the camera-related API endpoints.

These call the route functions directly with a lightweight fake request so no
HTTP client dependency is required. They verify that the endpoints reflect the
canonical monitored state and never trigger hardware detection themselves.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from mgo.api.app import camera_capture, camera_status, health
from mgo.camera import CaptureService, MockBackend
from mgo.camera.backend import NullBackend
from mgo.core.camera import CameraReadiness, CameraState, CameraStatus
from mgo.core.config import CameraConfig


def _request(state: CameraState | None) -> SimpleNamespace:
    """Build a fake request exposing ``app.state.camera_state``."""
    if state is None:
        app_state = SimpleNamespace()
    else:
        app_state = SimpleNamespace(camera_state=state)
    return SimpleNamespace(app=SimpleNamespace(state=app_state))


def _capture_request(service: CaptureService) -> SimpleNamespace:
    """Build a fake request exposing ``app.state.capture_service``."""
    app_state = SimpleNamespace(capture_service=service)
    return SimpleNamespace(app=SimpleNamespace(state=app_state))


def _capture_config(capture_directory: Path, *, enabled: bool = True) -> CameraConfig:
    """Build a camera config for capture-endpoint tests."""
    return CameraConfig(
        enabled=enabled,
        backend="mock",
        device_index=None,
        detection_interval_seconds=30,
        capture_directory=capture_directory,
    )


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


def test_camera_capture_returns_metadata(tmp_path: Path) -> None:
    """POST /camera/capture returns HTTP 200 metadata for a mock backend."""
    service = CaptureService(
        _capture_config(tmp_path / "captures"),
        MockBackend(width=4608, height=2592, name="mock"),
    )

    result = asyncio.run(camera_capture(_capture_request(service)))

    assert result["success"] is True
    assert result["filename"].endswith(".jpg")
    assert result["width"] == 4608
    assert result["height"] == 2592
    assert result["filesize_bytes"] > 0
    assert Path(result["absolute_path"]).exists()


def test_camera_capture_unavailable_returns_503(tmp_path: Path) -> None:
    """An unavailable camera maps to HTTP 503."""
    service = CaptureService(
        _capture_config(tmp_path / "captures"),
        NullBackend(),
    )

    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(camera_capture(_capture_request(service)))

    assert excinfo.value.status_code == 503


def test_camera_capture_disabled_returns_503(tmp_path: Path) -> None:
    """Capturing while the camera is disabled maps to HTTP 503."""
    service = CaptureService(
        _capture_config(tmp_path / "captures", enabled=False),
        MockBackend(),
    )

    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(camera_capture(_capture_request(service)))

    assert excinfo.value.status_code == 503
