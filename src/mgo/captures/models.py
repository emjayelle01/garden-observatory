"""Persistent capture-archive model for Matt's Garden Observatory.

This module defines the authoritative catalogue record for every photograph
taken by MGO. It stores *metadata only*: the JPEG itself remains on disk exactly
as produced by the capture pipeline (Task 2B). No binary image data is ever
written to the database.

The model is deliberately future-proofed. A JSON ``extra_metadata`` column lets
additional, as-yet-unknown metadata be attached to a capture without a schema
redesign, so later tasks can enrich the catalogue without a migration.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, Column
from sqlmodel import Field, SQLModel


class Capture(SQLModel, table=True):
    """One catalogue record for a single successfully captured still image.

    Exactly one row exists per verified capture. ``id`` is a UUID primary key
    so records are globally identifiable and never collide across installations.
    Timestamps are stored as UTC; ``captured_at_utc`` is the instant the capture
    was initiated (and the basis for the on-disk filename) while
    ``created_at_utc`` is when this catalogue record was persisted.
    """

    __tablename__ = "captures"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    filename: str = Field(index=True)
    absolute_path: str
    captured_at_utc: datetime = Field(index=True)
    width: int
    height: int
    filesize_bytes: int
    camera_backend: str
    created_at_utc: datetime
    #: Free-form, forward-compatible metadata. Defaults to an empty object so
    #: additional fields can be introduced later without altering the schema.
    extra_metadata: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSON, nullable=False),
    )


def _ensure_aware_utc(value: datetime) -> datetime:
    """Return ``value`` as a timezone-aware UTC datetime.

    SQLite has no native timezone-aware storage, so datetimes round-trip through
    the database as naive values. Because every timestamp is written as UTC, a
    naive value read back is reinterpreted as UTC; an already-aware value is
    normalised to UTC. This keeps serialised timestamps unambiguous (``+00:00``).
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def to_naive_utc(value: datetime) -> datetime:
    """Return ``value`` as a naive UTC datetime for stable SQLite storage.

    Storing a naive UTC value avoids the SQLite datetime parser rejecting an
    offset suffix, while :func:`_ensure_aware_utc` restores the timezone on read.
    """
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def capture_summary(capture: Capture) -> dict[str, Any]:
    """Serialise a capture for the ``GET /captures`` archive listing.

    Returns the compact catalogue projection: identifier, capture timestamp,
    filename and the core image metadata. No binary image data is included.
    """
    return {
        "capture_id": str(capture.id),
        "timestamp": _ensure_aware_utc(capture.captured_at_utc).isoformat(),
        "filename": capture.filename,
        "width": capture.width,
        "height": capture.height,
        "filesize_bytes": capture.filesize_bytes,
        "backend": capture.camera_backend,
    }


def capture_detail(capture: Capture) -> dict[str, Any]:
    """Serialise the full stored metadata for a single capture.

    Extends :func:`capture_summary` with the on-disk location, the record's
    own creation time and any forward-compatible ``extra_metadata``.
    """
    detail = capture_summary(capture)
    detail.update(
        {
            "absolute_path": capture.absolute_path,
            "created_at": _ensure_aware_utc(capture.created_at_utc).isoformat(),
            "extra_metadata": capture.extra_metadata,
        }
    )
    return detail
