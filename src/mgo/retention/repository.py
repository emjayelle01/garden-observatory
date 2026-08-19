"""Retention-specific SQLite queries and lifecycle state transitions.

This is the only place retention touches the database. It owns three things:

* a purpose-specific **projection** of the capture catalogue joined to the media
  lifecycle table, carrying exactly what the planner and the executor need;
* the **conditional** state transitions that make overlapping execution safe --
  a claim that can only succeed once, a finalisation that can only fire once;
* the **composite transactions** that commit a lifecycle change and its
  immutable observation together, using the observation engine's own validation
  and ``INSERT`` rather than a second copy of them.

It deliberately does not duplicate :class:`~mgo.captures.archive.CaptureArchive`.
The archive is the capture catalogue's public API and retention is not a reason
to widen it; a narrow projection here is cheaper to reason about than a shared
one that has to satisfy both.

**Failing closed is the house rule.** A capture row whose ``extra_metadata`` will
not parse, or whose stored lifecycle state or reason is not in the vocabulary,
raises rather than being interpreted generously. Retention deletes files; a
catalogue it cannot read is not one it may act on.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mgo.core.database import database_connection
from mgo.core.observations import record_observation_in_transaction
from mgo.retention.models import (
    CaptureLifecycleRecord,
    MediaLifecycleState,
    RetentionReason,
)

LOGGER = logging.getLogger(__name__)

Clock = Callable[[], datetime]

#: The catalogue projection. A ``LEFT JOIN`` because most captures have no
#: lifecycle row at all, and the absence of one is the ``PRESENT`` state rather
#: than a missing record.
_PROJECTION_SQL = """
    SELECT
        c.id AS capture_id,
        c.filename AS filename,
        c.absolute_path AS absolute_path,
        c.captured_at_utc AS captured_at_utc,
        c.created_at_utc AS created_at_utc,
        c.filesize_bytes AS filesize_bytes,
        c.extra_metadata AS extra_metadata,
        l.state AS lifecycle_state,
        l.requested_at_utc AS requested_at_utc,
        l.deleted_at_utc AS deleted_at_utc,
        l.reason AS reason
    FROM captures AS c
    LEFT JOIN capture_media_lifecycle AS l
        ON l.capture_id = c.id
    ORDER BY c.captured_at_utc ASC, c.created_at_utc ASC, c.id ASC
"""


class RetentionRepositoryError(RuntimeError):
    """Raised when a retention database operation cannot be completed."""


class RetentionCatalogueError(RetentionRepositoryError):
    """Raised when a stored catalogue or lifecycle row cannot be trusted.

    Separate from the generic error because it means something different: not
    "the database was unavailable" but "the database answered, and the answer
    cannot be interpreted". Both stop a destructive run; only this one says the
    stored data is the problem.
    """


def _utc_now() -> datetime:
    """Return the current timezone-aware UTC instant."""
    return datetime.now(UTC)


def _parse_origin(capture_id: str, raw_metadata: str) -> str | None:
    """Return the capture's declared origin, failing closed on bad JSON.

    A non-string ``origin`` (a number, a list, ``null``) is reported as ``None``
    rather than coerced with ``str()``: ``str(["motion"])`` is not the string
    ``"motion"``, and a value that is not the exact managed origin must fall into
    the protected set, not near it.

    Malformed JSON is a different matter entirely and raises. The origin is the
    single field standing between an automatic capture and a manual one; guessing
    it from a document that will not parse is how a manual capture gets deleted.
    """
    try:
        metadata = json.loads(raw_metadata)
    except json.JSONDecodeError as exc:
        raise RetentionCatalogueError(
            f"Capture {capture_id} has malformed extra_metadata JSON"
        ) from exc

    if not isinstance(metadata, dict):
        raise RetentionCatalogueError(
            f"Capture {capture_id} has non-object extra_metadata"
        )

    origin = metadata.get("origin")
    return origin if isinstance(origin, str) else None


def _parse_state(capture_id: str, raw_state: Any) -> MediaLifecycleState:
    """Return the stored lifecycle state, or ``PRESENT`` when there is no row."""
    if raw_state is None:
        return MediaLifecycleState.PRESENT

    try:
        state = MediaLifecycleState(str(raw_state))
    except ValueError as exc:
        raise RetentionCatalogueError(
            f"Capture {capture_id} has an unrecognised media lifecycle state"
        ) from exc

    if state is MediaLifecycleState.PRESENT:
        # The table's CHECK constraint forbids storing this, so seeing it means
        # the constraint was bypassed and the row's meaning is unknowable.
        raise RetentionCatalogueError(
            f"Capture {capture_id} has a stored 'present' lifecycle row"
        )
    return state


def _parse_reason(capture_id: str, raw_reason: Any) -> RetentionReason | None:
    """Return the stored policy reason, failing closed on anything unknown."""
    if raw_reason is None:
        return None

    try:
        return RetentionReason(str(raw_reason))
    except ValueError as exc:
        raise RetentionCatalogueError(
            f"Capture {capture_id} has an unrecognised retention reason"
        ) from exc


def _parse_timestamp(
    capture_id: str, label: str, raw_value: Any
) -> datetime | None:
    """Return a stored ISO-8601 timestamp as an aware UTC datetime."""
    if raw_value is None:
        return None

    try:
        parsed = datetime.fromisoformat(str(raw_value))
    except ValueError as exc:
        raise RetentionCatalogueError(
            f"Capture {capture_id} has an unparseable {label} timestamp"
        ) from exc

    if parsed.tzinfo is None:
        raise RetentionCatalogueError(
            f"Capture {capture_id} has a naive {label} timestamp"
        )
    return parsed.astimezone(UTC)


def _require_text(capture_id: str, label: str, raw_value: Any) -> str:
    """Return a required non-empty text column, failing closed on anything else.

    ``str(raw_value)`` is deliberately **not** used. SQLite's column affinity is
    a conversion preference, not a constraint -- the ``captures`` table is not
    ``STRICT`` -- so a damaged or hand-edited row can hold ``NULL``, a number or
    a blob here. Coercing one would manufacture a plausible-looking value:
    ``str(None)`` is the four-character filename ``"None"``, and a filename is
    one of the two columns that decides which file is about to be removed.
    """
    if not isinstance(raw_value, str) or not raw_value:
        raise RetentionCatalogueError(
            f"Capture {capture_id} has an invalid {label}"
        )
    return raw_value


def _parse_filesize(capture_id: str, raw_value: Any) -> int:
    """Return the catalogued media size, failing closed on anything unusable.

    Three rules, all fail-closed:

    * the value must already be an integer. Under the column's ``INTEGER``
      affinity SQLite converts a *numeric* string on insert, so a string that
      survives to be read back is one it could not convert -- and ``int()`` on
      it would raise a bare ``ValueError`` out of catalogue decoding and strand
      a destructive run. A float is refused for the same reason it is
      suspicious: the capture pipeline never writes one;
    * ``bool`` is excluded explicitly. It is a subclass of ``int``, so
      ``isinstance`` alone would let ``True`` through as the size ``1``;
    * the value must be positive. The capture service only ever catalogues a
      verified non-empty JPEG, so zero or negative is not a small file -- it is
      a corrupt record, and it must not be allowed to masquerade as a size
      mismatch against a real file on disk.
    """
    if isinstance(raw_value, bool) or not isinstance(raw_value, int):
        raise RetentionCatalogueError(
            f"Capture {capture_id} has a non-integer filesize_bytes"
        )
    if raw_value <= 0:
        raise RetentionCatalogueError(
            f"Capture {capture_id} has a non-positive filesize_bytes"
        )
    return raw_value


def _record_from_row(row: sqlite3.Row) -> CaptureLifecycleRecord:
    """Convert one projection row into the retention domain projection.

    Every column is validated rather than coerced. A retention catalogue that
    cannot be decoded must surface as :class:`RetentionCatalogueError` -- which
    the service maps to the fixed ``catalogue_invalid`` category -- and never as
    an arbitrary Python conversion exception escaping a destructive run.
    """
    raw_identifier = row["capture_id"]
    if not isinstance(raw_identifier, str) or not raw_identifier:
        raise RetentionCatalogueError("A capture row has an invalid identifier")
    capture_id = raw_identifier

    captured_at = _parse_timestamp(
        capture_id, "captured_at_utc", row["captured_at_utc"]
    )
    created_at = _parse_timestamp(
        capture_id, "created_at_utc", row["created_at_utc"]
    )
    if captured_at is None or created_at is None:
        # Both columns are NOT NULL in the schema, so this is unreachable
        # through the application; it is here so a hand-edited database is
        # refused rather than planned against with a missing ordering key.
        raise RetentionCatalogueError(
            f"Capture {capture_id} is missing a required timestamp"
        )

    return CaptureLifecycleRecord(
        capture_id=capture_id,
        filename=_require_text(capture_id, "filename", row["filename"]),
        absolute_path=_require_text(
            capture_id, "absolute_path", row["absolute_path"]
        ),
        captured_at_utc=captured_at,
        created_at_utc=created_at,
        filesize_bytes=_parse_filesize(capture_id, row["filesize_bytes"]),
        origin=_parse_origin(
            capture_id,
            _require_text(capture_id, "extra_metadata", row["extra_metadata"]),
        ),
        lifecycle_state=_parse_state(capture_id, row["lifecycle_state"]),
        requested_at_utc=_parse_timestamp(
            capture_id, "requested_at_utc", row["requested_at_utc"]
        ),
        deleted_at_utc=_parse_timestamp(
            capture_id, "deleted_at_utc", row["deleted_at_utc"]
        ),
        reason=_parse_reason(capture_id, row["reason"]),
    )


class RetentionRepository:
    """Reads the retention projection and performs lifecycle transitions.

    Holds only the database path; each operation opens a bounded, transactional
    connection and closes it, mirroring the stateless design of
    :mod:`mgo.core.observations` and :class:`~mgo.captures.archive.CaptureArchive`.
    No connection is retained, and no transaction is ever held open across a
    filesystem operation.
    """

    def __init__(self, database_path: Path, *, clock: Clock = _utc_now) -> None:
        self._database_path = database_path
        self._clock = clock

    def list_lifecycle_records(self) -> list[CaptureLifecycleRecord]:
        """Return every catalogued capture with its media lifecycle state.

        Read-only. Ordered oldest first at the database, though the planner sorts
        again for itself: the order is a convenience here, never a dependency,
        because a destructive decision must not rest on a collation.
        """
        try:
            with database_connection(self._database_path) as connection:
                rows = connection.execute(_PROJECTION_SQL).fetchall()
        except sqlite3.Error as exc:
            LOGGER.error("Failed to read the retention projection: %s", exc)
            raise RetentionRepositoryError(
                f"Could not read the capture retention projection: {exc}"
            ) from exc

        return [_record_from_row(row) for row in rows]

    def claim_pending_delete(
        self,
        capture_id: str,
        reason: RetentionReason,
        *,
        requested_at: datetime | None = None,
    ) -> bool:
        """Persist and commit a durable deletion intent. Stage A.

        Returns whether *this* call created the intent. The insert is
        conditional on there being no row for the capture at all, so two
        overlapping executions cannot both claim the same capture: one inserts,
        the other is told ``False`` and leaves the capture alone. A capture that
        is already ``deleted`` is likewise refused, because a second claim on
        reclaimed media could only produce a second deletion observation for one
        deletion.

        This commits before the caller touches the filesystem, and holds no
        transaction afterwards. That ordering is the whole recovery story: an
        intent that outlives a crash is what tells the next run that a missing
        file was authorised rather than unexplained.
        """
        timestamp = requested_at or self._clock()
        try:
            with database_connection(self._database_path) as connection:
                cursor = connection.execute(
                    """
                    INSERT INTO capture_media_lifecycle (
                        capture_id,
                        state,
                        requested_at_utc,
                        deleted_at_utc,
                        reason
                    )
                    VALUES (?, ?, ?, NULL, ?)
                    ON CONFLICT(capture_id) DO NOTHING
                    """,
                    (
                        capture_id,
                        MediaLifecycleState.PENDING_DELETE.value,
                        timestamp.astimezone(UTC).isoformat(),
                        reason.value,
                    ),
                )
                claimed = cursor.rowcount == 1
        except sqlite3.Error as exc:
            LOGGER.error(
                "Failed to record a retention deletion intent for capture %s: %s",
                capture_id,
                exc,
            )
            raise RetentionRepositoryError(
                f"Could not record a deletion intent for capture {capture_id}"
            ) from exc

        return claimed

    def finalize_deletion(
        self,
        capture_id: str,
        *,
        deleted_at: datetime | None = None,
        observation_fields: dict[str, Any],
    ) -> bool:
        """Complete a deletion and record it, atomically. Stage C.

        The transition and the immutable observation share one transaction, so
        the timeline can never claim media was reclaimed by a run whose lifecycle
        row did not move -- nor the reverse.

        The ``UPDATE`` is conditional on the row still being ``pending_delete``.
        Returns ``False`` when it matched nothing, and in that case the
        observation is *not* written: some other execution finalised this
        capture, and one deletion must produce exactly one success observation.
        """
        timestamp = (deleted_at or self._clock()).astimezone(UTC)
        try:
            with database_connection(self._database_path) as connection:
                cursor = connection.execute(
                    """
                    UPDATE capture_media_lifecycle
                    SET state = ?, deleted_at_utc = ?
                    WHERE capture_id = ? AND state = ?
                    """,
                    (
                        MediaLifecycleState.DELETED.value,
                        timestamp.isoformat(),
                        capture_id,
                        MediaLifecycleState.PENDING_DELETE.value,
                    ),
                )
                # Named because it is the guarantee, not a detail: the UPDATE
                # matched a still-pending row, so this execution -- and only
                # this one -- is the one that completes the deletion.
                advanced = cursor.rowcount == 1
                if not advanced:
                    # Nothing to finalise. Rolling back discards the partial
                    # effect, and the caller reads the ``False`` below rather
                    # than an exception.
                    connection.rollback()
                    LOGGER.warning(
                        "Retention finalisation for capture %s matched no "
                        "pending intent; it was already completed elsewhere",
                        capture_id,
                    )
                    return False
                record_observation_in_transaction(connection, **observation_fields)
        except sqlite3.Error as exc:
            LOGGER.error(
                "Failed to finalise retention deletion for capture %s: %s",
                capture_id,
                exc,
            )
            raise RetentionRepositoryError(
                f"Could not finalise the deletion of capture {capture_id}"
            ) from exc

        return True

    def cancel_pending_delete(
        self,
        capture_id: str,
        *,
        observation_fields: dict[str, Any],
    ) -> bool:
        """Return a failed candidate's media to ``PRESENT``, atomically.

        Used only when the filesystem deletion did *not* happen and the file is
        still there: removing the intent restores the truth that the media
        exists, and the failure observation recording that is committed with it.

        Conditional on ``pending_delete`` for the same reason as finalisation. If
        this transaction itself fails the caller must leave the intent standing
        rather than assume recovery worked -- a durable pending row is
        recoverable, and a false belief that it was cleared is not.
        """
        try:
            with database_connection(self._database_path) as connection:
                cursor = connection.execute(
                    """
                    DELETE FROM capture_media_lifecycle
                    WHERE capture_id = ? AND state = ?
                    """,
                    (capture_id, MediaLifecycleState.PENDING_DELETE.value),
                )
                removed = cursor.rowcount == 1
                if not removed:
                    connection.rollback()
                    LOGGER.warning(
                        "Retention cancellation for capture %s matched no "
                        "pending intent",
                        capture_id,
                    )
                    return False
                record_observation_in_transaction(connection, **observation_fields)
        except sqlite3.Error as exc:
            LOGGER.error(
                "Failed to cancel the retention deletion intent for capture "
                "%s: %s",
                capture_id,
                exc,
            )
            raise RetentionRepositoryError(
                f"Could not cancel the deletion intent for capture {capture_id}"
            ) from exc

        return True


__all__ = [
    "RetentionCatalogueError",
    "RetentionRepository",
    "RetentionRepositoryError",
]
