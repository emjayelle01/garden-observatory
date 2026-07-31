"""Tests for the camera coordinator (Task 12).

The coordinator is the single place that decides who may touch the camera. These
tests prove three separate things:

* **delegation** -- it drives the *real* ``PreviewService`` and the real capture
  service rather than reimplementing either, so idempotence, first-frame startup
  validation and the preview failure model are unchanged;
* **the capture transaction** -- the full matrix of (preview running / stopped) x
  (capture succeeds / fails) x (restoration on / off), including the rule that
  the capture's own outcome is always the answer;
* **serialisation** -- no two camera-mutating operations overlap, and a status
  read never has to wait behind one.

The concurrency tests use events rather than sleeps for the direction that must
happen, and a bounded wait only for the direction that must *not* -- an
operation blocked on a held lock cannot spuriously complete, so that direction is
never flaky. Every one of them fails if the coordinator's mutation lock is
removed.
"""

from __future__ import annotations

import ast
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

import mgo.camera.coordinator as coordinator_module
from mgo.camera.backend import MockBackend
from mgo.camera.capture import CaptureService
from mgo.camera.coordinator import CameraCoordinator
from mgo.camera.exceptions import (
    BackendCaptureError,
    CameraUnavailableError,
    PreviewStartError,
)
from mgo.camera.models import CaptureResult
from mgo.camera.preview import (
    UNEXPECTED_START_ERROR,
    PreviewService,
    PreviewState,
)
from mgo.camera.preview_backend import MockPreviewBackend, build_preview_backend
from mgo.camera.simulator import SIMULATOR_BACKEND_NAME
from mgo.core.config import CameraConfig, PreviewConfig

#: Generous upper bound for an operation that *must* complete. Never used as a
#: settling delay: every wait is on an explicit event.
_TIMEOUT = 5.0
#: Bounded window for confirming an operation is genuinely blocked. A thread
#: waiting on a held lock cannot finish, so this direction is deterministic.
_BLOCKED_WINDOW = 0.25


def _preview_config(*, enabled: bool = True) -> PreviewConfig:
    """Build a preview configuration for coordinator tests."""
    return PreviewConfig(
        enabled=enabled,
        width=1280,
        height=720,
        fps=15,
        startup_timeout_seconds=0.5,
        shutdown_timeout_seconds=0.5,
    )


def _camera_config(directory: Path) -> CameraConfig:
    """Build a camera configuration writing captures beneath ``directory``."""
    return CameraConfig(
        enabled=True,
        backend="mock",
        device_index=None,
        detection_interval_seconds=60,
        capture_directory=directory,
    )


def _preview_service(
    backend: MockPreviewBackend | None = None,
) -> PreviewService:
    """Build the real preview service over a hardware-free backend."""
    return PreviewService(_preview_config(), backend or MockPreviewBackend())


def _capture_result(name: str = "capture.jpg") -> CaptureResult:
    """Build a distinguishable capture result for identity assertions."""
    return CaptureResult(
        success=True,
        filename=name,
        absolute_path=Path("/nowhere") / name,
        timestamp=datetime(2026, 7, 30, 9, 0, tzinfo=UTC),
        width=4608,
        height=2592,
        filesize_bytes=1234,
        backend="stub",
    )


class _GatedCapture:
    """A capture service double that can be held open at will.

    ``proceed`` starts *set*, so a capture returns immediately unless a test
    clears it to hold the transaction open. ``max_active`` is the evidence for
    serialisation: with the coordinator's lock it can never exceed one.
    """

    def __init__(
        self,
        *,
        result: CaptureResult | None = None,
        error: Exception | None = None,
    ) -> None:
        self._result = result if result is not None else _capture_result()
        self._error = error
        self.entered = threading.Event()
        self.proceed = threading.Event()
        self.proceed.set()
        self.calls = 0
        self.active = 0
        self.max_active = 0

    def capture_image(self) -> CaptureResult:
        self.calls += 1
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.entered.set()
        try:
            assert self.proceed.wait(_TIMEOUT), "capture was never released"
        finally:
            self.active -= 1
        if self._error is not None:
            raise self._error
        return self._result


class _FailingPreviewBackend:
    """A preview backend that succeeds ``successes`` times, then always fails.

    Models the realistic restoration failure: preview started fine originally,
    and the camera then refuses to hand itself back after the capture.
    """

    def __init__(self, *, successes: int = 1) -> None:
        self._successes = successes
        self.start_calls = 0
        self.last_process: object | None = None

    @property
    def name(self) -> str:
        return "failing-mock"

    def start(self, config: PreviewConfig) -> object:
        self.start_calls += 1
        if self.start_calls > self._successes:
            raise PreviewStartError("camera busy")
        delegate = MockPreviewBackend()
        process = delegate.start(config)
        self.last_process = process
        return process


def _coordinator(
    capture: object,
    preview: PreviewService,
    *,
    restore_after_capture: bool = False,
) -> CameraCoordinator:
    """Build a coordinator over the supplied services."""
    return CameraCoordinator(
        capture,  # type: ignore[arg-type]
        preview,
        restore_after_capture=restore_after_capture,
    )


def _run(target: object) -> threading.Thread:
    """Start ``target`` on a daemon thread and return it."""
    thread = threading.Thread(target=target, daemon=True)  # type: ignore[arg-type]
    thread.start()
    return thread


# --- preview delegation -----------------------------------------------------


def test_start_preview_delegates_once() -> None:
    """A start reaches the preview service exactly once."""
    backend = MockPreviewBackend()
    preview = _preview_service(backend)
    coordinator = _coordinator(_GatedCapture(), preview)

    status = coordinator.start_preview()

    assert status.state is PreviewState.RUNNING
    assert backend.start_calls == 1
    assert preview.status().state is PreviewState.RUNNING
    coordinator.shutdown()


def test_repeated_start_preview_is_idempotent() -> None:
    """A second start never launches a duplicate camera process."""
    backend = MockPreviewBackend()
    coordinator = _coordinator(_GatedCapture(), _preview_service(backend))

    first = coordinator.start_preview()
    second = coordinator.start_preview()

    assert first.state is PreviewState.RUNNING
    assert second.state is PreviewState.RUNNING
    assert backend.start_calls == 1
    coordinator.shutdown()


def test_stop_preview_delegates_once() -> None:
    """A stop terminates the running preview process."""
    backend = MockPreviewBackend()
    coordinator = _coordinator(_GatedCapture(), _preview_service(backend))
    coordinator.start_preview()

    status = coordinator.stop_preview()

    assert status.state is PreviewState.STOPPED
    assert backend.last_process is not None
    assert backend.last_process.terminated is True


def test_repeated_stop_preview_is_idempotent() -> None:
    """Stopping an already-stopped preview is a no-op that still reports truth."""
    backend = MockPreviewBackend()
    coordinator = _coordinator(_GatedCapture(), _preview_service(backend))

    first = coordinator.stop_preview()
    second = coordinator.stop_preview()

    assert first.state is PreviewState.STOPPED
    assert second.state is PreviewState.STOPPED
    assert backend.start_calls == 0


def test_start_preview_failure_propagates_unchanged() -> None:
    """The coordinator does not soften or reclassify a preview start failure."""
    preview = PreviewService(
        _preview_config(),
        MockPreviewBackend(error=PreviewStartError("no encoder")),
    )
    coordinator = _coordinator(_GatedCapture(), preview)

    with pytest.raises(PreviewStartError, match="no encoder"):
        coordinator.start_preview()

    assert preview.status().state is PreviewState.FAILED


# --- capture transaction ----------------------------------------------------


def test_capture_with_preview_stopped_does_not_start_preview() -> None:
    """Restoration never starts a preview the operator had not started."""
    backend = MockPreviewBackend()
    preview = _preview_service(backend)
    coordinator = _coordinator(
        _GatedCapture(), preview, restore_after_capture=True
    )

    coordinator.capture_image()

    assert preview.status().state is PreviewState.STOPPED
    assert backend.start_calls == 0


def test_capture_releases_a_running_preview() -> None:
    """Capture takes exclusive ownership by releasing the active preview."""
    backend = MockPreviewBackend()
    preview = _preview_service(backend)
    coordinator = _coordinator(_GatedCapture(), preview)
    coordinator.start_preview()

    coordinator.capture_image()

    assert preview.status().state is PreviewState.STOPPED
    assert backend.last_process is not None
    assert backend.last_process.terminated is True


def test_successful_capture_restores_a_previously_running_preview() -> None:
    """With the policy on, preview comes back after a successful capture."""
    backend = MockPreviewBackend()
    preview = _preview_service(backend)
    coordinator = _coordinator(
        _GatedCapture(), preview, restore_after_capture=True
    )
    coordinator.start_preview()

    result = coordinator.capture_image()

    assert result.success is True
    assert preview.status().state is PreviewState.RUNNING
    # One original start plus one restoration -- never more.
    assert backend.start_calls == 2
    coordinator.shutdown()


def test_successful_capture_does_not_restore_when_the_policy_is_off() -> None:
    """With the policy off, Task 11 behaviour is preserved exactly."""
    backend = MockPreviewBackend()
    preview = _preview_service(backend)
    coordinator = _coordinator(
        _GatedCapture(), preview, restore_after_capture=False
    )
    coordinator.start_preview()

    coordinator.capture_image()

    assert preview.status().state is PreviewState.STOPPED
    assert backend.start_calls == 1


def test_failed_capture_restores_a_previously_running_preview() -> None:
    """A capture failure does not cost the operator their live preview."""
    backend = MockPreviewBackend()
    preview = _preview_service(backend)
    coordinator = _coordinator(
        _GatedCapture(error=BackendCaptureError("rpicam-still exited 1")),
        preview,
        restore_after_capture=True,
    )
    coordinator.start_preview()

    with pytest.raises(BackendCaptureError):
        coordinator.capture_image()

    assert preview.status().state is PreviewState.RUNNING
    assert backend.start_calls == 2
    coordinator.shutdown()


def test_failed_capture_preserves_the_original_exception() -> None:
    """The caller sees the capture's own exception object, not a substitute."""
    failure = CameraUnavailableError("camera is disabled")
    preview = _preview_service()
    coordinator = _coordinator(
        _GatedCapture(error=failure), preview, restore_after_capture=True
    )
    coordinator.start_preview()

    with pytest.raises(CameraUnavailableError) as excinfo:
        coordinator.capture_image()

    assert excinfo.value is failure


def test_capture_result_is_the_capture_services_own_result() -> None:
    """The coordinator returns the capture service's result object untouched."""
    expected = _capture_result("2026-07-30T09-00-00.000000Z.jpg")
    coordinator = _coordinator(
        _GatedCapture(result=expected), _preview_service()
    )

    result = coordinator.capture_image()

    assert result is expected


def test_capture_uses_the_real_capture_service(tmp_path: Path) -> None:
    """The transaction drives the production capture service end to end."""
    directory = tmp_path / "captures"
    capture_service = CaptureService(
        _camera_config(directory), MockBackend(width=4608, height=2592)
    )
    backend = MockPreviewBackend()
    preview = _preview_service(backend)
    coordinator = CameraCoordinator(
        capture_service, preview, restore_after_capture=True
    )
    coordinator.start_preview()

    result = coordinator.capture_image()

    assert result.success is True
    assert result.width == 4608
    assert result.height == 2592
    assert result.absolute_path.exists()
    assert result.filesize_bytes > 0
    assert preview.status().state is PreviewState.RUNNING
    coordinator.shutdown()


# --- restoration failure isolation ------------------------------------------


def test_restoration_failure_does_not_replace_a_successful_capture() -> None:
    """A capture that succeeded is still reported as a success.

    Telling the caller the capture failed would invite a retry, and the archive
    would gain a second record for an image that was captured once.
    """
    backend = _FailingPreviewBackend(successes=1)
    preview = PreviewService(_preview_config(), backend)  # type: ignore[arg-type]
    expected = _capture_result()
    coordinator = _coordinator(
        _GatedCapture(result=expected), preview, restore_after_capture=True
    )
    coordinator.start_preview()

    result = coordinator.capture_image()

    assert result is expected
    assert backend.start_calls == 2


def test_restoration_failure_leaves_preview_truthfully_failed() -> None:
    """Preview reports FAILED with its own error after a failed restoration."""
    backend = _FailingPreviewBackend(successes=1)
    preview = PreviewService(_preview_config(), backend)  # type: ignore[arg-type]
    coordinator = _coordinator(
        _GatedCapture(), preview, restore_after_capture=True
    )
    coordinator.start_preview()

    coordinator.capture_image()

    status = preview.status()
    assert status.state is PreviewState.FAILED
    assert status.last_error is not None
    assert "camera busy" in status.last_error
    assert status.owner is None


def test_restoration_failure_does_not_replace_a_failed_capture() -> None:
    """The original capture exception survives a failed restoration."""
    failure = BackendCaptureError("rpicam-still exited 1")
    backend = _FailingPreviewBackend(successes=1)
    preview = PreviewService(_preview_config(), backend)  # type: ignore[arg-type]
    coordinator = _coordinator(
        _GatedCapture(error=failure), preview, restore_after_capture=True
    )
    coordinator.start_preview()

    with pytest.raises(BackendCaptureError) as excinfo:
        coordinator.capture_image()

    assert excinfo.value is failure
    assert preview.status().state is PreviewState.FAILED


class _RestorationValidationExplodes(PreviewService):
    """A real preview service whose *restoration* startup fails unexpectedly.

    The first start (the operator's) validates normally; the restoration start
    launches its process and then hits an unexpected fault inside the production
    startup-validation boundary -- the situation that used to leave preview
    reporting ``STARTING`` with a live camera process and no error.
    """

    def __init__(self, *args: object, error: BaseException, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.error = error
        self.validations = 0

    def _validate_startup(self, process: object) -> str | None:
        self.validations += 1
        if self.validations > 1:
            raise self.error
        return super()._validate_startup(process)  # type: ignore[arg-type]


def _simulator_producers() -> int:
    """Count live simulator preview producer threads."""
    return sum(
        1
        for thread in threading.enumerate()
        if thread.name == "mgo-simulator-preview"
    )


def _readiness_readers() -> int:
    """Count live preview startup-readiness reader threads."""
    return sum(
        1
        for thread in threading.enumerate()
        if thread.name == "mgo-preview-readiness"
    )


def _await_no_camera_threads() -> None:
    """Wait, bounded, for every producer and readiness thread to exit."""
    deadline = time.monotonic() + _TIMEOUT
    while time.monotonic() < deadline and (
        _simulator_producers() or _readiness_readers()
    ):
        time.sleep(0.02)


def test_an_unexpected_restoration_fault_leaves_preview_truthfully_failed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The capture still succeeds, and preview reports the truth about itself.

    Logging the fault is not enough: an operator reading preview status has to
    see ``failed`` with an error, not a preview that claims to be starting while
    holding a camera nothing will ever release.
    """
    injected = RuntimeError("programming error inside startup validation")
    preview = _RestorationValidationExplodes(
        _preview_config(),
        build_preview_backend(SIMULATOR_BACKEND_NAME),
        error=injected,
    )
    expected = _capture_result()
    coordinator = _coordinator(
        _GatedCapture(result=expected), preview, restore_after_capture=True
    )
    coordinator.start_preview()
    assert _simulator_producers() == 1

    with caplog.at_level("ERROR"):
        result = coordinator.capture_image()

    # The capture outcome is untouched.
    assert result is expected

    status = preview.status()
    assert status.state is PreviewState.FAILED
    assert status.owner is None
    assert status.started_at is None
    assert status.uptime_seconds is None
    assert status.last_error == UNEXPECTED_START_ERROR

    # The restoration process was reaped: nothing holds the camera.
    _await_no_camera_threads()
    assert _simulator_producers() == 0
    assert _readiness_readers() == 0

    # The full original exception is still available for diagnosis.
    assert "programming error inside startup validation" in caplog.text
    coordinator.shutdown()


def test_an_unexpected_restoration_fault_after_a_failed_capture() -> None:
    """The original capture exception survives, and preview is still truthful."""
    failure = BackendCaptureError("rpicam-still exited 1")
    preview = _RestorationValidationExplodes(
        _preview_config(),
        build_preview_backend(SIMULATOR_BACKEND_NAME),
        error=RuntimeError("programming error"),
    )
    coordinator = _coordinator(
        _GatedCapture(error=failure), preview, restore_after_capture=True
    )
    coordinator.start_preview()

    with pytest.raises(BackendCaptureError) as excinfo:
        coordinator.capture_image()

    assert excinfo.value is failure

    status = preview.status()
    assert status.state is PreviewState.FAILED
    assert status.owner is None
    assert status.started_at is None
    assert status.uptime_seconds is None
    assert status.last_error == UNEXPECTED_START_ERROR

    _await_no_camera_threads()
    assert _simulator_producers() == 0
    assert _readiness_readers() == 0
    coordinator.shutdown()


def test_an_unexpected_restoration_fault_never_reaches_the_caller() -> None:
    """The restoration exception is not what the capture caller sees."""
    preview = _RestorationValidationExplodes(
        _preview_config(),
        build_preview_backend(SIMULATOR_BACKEND_NAME),
        error=RuntimeError("programming error"),
    )
    coordinator = _coordinator(
        _GatedCapture(), preview, restore_after_capture=True
    )
    coordinator.start_preview()

    # No RuntimeError escapes; the capture simply returns.
    result = coordinator.capture_image()

    assert result.success is True
    assert preview.status().state is PreviewState.FAILED
    _await_no_camera_threads()
    coordinator.shutdown()


# --- serialisation ----------------------------------------------------------


def test_two_captures_never_overlap() -> None:
    """A second capture waits for the first transaction to finish."""
    capture = _GatedCapture()
    capture.proceed.clear()
    coordinator = _coordinator(capture, _preview_service())
    second_done = threading.Event()

    first = _run(coordinator.capture_image)
    assert capture.entered.wait(_TIMEOUT)

    def _second() -> None:
        coordinator.capture_image()
        second_done.set()

    second = _run(_second)
    # The second capture must be blocked on the coordinator's lock: it cannot
    # have reached the capture service while the first transaction is open.
    assert not second_done.wait(_BLOCKED_WINDOW)
    assert capture.calls == 1

    capture.proceed.set()
    first.join(_TIMEOUT)
    assert second_done.wait(_TIMEOUT)
    second.join(_TIMEOUT)
    assert capture.calls == 2
    assert capture.max_active == 1


def test_preview_start_waits_for_an_active_capture() -> None:
    """A manual start cannot launch a second process during a capture."""
    capture = _GatedCapture()
    capture.proceed.clear()
    backend = MockPreviewBackend()
    coordinator = _coordinator(capture, _preview_service(backend))
    started = threading.Event()

    worker = _run(coordinator.capture_image)
    assert capture.entered.wait(_TIMEOUT)

    def _start() -> None:
        coordinator.start_preview()
        started.set()

    starter = _run(_start)
    assert not started.wait(_BLOCKED_WINDOW)
    assert backend.start_calls == 0

    capture.proceed.set()
    worker.join(_TIMEOUT)
    assert started.wait(_TIMEOUT)
    starter.join(_TIMEOUT)
    assert backend.start_calls == 1
    coordinator.shutdown()


def test_preview_stop_waits_for_an_active_capture() -> None:
    """A stop cannot interleave with a capture (or with its restoration)."""
    capture = _GatedCapture()
    capture.proceed.clear()
    backend = MockPreviewBackend()
    preview = _preview_service(backend)
    coordinator = _coordinator(capture, preview, restore_after_capture=True)
    coordinator.start_preview()
    stopped = threading.Event()

    worker = _run(coordinator.capture_image)
    assert capture.entered.wait(_TIMEOUT)

    def _stop() -> None:
        coordinator.stop_preview()
        stopped.set()

    stopper = _run(_stop)
    assert not stopped.wait(_BLOCKED_WINDOW)

    capture.proceed.set()
    worker.join(_TIMEOUT)
    assert stopped.wait(_TIMEOUT)
    stopper.join(_TIMEOUT)

    # The queued stop ran after the restoration, so it stops the restored
    # preview rather than being lost: two starts, and a stopped end state.
    assert backend.start_calls == 2
    assert preview.status().state is PreviewState.STOPPED


def test_shutdown_waits_for_an_active_capture_transaction() -> None:
    """Shutdown cannot race a restoration back into existence."""
    capture = _GatedCapture()
    capture.proceed.clear()
    backend = MockPreviewBackend()
    preview = _preview_service(backend)
    coordinator = _coordinator(capture, preview, restore_after_capture=True)
    coordinator.start_preview()
    shut_down = threading.Event()

    worker = _run(coordinator.capture_image)
    assert capture.entered.wait(_TIMEOUT)

    def _shutdown() -> None:
        coordinator.shutdown()
        shut_down.set()

    stopper = _run(_shutdown)
    assert not shut_down.wait(_BLOCKED_WINDOW)

    capture.proceed.set()
    worker.join(_TIMEOUT)
    assert shut_down.wait(_TIMEOUT)
    stopper.join(_TIMEOUT)

    assert preview.status().state is PreviewState.STOPPED
    assert backend.last_process is not None
    assert backend.last_process.terminated is True


def test_shutdown_leaves_no_preview_process() -> None:
    """Shutdown is idempotent and always releases the camera."""
    backend = MockPreviewBackend()
    preview = _preview_service(backend)
    coordinator = _coordinator(_GatedCapture(), preview)
    coordinator.start_preview()

    coordinator.shutdown()
    coordinator.shutdown()

    assert preview.status().state is PreviewState.STOPPED
    assert backend.last_process is not None
    assert backend.last_process.terminated is True
    assert backend.last_process.closed is True


def test_status_reads_never_wait_behind_a_capture() -> None:
    """An operator can always see preview state, even mid-capture."""
    capture = _GatedCapture()
    capture.proceed.clear()
    preview = _preview_service()
    coordinator = _coordinator(capture, preview, restore_after_capture=True)
    coordinator.start_preview()

    worker = _run(coordinator.capture_image)
    assert capture.entered.wait(_TIMEOUT)

    read = threading.Event()

    def _read() -> None:
        preview.status()
        preview.frame_stream()
        read.set()

    reader = _run(_read)
    # The read must complete while the capture transaction is still open.
    assert read.wait(_TIMEOUT)
    reader.join(_TIMEOUT)

    capture.proceed.set()
    worker.join(_TIMEOUT)
    coordinator.shutdown()


# --- boundaries -------------------------------------------------------------


def test_the_coordinator_knows_nothing_about_the_web_or_database_layers() -> None:
    """Its imports name only the camera domain and the standard library.

    A coordinator that reached into the archive or the web layer would hold the
    camera lock across unrelated database work, which is exactly the coupling
    this module exists to avoid.
    """
    source = Path(coordinator_module.__file__ or "").read_text(encoding="utf-8")
    tree = ast.parse(source)

    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)

    forbidden = ("fastapi", "sqlite3", "mgo.captures", "mgo.core.database")
    for module in modules:
        assert not module.startswith(forbidden), module
    assert all(
        module.startswith(("mgo.camera", "logging", "threading", "__future__"))
        for module in modules
    ), modules
