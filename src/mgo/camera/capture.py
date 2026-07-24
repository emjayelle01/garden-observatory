"""Camera capture service.

Orchestrates a single still capture: it decides *where* and *under what name*
an image is stored, ensures the destination exists, delegates the actual
image production to an injected :class:`~mgo.camera.backend.CaptureBackend`,
and returns structured :class:`~mgo.camera.models.CaptureResult` metadata.

The service is hardware-agnostic. It never imports a concrete backend and never
runs a subprocess, which keeps it fully testable with a mock backend.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from mgo.camera.backend import CaptureBackend
from mgo.camera.exceptions import (
    CameraCaptureError,
    CameraUnavailableError,
    CaptureWriteError,
)
from mgo.camera.models import CaptureResult
from mgo.core.config import CameraConfig

LOGGER = logging.getLogger(__name__)

#: Deterministic, filesystem-safe UTC filename stamp with microsecond precision
#: (``%f``). Microseconds prevent collisions between two captures in the same
#: second while keeping names chronologically sortable. Colons are avoided so
#: the name is valid on Windows as well as POSIX; the trailing ``Z`` marks UTC.
_FILENAME_FORMAT = "%Y-%m-%dT%H-%M-%S.%fZ"
_FILE_EXTENSION = ".jpg"

Clock = Callable[[], datetime]


def _utc_now() -> datetime:
    """Return the current timezone-aware UTC instant."""
    return datetime.now(UTC)


def build_capture_filename(timestamp: datetime) -> str:
    """Return the deterministic capture filename for a UTC ``timestamp``.

    The timestamp is normalised to UTC before formatting so the ``Z`` suffix is
    always truthful regardless of the caller's timezone.
    """
    stamp = timestamp.astimezone(UTC).strftime(_FILENAME_FORMAT)
    return f"{stamp}{_FILE_EXTENSION}"


class CaptureService:
    """Captures still images to the configured capture directory."""

    def __init__(
        self,
        config: CameraConfig,
        backend: CaptureBackend,
        *,
        clock: Clock = _utc_now,
    ) -> None:
        self._config = config
        self._backend = backend
        self._clock = clock

    def capture_image(self) -> CaptureResult:
        """Capture one still image and return its metadata.

        Raises a :mod:`mgo.camera.exceptions` error on any expected failure:
        the camera being disabled, the backend failing, or the image not being
        writable. Callers never see raw ``OSError`` or subprocess exceptions.
        """
        if not self._config.enabled:
            raise CameraUnavailableError(
                "Camera capture requested but the camera is disabled by "
                "configuration."
            )

        timestamp = self._clock()
        filename = build_capture_filename(timestamp)
        directory = self._ensure_capture_directory()
        destination = directory / filename

        LOGGER.info("Capturing still image to %s", destination)
        try:
            dimensions = self._backend.capture(destination)
            # Independently verify the invariant rather than trusting the
            # backend: a completed capture must have produced a non-empty file.
            filesize = self._verify_capture(destination)
        except CameraCaptureError:
            # Any expected failure after a destination was selected must not
            # leave a partial, empty or corrupt file behind. Cleanup never
            # masks the original error.
            self._remove_partial_capture(destination)
            raise

        result = CaptureResult(
            success=True,
            filename=filename,
            absolute_path=destination.resolve(),
            timestamp=timestamp.astimezone(UTC),
            width=dimensions.width,
            height=dimensions.height,
            filesize_bytes=filesize,
            backend=self._backend.name,
        )
        LOGGER.info(
            "Captured %s (%dx%d, %d bytes) via %s",
            result.filename,
            result.width,
            result.height,
            result.filesize_bytes,
            result.backend,
        )
        return result

    def _ensure_capture_directory(self) -> Path:
        """Create and return the configured capture directory.

        Images are only ever written beneath ``camera.capture_directory``. A
        failure to create it is surfaced as a :class:`CaptureWriteError` rather
        than silently writing elsewhere.
        """
        directory = self._config.capture_directory
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise CaptureWriteError(
                f"Could not create capture directory {directory}: {exc}"
            ) from exc
        return directory

    def _verify_capture(self, destination: Path) -> int:
        """Verify the captured file exists and is non-empty; return its size.

        This is the service's own guarantee of the "non-empty output" invariant
        and does not rely on the concrete backend enforcing it. A missing or
        zero-byte file is surfaced as a :class:`CaptureWriteError`.
        """
        try:
            size = destination.stat().st_size
        except OSError as exc:
            raise CaptureWriteError(
                f"Capture reported success but no file was found at "
                f"{destination}: {exc}"
            ) from exc

        if size <= 0:
            raise CaptureWriteError(
                f"Capture produced an empty file at {destination}."
            )
        return size

    def _remove_partial_capture(self, destination: Path) -> None:
        """Best-effort removal of a partial/invalid capture file.

        Never raises: a cleanup failure is logged and swallowed so it can never
        replace the original capture exception that triggered cleanup.
        """
        try:
            if destination.exists():
                destination.unlink()
                LOGGER.info("Removed partial capture file %s", destination)
        except OSError:
            LOGGER.warning(
                "Failed to remove partial capture file %s", destination,
                exc_info=True,
            )


__all__ = [
    "CameraCaptureError",
    "CaptureService",
    "build_capture_filename",
]
