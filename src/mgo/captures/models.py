"""Capture-archive domain model for Matt's Garden Observatory.

The :class:`Capture` here is a plain, immutable domain value object -- not an
ORM table model. Persistence is handled by the repository layer using the
application's existing raw-``sqlite3`` infrastructure, so this module stays free
of any database engine concern, mirroring :class:`mgo.core.observations.Observation`.

The archive stores *metadata only*: the JPEG itself remains on disk exactly as
produced by the capture pipeline (Task 2B). No binary image data is ever stored.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class Capture:
    """One catalogue record for a single successfully captured still image.

    Exactly one instance exists per verified capture. ``id`` is a UUID so
    records are globally identifiable. Timestamps are timezone-aware UTC:
    ``captured_at_utc`` is the instant the capture was initiated (and the basis
    for the on-disk filename) while ``created_at_utc`` is when this catalogue
    record was persisted.

    ``extra_metadata`` is free-form, forward-compatible metadata. It defaults to
    an independent empty dictionary (via ``default_factory``) so additional
    fields can be introduced later without a schema redesign and without sharing
    a mutable default between instances.
    """

    id: uuid.UUID
    filename: str
    absolute_path: str
    captured_at_utc: datetime
    width: int
    height: int
    filesize_bytes: int
    camera_backend: str
    created_at_utc: datetime
    extra_metadata: dict[str, Any] = field(default_factory=dict)


def capture_summary(capture: Capture) -> dict[str, Any]:
    """Serialise a capture for the ``GET /captures`` archive listing.

    Returns the compact catalogue projection: identifier, capture timestamp,
    filename and the core image metadata. No binary image data is included.
    """
    return {
        "capture_id": str(capture.id),
        "timestamp": capture.captured_at_utc.isoformat(),
        "filename": capture.filename,
        "width": capture.width,
        "height": capture.height,
        "filesize_bytes": capture.filesize_bytes,
        "backend": capture.camera_backend,
    }


def capture_detail(capture: Capture) -> dict[str, Any]:
    """Serialise the full stored metadata for a single capture.

    Extends :func:`capture_summary` with the on-disk location, the record's own
    creation time and a fresh copy of any forward-compatible ``extra_metadata``.
    """
    detail = capture_summary(capture)
    detail.update(
        {
            "absolute_path": capture.absolute_path,
            "created_at": capture.created_at_utc.isoformat(),
            "extra_metadata": dict(capture.extra_metadata),
        }
    )
    return detail
