"""Regression tests at the real preview subprocess boundary.

These verify the corrected stdout contract: the launcher captures stdout as a
binary pipe (not DEVNULL), the process handle exposes that pipe as the MJPEG
frame stream, the streaming reader consumes it, and the pipe/stderr resources
are released across every teardown path. Process behaviour is faked; no
Raspberry Pi hardware is required.
"""

from __future__ import annotations

import io
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import pytest

from mgo.camera.preview import PreviewService, PreviewState
from mgo.camera.preview_backend import (
    SubprocessPreviewProcess,
    launch_preview_subprocess,
)
from mgo.camera.streaming import PreviewProcessFrameSource, parse_mjpeg_frames
from mgo.core.config import PreviewConfig

_FRAME_A = b"\xff\xd8" + b"first-frame-data" + b"\xff\xd9"
_FRAME_B = b"\xff\xd8" + b"second-frame-data" + b"\xff\xd9"
_NOW = datetime(2026, 7, 24, 9, 0, 0, tzinfo=UTC)


class _TrackingStream(io.BytesIO):
    """A BytesIO that records how many times it was closed."""

    def __init__(self, data: bytes = b"") -> None:
        super().__init__(data)
        self.close_count = 0

    def close(self) -> None:
        self.close_count += 1
        super().close()


class _FakePopen:
    """A minimal stand-in for subprocess.Popen with a binary stdout stream."""

    def __init__(
        self, stdout: io.BytesIO | None, returncode: int | None = None
    ) -> None:
        self.stdout = stdout
        self.pid = 4321
        self._returncode = returncode
        self.terminated = False
        self.killed = False

    def poll(self) -> int | None:
        return self._returncode

    def wait(self, timeout: float | None = None) -> int | None:
        return self._returncode

    def terminate(self) -> None:
        self.terminated = True
        if self._returncode is None:
            self._returncode = 0

    def kill(self) -> None:
        self.killed = True
        self._returncode = -9


def _config() -> PreviewConfig:
    return PreviewConfig(
        enabled=True,
        width=1280,
        height=720,
        fps=15,
        startup_timeout_seconds=5.0,
        shutdown_timeout_seconds=5.0,
    )


def _handle(
    stdout: io.BytesIO | None, *, returncode: int | None = None, stderr: bool = True
) -> tuple[SubprocessPreviewProcess, Path | None]:
    """Build a real SubprocessPreviewProcess around a fake Popen."""
    stderr_path: Path | None = None
    if stderr:
        tmp = tempfile.NamedTemporaryFile(  # noqa: SIM115
            prefix="mgo-preview-test-", suffix=".log", delete=False
        )
        tmp.write(b"diagnostic stderr")
        tmp.close()
        stderr_path = Path(tmp.name)
    process = _FakePopen(stdout, returncode=returncode)
    return SubprocessPreviewProcess(process, stderr_path), stderr_path  # type: ignore[arg-type]


class _FixedHandleBackend:
    """A preview backend that hands out one pre-built process handle."""

    def __init__(self, handle: SubprocessPreviewProcess) -> None:
        self._handle = handle

    @property
    def name(self) -> str:
        return "rpicam-vid"

    def start(self, config: PreviewConfig) -> SubprocessPreviewProcess:
        return self._handle


# --- A. real launcher stdout contract -------------------------------------


def test_launcher_captures_stdout_as_pipe(monkeypatch: pytest.MonkeyPatch) -> None:
    """launch_preview_subprocess must request stdout=PIPE, not DEVNULL."""
    recorded: dict[str, object] = {}

    def fake_popen(args: object, **kwargs: object) -> _FakePopen:
        recorded["args"] = args
        recorded["kwargs"] = kwargs
        return _FakePopen(io.BytesIO(b""))

    monkeypatch.setattr(
        "mgo.camera.preview_backend.subprocess.Popen", fake_popen
    )

    handle = launch_preview_subprocess(["rpicam-vid", "--codec", "mjpeg"])
    handle.close()

    kwargs = recorded["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs["stdout"] is subprocess.PIPE
    assert kwargs["stdout"] is not subprocess.DEVNULL
    # stderr is still redirected to the separate temporary file.
    assert kwargs["stderr"] is not None
    assert "mgo-preview-" in getattr(kwargs["stderr"], "name", "")
    # The command is a shell-free list; shell=True is never passed.
    assert isinstance(recorded["args"], list)
    assert "shell" not in kwargs


# --- B. real frame_stream contract ----------------------------------------


def test_frame_stream_returns_the_stdout_pipe() -> None:
    """frame_stream() exposes the exact stdout stream, and it is not None."""
    stdout = io.BytesIO(_FRAME_A + _FRAME_B)
    handle, stderr_path = _handle(stdout)

    assert handle.frame_stream() is stdout
    assert handle.frame_stream() is not None
    # Bytes on that stream are visible to the streaming frame source.
    frames = list(PreviewProcessFrameSource(_Provider(handle)).frames())
    assert frames == [_FRAME_A, _FRAME_B]

    handle.close()
    if stderr_path is not None:
        assert not stderr_path.exists()


class _Provider:
    def __init__(self, handle: SubprocessPreviewProcess) -> None:
        self._handle = handle

    def frame_stream(self) -> io.BytesIO | None:
        stream = self._handle.frame_stream()
        assert stream is None or isinstance(stream, io.BytesIO)
        return stream


# --- C. close lifecycle ----------------------------------------------------


def test_close_releases_stdout_and_stderr_and_is_idempotent() -> None:
    """close() closes stdout once, removes the stderr file, and is repeatable."""
    stdout = _TrackingStream(_FRAME_A)
    handle, stderr_path = _handle(stdout)
    assert stderr_path is not None and stderr_path.exists()

    handle.close()

    assert stdout.close_count == 1
    assert not stderr_path.exists()

    # Repeated close is safe and does not re-close or error.
    handle.close()
    assert stdout.close_count == 1


def test_close_handles_absent_stdout() -> None:
    """close() is defensive when stdout is None (fake without a pipe)."""
    handle, stderr_path = _handle(None)

    handle.close()  # must not raise

    assert stderr_path is not None
    assert not stderr_path.exists()


# --- D. normal stop cleanup -----------------------------------------------


def test_stop_terminates_reaps_and_closes_stdout_pipe() -> None:
    """A running process is terminated, reaped and its stdout pipe closed."""
    stdout = _TrackingStream(_FRAME_A + _FRAME_B)
    handle, _ = _handle(stdout)
    service = PreviewService(_config(), _FixedHandleBackend(handle), clock=lambda: _NOW)

    assert service.start().state is PreviewState.RUNNING
    status = service.stop()

    assert status.state is PreviewState.STOPPED
    assert handle._process.terminated is True  # type: ignore[attr-defined]
    assert stdout.close_count >= 1


# --- E. startup failure cleanup -------------------------------------------


def test_startup_failure_closes_stdout_and_stderr() -> None:
    """An early process exit during startup closes stdout and stderr."""
    stdout = _TrackingStream(b"")  # EOF immediately -> no frame
    handle, stderr_path = _handle(stdout, returncode=255)
    service = PreviewService(_config(), _FixedHandleBackend(handle), clock=lambda: _NOW)

    with pytest.raises(Exception, match="exited during startup"):
        service.start()

    assert service.status().state is PreviewState.FAILED
    assert stdout.close_count >= 1
    assert stderr_path is not None
    assert not stderr_path.exists()


# --- F. unexpected exit reconciliation ------------------------------------


def test_unexpected_exit_closes_stdout_pipe() -> None:
    """An unexpected exit while RUNNING closes the captured stdout pipe."""
    stdout = _TrackingStream(_FRAME_A + _FRAME_B)
    fake = _FakePopen(stdout)
    handle = SubprocessPreviewProcess(fake, None)  # type: ignore[arg-type]
    service = PreviewService(_config(), _FixedHandleBackend(handle), clock=lambda: _NOW)
    assert service.start().state is PreviewState.RUNNING

    # Simulate the process dying unexpectedly, then observe status.
    fake._returncode = 3
    status = service.status()

    assert status.state is PreviewState.FAILED
    assert stdout.close_count >= 1


# --- G. streaming integration through the real handle ---------------------


def test_frames_flow_through_real_process_handle_contract() -> None:
    """At least two JPEG frames flow: handle -> frame source -> demuxer."""
    stdout = io.BytesIO(_FRAME_A + _FRAME_B)
    handle, _ = _handle(stdout, stderr=False)

    # Through the streaming frame source (the real sole-reader path).
    source_frames = list(PreviewProcessFrameSource(_Provider(handle)).frames())
    assert source_frames == [_FRAME_A, _FRAME_B]

    # And the same bytes remain demultiplexable directly.
    assert list(parse_mjpeg_frames(io.BytesIO(_FRAME_A + _FRAME_B))) == [
        _FRAME_A,
        _FRAME_B,
    ]
