"""Live camera preview service for Matt's Garden Observatory.

This module owns the *preview* side of the camera domain: starting, supervising
and stopping a long-running preview process, and exposing a truthful status.
It is deliberately separate from still capture (``mgo.camera.capture``) and from
readiness detection (``mgo.core.camera``).

Camera ownership is exclusive: at any instant either the preview process or a
still capture may use the camera, never both. Capture is authoritative -- when a
capture is requested the preview is released first (see
:meth:`PreviewService.release_for_capture`) and is *not* automatically restarted.

The service is a deterministic state machine (:class:`PreviewState`) guarded by a
re-entrant lock, so concurrent API calls and the capture path cannot interleave
into an inconsistent state. It depends only on the
:class:`~mgo.camera.preview_backend.PreviewBackend` protocol, keeping it fully
testable without camera hardware.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import IO, Any

from mgo.camera.exceptions import (
    PreviewError,
    PreviewStartError,
    PreviewUnavailableError,
)
from mgo.camera.preview_backend import PreviewBackend, PreviewProcess
from mgo.camera.streaming import parse_mjpeg_frames
from mgo.core.config import PreviewConfig

LOGGER = logging.getLogger(__name__)

Clock = Callable[[], datetime]

#: How long to wait after a forced kill for the process to actually reap.
_FORCE_KILL_REAP_SECONDS = 2.0
_MAX_ERROR_LENGTH = 500

#: The single, stable diagnostic published when a start fails for an
#: *unexpected* reason (a programming defect rather than a camera problem).
#:
#: It is safe by construction: it is a constant. An unexpected exception's
#: message is arbitrary application data and may carry a path, a username, an
#: environment value, a secret, a memory address or control characters, so it is
#: never rendered into a status response. The exception and its traceback go to
#: the application log instead, which is where diagnosis belongs.
UNEXPECTED_START_ERROR = "Preview startup failed unexpectedly."

#: The stable operational reason published when the startup readiness reader
#: cannot read the preview process's stream. Like every other expected startup
#: failure it is a bounded, non-sensitive phrase: the exception's own message
#: could name a path or a device and adds nothing an operator can act on, so it
#: goes to the log instead.
_STREAM_READ_FAILURE = "stream read failed during startup"


@dataclass
class _ReadinessOutcome:
    """What the startup-readiness reader thread observed.

    A typed carrier rather than a loose dict: the distinction between "the
    stream could not be read" (expected, operational) and "the reader itself is
    broken" (unexpected, a defect) is the whole point, and it must not depend on
    which string key happened to be set.
    """

    frame: bool = False
    eof: bool = False
    stream_error: bool = False
    unexpected: Exception | None = None


class PreviewState(StrEnum):
    """The lifecycle states of the preview subsystem.

    ``STARTING`` and ``STOPPING`` are transient states passed through while the
    service holds its lock; external observers see the resolved terminal states
    (``RUNNING``, ``STOPPED`` or ``FAILED``).
    """

    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    FAILED = "failed"


#: The camera owner reported in preview status when preview holds the camera.
_OWNER_PREVIEW = "preview"


@dataclass(frozen=True)
class PreviewStatus:
    """A truthful, JSON-serialisable snapshot of preview state."""

    enabled: bool
    state: PreviewState
    backend: str
    owner: str | None
    started_at: datetime | None
    uptime_seconds: float | None
    resolution: str
    fps: int
    last_error: str | None

    def as_dict(self) -> dict[str, Any]:
        """Return the full status as JSON-compatible values."""
        return {
            "enabled": self.enabled,
            "state": self.state.value,
            "backend": self.backend,
            "owner": self.owner,
            "started_at": (
                self.started_at.isoformat() if self.started_at is not None else None
            ),
            "uptime_seconds": self.uptime_seconds,
            "resolution": self.resolution,
            "fps": self.fps,
            "last_error": self.last_error,
        }

    def health_dict(self) -> dict[str, Any]:
        """Return the compact projection embedded in ``GET /health``."""
        return {
            "enabled": self.enabled,
            "state": self.state.value,
            "owner": self.owner,
            "uptime_seconds": self.uptime_seconds,
        }


def _utc_now() -> datetime:
    """Return the current timezone-aware UTC instant."""
    return datetime.now(UTC)


def _stream_is_closed(stream: IO[bytes]) -> bool:
    """Return ``True`` only when ``stream`` can be *proven* closed.

    This is the authority for classifying a :class:`ValueError` raised while
    reading: Python raises one for an ordinary read against an already-closed
    file or pipe, which is an operational failure, but it also raises one for
    genuine programming mistakes against a perfectly healthy stream.

    The stream's own state decides, never the exception's message -- a message
    is arbitrary text that a defect can imitate ("...closed...") and that a real
    closed-file error is not obliged to contain.

    An object that cannot answer the question at all (no attribute, or a
    property that raises) has not proven itself closed, so the caller keeps the
    stricter classification. The check never raises: it runs on the reader
    thread, where an escaping exception would be reported nowhere useful.
    """
    try:
        return stream.closed is True
    except Exception:
        LOGGER.debug(
            "Preview startup stream could not report its closed state",
            exc_info=True,
        )
        return False


def _bounded_error(text: str) -> str:
    """Trim error text to a safe, log-friendly length."""
    collapsed = " ".join(text.split())
    if len(collapsed) <= _MAX_ERROR_LENGTH:
        return collapsed
    return collapsed[: _MAX_ERROR_LENGTH - 1].rstrip() + "…"


class PreviewService:
    """Owns all preview operations and enforces exclusive camera ownership.

    The service supervises at most one preview process. Start and stop are
    idempotent, an unexpected process exit is reconciled to ``FAILED`` on the
    next observation, and a capture always takes the camera by releasing an
    active preview first.
    """

    def __init__(
        self,
        config: PreviewConfig,
        backend: PreviewBackend,
        *,
        clock: Clock = _utc_now,
    ) -> None:
        self._config = config
        self._backend = backend
        self._clock = clock
        self._lock = threading.RLock()
        self._state = PreviewState.STOPPED
        self._process: PreviewProcess | None = None
        self._started_at: datetime | None = None
        self._last_error: str | None = None

    # -- public API --------------------------------------------------------

    def status(self) -> PreviewStatus:
        """Return the current preview status, reconciling any unexpected exit."""
        with self._lock:
            self._reconcile_locked()
            return self._snapshot_locked()

    def frame_stream(self) -> IO[bytes] | None:
        """Return the running preview process's frame stream, if any.

        The streaming layer reads frames from this existing process so the
        camera keeps a single owner; the service still solely supervises the
        process. Returns ``None`` unless preview is running with a captured
        frame stream.
        """
        with self._lock:
            self._reconcile_locked()
            if self._state is PreviewState.RUNNING and self._process is not None:
                return self._process.frame_stream()
            return None

    def start(self) -> PreviewStatus:
        """Start preview, validating that the process stays up, or return status.

        Startup is not considered successful merely because the process was
        created: the child is validated for stability before ``RUNNING`` is
        reported, because a preview process can pass creation and then exit
        during camera/encoder setup. Concretely, after launch the service waits
        up to ``startup_timeout_seconds`` for the process to exit; if it exits in
        that window the start fails (``FAILED``), otherwise it is ``RUNNING``.

        Idempotent: a second call while already running (or while a start is in
        progress) never launches a duplicate process. Raises
        :class:`PreviewUnavailableError` when preview is disabled and
        :class:`PreviewStartError` when the process fails to start or stay up.

        Every invocation resolves the service into a truthful state. An
        *unexpected* exception -- a programming defect anywhere in the startup
        transaction -- is re-raised unchanged so callers can still tell it apart
        from ordinary hardware absence, but only after the process this start
        launched has been reaped and the service has settled into ``FAILED``
        (see :meth:`_settle_unexpected_start`). A start can therefore never leave
        preview reporting ``STARTING`` with a live process and no error.
        """
        process: PreviewProcess | None = None
        try:
            result = self._begin_start()
            if isinstance(result, PreviewStatus):
                # Already running, or a start is already in progress: return
                # status.
                return result

            # Validate startup OUTSIDE the lock so status()/stop()/capture calls
            # are never blocked during the (bounded) window.
            process = result
            failure = self._validate_startup(process)
            return self._finish_start(process, failure)
        except PreviewError:
            # Expected preview failures already settled their own truthful
            # state and error; that contract is unchanged.
            raise
        except BaseException:
            # Cleanup and state settlement come FIRST: a failure while logging
            # must never be what leaves a camera process running.
            self._settle_unexpected_start(process)
            LOGGER.exception("Preview startup failed unexpectedly")
            # Re-raised unchanged, and deliberately NOT wrapped in a
            # PreviewError: lifespan auto-start distinguishes a programming
            # defect from an expected hardware failure by exception type.
            raise

    def _settle_unexpected_start(self, process: PreviewProcess | None) -> None:
        """Reap ``process`` and settle ``FAILED`` after an unexpected fault.

        Ownership is proven before the state is rewritten: if a concurrent stop
        or capture already superseded this start, its process is still fully
        reaped -- the camera must never be left held -- but the later, valid
        state is not overwritten with this start's failure.

        Never raises: it runs on the way out of an already-failing call, and an
        exception here would replace the original fault with a less informative
        one.
        """
        try:
            with self._lock:
                owns_process = process is not None and self._process is process
                if process is not None:
                    # Idempotent with a concurrent stop that already terminated
                    # it. Closing the process also releases its stdout pipe,
                    # which unblocks and ends the startup-readiness reader.
                    self._discard_process(process)
                if owns_process or (
                    process is None and self._state is PreviewState.STARTING
                ):
                    self._process = None
                    self._started_at = None
                    self._last_error = UNEXPECTED_START_ERROR
                    self._state = PreviewState.FAILED
        except Exception:
            LOGGER.exception(
                "Preview cleanup after an unexpected startup failure did not "
                "complete"
            )

    def _validate_startup(self, process: PreviewProcess) -> str | None:
        """Confirm the process reached a healthy running state, or say why not.

        Returns ``None`` when startup succeeded, otherwise a concise failure
        reason. When the process exposes its MJPEG stdout pipe, readiness is the
        arrival of the first complete JPEG frame within
        ``startup_timeout_seconds`` -- this both proves the camera/encoder work
        *and* drains the pipe during the window, so the producer cannot block on
        a full pipe (see :meth:`_await_first_frame`). Test doubles without a
        pipe fall back to a bounded liveness check (the process must not exit
        during the window).
        """
        stream = process.frame_stream()
        if stream is None:
            exit_code = process.wait(self._config.startup_timeout_seconds)
            if exit_code is not None:
                detail = process.read_error()
                return f"exited during startup with code {exit_code}" + (
                    f": {detail}" if detail else ""
                )
            return None
        return self._await_first_frame(
            process, stream, self._config.startup_timeout_seconds
        )

    def _await_first_frame(
        self, process: PreviewProcess, stream: IO[bytes], timeout: float
    ) -> str | None:
        """Drain the MJPEG pipe until the first frame, EOF, or ``timeout``.

        The drain is the *sole* reader of stdout during startup: the streaming
        broker only reads once the state is ``RUNNING`` (its stream endpoint
        returns 409 while starting), so this never competes with it. Reading
        here keeps the pipe from filling during the window. Returns ``None`` on
        the first frame, or a failure reason on EOF/early-exit, stream I/O
        failure or timeout.

        The reader runs on another thread, so a failure there has to be carried
        back deliberately. It is classified, not merely reported: an ordinary
        stream-operation failure becomes the stable operational reason
        :data:`_STREAM_READ_FAILURE` (and so an expected ``PreviewStartError``),
        while anything else is a programming defect whose original exception is
        re-raised **in this thread** for the transaction handler in
        :meth:`start` to settle. Either way the reader thread exits normally, so
        it can never surface as an unhandled-thread warning.
        """
        outcome = _ReadinessOutcome()
        done = threading.Event()

        def _drain() -> None:
            try:
                for _frame in parse_mjpeg_frames(stream):
                    outcome.frame = True
                    break
                else:
                    outcome.eof = True
            except OSError:
                # Ordinary stream-operation failure: a torn-down pipe, a device
                # read error. Nothing about the exception is recorded -- its
                # message is arbitrary data and the operational fact ("the
                # stream could not be read") is what a status consumer needs.
                # The detail goes to the log.
                LOGGER.warning(
                    "Preview startup stream read failed", exc_info=True
                )
                outcome.stream_error = True
            except ValueError as exc:
                # Operational *only* against a stream that is demonstrably
                # closed -- the standard "read from a closed file" case. A
                # ValueError from an open stream is a defect in the read path
                # and is classified as one; see :func:`_stream_is_closed`.
                if _stream_is_closed(stream):
                    LOGGER.warning(
                        "Preview startup stream read failed", exc_info=True
                    )
                    outcome.stream_error = True
                else:
                    outcome.unexpected = exc
            except Exception as exc:
                # A defect in the reader path itself. Carried back untouched so
                # the caller can re-raise the original object.
                outcome.unexpected = exc
            finally:
                done.set()

        reader = threading.Thread(
            target=_drain, name="mgo-preview-readiness", daemon=True
        )
        reader.start()
        if not done.wait(timeout):
            # Alive but silent: the caller reaps the process, which closes the
            # pipe and lets this daemon reader unblock and exit.
            return "did not produce a frame within the startup window"
        if outcome.unexpected is not None:
            # Re-raised on the calling thread, unchanged, so start() applies the
            # unexpected-start contract to it exactly as it would to a defect
            # raised here directly.
            raise outcome.unexpected
        if outcome.frame:
            return None
        if outcome.eof:
            exit_code = process.poll()
            detail = process.read_error()
            code_text = "unknown" if exit_code is None else str(exit_code)
            return f"exited during startup with code {code_text}" + (
                f": {detail}" if detail else ""
            )
        return _STREAM_READ_FAILURE

    def _begin_start(self) -> PreviewProcess | PreviewStatus:
        """Launch phase (locked). Return the process, or a status to short-circuit."""
        with self._lock:
            if not self._config.enabled:
                raise PreviewUnavailableError(
                    "Preview is disabled by configuration."
                )

            self._reconcile_locked()
            if self._process is not None and self._state in (
                PreviewState.RUNNING,
                PreviewState.STARTING,
            ):
                # Already running, or a startup is already validating: never
                # launch a second camera process.
                return self._snapshot_locked()

            self._state = PreviewState.STARTING
            self._last_error = None
            self._started_at = None
            LOGGER.info(
                "Preview starting (backend=%s, %dx%d @ %dfps)",
                self._backend.name,
                self._config.width,
                self._config.height,
                self._config.fps,
            )
            try:
                process = self._backend.start(self._config)
            except PreviewError as exc:
                # An *expected* launch failure. The backend adapter is
                # responsible for mapping operating-system and camera failures
                # (missing tool, permission denial, launch OSError, refused
                # configuration) into the preview domain before they get here,
                # so this message is a bounded operational one.
                self._fail_locked(str(exc))
                LOGGER.error("Preview failed to start: %s", exc)
                raise
            # Anything that is *not* a PreviewError is a violation of the
            # backend contract -- a programming defect, not a camera problem.
            # It is deliberately NOT caught here: it escapes to the transaction
            # handler in start(), which settles FAILED with the constant safe
            # diagnostic and re-raises the original object. Rendering it into a
            # PreviewStartError here would have made a bug indistinguishable
            # from an absent camera *and* published arbitrary exception text.

            self._process = process
            LOGGER.info(
                "Preview process launched (backend=%s, pid=%s); validating "
                "startup for up to %.1fs",
                self._backend.name,
                process.pid,
                self._config.startup_timeout_seconds,
            )
            return process

    def _finish_start(
        self, process: PreviewProcess, failure: str | None
    ) -> PreviewStatus:
        """Finalise phase (locked). Promote to RUNNING or settle FAILED."""
        with self._lock:
            if self._process is not process:
                # A concurrent stop/capture superseded this start during
                # validation; make sure our process is fully reaped.
                self._discard_process(process)
                return self._snapshot_locked()

            if failure is not None:
                # Deterministically reap/terminate the process (it may still be
                # alive after a no-frame timeout) and release its stdout pipe and
                # stderr resources before settling into FAILED.
                self._discard_process(process)
                message = f"Preview process {failure}."
                self._fail_locked(message)
                LOGGER.error("%s", message)
                LOGGER.info(
                    "Preview process reaped and resources released after "
                    "failed startup"
                )
                raise PreviewStartError(message)

            self._started_at = self._clock()
            self._state = PreviewState.RUNNING
            LOGGER.info(
                "Preview startup validated; running (backend=%s, pid=%s)",
                self._backend.name,
                process.pid,
            )
            return self._snapshot_locked()

    def stop(self) -> PreviewStatus:
        """Stop preview and return the final status. Idempotent."""
        with self._lock:
            self._reconcile_locked()
            if self._process is None:
                # Already stopped or failed-without-process: settle on STOPPED.
                self._state = PreviewState.STOPPED
                self._started_at = None
                self._last_error = None
                return self._snapshot_locked()

            self._state = PreviewState.STOPPING
            self._terminate_locked(self._process)
            self._process = None
            self._started_at = None
            self._state = PreviewState.STOPPED
            self._last_error = None
            LOGGER.info("Preview stopped (backend=%s)", self._backend.name)
            return self._snapshot_locked()

    def release_for_capture(self) -> None:
        """Release the camera so a still capture can take exclusive ownership.

        If a preview is active it is stopped cleanly and left stopped (no
        automatic restart). A no-op when preview is not active.
        """
        with self._lock:
            self._reconcile_locked()
            active = (
                self._state in (PreviewState.RUNNING, PreviewState.STARTING)
                and self._process is not None
            )
            if active:
                LOGGER.info(
                    "Capture requested: interrupting active preview and "
                    "transferring camera ownership to capture"
                )
                self.stop()

    def shutdown(self) -> None:
        """Stop preview during application shutdown; never leaves an orphan."""
        with self._lock:
            if self._process is not None:
                LOGGER.info("Application shutdown: stopping active preview")
            self.stop()

    # -- internal helpers (call with the lock held) ------------------------

    def _reconcile_locked(self) -> None:
        """Detect an unexpected process exit and transition to ``FAILED``."""
        if self._state is not PreviewState.RUNNING or self._process is None:
            return
        exit_code = self._process.poll()
        if exit_code is None:
            return
        detail = self._process.read_error()
        self._process.close()
        self._process = None
        message = f"Preview process terminated unexpectedly with code {exit_code}" + (
            f": {detail}" if detail else "."
        )
        self._last_error = _bounded_error(message)
        self._started_at = None
        self._state = PreviewState.FAILED
        LOGGER.error("%s", message)

    def _terminate_locked(self, process: PreviewProcess) -> None:
        """Gracefully stop ``process``, escalating to a forced kill if needed."""
        process.terminate()
        exit_code = process.wait(self._config.shutdown_timeout_seconds)
        if exit_code is None:
            LOGGER.warning(
                "Preview did not stop within %.1fs; forcing kill",
                self._config.shutdown_timeout_seconds,
            )
            process.kill()
            process.wait(_FORCE_KILL_REAP_SECONDS)
        process.close()

    def _discard_process(self, process: PreviewProcess) -> None:
        """Fully reap a process this start no longer owns (superseded mid-start).

        Idempotent with a concurrent :meth:`stop` that already terminated it:
        a dead process is only closed, a still-live one is terminated first, so
        the camera is always released and no stray process survives.
        """
        if process.poll() is None:
            process.terminate()
            if process.wait(self._config.shutdown_timeout_seconds) is None:
                process.kill()
                process.wait(_FORCE_KILL_REAP_SECONDS)
        process.close()

    def _fail_locked(self, message: str) -> None:
        """Record a failure and settle into the ``FAILED`` state."""
        self._process = None
        self._started_at = None
        self._last_error = _bounded_error(message)
        self._state = PreviewState.FAILED

    def _snapshot_locked(self) -> PreviewStatus:
        """Build a status snapshot from the current state."""
        owner = (
            _OWNER_PREVIEW
            if self._state in (PreviewState.RUNNING, PreviewState.STARTING)
            else None
        )
        uptime: float | None = None
        if self._state is PreviewState.RUNNING and self._started_at is not None:
            uptime = round(
                (self._clock() - self._started_at).total_seconds(), 1
            )
        return PreviewStatus(
            enabled=self._config.enabled,
            state=self._state,
            backend=self._backend.name,
            owner=owner,
            started_at=self._started_at,
            uptime_seconds=uptime,
            resolution=f"{self._config.width}x{self._config.height}",
            fps=self._config.fps,
            last_error=self._last_error,
        )
