"""Persistence for the MGO capture archive.

The archive is the authoritative catalogue of every successful capture. It owns
a SQLModel engine bound to the shared MGO SQLite database and exposes narrow,
typed operations for recording and reading capture metadata.

Persistence is intentionally decoupled from the capture pipeline: the
:class:`~mgo.camera.capture.CaptureService` produces and verifies a
:class:`~mgo.camera.models.CaptureResult`, and only *after* that verification
does the API layer ask the archive to record it. A persistence failure never
deletes the captured JPEG — the capture remains valid even if cataloguing fails.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import Engine
from sqlmodel import Session, SQLModel, col, create_engine, select

from mgo.camera.models import CaptureResult
from mgo.captures.models import Capture, to_naive_utc

LOGGER = logging.getLogger(__name__)


class CaptureArchiveError(RuntimeError):
    """Raised when a capture-archive database operation cannot be completed."""


def _utc_now() -> datetime:
    """Return the current timezone-aware UTC instant."""
    return datetime.now(UTC)


class CaptureArchive:
    """Records and retrieves capture metadata in the MGO database.

    The archive creates its own SQLModel engine for ``database_path`` and, on
    :meth:`initialize`, ensures the ``captures`` table exists. Table creation is
    idempotent, so new installations are provisioned automatically while
    existing installations are left untouched.
    """

    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path
        database_path.parent.mkdir(parents=True, exist_ok=True)
        # ``check_same_thread`` is disabled because captures are recorded from a
        # worker thread (the API runs the blocking capture off the event loop).
        # ``timeout`` sets SQLite's busy timeout so writes wait for, rather than
        # error on, a lock held by the raw-sqlite observation writer.
        self._engine: Engine = create_engine(
            f"sqlite:///{database_path}",
            connect_args={"check_same_thread": False, "timeout": 5.0},
        )

    @property
    def engine(self) -> Engine:
        """Return the underlying SQLModel engine (primarily for tests)."""
        return self._engine

    def initialize(self) -> None:
        """Create the capture table if it does not already exist.

        Only tables registered on :class:`~sqlmodel.SQLModel`'s metadata are
        created, so this provisions the ``captures`` table without touching the
        raw-SQL ``observations`` schema managed by the migration runner.
        """
        SQLModel.metadata.create_all(self._engine)

    def record_capture(self, result: CaptureResult) -> Capture:
        """Persist exactly one catalogue record for a verified capture.

        ``result`` must already have been produced and verified by the capture
        service. Any database failure is surfaced as :class:`CaptureArchiveError`
        and logged; the caller is responsible for preserving the JPEG on disk.
        """
        capture = Capture(
            filename=result.filename,
            absolute_path=str(result.absolute_path),
            captured_at_utc=to_naive_utc(result.timestamp),
            width=result.width,
            height=result.height,
            filesize_bytes=result.filesize_bytes,
            camera_backend=result.backend,
            created_at_utc=to_naive_utc(_utc_now()),
        )

        try:
            with Session(self._engine) as session:
                session.add(capture)
                session.commit()
                session.refresh(capture)
        except Exception as exc:
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

    def list_captures(self) -> Sequence[Capture]:
        """Return every capture, newest first.

        Ordering is by capture time descending, with the record creation time as
        a deterministic tie-breaker for captures sharing a timestamp.
        """
        statement = select(Capture).order_by(
            col(Capture.captured_at_utc).desc(),
            col(Capture.created_at_utc).desc(),
        )
        try:
            with Session(self._engine) as session:
                return session.exec(statement).all()
        except Exception as exc:
            LOGGER.error("Failed to list captures: %s", exc)
            raise CaptureArchiveError(f"Could not list captures: {exc}") from exc

    def get_capture(self, capture_id: uuid.UUID) -> Capture | None:
        """Return the capture with ``capture_id`` or ``None`` if unknown."""
        try:
            with Session(self._engine) as session:
                return session.get(Capture, capture_id)
        except Exception as exc:
            LOGGER.error("Failed to fetch capture %s: %s", capture_id, exc)
            raise CaptureArchiveError(
                f"Could not fetch capture {capture_id}: {exc}"
            ) from exc
