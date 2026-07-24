"""Regression tests for the Task 2D/2E production preview defect.

Two production defects are covered:

1. The rpicam-vid command lacked an explicit codec, so it defaulted to H.264
   through libav, which cannot infer an output container from ``-`` (stdout).
   The command now explicitly requests MJPEG frames.
2. ``PreviewService.start`` treated process creation as success, so a process
   that exited during camera/encoder setup was briefly reported RUNNING. Start
   now validates that the process stays up before reporting RUNNING.

All process behaviour is faked; no Raspberry Pi hardware is required.
"""

from __future__ import annotations

import contextlib
import io
from collections.abc import Sequence
from datetime import UTC, datetime

from mgo.camera.exceptions import PreviewStartError
from mgo.camera.preview import PreviewService, PreviewState
from mgo.camera.preview_backend import (
    MockPreviewBackend,
    MockPreviewProcess,
    PreviewProcess,
    RPiCamPreviewBackend,
)
from mgo.camera.streaming import PreviewProcessFrameSource, parse_mjpeg_frames
from mgo.core.config import PreviewConfig

_T0 = datetime(2026, 7, 24, 9, 0, 0, tzinfo=UTC)


def _config(*, enabled: bool = True) -> PreviewConfig:
    return PreviewConfig(
        enabled=enabled,
        width=1280,
        height=720,
        fps=15,
        startup_timeout_seconds=5.0,
        shutdown_timeout_seconds=5.0,
    )


def _service(backend: MockPreviewBackend) -> PreviewService:
    return PreviewService(_config(), backend, clock=lambda: _T0)


def _captured_args(
    width: int = 1280, height: int = 720, fps: int = 15
) -> Sequence[str]:
    """Return the argument list the rpicam preview backend would launch."""
    captured: dict[str, Sequence[str]] = {}

    def launcher(args: Sequence[str]) -> PreviewProcess:
        captured["args"] = tuple(args)
        return MockPreviewProcess()

    backend = RPiCamPreviewBackend("rpicam-vid", launcher=launcher)
    backend.start(
        PreviewConfig(
            enabled=True,
            width=width,
            height=height,
            fps=fps,
            startup_timeout_seconds=5.0,
            shutdown_timeout_seconds=5.0,
        )
    )
    return captured["args"]


# --- A. command construction ----------------------------------------------


def test_command_requests_mjpeg_codec_to_stdout() -> None:
    """rpicam-vid explicitly emits MJPEG frames to stdout."""
    args = _captured_args()

    assert args[0] == "rpicam-vid"
    assert "--codec" in args
    assert args[args.index("--codec") + 1] == "mjpeg"
    assert "--output" in args
    assert args[args.index("--output") + 1] == "-"


def test_command_includes_resolution_framerate_and_continuous_timeout() -> None:
    """Width, height, framerate are passed and the timeout is continuous."""
    args = _captured_args(width=640, height=480, fps=24)

    assert args[args.index("--width") + 1] == "640"
    assert args[args.index("--height") + 1] == "480"
    assert args[args.index("--framerate") + 1] == "24"
    assert args[args.index("--timeout") + 1] == "0"


def test_command_is_shell_free_argument_list_without_libav() -> None:
    """The command is a plain argument list and introduces no libav output."""
    args = _captured_args()

    assert isinstance(args, tuple)
    assert all(isinstance(arg, str) for arg in args)
    assert not any("libav" in arg for arg in args)
    # No shell metacharacters are smuggled into any single argument.
    assert not any((";" in arg or "|" in arg or "&" in arg) for arg in args)


# --- B. successful startup -------------------------------------------------


def test_successful_startup_transitions_to_running() -> None:
    """A process that stays up through validation reaches RUNNING."""
    backend = MockPreviewBackend()
    service = _service(backend)

    status = service.start()

    assert status.state is PreviewState.RUNNING
    assert status.owner == "preview"
    assert status.started_at is not None
    assert status.last_error is None
    assert backend.start_calls == 1


# --- C. early process exit -------------------------------------------------


def test_early_exit_reports_failed_not_running() -> None:
    """A process that exits during startup fails instead of reporting RUNNING."""
    backend = MockPreviewBackend(launched_dead=True)
    service = _service(backend)

    raised = False
    try:
        service.start()
    except PreviewStartError as exc:
        raised = True
        assert "exited during startup" in str(exc)
    assert raised

    status = service.status()
    assert status.state is PreviewState.FAILED
    assert status.owner is None
    assert status.started_at is None
    assert status.last_error is not None


def test_early_exit_retains_stderr_in_last_error() -> None:
    """Startup failure surfaces the process's stderr in last_error."""
    process = MockPreviewProcess(launched_dead=True, error_text="camera busy")
    backend = MockPreviewBackend()
    # Force the backend to hand out our stderr-bearing dead process.
    backend.last_process = process
    service = PreviewService(
        _config(), _FixedProcessBackend(process), clock=lambda: _T0
    )

    raised = False
    try:
        service.start()
    except PreviewStartError:
        raised = True
    assert raised

    status = service.status()
    assert status.state is PreviewState.FAILED
    assert status.last_error is not None
    assert "camera busy" in status.last_error


def test_early_exit_reaps_and_cleans_up_process() -> None:
    """A failed startup fully closes/reaps the process; no stale reference."""
    process = MockPreviewProcess(launched_dead=True)
    service = PreviewService(
        _config(), _FixedProcessBackend(process), clock=lambda: _T0
    )

    with contextlib.suppress(PreviewStartError):
        service.start()

    assert process.closed is True
    # No stale process is retained: streaming exposes no frame stream.
    assert service.frame_stream() is None


# --- D. idempotent healthy start ------------------------------------------


def test_idempotent_start_while_running_keeps_same_process() -> None:
    """A second start while genuinely running launches no new process."""
    backend = MockPreviewBackend()
    service = _service(backend)

    first = service.start()
    process = backend.last_process
    second = service.start()

    assert first.state is PreviewState.RUNNING
    assert second.state is PreviewState.RUNNING
    assert backend.start_calls == 1
    assert backend.last_process is process
    assert second.started_at == first.started_at


# --- E. recovery after failure --------------------------------------------


def test_recovery_after_failed_startup() -> None:
    """After a failed startup, a later explicit start creates exactly one process."""
    backend = MockPreviewBackend(launched_dead=True)
    service = _service(backend)
    with contextlib.suppress(PreviewStartError):
        service.start()
    assert service.status().state is PreviewState.FAILED

    # The next start uses a healthy backend and succeeds.
    healthy = MockPreviewBackend()
    service = PreviewService(_config(), healthy, clock=lambda: _T0)
    status = service.start()

    assert status.state is PreviewState.RUNNING
    assert healthy.start_calls == 1


# --- F. stop behaviour -----------------------------------------------------


def test_stop_from_failed_transitions_to_stopped() -> None:
    """Stop after a failed startup settles to STOPPED and is idempotent."""
    backend = MockPreviewBackend(launched_dead=True)
    service = _service(backend)
    with contextlib.suppress(PreviewStartError):
        service.start()

    first = service.stop()
    second = service.stop()

    assert first.state is PreviewState.STOPPED
    assert second.state is PreviewState.STOPPED
    assert first.owner is None


# --- G. streaming compatibility -------------------------------------------


class _Provider:
    def __init__(self, stream: io.BytesIO | None) -> None:
        self._stream = stream

    def frame_stream(self) -> io.BytesIO | None:
        return self._stream


def test_mjpeg_bytes_remain_demultiplexable() -> None:
    """Concatenated MJPEG frames (as --codec mjpeg emits) stay extractable."""
    frame_a = b"\xff\xd8" + b"first-frame" + b"\xff\xd9"
    frame_b = b"\xff\xd8" + b"second-frame" + b"\xff\xd9"
    provider = _Provider(io.BytesIO(frame_a + frame_b))

    # The streaming layer's single reader extracts complete JPEG frames; the
    # startup validator never touches this stdout stream.
    frames = list(PreviewProcessFrameSource(provider).frames())

    assert frames == [frame_a, frame_b]
    # And the raw demultiplexer agrees on frame boundaries.
    assert list(parse_mjpeg_frames(io.BytesIO(frame_a + frame_b))) == [
        frame_a,
        frame_b,
    ]


# --- H. capture interaction unchanged -------------------------------------


def test_capture_release_still_stops_preview_without_restart() -> None:
    """release_for_capture still stops a running preview and never restarts it."""
    backend = MockPreviewBackend()
    service = _service(backend)
    service.start()
    process = backend.last_process
    assert process is not None

    service.release_for_capture()

    assert service.status().state is PreviewState.STOPPED
    assert process.terminated is True
    # Preview is not auto-restarted.
    assert backend.start_calls == 1


class _FixedProcessBackend:
    """A preview backend that hands out one pre-built process (for tests)."""

    def __init__(self, process: MockPreviewProcess) -> None:
        self._process = process

    @property
    def name(self) -> str:
        return "fixed"

    def start(self, config: PreviewConfig) -> PreviewProcess:
        return self._process
