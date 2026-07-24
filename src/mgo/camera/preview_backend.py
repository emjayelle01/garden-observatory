"""Preview backend abstraction for the camera layer.

A *preview backend* owns the mechanics of launching and supervising the
long-running preview process. It is the only place that knows how a Raspberry Pi
video/preview process is spawned, mirroring the split used by the still-capture
:class:`~mgo.camera.backend.CaptureBackend`.

Unlike capture (a short, bounded subprocess), preview is a *persistent* process,
so the abstraction is a supervised handle -- :class:`PreviewProcess` -- rather
than a one-shot command runner. The service drives the handle's lifecycle
(``poll``/``terminate``/``kill``/``wait``/``close``) and never touches
``subprocess`` directly.

Implementations provided here:

* :class:`RPiCamPreviewBackend` -- launches a Raspberry Pi ``*-vid`` process
  (``rpicam-vid`` on Bookworm, ``libcamera-vid`` on older stacks);
* :class:`NullPreviewBackend` -- always reports preview as unavailable;
* :class:`MockPreviewBackend` / :class:`MockPreviewProcess` -- hardware-free
  doubles that make the full lifecycle testable without a camera.
"""

from __future__ import annotations

import logging
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import IO, Protocol

from mgo.camera.exceptions import PreviewStartError, PreviewUnavailableError
from mgo.core.config import PreviewConfig

LOGGER = logging.getLogger(__name__)

#: ``--timeout 0`` runs the preview process continuously until it is stopped.
_PREVIEW_RUN_FOREVER_MS = "0"
#: Explicit encoder: emit Motion-JPEG (a concatenation of complete JPEG frames)
#: to stdout. This is required because rpicam-vid otherwise defaults to H.264
#: written through libav, which cannot infer an output container from the ``-``
#: (stdout) filename and fails with "Unable to choose an output format for '-'".
#: MJPEG needs no container: each frame is a standalone JPEG, which is exactly
#: what the streaming layer's SOI/EOI demultiplexer expects.
_PREVIEW_CODEC = "mjpeg"
_MAX_ERROR_CHARS = 2000


class PreviewProcess(Protocol):
    """A supervised handle to a running preview process.

    All methods are non-raising: operating-system quirks (an already-dead
    process, a missing PID) are swallowed so the owning service can drive a
    deterministic state machine without guarding every call.
    """

    @property
    def pid(self) -> int | None:
        """The process identifier, or ``None`` if unavailable."""
        ...

    def poll(self) -> int | None:
        """Return the exit code, or ``None`` while the process is still running."""
        ...

    def terminate(self) -> None:
        """Request a graceful shutdown (SIGTERM)."""
        ...

    def kill(self) -> None:
        """Force an immediate shutdown (SIGKILL)."""
        ...

    def wait(self, timeout: float | None = None) -> int | None:
        """Wait up to ``timeout`` for exit; return the code or ``None`` on timeout."""
        ...

    def read_error(self) -> str:
        """Return a best-effort snapshot of the process's captured stderr."""
        ...

    def frame_stream(self) -> IO[bytes] | None:
        """Return the process's MJPEG stdout stream, or ``None`` if not captured.

        The streaming layer reads frames from this *existing* process, so the
        camera keeps a single owner. It is ``None`` unless the process was
        launched to emit frames.
        """
        ...

    def close(self) -> None:
        """Release any resources (e.g. the captured-stderr file)."""
        ...


def _tail(text: str) -> str:
    """Return the final, bounded portion of captured error text."""
    collapsed = text.strip()
    if len(collapsed) <= _MAX_ERROR_CHARS:
        return collapsed
    return "…" + collapsed[-(_MAX_ERROR_CHARS - 1) :]


class SubprocessPreviewProcess:
    """A :class:`PreviewProcess` backed by a real :class:`subprocess.Popen`.

    ``stdout`` is discarded; ``stderr`` is captured to a temporary file so a
    chatty process can never deadlock on a full pipe, and its tail can be
    surfaced as ``last_error`` on failure. :meth:`close` removes the temp file.
    """

    def __init__(
        self,
        process: subprocess.Popen[bytes],
        stderr_path: Path | None,
    ) -> None:
        self._process = process
        self._stderr_path = stderr_path

    @property
    def pid(self) -> int | None:
        return self._process.pid

    def poll(self) -> int | None:
        return self._process.poll()

    def terminate(self) -> None:
        try:
            self._process.terminate()
        except (ProcessLookupError, OSError):
            LOGGER.debug("Preview terminate() on an already-exited process")

    def kill(self) -> None:
        try:
            self._process.kill()
        except (ProcessLookupError, OSError):
            LOGGER.debug("Preview kill() on an already-exited process")

    def wait(self, timeout: float | None = None) -> int | None:
        try:
            return self._process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return None

    def read_error(self) -> str:
        if self._stderr_path is None:
            return ""
        try:
            text = self._stderr_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        return _tail(text)

    def frame_stream(self) -> IO[bytes] | None:
        # ``None`` when stdout was discarded (the default launch); a readable
        # pipe when the process was launched to emit MJPEG frames.
        return self._process.stdout

    def close(self) -> None:
        if self._stderr_path is not None:
            self._stderr_path.unlink(missing_ok=True)
            self._stderr_path = None


def launch_preview_subprocess(args: Sequence[str]) -> PreviewProcess:
    """Launch ``args`` as a supervised preview process.

    Discards stdout and captures stderr to a temporary file. A missing command
    maps to :class:`PreviewUnavailableError`; any other launch failure maps to
    :class:`PreviewStartError`. Never leaks a raw ``subprocess`` exception.
    """
    stderr_file = tempfile.NamedTemporaryFile(  # noqa: SIM115 - handed to Popen
        prefix="mgo-preview-", suffix=".log", delete=False
    )
    stderr_path = Path(stderr_file.name)
    try:
        process: subprocess.Popen[bytes] = subprocess.Popen(
            list(args),
            stdout=subprocess.DEVNULL,
            stderr=stderr_file,
        )
    except FileNotFoundError as exc:
        stderr_file.close()
        stderr_path.unlink(missing_ok=True)
        raise PreviewUnavailableError(
            f"Preview tool '{args[0]}' is not installed."
        ) from exc
    except OSError as exc:
        stderr_file.close()
        stderr_path.unlink(missing_ok=True)
        raise PreviewStartError(
            f"Failed to launch preview tool '{args[0]}': {exc}"
        ) from exc

    # The child holds its own dup of the stderr fd; the parent copy is no longer
    # needed and closing it avoids leaking a descriptor.
    stderr_file.close()
    return SubprocessPreviewProcess(process, stderr_path)


class PreviewBackend(Protocol):
    """A hardware/OS-specific adapter that starts a preview process."""

    @property
    def name(self) -> str:
        """A short, stable identifier for this backend (for status)."""
        ...

    def start(self, config: PreviewConfig) -> PreviewProcess:
        """Start the preview process for ``config`` and return its handle."""
        ...


class RPiCamPreviewBackend:
    """Starts a preview via a Raspberry Pi ``*-vid`` command.

    The command is run as an argument array (never a shell) that requests the
    configured resolution and frame rate and runs until stopped. No preview
    window is opened (``--nopreview``). Output is an explicit Motion-JPEG stream
    on stdout (``--codec mjpeg --output -``): a sequence of complete JPEG frames
    that the streaming layer's SOI/EOI demultiplexer consumes directly, with no
    libav container and no transcoding stage.
    """

    def __init__(
        self,
        command: str,
        *,
        launcher: Callable[[Sequence[str]], PreviewProcess] = launch_preview_subprocess,
    ) -> None:
        self._command = command
        self._launcher = launcher

    @property
    def name(self) -> str:
        return self._command

    def _build_args(self, config: PreviewConfig) -> Sequence[str]:
        """Assemble the preview command's argument array.

        ``--codec mjpeg`` makes stdout an explicit MJPEG frame stream (see
        :data:`_PREVIEW_CODEC`). ``--flush`` writes each encoded frame to the
        pipe as soon as it is produced instead of letting rpicam-vid buffer
        output, so frames reach the streaming reader (and the browser) promptly
        and the SOI/EOI demultiplexer is never starved behind a full buffer.
        """
        return (
            self._command,
            "--nopreview",
            "--codec",
            _PREVIEW_CODEC,
            "--width",
            str(config.width),
            "--height",
            str(config.height),
            "--framerate",
            str(config.fps),
            "--flush",
            "--timeout",
            _PREVIEW_RUN_FOREVER_MS,
            "--output",
            "-",
        )

    def start(self, config: PreviewConfig) -> PreviewProcess:
        return self._launcher(self._build_args(config))


class NullPreviewBackend:
    """A backend that never starts preview.

    Useful where preview must be wired up but no hardware should ever be used.
    """

    @property
    def name(self) -> str:
        return "null"

    def start(self, config: PreviewConfig) -> PreviewProcess:
        """Always raise :class:`PreviewUnavailableError`."""
        raise PreviewUnavailableError(
            "Null preview backend never starts a preview process."
        )


class MockPreviewProcess:
    """A hardware-free :class:`PreviewProcess` double for tests.

    ``launched_dead`` simulates a process that exits immediately (a failed
    launch). ``stops_on_terminate=False`` simulates a stubborn process that
    ignores SIGTERM, exercising the forced-kill path. :meth:`force_exit` lets a
    test simulate an *unexpected* termination while "running".
    """

    def __init__(
        self,
        *,
        launched_dead: bool = False,
        stops_on_terminate: bool = True,
        error_text: str = "",
        frame_stream: IO[bytes] | None = None,
    ) -> None:
        self._alive = not launched_dead
        self._exit_code: int | None = None if not launched_dead else 1
        self._stops_on_terminate = stops_on_terminate
        self._error_text = error_text
        self._frame_stream = frame_stream
        self.terminated = False
        self.killed = False
        self.closed = False

    @property
    def pid(self) -> int | None:
        return 4242

    def poll(self) -> int | None:
        return None if self._alive else self._exit_code

    def terminate(self) -> None:
        self.terminated = True
        if self._stops_on_terminate:
            self._alive = False
            self._exit_code = 0

    def kill(self) -> None:
        self.killed = True
        self._alive = False
        self._exit_code = -9

    def wait(self, timeout: float | None = None) -> int | None:
        return None if self._alive else self._exit_code

    def read_error(self) -> str:
        return self._error_text

    def frame_stream(self) -> IO[bytes] | None:
        return self._frame_stream

    def close(self) -> None:
        self.closed = True

    def force_exit(self, code: int = 1) -> None:
        """Test hook: simulate the process dying unexpectedly while running."""
        self._alive = False
        self._exit_code = code


class MockPreviewBackend:
    """A hardware-free :class:`PreviewBackend` double for tests.

    Configurable to raise on ``start`` (``error``) or to hand out a process that
    is already dead / stubborn. Records every process it created so a test can
    drive unexpected-exit scenarios.
    """

    def __init__(
        self,
        *,
        name: str = "mock",
        error: Exception | None = None,
        launched_dead: bool = False,
        stops_on_terminate: bool = True,
    ) -> None:
        self._name = name
        self._error = error
        self._launched_dead = launched_dead
        self._stops_on_terminate = stops_on_terminate
        self.start_calls = 0
        self.last_process: MockPreviewProcess | None = None

    @property
    def name(self) -> str:
        return self._name

    def start(self, config: PreviewConfig) -> PreviewProcess:
        self.start_calls += 1
        if self._error is not None:
            raise self._error
        process = MockPreviewProcess(
            launched_dead=self._launched_dead,
            stops_on_terminate=self._stops_on_terminate,
        )
        self.last_process = process
        return process


def build_preview_backend(backend: str) -> PreviewBackend:
    """Construct the preview backend for a configured backend name.

    Mirrors :func:`mgo.camera.backend.build_capture_backend` so preview and
    capture agree on backend vocabulary. ``rpicam``/``libcamera`` select the
    matching ``*-vid`` command; ``null``/``none`` select :class:`NullPreviewBackend`.
    """
    normalized = backend.strip().lower()

    if normalized == "rpicam":
        return RPiCamPreviewBackend("rpicam-vid")
    if normalized == "libcamera":
        return RPiCamPreviewBackend("libcamera-vid")
    if normalized in {"null", "none"}:
        return NullPreviewBackend()

    raise ValueError(f"Unsupported camera backend: {backend!r}")
