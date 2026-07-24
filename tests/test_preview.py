"""Tests for the live camera preview subsystem (service + backends).

No Raspberry Pi hardware or camera tooling is required. The full PreviewService
lifecycle is exercised through :class:`MockPreviewBackend`/:class:`MockPreviewProcess`,
and the real subprocess launcher's failure mapping is verified by launching a
command that does not exist.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import pytest

from mgo.camera.exceptions import PreviewStartError, PreviewUnavailableError
from mgo.camera.preview import (
    PreviewService,
    PreviewState,
    PreviewStatus,
)
from mgo.camera.preview_backend import (
    MockPreviewBackend,
    MockPreviewProcess,
    NullPreviewBackend,
    PreviewProcess,
    RPiCamPreviewBackend,
    build_preview_backend,
    launch_preview_subprocess,
)
from mgo.core.config import PreviewConfig

_T0 = datetime(2026, 7, 24, 9, 0, 0, tzinfo=UTC)


class _FakeClock:
    """A settable clock so uptime is deterministic in tests."""

    def __init__(self, now: datetime = _T0) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def _config(
    *,
    enabled: bool = True,
    width: int = 1280,
    height: int = 720,
    fps: int = 15,
    startup: float = 5.0,
    shutdown: float = 5.0,
) -> PreviewConfig:
    """Build a preview configuration for tests."""
    return PreviewConfig(
        enabled=enabled,
        width=width,
        height=height,
        fps=fps,
        startup_timeout_seconds=startup,
        shutdown_timeout_seconds=shutdown,
    )


def _service(
    backend: MockPreviewBackend | None = None,
    *,
    enabled: bool = True,
    clock: _FakeClock | None = None,
) -> PreviewService:
    """Build a preview service over a mock backend."""
    return PreviewService(
        _config(enabled=enabled),
        backend or MockPreviewBackend(),
        clock=clock or _FakeClock(),
    )


# --- lifecycle ------------------------------------------------------------


def test_initial_state_is_stopped() -> None:
    """A fresh service is STOPPED and owns nothing."""
    status = _service().status()

    assert status.state is PreviewState.STOPPED
    assert status.owner is None
    assert status.enabled is True
    assert status.uptime_seconds is None
    assert status.last_error is None


def test_start_transitions_to_running() -> None:
    """Starting launches exactly one process and reports RUNNING/ownership."""
    backend = MockPreviewBackend()
    service = _service(backend)

    status = service.start()

    assert status.state is PreviewState.RUNNING
    assert status.owner == "preview"
    assert status.started_at is not None
    assert backend.start_calls == 1


def test_start_is_idempotent() -> None:
    """A second start while running does not launch a duplicate process."""
    backend = MockPreviewBackend()
    service = _service(backend)

    first = service.start()
    second = service.start()

    assert first.state is PreviewState.RUNNING
    assert second.state is PreviewState.RUNNING
    assert backend.start_calls == 1


def test_stop_transitions_to_stopped_and_terminates_process() -> None:
    """Stopping gracefully terminates the process and clears ownership."""
    backend = MockPreviewBackend()
    service = _service(backend)
    service.start()
    process = backend.last_process
    assert process is not None

    status = service.stop()

    assert status.state is PreviewState.STOPPED
    assert status.owner is None
    assert status.started_at is None
    assert process.terminated is True
    assert process.closed is True


def test_stop_is_idempotent() -> None:
    """Stopping an already-stopped preview is a safe no-op."""
    service = _service()

    first = service.stop()
    second = service.stop()

    assert first.state is PreviewState.STOPPED
    assert second.state is PreviewState.STOPPED


def test_start_stop_start_relaunches() -> None:
    """After a stop, starting again launches a fresh process."""
    backend = MockPreviewBackend()
    service = _service(backend)

    service.start()
    service.stop()
    status = service.start()

    assert status.state is PreviewState.RUNNING
    assert backend.start_calls == 2


# --- disabled / failure modes --------------------------------------------


def test_disabled_preview_start_raises() -> None:
    """Starting a disabled preview raises and leaves the state STOPPED."""
    backend = MockPreviewBackend()
    service = _service(backend, enabled=False)

    with pytest.raises(PreviewUnavailableError):
        service.start()

    status = service.status()
    assert status.state is PreviewState.STOPPED
    assert status.enabled is False
    assert backend.start_calls == 0


def test_failed_launch_transitions_to_failed() -> None:
    """A backend that fails to start leaves the service FAILED with an error."""
    backend = MockPreviewBackend(error=PreviewStartError("no encoder"))
    service = _service(backend)

    with pytest.raises(PreviewStartError, match="no encoder"):
        service.start()

    status = service.status()
    assert status.state is PreviewState.FAILED
    assert status.last_error is not None
    assert "no encoder" in status.last_error


def test_unexpected_backend_error_is_wrapped_as_start_error() -> None:
    """A non-preview backend exception is wrapped and marks the service FAILED."""
    backend = MockPreviewBackend(error=RuntimeError("boom"))
    service = _service(backend)

    with pytest.raises(PreviewStartError):
        service.start()

    assert service.status().state is PreviewState.FAILED


def test_immediate_process_exit_is_start_failure() -> None:
    """A process that exits immediately after launch is a start failure."""
    backend = MockPreviewBackend(launched_dead=True)
    service = _service(backend)

    with pytest.raises(PreviewStartError, match="exited immediately"):
        service.start()

    status = service.status()
    assert status.state is PreviewState.FAILED
    assert status.last_error is not None


def test_unexpected_exit_reconciles_to_failed() -> None:
    """A running process that dies unexpectedly is reconciled to FAILED."""
    backend = MockPreviewBackend()
    service = _service(backend)
    service.start()
    assert backend.last_process is not None

    # Simulate the process crashing while "running".
    backend.last_process.force_exit(code=3)
    status = service.status()

    assert status.state is PreviewState.FAILED
    assert status.owner is None
    assert status.last_error is not None
    assert "code 3" in status.last_error


def test_start_recovers_from_failed_state() -> None:
    """After a FAILED reconciliation, a fresh start succeeds."""
    backend = MockPreviewBackend()
    service = _service(backend)
    service.start()
    assert backend.last_process is not None
    backend.last_process.force_exit(code=1)
    assert service.status().state is PreviewState.FAILED

    status = service.start()

    assert status.state is PreviewState.RUNNING
    assert backend.start_calls == 2


def test_stubborn_process_is_force_killed_on_stop() -> None:
    """A process that ignores terminate() is force-killed during stop."""
    backend = MockPreviewBackend(stops_on_terminate=False)
    service = _service(backend)
    service.start()
    process = backend.last_process
    assert process is not None

    status = service.stop()

    assert status.state is PreviewState.STOPPED
    assert process.terminated is True
    assert process.killed is True


# --- camera ownership -----------------------------------------------------


def test_release_for_capture_stops_active_preview(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A capture releases the camera by stopping an active preview."""
    backend = MockPreviewBackend()
    service = _service(backend)
    service.start()
    process = backend.last_process
    assert process is not None

    with caplog.at_level(logging.INFO):
        service.release_for_capture()

    status = service.status()
    assert status.state is PreviewState.STOPPED
    assert status.owner is None
    assert process.terminated is True
    assert any(
        "transferring camera ownership to capture" in record.message
        for record in caplog.records
    )


def test_release_for_capture_is_noop_when_stopped() -> None:
    """Releasing the camera when preview is stopped does nothing."""
    backend = MockPreviewBackend()
    service = _service(backend)

    service.release_for_capture()

    assert service.status().state is PreviewState.STOPPED
    assert backend.start_calls == 0


def test_release_for_capture_does_not_restart_preview() -> None:
    """Preview stays stopped after a capture release (no auto-restart)."""
    backend = MockPreviewBackend()
    service = _service(backend)
    service.start()

    service.release_for_capture()

    assert service.status().state is PreviewState.STOPPED
    assert backend.start_calls == 1


def test_shutdown_stops_running_preview() -> None:
    """Application shutdown stops preview so no orphan process remains."""
    backend = MockPreviewBackend()
    service = _service(backend)
    service.start()
    process = backend.last_process
    assert process is not None

    service.shutdown()

    assert service.status().state is PreviewState.STOPPED
    assert process.terminated is True


# --- status shape / uptime ------------------------------------------------


def test_status_reports_resolution_and_fps() -> None:
    """Status reflects the configured resolution and frame rate."""
    service = PreviewService(
        _config(width=640, height=480, fps=30),
        MockPreviewBackend(name="mock-vid"),
    )

    status = service.status()

    assert status.resolution == "640x480"
    assert status.fps == 30
    assert status.backend == "mock-vid"


def test_uptime_is_computed_from_started_at() -> None:
    """Uptime grows with wall-clock time while running."""
    clock = _FakeClock(_T0)
    service = _service(clock=clock)
    service.start()

    clock.now = _T0 + timedelta(seconds=12.5)
    status = service.status()

    assert status.uptime_seconds == 12.5


def test_status_as_dict_is_json_shaped() -> None:
    """The status serialises to the documented API shape."""
    service = _service()
    service.start()

    payload = service.status().as_dict()

    assert set(payload) == {
        "enabled",
        "state",
        "backend",
        "owner",
        "started_at",
        "uptime_seconds",
        "resolution",
        "fps",
        "last_error",
    }
    assert payload["state"] == "running"
    assert payload["owner"] == "preview"


def test_health_dict_is_compact() -> None:
    """The health projection carries only the compact preview fields."""
    payload = _service().status().health_dict()

    assert set(payload) == {"enabled", "state", "owner", "uptime_seconds"}


# --- backends -------------------------------------------------------------


def test_build_preview_backend_selects_backend() -> None:
    """The factory maps backend names to the right preview adapters."""
    rpicam = build_preview_backend("rpicam")
    libcamera = build_preview_backend("libcamera")
    null = build_preview_backend("null")

    assert isinstance(rpicam, RPiCamPreviewBackend)
    assert rpicam.name == "rpicam-vid"
    assert isinstance(libcamera, RPiCamPreviewBackend)
    assert libcamera.name == "libcamera-vid"
    assert isinstance(null, NullPreviewBackend)


def test_build_preview_backend_rejects_unknown() -> None:
    """An unknown backend name raises a clear error."""
    with pytest.raises(ValueError, match="Unsupported camera backend"):
        build_preview_backend("webcam")


def test_null_preview_backend_is_unavailable() -> None:
    """The null preview backend never starts a process."""
    with pytest.raises(PreviewUnavailableError):
        NullPreviewBackend().start(_config())


def test_rpicam_preview_backend_builds_expected_args() -> None:
    """The preview command is a shell-free argument array with resolution/fps."""
    captured: dict[str, Sequence[str]] = {}

    def launcher(args: Sequence[str]) -> PreviewProcess:
        captured["args"] = tuple(args)
        return MockPreviewProcess()

    backend = RPiCamPreviewBackend("rpicam-vid", launcher=launcher)
    backend.start(_config(width=800, height=600, fps=24))

    args = captured["args"]
    assert args[0] == "rpicam-vid"
    assert "--nopreview" in args
    assert "800" in args
    assert "600" in args
    assert "24" in args
    assert "--output" in args


def test_launch_subprocess_missing_command_is_unavailable() -> None:
    """The real launcher maps a missing command to PreviewUnavailableError."""
    with pytest.raises(PreviewUnavailableError):
        launch_preview_subprocess(["mgo-nonexistent-preview-tool-xyz"])


def test_preview_status_is_a_dataclass_instance() -> None:
    """status() returns the typed PreviewStatus value object."""
    assert isinstance(_service().status(), PreviewStatus)
