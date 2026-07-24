"""Tests for the persistent capture archive (repository layer).

These exercise :class:`~mgo.captures.archive.CaptureArchive` directly against a
temporary SQLite database provisioned by the existing numbered migration runner.
No HTTP, ORM or hardware is involved. They cover persistence, metadata fidelity,
deterministic newest-first ordering (including equal-timestamp tie-breaking),
single-record lookup, database-failure translation, database-level duplicate
protection, JSON round-tripping and observation/capture coexistence.
"""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from mgo.camera.models import CaptureResult
from mgo.captures.archive import (
    CaptureArchive,
    CaptureArchiveError,
    DuplicateCaptureError,
)
from mgo.core.database import apply_migrations
from mgo.core.observations import list_observations, record_observation


def _result(
    *,
    filename: str = "2026-07-24T10-00-00.000001Z.jpg",
    absolute_path: str | None = None,
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
        absolute_path=Path(absolute_path or f"/data/captures/{filename}"),
        timestamp=timestamp or datetime(2026, 7, 24, 10, 0, 0, 1, tzinfo=UTC),
        width=width,
        height=height,
        filesize_bytes=filesize_bytes,
        backend=backend,
    )


def _archive(
    tmp_path: Path,
    *,
    clock: Callable[[], datetime] | None = None,
) -> CaptureArchive:
    """Build a migration-provisioned archive over a temporary database."""
    database_path = tmp_path / "mgo.db"
    apply_migrations(database_path)
    if clock is not None:
        return CaptureArchive(database_path, clock=clock)
    return CaptureArchive(database_path)


def _fixed_clock(*instants: datetime) -> Callable[[], datetime]:
    """Return a clock yielding ``instants`` in order, repeating the last one."""
    remaining = list(instants)

    def _clock() -> datetime:
        return remaining.pop(0) if len(remaining) > 1 else remaining[0]

    return _clock


def test_record_capture_persists_single_record(tmp_path: Path) -> None:
    """A successful capture creates exactly one catalogue record."""
    archive = _archive(tmp_path)

    archive.record_capture(_result())

    assert len(archive.list_captures()) == 1


def test_record_capture_returns_uuid_identifier(tmp_path: Path) -> None:
    """The persisted record carries a UUID identity."""
    archive = _archive(tmp_path)

    capture = archive.record_capture(_result())

    assert isinstance(capture.id, uuid.UUID)


def test_record_capture_preserves_metadata(tmp_path: Path) -> None:
    """Every stored field matches the originating capture result."""
    archive = _archive(tmp_path)
    result = _result(
        filename="2026-07-24T11-30-00.500000Z.jpg",
        timestamp=datetime(2026, 7, 24, 11, 30, 0, 500_000, tzinfo=UTC),
        width=1920,
        height=1080,
        filesize_bytes=98_765,
        backend="rpicam",
    )

    stored = archive.record_capture(result)
    fetched = archive.get_capture(stored.id)

    assert fetched is not None
    assert fetched == stored
    assert fetched.filename == result.filename
    assert fetched.absolute_path == str(result.absolute_path)
    assert fetched.width == result.width
    assert fetched.height == result.height
    assert fetched.filesize_bytes == result.filesize_bytes
    assert fetched.camera_backend == result.backend
    # The capture instant round-trips as the same timezone-aware UTC moment.
    assert fetched.captured_at_utc == result.timestamp
    assert fetched.captured_at_utc.tzinfo is not None
    assert fetched.created_at_utc.tzinfo is not None


def test_record_capture_rejects_naive_timestamp(tmp_path: Path) -> None:
    """Stored capture times must be timezone-aware, matching observations."""
    archive = _archive(tmp_path)
    naive = _result(timestamp=datetime(2026, 7, 24, 10, 0))

    with pytest.raises(ValueError, match="must be timezone-aware"):
        archive.record_capture(naive)


def test_extra_metadata_defaults_to_independent_empty_dict(tmp_path: Path) -> None:
    """The forward-compatible metadata defaults to a *separate* empty object."""
    archive = _archive(tmp_path)

    first = archive.record_capture(_result(filename="a.jpg"))
    second = archive.record_capture(_result(filename="b.jpg"))

    assert first.extra_metadata == {}
    assert second.extra_metadata == {}
    # Not a shared mutable default.
    assert first.extra_metadata is not second.extra_metadata


def test_extra_metadata_json_round_trip(tmp_path: Path) -> None:
    """Non-empty extra_metadata round-trips as an independent dict."""
    archive = _archive(tmp_path)
    payload = {"lens": "imx708", "iso": 100, "nested": {"gain": 1.5}}

    stored = archive.record_capture(_result(), extra_metadata=payload)
    fetched = archive.get_capture(stored.id)

    assert fetched is not None
    assert fetched.extra_metadata == payload
    # A fresh instance, not the caller's dict nor a shared one.
    assert fetched.extra_metadata is not payload


def test_malformed_stored_json_raises_domain_error(tmp_path: Path) -> None:
    """Corrupt stored JSON is surfaced deliberately, not as a raw decode error."""
    database_path = tmp_path / "mgo.db"
    apply_migrations(database_path)
    archive = CaptureArchive(database_path)
    connection = sqlite3.connect(database_path)
    try:
        connection.execute(
            """
            INSERT INTO captures (
                id, filename, absolute_path, captured_at_utc, width, height,
                filesize_bytes, camera_backend, created_at_utc, extra_metadata
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid.uuid4()),
                "broken.jpg",
                "/data/captures/broken.jpg",
                datetime(2026, 7, 24, 10, 0, tzinfo=UTC).isoformat(),
                640,
                480,
                100,
                "mock",
                datetime(2026, 7, 24, 10, 0, tzinfo=UTC).isoformat(),
                "{not valid json",
            ),
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(CaptureArchiveError, match="malformed extra_metadata"):
        archive.list_captures()


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

    ordered = [capture.filename for capture in archive.list_captures()]

    assert ordered == ["capture-2.jpg", "capture-1.jpg", "capture-0.jpg"]


def test_equal_capture_timestamps_break_ties_on_created_at(tmp_path: Path) -> None:
    """Identical captured_at values order deterministically by created_at."""
    earlier_created = datetime(2026, 7, 24, 9, 0, tzinfo=UTC)
    later_created = datetime(2026, 7, 24, 9, 5, tzinfo=UTC)
    archive = _archive(tmp_path, clock=_fixed_clock(earlier_created, later_created))
    shared = datetime(2026, 7, 24, 8, 0, tzinfo=UTC)

    first = archive.record_capture(_result(filename="first.jpg", timestamp=shared))
    second = archive.record_capture(_result(filename="second.jpg", timestamp=shared))

    captures = archive.list_captures()
    # Same capture instant, so the later-created record must come first.
    assert [c.id for c in captures] == [second.id, first.id]


def test_equal_timestamps_and_created_at_break_ties_on_id(tmp_path: Path) -> None:
    """Fully identical timestamps still order deterministically by id."""
    frozen = datetime(2026, 7, 24, 9, 0, tzinfo=UTC)
    archive = _archive(tmp_path, clock=_fixed_clock(frozen))
    shared = datetime(2026, 7, 24, 8, 0, tzinfo=UTC)

    a = archive.record_capture(_result(filename="a.jpg", timestamp=shared))
    b = archive.record_capture(_result(filename="b.jpg", timestamp=shared))

    listed = [str(c.id) for c in archive.list_captures()]
    # Deterministic id-descending order, and stable across repeated queries.
    assert listed == sorted([str(a.id), str(b.id)], reverse=True)
    assert listed == [str(c.id) for c in archive.list_captures()]


def test_each_capture_creates_a_distinct_record(tmp_path: Path) -> None:
    """Recording two distinct captures yields two records with distinct ids."""
    archive = _archive(tmp_path)

    first = archive.record_capture(_result(filename="a.jpg"))
    second = archive.record_capture(_result(filename="b.jpg"))

    assert len(archive.list_captures()) == 2
    assert first.id != second.id


def test_duplicate_absolute_path_is_rejected(tmp_path: Path) -> None:
    """A second record for the same on-disk path is refused at the DB level."""
    archive = _archive(tmp_path)
    result = _result()

    archive.record_capture(result)
    with pytest.raises(DuplicateCaptureError):
        archive.record_capture(result)

    # The rejection leaves exactly one row: no duplicate, no partial second row.
    assert len(archive.list_captures()) == 1


def test_get_capture_returns_none_for_unknown_id(tmp_path: Path) -> None:
    """An unknown identifier resolves to ``None`` (the 404 signal)."""
    archive = _archive(tmp_path)

    assert archive.get_capture(uuid.uuid4()) is None


def test_record_capture_raises_when_table_missing(tmp_path: Path) -> None:
    """A database failure is surfaced as a domain error, not a raw exception."""
    # No migrations applied, so the ``captures`` table does not exist.
    archive = CaptureArchive(tmp_path / "mgo.db")

    with pytest.raises(CaptureArchiveError):
        archive.record_capture(_result())


def test_repeated_migration_is_idempotent(tmp_path: Path) -> None:
    """Re-running the migration runner is safe and preserves existing rows."""
    database_path = tmp_path / "mgo.db"
    first = apply_migrations(database_path)
    archive = CaptureArchive(database_path)
    archive.record_capture(_result())

    # Simulates an existing installation being provisioned again on restart.
    second = apply_migrations(database_path)

    assert first == [1, 2]
    assert second == []
    assert len(archive.list_captures()) == 1


def test_observations_and_captures_coexist(tmp_path: Path) -> None:
    """Observations and captures live in, and are readable from, one database."""
    database_path = tmp_path / "mgo.db"
    apply_migrations(database_path)

    observation = record_observation(
        database_path,
        kind="application_start",
        source="test",
        status="success",
        summary="Started",
    )
    archive = CaptureArchive(database_path)
    capture = archive.record_capture(_result())

    observations = list_observations(database_path)
    captures = archive.list_captures()

    assert [o.id for o in observations] == [observation.id]
    assert [c.id for c in captures] == [capture.id]
