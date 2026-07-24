"""Tests for the persistent capture archive (repository layer).

These exercise :class:`~mgo.captures.archive.CaptureArchive` directly against a
temporary SQLite database, with no HTTP or hardware involved. They cover
persistence, metadata fidelity, newest-first ordering, single-record lookup,
404-equivalent misses and database-failure behaviour.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from mgo.camera.models import CaptureResult
from mgo.captures.archive import CaptureArchive, CaptureArchiveError


def _result(
    *,
    filename: str = "2026-07-24T10-00-00.000001Z.jpg",
    absolute_path: str = "/data/captures/2026-07-24T10-00-00.000001Z.jpg",
    timestamp: datetime | None = None,
    width: int = 4608,
    height: int = 2592,
    filesize_bytes: int = 123_456,
    backend: str = "mock",
) -> CaptureResult:
    """Build a verified :class:`CaptureResult` for archive tests."""
    return CaptureResult(
        success=True,
        filename=filename,
        absolute_path=Path(absolute_path),
        timestamp=timestamp or datetime(2026, 7, 24, 10, 0, 0, 1, tzinfo=UTC),
        width=width,
        height=height,
        filesize_bytes=filesize_bytes,
        backend=backend,
    )


def _archive(tmp_path: Path) -> CaptureArchive:
    """Build and initialise an archive backed by a temporary database."""
    archive = CaptureArchive(tmp_path / "mgo.db")
    archive.initialize()
    return archive


def test_record_capture_persists_single_record(tmp_path: Path) -> None:
    """A successful capture creates exactly one catalogue record."""
    archive = _archive(tmp_path)

    archive.record_capture(_result())

    captures = archive.list_captures()
    assert len(captures) == 1


def test_record_capture_returns_uuid_identifier(tmp_path: Path) -> None:
    """The persisted record carries a UUID primary key."""
    archive = _archive(tmp_path)

    capture = archive.record_capture(_result())

    assert isinstance(capture.id, uuid.UUID)


def test_record_capture_preserves_metadata(tmp_path: Path) -> None:
    """Every stored field matches the originating capture result."""
    archive = _archive(tmp_path)
    result = _result(
        filename="2026-07-24T11-30-00.500000Z.jpg",
        absolute_path="/data/captures/2026-07-24T11-30-00.500000Z.jpg",
        timestamp=datetime(2026, 7, 24, 11, 30, 0, 500_000, tzinfo=UTC),
        width=1920,
        height=1080,
        filesize_bytes=98_765,
        backend="rpicam",
    )

    stored = archive.record_capture(result)
    fetched = archive.get_capture(stored.id)

    assert fetched is not None
    assert fetched.filename == result.filename
    assert fetched.absolute_path == str(result.absolute_path)
    assert fetched.width == result.width
    assert fetched.height == result.height
    assert fetched.filesize_bytes == result.filesize_bytes
    assert fetched.camera_backend == result.backend
    # The capture instant round-trips as the same UTC moment.
    assert fetched.captured_at_utc.replace(tzinfo=UTC) == result.timestamp


def test_extra_metadata_defaults_to_empty(tmp_path: Path) -> None:
    """The forward-compatible metadata column defaults to an empty object."""
    archive = _archive(tmp_path)

    capture = archive.record_capture(_result())

    assert capture.extra_metadata == {}


def test_list_captures_orders_newest_first(tmp_path: Path) -> None:
    """Captures are returned in descending capture-time order."""
    archive = _archive(tmp_path)
    base = datetime(2026, 7, 24, 8, 0, tzinfo=UTC)
    for offset in (0, 2, 1):
        archive.record_capture(
            _result(
                filename=f"capture-{offset}.jpg",
                timestamp=base + timedelta(minutes=offset),
            )
        )

    captures = archive.list_captures()

    ordered = [capture.filename for capture in captures]
    assert ordered == ["capture-2.jpg", "capture-1.jpg", "capture-0.jpg"]


def test_each_capture_creates_a_distinct_record(tmp_path: Path) -> None:
    """Recording twice yields two records with distinct identifiers."""
    archive = _archive(tmp_path)

    first = archive.record_capture(_result(filename="a.jpg"))
    second = archive.record_capture(_result(filename="b.jpg"))

    captures = archive.list_captures()
    assert len(captures) == 2
    assert first.id != second.id


def test_get_capture_returns_none_for_unknown_id(tmp_path: Path) -> None:
    """An unknown identifier resolves to ``None`` (the 404 signal)."""
    archive = _archive(tmp_path)

    assert archive.get_capture(uuid.uuid4()) is None


def test_record_capture_raises_when_table_missing(tmp_path: Path) -> None:
    """A database failure is surfaced as a domain error, not a raw exception."""
    # Deliberately skip initialize() so the ``captures`` table does not exist.
    archive = CaptureArchive(tmp_path / "mgo.db")

    with pytest.raises(CaptureArchiveError):
        archive.record_capture(_result())


def test_initialize_is_idempotent(tmp_path: Path) -> None:
    """Re-initialising an existing database is safe and preserves data."""
    archive = _archive(tmp_path)
    archive.record_capture(_result())

    # Simulates an existing installation being provisioned again on restart.
    archive.initialize()

    assert len(archive.list_captures()) == 1
