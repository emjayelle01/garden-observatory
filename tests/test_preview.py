"""Tests for the live camera preview subsystem (service + backends).

No Raspberry Pi hardware or camera tooling is required. The full PreviewService
lifecycle is exercised through :class:`MockPreviewBackend`/:class:`MockPreviewProcess`,
and the real subprocess launcher's failure mapping is verified by launching a
command that does not exist.
"""

from __future__ import annotations

import contextlib
import io
import logging
import os
import threading
import time
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import IO

import pytest

import mgo.camera.preview_backend as preview_backend_module
from mgo.camera.exceptions import (
    PreviewError,
    PreviewStartError,
    PreviewUnavailableError,
)
from mgo.camera.preview import (
    UNEXPECTED_START_ERROR,
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

#: A deliberately hostile exception message. An exception's text is arbitrary
#: application data, so the one used to prove diagnostics are safe carries every
#: category worth worrying about: Windows, POSIX and UNC paths, environment- and
#: secret-looking values, a memory address, newlines and tabs, an ANSI control
#: sequence, and Unicode direction overrides.
_HOSTILE_MESSAGE = (
    "C:\\Users\\MatthewLewis\\private.toml "
    "\\\\fileserver\\share\\camera.log "
    "/etc/garden-observatory/mgo.toml "
    "MGO_SECRET=hunter2 token=sk-live-abcdef 0xDEADBEEF"
    "\n\tsecond line\x1b[31m\u202e\u200f"
)

_HOSTILE_FRAGMENTS = (
    "MatthewLewis",
    "private.toml",
    "\\\\fileserver\\share",
    "/etc/garden-observatory",
    "MGO_SECRET",
    "hunter2",
    "sk-live-abcdef",
    "0xDEADBEEF",
    "\x1b",
    "\u202e",
    "\u200f",
    "\n",
    "\t",
)


def _assert_no_hostile_fragment(text: str | None) -> None:
    """Assert no part of :data:`_HOSTILE_MESSAGE` survived into ``text``."""
    assert text is not None
    for fragment in _HOSTILE_FRAGMENTS:
        assert fragment not in text, repr(fragment)


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


def test_unexpected_backend_error_propagates_unchanged() -> None:
    """A non-PreviewError from a backend is a defect, not a camera failure.

    Rendering it into a ``PreviewStartError`` would make a programming bug
    indistinguishable from an absent camera -- lifespan auto-start would catch
    it and serve on -- *and* would publish the exception's arbitrary text.
    """
    injected = RuntimeError(_HOSTILE_MESSAGE)
    backend = MockPreviewBackend(error=injected)
    service = _service(backend)

    with pytest.raises(RuntimeError) as excinfo:
        service.start()

    assert excinfo.value is injected
    assert not isinstance(excinfo.value, PreviewError)

    status = service.status()
    assert status.state is PreviewState.FAILED
    assert status.owner is None
    assert status.started_at is None
    assert status.uptime_seconds is None
    assert status.last_error == UNEXPECTED_START_ERROR
    _assert_no_hostile_fragment(status.last_error)
    # No process was ever launched, so nothing can be left behind.
    assert backend.last_process is None
    assert _readiness_readers() == 0


def test_the_backend_fault_traceback_reaches_the_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The detail an operator actually needs is preserved -- in the log."""
    service = _service(MockPreviewBackend(error=RuntimeError(_HOSTILE_MESSAGE)))

    with caplog.at_level("ERROR"), pytest.raises(RuntimeError):
        service.start()

    assert "Preview startup failed unexpectedly" in caplog.text
    # The full exception -- the part deliberately kept out of the API -- is
    # still there for whoever has to diagnose it.
    for fragment in ("MatthewLewis", "MGO_SECRET", "0xDEADBEEF", "RuntimeError"):
        assert fragment in caplog.text, fragment


def test_process_exit_during_startup_is_start_failure() -> None:
    """A process that exits during the startup window is a start failure."""
    backend = MockPreviewBackend(launched_dead=True)
    service = _service(backend)

    with pytest.raises(PreviewStartError, match="exited during startup"):
        service.start()

    status = service.status()
    assert status.state is PreviewState.FAILED
    assert status.last_error is not None
    # The dead process was reaped and its resources released.
    assert backend.last_process is not None
    assert backend.last_process.closed is True


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


# --- unexpected startup faults (Task 12 review correction) ------------------
#
# An *expected* preview failure already settles a truthful FAILED state. An
# UNEXPECTED exception -- a programming defect anywhere in the startup
# transaction -- used to escape with the service still reporting STARTING, still
# claiming ownership, carrying no error, and holding a live camera process. These
# tests pin the corrected invariant: cleanup and settlement happen first, the
# published diagnostic is a safe constant, and the original exception is then
# re-raised unchanged.


def _readiness_readers() -> int:
    """Count live preview startup-readiness reader threads."""
    return sum(
        1
        for thread in threading.enumerate()
        if thread.name == "mgo-preview-readiness"
    )


def _await_no_readiness_readers() -> None:
    """Wait, bounded, for every readiness reader thread to exit."""
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and _readiness_readers():
        time.sleep(0.02)


class _ValidationExplodes(PreviewService):
    """A real preview service whose startup validation fails unexpectedly.

    The fault is injected at a production boundary *after* the process has been
    launched, which is the situation that leaves a camera running.
    """

    def __init__(self, *args: object, error: BaseException, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.error = error

    def _validate_startup(self, process: PreviewProcess) -> str | None:
        raise self.error


def test_an_unexpected_startup_fault_settles_failed_and_reaps_the_process() -> None:
    """A programming defect never leaves preview STARTING with a live process."""
    backend = MockPreviewBackend()
    injected = RuntimeError("programming error inside startup validation")
    service = _ValidationExplodes(_config(), backend, error=injected)

    with pytest.raises(RuntimeError):
        service.start()

    status = service.status()
    assert status.state is PreviewState.FAILED
    assert status.owner is None
    assert status.started_at is None
    assert status.uptime_seconds is None
    assert status.last_error == UNEXPECTED_START_ERROR
    # The process this start launched was terminated and closed.
    assert backend.last_process is not None
    assert backend.last_process.terminated is True
    assert backend.last_process.closed is True


def test_an_unexpected_startup_fault_is_re_raised_unchanged() -> None:
    """The original exception object reaches the caller, not a substitute.

    Wrapping it in a PreviewError would make a programming defect
    indistinguishable from ordinary hardware absence, which is exactly the
    distinction lifespan auto-start relies on.
    """
    injected = RuntimeError("programming error")
    service = _ValidationExplodes(
        _config(), MockPreviewBackend(), error=injected
    )

    with pytest.raises(RuntimeError) as excinfo:
        service.start()

    assert excinfo.value is injected
    assert not isinstance(excinfo.value, PreviewError)


def test_the_unexpected_start_diagnostic_leaks_nothing() -> None:
    """The published diagnostic is a constant, so it cannot carry hostile text.

    An exception message is arbitrary application data: it may hold a path, a
    username, an environment value, a secret, an address or control characters.
    None of it may reach a status response.
    """
    hostile = RuntimeError(_HOSTILE_MESSAGE)
    service = _ValidationExplodes(_config(), MockPreviewBackend(), error=hostile)

    with pytest.raises(RuntimeError):
        service.start()

    last_error = service.status().last_error
    assert last_error == UNEXPECTED_START_ERROR
    _assert_no_hostile_fragment(last_error)


def test_the_original_unexpected_exception_reaches_the_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The full exception and traceback are logged, where diagnosis belongs."""
    service = _ValidationExplodes(
        _config(),
        MockPreviewBackend(),
        error=RuntimeError("programming error inside startup validation"),
    )

    with caplog.at_level("ERROR"), pytest.raises(RuntimeError):
        service.start()

    records = [
        record
        for record in caplog.records
        if "Preview startup failed unexpectedly" in record.getMessage()
    ]
    assert records, [record.getMessage() for record in caplog.records]
    assert records[0].exc_info is not None
    assert "programming error inside startup validation" in caplog.text


def test_an_unexpected_start_does_not_overwrite_a_superseded_state() -> None:
    """A concurrent stop that already won keeps its state; the process is reaped.

    Settling FAILED unconditionally would let a losing start rewrite the truth
    established by the operation that superseded it.
    """
    backend = MockPreviewBackend()

    class _StopsThenExplodes(PreviewService):
        def _validate_startup(self, process: PreviewProcess) -> str | None:
            # Supersede this start exactly as a concurrent stop would.
            self.stop()
            raise RuntimeError("programming error")

    service = _StopsThenExplodes(_config(), backend)

    with pytest.raises(RuntimeError):
        service.start()

    status = service.status()
    # The later, valid state stands.
    assert status.state is PreviewState.STOPPED
    assert status.last_error is None
    # The camera is still released either way.
    assert backend.last_process is not None
    assert backend.last_process.closed is True


class _SilentPipeProcess:
    """A preview process whose MJPEG pipe never produces a frame.

    Real enough for the production readiness reader to be created and to block,
    which is the only way to prove that reader is released when a start fails
    unexpectedly after validation has already begun.
    """

    def __init__(self) -> None:
        read_fd, self._write_fd = os.pipe()
        self._stream = os.fdopen(read_fd, "rb", buffering=0)
        self._alive = True
        self._closed = False
        self.terminated = False
        self.closed = False

    @property
    def pid(self) -> int | None:
        return 9999

    def poll(self) -> int | None:
        return None if self._alive else 0

    def terminate(self) -> None:
        self.terminated = True
        self._alive = False

    def kill(self) -> None:
        self._alive = False

    def wait(self, timeout: float | None = None) -> int | None:
        return None if self._alive else 0

    def read_error(self) -> str:
        return ""

    def frame_stream(self) -> IO[bytes]:
        return self._stream  # type: ignore[return-value]

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.closed = True
        # Closing the write end gives the blocked reader a clean EOF.
        os.close(self._write_fd)
        self._stream.close()


def test_an_unexpected_start_releases_the_readiness_reader() -> None:
    """The startup-readiness reader thread never survives a failed start.

    The fault is injected *after* the real readiness wait has run, so a reader
    thread is genuinely blocked on the process's pipe when it happens.
    """
    process = _SilentPipeProcess()

    class _Backend:
        @property
        def name(self) -> str:
            return "silent"

        def start(self, config: PreviewConfig) -> PreviewProcess:
            return process  # type: ignore[return-value]

    observed: dict[str, int] = {}

    class _FinishExplodes(PreviewService):
        def _finish_start(
            self, launched: PreviewProcess, failure: str | None
        ) -> PreviewStatus:
            # The real readiness wait has already timed out; its reader is
            # still blocked on the pipe at this instant.
            observed["readers_at_fault"] = _readiness_readers()
            raise RuntimeError("programming error")

    service = _FinishExplodes(
        _config(startup=0.15, shutdown=0.5),
        _Backend(),  # type: ignore[arg-type]
    )

    with pytest.raises(RuntimeError):
        service.start()

    assert observed["readers_at_fault"] >= 1
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and _readiness_readers():
        time.sleep(0.02)
    assert _readiness_readers() == 0
    assert process.closed is True
    assert service.status().state is PreviewState.FAILED
    assert service.status().last_error == UNEXPECTED_START_ERROR


# --- the two sides of the classification ------------------------------------


@pytest.mark.parametrize(
    "error",
    [
        PreviewUnavailableError("Preview tool 'rpicam-vid' is not installed."),
        PreviewStartError("Failed to launch preview tool 'rpicam-vid': denied"),
    ],
)
def test_expected_backend_errors_pass_through_unchanged(
    error: PreviewError,
) -> None:
    """An operational failure keeps its own type, object and message.

    These are the failures a deployment legitimately hits -- absent tooling, a
    refused launch -- and lifespan auto-start is entitled to catch them and keep
    serving. Collapsing them into the unexpected diagnostic would throw away the
    only actionable information an operator has.
    """
    service = _service(MockPreviewBackend(error=error))

    with pytest.raises(PreviewError) as excinfo:
        service.start()

    assert excinfo.value is error
    status = service.status()
    assert status.state is PreviewState.FAILED
    assert status.last_error is not None
    assert str(error) in status.last_error
    assert status.last_error != UNEXPECTED_START_ERROR


def test_the_real_launcher_maps_a_missing_command_to_unavailable() -> None:
    """The adapter -- not the service -- classifies an absent camera tool."""
    with pytest.raises(PreviewUnavailableError):
        launch_preview_subprocess(["mgo-no-such-preview-command"])


def test_the_real_launcher_maps_an_os_error_to_a_start_error() -> None:
    """An ordinary launch OSError is expected, and is mapped by the adapter."""

    def _refuse(*args: object, **kwargs: object) -> None:
        raise OSError("permission denied")

    original = preview_backend_module.subprocess.Popen
    preview_backend_module.subprocess.Popen = _refuse  # type: ignore[assignment]
    try:
        with pytest.raises(PreviewStartError):
            launch_preview_subprocess(["rpicam-vid"])
    finally:
        preview_backend_module.subprocess.Popen = original  # type: ignore[assignment]


def test_the_null_backend_remains_an_expected_unavailable_failure() -> None:
    """The null backend is a deliberate absence, never a programming defect."""
    service = PreviewService(_config(), NullPreviewBackend())

    with pytest.raises(PreviewUnavailableError):
        service.start()

    status = service.status()
    assert status.state is PreviewState.FAILED
    assert status.last_error is not None
    assert status.last_error != UNEXPECTED_START_ERROR
    assert "never starts" in status.last_error


# --- startup stream classification ------------------------------------------
#
# The readiness reader runs on another thread, so what it observed has to be
# carried back deliberately. An ordinary stream-operation failure is an expected
# camera problem; a defect in the reader path is not, and the two must not share
# a diagnostic.


class _FailingStream(io.RawIOBase):
    """A byte stream whose reads always raise the configured exception.

    ``closed_state`` controls what the stream says about itself when the reader
    asks -- and that answer, not the exception's message, is what decides
    whether a ``ValueError`` is operational.
    """

    def __init__(
        self, error: BaseException, *, closed_state: str = "open"
    ) -> None:
        super().__init__()
        self._error = error
        self._closed_state = closed_state

    @property
    def closed(self) -> bool:
        if self._closed_state == "raises":
            raise RuntimeError("this stream cannot report whether it is closed")
        return self._closed_state == "closed"

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: object) -> int:
        raise self._error


class _StreamProcess:
    """A preview process handing out a caller-supplied frame stream."""

    def __init__(self, stream: IO[bytes]) -> None:
        self._stream = stream
        self.terminated = False
        self.closed = False
        self._alive = True

    @property
    def pid(self) -> int | None:
        return 4321

    def poll(self) -> int | None:
        return None if self._alive else 0

    def terminate(self) -> None:
        self.terminated = True
        self._alive = False

    def kill(self) -> None:
        self._alive = False

    def wait(self, timeout: float | None = None) -> int | None:
        return None if self._alive else 0

    def read_error(self) -> str:
        return ""

    def frame_stream(self) -> IO[bytes]:
        return self._stream

    def close(self) -> None:
        self.closed = True
        self._stream.close()


def _service_over_stream(
    stream: IO[bytes],
) -> tuple[PreviewService, _StreamProcess]:
    """Build a service whose preview process hands out ``stream``."""
    process = _StreamProcess(stream)

    class _Backend:
        @property
        def name(self) -> str:
            return "stream"

        def start(self, config: PreviewConfig) -> PreviewProcess:
            return process  # type: ignore[return-value]

    service = PreviewService(
        _config(startup=1.0, shutdown=0.5),
        _Backend(),  # type: ignore[arg-type]
    )
    return service, process


def _stream_service(
    error: BaseException, *, closed_state: str = "open"
) -> tuple[PreviewService, _StreamProcess]:
    """Build a service whose process stream fails with ``error`` on read."""
    return _service_over_stream(
        _FailingStream(error, closed_state=closed_state)  # type: ignore[arg-type]
    )


def test_an_expected_stream_io_failure_is_an_operational_start_error() -> None:
    """A stream that cannot be read is a camera problem, not a defect."""
    service, process = _stream_service(OSError(_HOSTILE_MESSAGE))

    with pytest.raises(PreviewStartError) as excinfo:
        service.start()

    assert isinstance(excinfo.value, PreviewError)
    status = service.status()
    assert status.state is PreviewState.FAILED
    assert status.last_error is not None
    assert "stream read failed during startup" in status.last_error
    # The exception's own text names a device or a path and helps nobody here.
    _assert_no_hostile_fragment(status.last_error)
    _assert_no_hostile_fragment(str(excinfo.value))
    assert process.closed is True
    _await_no_readiness_readers()


def test_a_genuinely_closed_stream_value_error_is_operational() -> None:
    """The real "read from a closed file" case stays an expected failure.

    A genuinely closed :class:`io.BytesIO` is used rather than a double, so the
    ``ValueError`` is Python's own and the stream's closed state is real.
    """
    closed = io.BytesIO(b"")
    closed.close()
    service, process = _service_over_stream(closed)

    with pytest.raises(PreviewStartError, match="stream read failed"):
        service.start()

    assert closed.closed is True
    assert service.status().state is PreviewState.FAILED
    assert service.status().last_error != UNEXPECTED_START_ERROR
    assert process.closed is True
    _await_no_readiness_readers()


def test_a_value_error_from_an_open_stream_is_a_programming_fault() -> None:
    """An open stream that raises ValueError is a defect, not a camera problem.

    Treating every ``ValueError`` as operational made auto-start swallow a bug
    in the read path and keep serving. The stream's own state is the authority:
    this one reports itself open at the instant it raises.
    """
    injected = ValueError("frame index -1 is not valid")
    stream = _FailingStream(injected)
    service, process = _service_over_stream(stream)  # type: ignore[arg-type]

    with pytest.raises(ValueError) as excinfo:
        service.start()

    assert stream.closed is False
    assert excinfo.value is injected
    assert not isinstance(excinfo.value, PreviewError)
    status = service.status()
    assert status.state is PreviewState.FAILED
    assert status.owner is None
    assert status.started_at is None
    assert status.uptime_seconds is None
    assert status.last_error == UNEXPECTED_START_ERROR
    assert process.closed is True
    _await_no_readiness_readers()


def test_a_misleading_value_error_message_does_not_make_it_operational() -> None:
    """Classification is by stream state, never by matching the message.

    A defect can produce any text it likes, including text that reads exactly
    like the closed-file error -- so the message must not be the test.
    """
    injected = ValueError("I/O operation on closed file")
    stream = _FailingStream(injected)
    service, _process = _service_over_stream(stream)  # type: ignore[arg-type]

    with pytest.raises(ValueError) as excinfo:
        service.start()

    assert stream.closed is False
    assert excinfo.value is injected
    assert service.status().last_error == UNEXPECTED_START_ERROR
    _await_no_readiness_readers()


def test_an_unprovable_closed_state_keeps_the_stricter_classification() -> None:
    """A stream that cannot answer has not proven itself closed.

    The exception raised while asking must not replace the read's own
    ``ValueError`` either -- the caller has to see what actually went wrong.
    """
    injected = ValueError("frame index -1 is not valid")
    service, process = _stream_service(injected, closed_state="raises")

    with pytest.raises(ValueError) as excinfo:
        service.start()

    assert excinfo.value is injected
    assert "cannot report whether it is closed" not in str(excinfo.value)
    status = service.status()
    assert status.state is PreviewState.FAILED
    assert status.last_error == UNEXPECTED_START_ERROR
    assert process.closed is True
    _await_no_readiness_readers()


def test_an_unexpected_stream_fault_propagates_unchanged() -> None:
    """A defect in the reader path keeps its own exception object."""
    injected = RuntimeError(_HOSTILE_MESSAGE)
    service, process = _stream_service(injected)

    with pytest.raises(RuntimeError) as excinfo:
        service.start()

    assert excinfo.value is injected
    assert not isinstance(excinfo.value, PreviewError)
    status = service.status()
    assert status.state is PreviewState.FAILED
    assert status.owner is None
    assert status.started_at is None
    assert status.uptime_seconds is None
    assert status.last_error == UNEXPECTED_START_ERROR
    _assert_no_hostile_fragment(status.last_error)
    assert process.closed is True
    _await_no_readiness_readers()


def test_the_readiness_reader_never_raises_into_its_own_thread() -> None:
    """Whatever the reader hits, the thread ends normally.

    An exception escaping a thread would surface as an unhandled-thread warning
    and, worse, would be reported nowhere the caller can classify it.
    """
    cases: list[tuple[BaseException, str]] = [
        (OSError(_HOSTILE_MESSAGE), "open"),
        (ValueError("I/O operation on closed file"), "closed"),
        (ValueError("frame index -1 is not valid"), "open"),
        (ValueError("frame index -1 is not valid"), "raises"),
        (RuntimeError(_HOSTILE_MESSAGE), "open"),
    ]
    for error, closed_state in cases:
        service, _process = _stream_service(error, closed_state=closed_state)
        with contextlib.suppress(PreviewError, RuntimeError, ValueError):
            service.start()
        _await_no_readiness_readers()
        assert _readiness_readers() == 0


def test_expected_start_failures_keep_their_own_error() -> None:
    """The correction does not overwrite an expected failure's diagnostic."""
    service = _service(MockPreviewBackend(error=PreviewStartError("no encoder")))

    with pytest.raises(PreviewStartError):
        service.start()

    status = service.status()
    assert status.state is PreviewState.FAILED
    assert status.last_error is not None
    assert "no encoder" in status.last_error
    assert status.last_error != UNEXPECTED_START_ERROR


@pytest.mark.parametrize("command", ["rpicam-vid", "libcamera-vid"])
def test_the_preview_command_array_is_exactly_preserved(command: str) -> None:
    """Task 12 adds no argument to the production preview command.

    Pinned as an *exact* array rather than a membership check: the physical
    camera acceptance procedure evaluates the current default autofocus,
    exposure and white-balance behaviour first, so no tuning flag may be
    smuggled into the command before that assessment has happened.
    """
    captured: dict[str, Sequence[str]] = {}

    def launcher(args: Sequence[str]) -> PreviewProcess:
        captured["args"] = tuple(args)
        return MockPreviewProcess()

    backend = RPiCamPreviewBackend(command, launcher=launcher)
    backend.start(_config(width=1280, height=720, fps=15))

    assert captured["args"] == (
        command,
        "--nopreview",
        "--codec",
        "mjpeg",
        "--width",
        "1280",
        "--height",
        "720",
        "--framerate",
        "15",
        "--flush",
        "--timeout",
        "0",
        "--output",
        "-",
    )
    # No autofocus, exposure, white-balance, ROI or lens-position tuning.
    for forbidden in (
        "--autofocus-mode",
        "--autofocus-range",
        "--autofocus-speed",
        "--autofocus-window",
        "--autofocus-on-capture",
        "--lens-position",
        "--exposure",
        "--awb",
        "--roi",
    ):
        assert forbidden not in captured["args"]


def test_managed_preview_settings_do_not_reach_the_command() -> None:
    """The managed policies are application behaviour, not camera arguments."""
    captured: dict[str, Sequence[str]] = {}

    def launcher(args: Sequence[str]) -> PreviewProcess:
        captured["args"] = tuple(args)
        return MockPreviewProcess()

    managed = PreviewConfig(
        enabled=True,
        width=1280,
        height=720,
        fps=15,
        startup_timeout_seconds=5.0,
        shutdown_timeout_seconds=5.0,
        auto_start=True,
        restore_after_capture=True,
    )
    RPiCamPreviewBackend("rpicam-vid", launcher=launcher).start(managed)

    assert not any(
        "auto_start" in arg or "restore" in arg for arg in captured["args"]
    )


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
    # Explicit MJPEG output to stdout (the streaming contract), not libav.
    assert "--codec" in args
    assert args[args.index("--codec") + 1] == "mjpeg"
    assert args[args.index("--output") + 1] == "-"
    assert not any("libav" in arg for arg in args)


def test_launch_subprocess_missing_command_is_unavailable() -> None:
    """The real launcher maps a missing command to PreviewUnavailableError."""
    with pytest.raises(PreviewUnavailableError):
        launch_preview_subprocess(["mgo-nonexistent-preview-tool-xyz"])


def test_preview_status_is_a_dataclass_instance() -> None:
    """status() returns the typed PreviewStatus value object."""
    assert isinstance(_service().status(), PreviewStatus)
