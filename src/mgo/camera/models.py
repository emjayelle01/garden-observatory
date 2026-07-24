"""Typed data models for the camera capture layer.

These models are pure value objects. They carry no behaviour beyond safe
serialisation and know nothing about how an image was produced. Keeping them
free of backend or subprocess concerns means the API and tests can depend on a
stable, hardware-agnostic shape.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ImageDimensions:
    """The pixel dimensions of a captured image.

    Backends report the dimensions they produced so the capture service never
    needs to decode image files or depend on an imaging library.
    """

    width: int
    height: int


@dataclass(frozen=True)
class CaptureResult:
    """Structured metadata describing a single captured still image.

    ``absolute_path`` is the resolved on-disk location; ``filename`` is its
    basename. ``timestamp`` is the timezone-aware UTC instant the capture was
    initiated and is also the basis for the deterministic filename.
    """

    success: bool
    filename: str
    absolute_path: Path
    timestamp: datetime
    width: int
    height: int
    filesize_bytes: int
    backend: str

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable representation of the result."""
        return {
            "success": self.success,
            "filename": self.filename,
            "absolute_path": str(self.absolute_path),
            "timestamp": self.timestamp.isoformat(),
            "width": self.width,
            "height": self.height,
            "filesize_bytes": self.filesize_bytes,
            "backend": self.backend,
        }
