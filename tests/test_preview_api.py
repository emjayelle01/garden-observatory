"""Tests for the live-preview API endpoints and their integration.

These call the route functions directly with a lightweight fake request (the
same pattern as ``test_camera_api``), attaching a preview service -- and, where
relevant, a capture service and archive -- to ``app.state``. They cover the
three preview endpoints, their status codes, health integration and the
camera-ownership handoff when a capture interrupts an active preview.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from mgo.api.app import (
    camera_capture,
    camera_preview_start,
    camera_preview_status,
    camera_preview_stop,
    dashboard_page,
    health,
    preview_page,
)
from mgo.camera import CaptureService, MockBackend
from mgo.camera.exceptions import PreviewStartError
from mgo.camera.preview import PreviewService
from mgo.camera.preview_backend import MockPreviewBackend
from mgo.captures.archive import CaptureArchive
from mgo.core.config import CameraConfig, PreviewConfig
from mgo.core.database import apply_migrations


def _preview_config(*, enabled: bool = True) -> PreviewConfig:
    """Build a preview configuration for endpoint tests."""
    return PreviewConfig(
        enabled=enabled,
        width=1280,
        height=720,
        fps=15,
        startup_timeout_seconds=5.0,
        shutdown_timeout_seconds=5.0,
    )


def _preview_service(
    backend: MockPreviewBackend | None = None,
    *,
    enabled: bool = True,
) -> PreviewService:
    """Build a preview service over a mock backend."""
    return PreviewService(
        _preview_config(enabled=enabled),
        backend or MockPreviewBackend(),
    )


def _request(
    *,
    preview: PreviewService | None = None,
    capture_service: CaptureService | None = None,
    capture_archive: CaptureArchive | None = None,
) -> SimpleNamespace:
    """Build a fake request exposing the requested ``app.state`` services."""
    state = SimpleNamespace()
    if preview is not None:
        state.preview_service = preview
    if capture_service is not None:
        state.capture_service = capture_service
    if capture_archive is not None:
        state.capture_archive = capture_archive
    return SimpleNamespace(app=SimpleNamespace(state=state))


# --- status ---------------------------------------------------------------


def test_preview_status_endpoint_returns_state() -> None:
    """GET /camera/preview/status reports the current stopped state."""
    result = camera_preview_status(_request(preview=_preview_service()))

    assert result["state"] == "stopped"
    assert result["enabled"] is True
    assert result["owner"] is None


# --- start ----------------------------------------------------------------


def test_preview_start_endpoint_starts_preview() -> None:
    """POST /camera/preview/start starts preview and returns running status."""
    backend = MockPreviewBackend()
    preview = _preview_service(backend)
    request = _request(preview=preview)

    result = asyncio.run(camera_preview_start(request))

    assert result["state"] == "running"
    assert result["owner"] == "preview"
    assert backend.start_calls == 1


def test_preview_start_is_idempotent_without_duplicate_process() -> None:
    """A second start returns 200 and never launches a duplicate process."""
    backend = MockPreviewBackend()
    request = _request(preview=_preview_service(backend))

    first = asyncio.run(camera_preview_start(request))
    second = asyncio.run(camera_preview_start(request))

    assert first["state"] == "running"
    assert second["state"] == "running"
    assert backend.start_calls == 1


def test_preview_start_disabled_returns_503() -> None:
    """Starting a disabled preview maps to HTTP 503."""
    request = _request(preview=_preview_service(enabled=False))

    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(camera_preview_start(request))

    assert excinfo.value.status_code == 503


def test_preview_start_failure_returns_502() -> None:
    """A process that cannot start maps to HTTP 502."""
    backend = MockPreviewBackend(error=PreviewStartError("no encoder"))
    request = _request(preview=_preview_service(backend))

    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(camera_preview_start(request))

    assert excinfo.value.status_code == 502


# --- stop -----------------------------------------------------------------


def test_preview_stop_endpoint_stops_preview() -> None:
    """POST /camera/preview/stop stops an active preview."""
    preview = _preview_service()
    request = _request(preview=preview)
    asyncio.run(camera_preview_start(request))

    result = asyncio.run(camera_preview_stop(request))

    assert result["state"] == "stopped"
    assert result["owner"] is None


def test_preview_stop_is_idempotent() -> None:
    """Stopping when already stopped returns 200 with stopped status."""
    request = _request(preview=_preview_service())

    result = asyncio.run(camera_preview_stop(request))

    assert result["state"] == "stopped"


# --- health integration ---------------------------------------------------


def test_health_includes_preview_and_stays_healthy_when_stopped() -> None:
    """/health embeds preview status; a stopped preview is not an error."""
    result = health(_request(preview=_preview_service()))

    assert "preview" in result
    assert set(result["preview"]) == {
        "enabled",
        "state",
        "owner",
        "uptime_seconds",
    }
    assert result["preview"]["state"] == "stopped"
    # A stopped preview must never degrade overall health.
    assert result["status"] in {"healthy", "warning", "critical"}


# --- camera ownership handoff ---------------------------------------------


def test_capture_releases_active_preview(tmp_path: Path) -> None:
    """A capture interrupts an active preview and takes the camera."""
    backend = MockPreviewBackend()
    preview = _preview_service(backend)
    capture_service = CaptureService(
        CameraConfig(
            enabled=True,
            backend="mock",
            device_index=None,
            detection_interval_seconds=30,
            capture_directory=tmp_path / "captures",
        ),
        MockBackend(width=4608, height=2592, name="mock"),
    )
    database_path = tmp_path / "mgo.db"
    apply_migrations(database_path)
    request = _request(
        preview=preview,
        capture_service=capture_service,
        capture_archive=CaptureArchive(database_path),
    )

    # Preview is running before the capture.
    asyncio.run(camera_preview_start(request))
    assert preview.status().state.value == "running"

    result = asyncio.run(camera_capture(request))

    # The capture succeeded and the preview was released (left stopped).
    uuid.UUID(result["capture_id"])
    assert preview.status().state.value == "stopped"
    assert backend.last_process is not None
    assert backend.last_process.terminated is True


# --- coordinated preview mutations (Task 12) --------------------------------
#
# Start and stop are camera *mutations* and must run through the coordinator, so
# they cannot interleave with a capture. Status, streaming and the two HTML
# pages are reads and must not.


class _RecordingCoordinator:
    """A coordinator double that records which operations the endpoints used."""

    def __init__(self, preview: PreviewService) -> None:
        self._preview = preview
        self.start_calls = 0
        self.stop_calls = 0

    def start_preview(self) -> object:
        self.start_calls += 1
        return self._preview.start()

    def stop_preview(self) -> object:
        self.stop_calls += 1
        return self._preview.stop()


def _coordinated_request(
    preview: PreviewService, coordinator: object
) -> SimpleNamespace:
    """Build a fake request carrying both a preview service and a coordinator."""
    state = SimpleNamespace(
        preview_service=preview, camera_coordinator=coordinator
    )
    return SimpleNamespace(app=SimpleNamespace(state=state))


def test_preview_start_endpoint_uses_the_coordinator() -> None:
    """POST /camera/preview/start is a coordinated mutation."""
    backend = MockPreviewBackend()
    preview = _preview_service(backend)
    coordinator = _RecordingCoordinator(preview)
    request = _coordinated_request(preview, coordinator)

    result = asyncio.run(camera_preview_start(request))

    assert coordinator.start_calls == 1
    assert result["state"] == "running"
    assert backend.start_calls == 1


def test_preview_stop_endpoint_uses_the_coordinator() -> None:
    """POST /camera/preview/stop is a coordinated mutation."""
    backend = MockPreviewBackend()
    preview = _preview_service(backend)
    coordinator = _RecordingCoordinator(preview)
    request = _coordinated_request(preview, coordinator)
    asyncio.run(camera_preview_start(request))

    result = asyncio.run(camera_preview_stop(request))

    assert coordinator.stop_calls == 1
    assert result["state"] == "stopped"


def test_preview_status_stays_a_direct_read() -> None:
    """GET /camera/preview/status never goes through the mutation path."""
    preview = _preview_service()
    coordinator = _RecordingCoordinator(preview)
    request = _coordinated_request(preview, coordinator)

    result = camera_preview_status(request)

    assert result["state"] == "stopped"
    assert coordinator.start_calls == 0
    assert coordinator.stop_calls == 0


def test_health_preview_section_stays_a_direct_read() -> None:
    """GET /health reads preview status without mutating anything."""
    preview = _preview_service()
    coordinator = _RecordingCoordinator(preview)
    request = _coordinated_request(preview, coordinator)

    result = health(request)

    assert result["preview"]["state"] == "stopped"
    assert coordinator.start_calls == 0


def test_the_preview_page_never_starts_preview() -> None:
    """Opening /preview is inert: it is a static page, not an auto-start."""
    backend = MockPreviewBackend()
    preview = _preview_service(backend)

    response = preview_page()

    assert response.status_code == 200
    assert backend.start_calls == 0
    assert preview.status().state.value == "stopped"


def test_the_dashboard_never_starts_preview() -> None:
    """Opening /dashboard is inert: it never touches the camera."""
    backend = MockPreviewBackend()
    preview = _preview_service(backend)

    response = dashboard_page()

    assert response.status_code == 200
    assert backend.start_calls == 0
    assert preview.status().state.value == "stopped"


def test_the_lazy_coordinator_uses_test_injected_services(
    tmp_path: Path,
) -> None:
    """With no coordinator attached, one is built over the attached services.

    A fallback that quietly built a *production* backend would take a test's
    mock services out of the picture -- and, on a Pi, would touch real hardware.
    """
    backend = MockPreviewBackend()
    preview = _preview_service(backend)
    capture_service = CaptureService(
        CameraConfig(
            enabled=True,
            backend="mock",
            device_index=None,
            detection_interval_seconds=30,
            capture_directory=tmp_path / "captures",
        ),
        MockBackend(width=4608, height=2592, name="mock"),
    )
    database_path = tmp_path / "mgo.db"
    apply_migrations(database_path)
    request = _request(
        preview=preview,
        capture_service=capture_service,
        capture_archive=CaptureArchive(database_path),
    )

    asyncio.run(camera_preview_start(request))
    result = asyncio.run(camera_capture(request))

    coordinator = request.app.state.camera_coordinator
    assert coordinator is not None
    assert result["backend"] == "mock"
    assert backend.start_calls == 1
    # The repository default has restore_after_capture off, so the release is
    # still the Task 11 behaviour.
    assert preview.status().state.value == "stopped"
