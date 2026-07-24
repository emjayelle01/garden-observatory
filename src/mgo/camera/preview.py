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
from mgo.core.config import PreviewConfig

LOGGER = logging.getLogger(__name__)

Clock = Callable[[], datetime]

#: How long to wait after a forced kill for the process to actually reap.
_FORCE_KILL_REAP_SECONDS = 2.0
_MAX_ERROR_LENGTH = 500


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
        """Start preview, or return the running status if already active.

        Idempotent: a second call while running never launches a duplicate
        process. Raises :class:`PreviewUnavailableError` when preview is
        disabled and :class:`PreviewStartError` when the process cannot start.
        """
        with self._lock:
            if not self._config.enabled:
                raise PreviewUnavailableError(
                    "Preview is disabled by configuration."
                )

            self._reconcile_locked()
            if self._state is PreviewState.RUNNING and self._process is not None:
                return self._snapshot_locked()

            self._state = PreviewState.STARTING
            self._last_error = None
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
                self._fail_locked(str(exc))
                LOGGER.error("Preview failed to start: %s", exc)
                raise
            except Exception as exc:  # unexpected backend fault
                self._fail_locked(str(exc))
                LOGGER.exception("Preview backend raised an unexpected error")
                raise PreviewStartError(
                    f"Preview backend failed unexpectedly: {exc}"
                ) from exc

            exit_code = process.poll()
            if exit_code is not None:
                detail = process.read_error()
                process.close()
                message = (
                    f"Preview process exited immediately with code {exit_code}"
                    + (f": {detail}" if detail else ".")
                )
                self._fail_locked(message)
                LOGGER.error("%s", message)
                raise PreviewStartError(message)

            self._process = process
            self._started_at = self._clock()
            self._state = PreviewState.RUNNING
            LOGGER.info(
                "Preview running (backend=%s, pid=%s)",
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
