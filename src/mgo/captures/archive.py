"""Persistence for the MGO capture archive.

The archive is the authoritative catalogue of every successful capture. It uses
the application's existing raw-``sqlite3`` infrastructure -- the shared database
path, the :func:`~mgo.core.database.database_connection` transaction helper, and
the same connection, transaction and row-conversion conventions as
:mod:`mgo.core.observations`. It does *not* own a database engine or manage its
own schema: the ``captures`` table is created by the numbered migration runner.

Persistence is intentionally decoupled from the capture pipeline: the
:class:`~mgo.camera.capture.CaptureService` produces and verifies a
:class:`~mgo.camera.models.CaptureResult`, and only *after* that verification
does the API layer ask the archive to record it. A persistence failure never
deletes the captured JPEG -- the capture remains valid even if cataloguing fails.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mgo.camera.models import CaptureResult
from mgo.captures.models import Capture
from mgo.core.database import database_connection

LOGGER = logging.getLogger(__name__)

Clock = Callable[[], datetime]


class CaptureArchiveError(RuntimeError):
    """Raised when a capture-archive database operation cannot be completed."""


class DuplicateCaptureError(CaptureArchiveError):
    """Raised when a capture with the same on-disk path already exists.

    Enforced by a database-level ``UNIQUE`` constraint so a single physical JPEG
    can never be catalogued twice, independent of application control flow.
    """


def _utc_now() -> datetime:
    """Return the current timezone-aware UTC instant."""
    return datetime.now(UTC)


def _require_utc(value: datetime, label: str) -> datetime:
    """Return ``value`` normalised to UTC, rejecting naive datetimes.

    Matches the observation engine's rule that every persisted timestamp is
    timezone-aware; captures share the same serialisation convention.
    """
    if value.tzinfo is None:
        raise ValueError(f"Capture {label} must be timezone-aware")
    return value.astimezone(UTC)


class CaptureArchive:
    """Records and retrieves capture metadata in the shared MGO database.

    The archive holds only the database path; each operation opens a bounded,
    transactional connection via :func:`~mgo.core.database.database_connection`
    and closes it, so no long-lived connection is retained. This mirrors the
    stateless design of :mod:`mgo.core.observations`.
    """

    def __init__(self, database_path: Path, *, clock: Clock = _utc_now) -> None:
        self._database_path = database_path
        self._clock = clock

    def record_capture(
        self,
        result: CaptureResult,
        *,
        extra_metadata: dict[str, Any] | None = None,
    ) -> Capture:
        """Persist exactly one catalogue record for a verified capture.

        ``result`` must already have been produced and verified by the capture
        service. Returns the persisted :class:`Capture`. A duplicate on-disk path
        raises :class:`DuplicateCaptureError`; any other database failure raises
        :class:`CaptureArchiveError`. Both are logged and chain the underlying
        exception. The caller is responsible for preserving the JPEG on disk.
        """
        capture = Capture(
            id=uuid.uuid4(),
            filename=result.filename,
            absolute_path=str(result.absolute_path),
            captured_at_utc=_require_utc(result.timestamp, "captured_at_utc"),
            width=result.width,
            height=result.height,
            filesize_bytes=result.filesize_bytes,
            camera_backend=result.backend,
            created_at_utc=_require_utc(self._clock(), "created_at_utc"),
            extra_metadata=dict(extra_metadata) if extra_metadata else {},
        )

        try:
            with database_connection(self._database_path) as connection:
                connection.execute(
                    """
                    INSERT INTO captures (
                        id,
                        filename,
                        absolute_path,
                        captured_at_utc,
                        width,
                        height,
                        filesize_bytes,
                        camera_backend,
                        created_at_utc,
                        extra_metadata
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(capture.id),
                        capture.filename,
                        capture.absolute_path,
                        capture.captured_at_utc.isoformat(),
                        capture.width,
                        capture.height,
                        capture.filesize_bytes,
                        capture.camera_backend,
                        capture.created_at_utc.isoformat(),
                        json.dumps(capture.extra_metadata, sort_keys=True),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            LOGGER.error(
                "Refusing to persist duplicate capture for %s: %s",
                capture.absolute_path,
                exc,
            )
            raise DuplicateCaptureError(
                f"A capture already exists for {capture.absolute_path}"
            ) from exc
        except sqlite3.Error as exc:
            LOGGER.error(
                "Failed to persist capture metadata for %s: %s",
                result.filename,
                exc,
            )
            raise CaptureArchiveError(
                f"Could not persist capture metadata for {result.filename}: {exc}"
            ) from exc

        LOGGER.info("Recorded capture %s as %s", capture.filename, capture.id)
        return capture

    def list_captures(self) -> list[Capture]:
        """Return every capture, newest first.

        Ordering is fully deterministic even when captures share a
        ``captured_at_utc``: it breaks ties on ``created_at_utc`` and finally the
        ``id`` primary key, never relying on SQLite's incidental row order.
        """
        try:
            with database_connection(self._database_path) as connection:
                rows = connection.execute(
                    """
                    SELECT
                        id,
                        filename,
                        absolute_path,
                        captured_at_utc,
                        width,
                        height,
                        filesize_bytes,
                        camera_backend,
                        created_at_utc,
                        extra_metadata
                    FROM captures
                    ORDER BY captured_at_utc DESC, created_at_utc DESC, id DESC
                    """
                ).fetchall()
        except sqlite3.Error as exc:
            LOGGER.error("Failed to list captures: %s", exc)
            raise CaptureArchiveError(f"Could not list captures: {exc}") from exc

        return [_capture_from_row(row) for row in rows]

    def get_capture(self, capture_id: uuid.UUID) -> Capture | None:
        """Return the capture with ``capture_id`` or ``None`` if unknown."""
        try:
            with database_connection(self._database_path) as connection:
                row = connection.execute(
                    """
                    SELECT
                        id,
                        filename,
                        absolute_path,
                        captured_at_utc,
                        width,
                        height,
                        filesize_bytes,
                        camera_backend,
                        created_at_utc,
                        extra_metadata
                    FROM captures
                    WHERE id = ?
                    """,
                    (str(capture_id),),
                ).fetchone()
        except sqlite3.Error as exc:
            LOGGER.error("Failed to fetch capture %s: %s", capture_id, exc)
            raise CaptureArchiveError(
                f"Could not fetch capture {capture_id}: {exc}"
            ) from exc

        return _capture_from_row(row) if row is not None else None


def _capture_from_row(row: sqlite3.Row) -> Capture:
    """Convert a SQLite ``captures`` row into a :class:`Capture` domain object.

    Malformed stored JSON is surfaced deliberately as :class:`CaptureArchiveError`
    rather than being allowed to raise a raw decode error mid-listing.
    """
    try:
        extra_metadata = json.loads(str(row["extra_metadata"]))
    except json.JSONDecodeError as exc:
        raise CaptureArchiveError(
            f"Capture {row['id']} has malformed extra_metadata JSON: {exc}"
        ) from exc

    if not isinstance(extra_metadata, dict):
        raise CaptureArchiveError(
            f"Capture {row['id']} has non-object extra_metadata"
        )

    return Capture(
        id=uuid.UUID(str(row["id"])),
        filename=str(row["filename"]),
        absolute_path=str(row["absolute_path"]),
        captured_at_utc=datetime.fromisoformat(str(row["captured_at_utc"])),
        width=int(row["width"]),
        height=int(row["height"]),
        filesize_bytes=int(row["filesize_bytes"]),
        camera_backend=str(row["camera_backend"]),
        created_at_utc=datetime.fromisoformat(str(row["created_at_utc"])),
        extra_metadata=extra_metadata,
    )
