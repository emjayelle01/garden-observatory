"""Tests for the camera-related API endpoints.

These call the route functions directly with a lightweight fake request so no
HTTP client dependency is required. They verify that the endpoints reflect the
canonical monitored state and never trigger hardware detection themselves.
"""

from __future__ import annotations

import asyncio
import re
import threading
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from mgo.api.app import (
    camera_capture,
    camera_preview_status,
    camera_status,
    health,
)
from mgo.camera import CameraCoordinator, CaptureService, MockBackend
from mgo.camera.backend import NullBackend
from mgo.camera.exceptions import BackendCaptureError, PreviewStartError
from mgo.camera.preview import PreviewService
from mgo.camera.preview_backend import MockPreviewBackend
from mgo.captures.archive import CaptureArchive
from mgo.core.camera import CameraReadiness, CameraState, CameraStatus
from mgo.core.config import CameraConfig, PreviewConfig
from mgo.core.database import apply_migrations


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
    # The API surfaces the microsecond-precision filename.
    assert re.match(
        r"^\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}\.\d{6}Z\.jpg$",
        result["filename"],
    )
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


# --- coordinated capture (Task 12) ------------------------------------------
#
# The capture endpoint no longer touches the preview service itself: it runs one
# coordinated camera transaction and then persists the archive record. These
# tests pin that routing, the unchanged response contract, and the rule that a
# preview-restoration failure never becomes the capture's answer.


def _preview_config() -> PreviewConfig:
    """Build an enabled preview configuration for endpoint tests."""
    return PreviewConfig(
        enabled=True,
        width=1280,
        height=720,
        fps=15,
        startup_timeout_seconds=0.5,
        shutdown_timeout_seconds=0.5,
    )


def _coordinated_request(
    coordinator: object,
    *,
    capture_archive: CaptureArchive | None = None,
    preview: PreviewService | None = None,
) -> SimpleNamespace:
    """Build a fake request whose app state carries a camera coordinator.

    ``preview`` is attached exactly as the lifespan attaches it, so a status
    read reflects the same service the coordinator drives.
    """
    app_state = SimpleNamespace(camera_coordinator=coordinator)
    if capture_archive is not None:
        app_state.capture_archive = capture_archive
    if preview is not None:
        app_state.preview_service = preview
    return SimpleNamespace(app=SimpleNamespace(state=app_state))


def _archive(tmp_path: Path) -> CaptureArchive:
    """Build a capture archive over a migrated temporary database."""
    database = tmp_path / "mgo.db"
    apply_migrations(database)
    return CaptureArchive(database)


class _RecordingCoordinator:
    """A coordinator double that records how the endpoint used it."""

    def __init__(self, result: object | None = None) -> None:
        self._result = result
        self.capture_calls = 0

    def capture_image(self) -> object:
        self.capture_calls += 1
        assert self._result is not None
        return self._result


def test_capture_endpoint_uses_the_camera_coordinator(tmp_path: Path) -> None:
    """The mutation goes through the coordinator, not the capture service."""
    service = CaptureService(
        _capture_config(tmp_path / "captures"),
        MockBackend(width=4608, height=2592, name="mock"),
    )
    coordinator = _RecordingCoordinator(service.capture_image())
    request = _coordinated_request(coordinator, capture_archive=_archive(tmp_path))

    result = asyncio.run(camera_capture(request))

    assert coordinator.capture_calls == 1
    assert result["success"] is True


def test_capture_response_fields_are_unchanged(tmp_path: Path) -> None:
    """Task 12 adds no field to -- and removes none from -- the response."""
    capture_service = CaptureService(
        _capture_config(tmp_path / "captures"),
        MockBackend(width=4608, height=2592, name="mock"),
    )
    coordinator = CameraCoordinator(
        capture_service,
        PreviewService(_preview_config(), MockPreviewBackend()),
        restore_after_capture=True,
    )
    request = _coordinated_request(coordinator, capture_archive=_archive(tmp_path))

    result = asyncio.run(camera_capture(request))

    assert set(result) == {
        "success",
        "filename",
        "absolute_path",
        "timestamp",
        "width",
        "height",
        "filesize_bytes",
        "backend",
        "capture_id",
    }


def test_successful_capture_is_returned_when_restoration_fails(
    tmp_path: Path,
) -> None:
    """A capture that succeeded is HTTP 200 even if preview did not come back.

    Reporting a failure here would invite a retry and duplicate the evidence
    for an image that was captured exactly once.
    """
    preview_backend = _PreviewBackendFailingAfterFirstStart()
    preview = PreviewService(_preview_config(), preview_backend)
    coordinator = CameraCoordinator(
        CaptureService(
            _capture_config(tmp_path / "captures"),
            MockBackend(width=4608, height=2592, name="mock"),
        ),
        preview,
        restore_after_capture=True,
    )
    preview.start()
    request = _coordinated_request(
        coordinator, capture_archive=_archive(tmp_path), preview=preview
    )

    result = asyncio.run(camera_capture(request))

    assert result["success"] is True
    assert Path(result["absolute_path"]).exists()
    # The restoration failure is visible only through preview status.
    status = camera_preview_status(request)
    assert status["state"] == "failed"
    assert status["last_error"] is not None
    # No restoration outcome leaked into the capture response.
    assert not any(
        "preview" in key or "restor" in key for key in result
    ), sorted(result)


def test_capture_failure_remains_the_response_when_restoration_fails(
    tmp_path: Path,
) -> None:
    """A failed capture keeps its own status code; preview never overrides it."""
    preview_backend = _PreviewBackendFailingAfterFirstStart()
    preview = PreviewService(_preview_config(), preview_backend)
    coordinator = CameraCoordinator(
        CaptureService(
            _capture_config(tmp_path / "captures"),
            MockBackend(error=BackendCaptureError("rpicam-still exited 1")),
        ),
        preview,
        restore_after_capture=True,
    )
    preview.start()
    request = _coordinated_request(coordinator, capture_archive=_archive(tmp_path))

    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(camera_capture(request))

    # 502 is the backend-failure mapping; a preview failure would not map here.
    assert excinfo.value.status_code == 502
    assert "rpicam-still exited 1" in str(excinfo.value.detail)
    assert preview.status().state.value == "failed"


def test_archive_persistence_runs_without_the_camera_lock(
    tmp_path: Path,
) -> None:
    """Database work never holds the camera: the lock is free by then.

    The archive is driven to attempt a *camera-mutating* operation while it is
    persisting the record. If the coordinator still held its lock, that
    operation could never complete.
    """
    preview_backend = MockPreviewBackend()
    preview = PreviewService(_preview_config(), preview_backend)
    coordinator = CameraCoordinator(
        CaptureService(
            _capture_config(tmp_path / "captures"),
            MockBackend(width=4608, height=2592, name="mock"),
        ),
        preview,
    )
    observed: dict[str, bool] = {}

    class _ProbingArchive(CaptureArchive):
        def record_capture(self, result: object) -> object:
            started = threading.Event()

            def _probe() -> None:
                coordinator.start_preview()
                started.set()

            worker = threading.Thread(target=_probe, daemon=True)
            worker.start()
            observed["lock_free"] = started.wait(5.0)
            worker.join(5.0)
            return super().record_capture(result)  # type: ignore[arg-type]

    database = tmp_path / "mgo.db"
    apply_migrations(database)
    request = _coordinated_request(
        coordinator, capture_archive=_ProbingArchive(database)
    )

    result = asyncio.run(camera_capture(request))

    assert result["success"] is True
    assert observed["lock_free"] is True
    coordinator.shutdown()


class _PreviewBackendFailingAfterFirstStart:
    """Starts preview once, then refuses -- a realistic restoration failure."""

    def __init__(self) -> None:
        self._delegate = MockPreviewBackend()
        self.start_calls = 0

    @property
    def name(self) -> str:
        return "mock"

    def start(self, config: PreviewConfig) -> object:
        self.start_calls += 1
        if self.start_calls > 1:
            raise PreviewStartError("camera busy")
        return self._delegate.start(config)
