"""Observation timeline services for Matt's Garden Observatory."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from mgo.core.database import database_connection


@dataclass(frozen=True)
class Observation:
    """A single immutable event recorded by the observatory."""

    id: str
    observed_at: datetime
    kind: str
    source: str
    status: str
    summary: str
    payload: dict[str, Any]
    correlation_id: str | None
    created_at: datetime


def _utc_datetime(value: datetime | None = None) -> datetime:
    """Return a timezone-aware UTC datetime."""
    result = value or datetime.now(UTC)

    if result.tzinfo is None:
        raise ValueError("Observation timestamps must be timezone-aware")

    return result.astimezone(UTC)


def build_observation(
    *,
    kind: str,
    source: str,
    status: str,
    summary: str,
    payload: dict[str, Any] | None = None,
    correlation_id: str | None = None,
    observed_at: datetime | None = None,
) -> Observation:
    """Validate the fields of one observation and return it, unpersisted.

    Every rule about what an observation may contain lives here and only here:
    the four required strings must be non-blank, timestamps must be
    timezone-aware UTC, the identifier is generated, and an absent payload
    becomes an empty mapping. Splitting it out of :func:`record_observation` is
    what lets a caller holding an *open transaction* -- retention finalisation,
    which must commit a lifecycle transition and its observation together --
    write an observation under exactly the same rules, without a second
    validation path or a second ``INSERT`` drifting away from this one.
    """
    if not kind.strip():
        raise ValueError("Observation kind cannot be empty")

    if not source.strip():
        raise ValueError("Observation source cannot be empty")

    if not status.strip():
        raise ValueError("Observation status cannot be empty")

    if not summary.strip():
        raise ValueError("Observation summary cannot be empty")

    return Observation(
        id=str(uuid4()),
        observed_at=_utc_datetime(observed_at),
        kind=kind,
        source=source,
        status=status,
        summary=summary,
        payload=payload or {},
        correlation_id=correlation_id,
        created_at=datetime.now(UTC),
    )


def insert_observation(
    connection: sqlite3.Connection, observation: Observation
) -> None:
    """Insert one already-validated observation on an existing connection.

    The single ``INSERT`` for the observations table. It neither opens nor
    commits a transaction, so the caller decides what this row commits *with*:
    :func:`record_observation` gives it a transaction of its own, while the
    retention finaliser makes it share the transaction that transitions a
    capture's media lifecycle -- so a reclaimed JPEG can never end up recorded
    in one of those places and not the other.
    """
    connection.execute(
        """
        INSERT INTO observations (
            id,
            observed_at,
            kind,
            source,
            status,
            summary,
            payload_json,
            correlation_id,
            created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            observation.id,
            observation.observed_at.isoformat(),
            observation.kind,
            observation.source,
            observation.status,
            observation.summary,
            json.dumps(observation.payload, sort_keys=True),
            observation.correlation_id,
            observation.created_at.isoformat(),
        ),
    )


def record_observation_in_transaction(
    connection: sqlite3.Connection,
    *,
    kind: str,
    source: str,
    status: str,
    summary: str,
    payload: dict[str, Any] | None = None,
    correlation_id: str | None = None,
    observed_at: datetime | None = None,
) -> Observation:
    """Validate and insert an observation inside the caller's transaction.

    Same validation and same ``INSERT`` as :func:`record_observation`; the only
    difference is who owns the transaction. Nothing here commits, so if the
    caller's transaction rolls back, the observation goes with it -- which is
    the entire point for retention, where an observation claiming media was
    reclaimed must not survive a lifecycle transition that did not.
    """
    observation = build_observation(
        kind=kind,
        source=source,
        status=status,
        summary=summary,
        payload=payload,
        correlation_id=correlation_id,
        observed_at=observed_at,
    )
    insert_observation(connection, observation)
    return observation


def record_observation(
    database_path: Path,
    *,
    kind: str,
    source: str,
    status: str,
    summary: str,
    payload: dict[str, Any] | None = None,
    correlation_id: str | None = None,
    observed_at: datetime | None = None,
) -> Observation:
    """Create and persist an immutable observation."""
    observation = build_observation(
        kind=kind,
        source=source,
        status=status,
        summary=summary,
        payload=payload,
        correlation_id=correlation_id,
        observed_at=observed_at,
    )

    with database_connection(database_path) as connection:
        insert_observation(connection, observation)

    return observation


def list_observations(
    database_path: Path,
    *,
    limit: int = 100,
    kind: str | None = None,
) -> list[Observation]:
    """Return recent observations in reverse chronological order."""
    if limit < 1 or limit > 1000:
        raise ValueError("Observation limit must be between 1 and 1000")

    query = """
        SELECT
            id,
            observed_at,
            kind,
            source,
            status,
            summary,
            payload_json,
            correlation_id,
            created_at
        FROM observations
    """
    parameters: list[str | int] = []

    if kind is not None:
        query += " WHERE kind = ?"
        parameters.append(kind)

    query += " ORDER BY observed_at DESC, created_at DESC LIMIT ?"
    parameters.append(limit)

    with database_connection(database_path) as connection:
        rows = connection.execute(query, parameters).fetchall()

    return [_observation_from_row(row) for row in rows]


def _observation_from_row(row: sqlite3.Row) -> Observation:
    """Convert a SQLite observation row into a domain object."""
    payload = json.loads(str(row["payload_json"]))

    if not isinstance(payload, dict):
        raise ValueError(f"Observation {row['id']} has an invalid payload")

    return Observation(
        id=str(row["id"]),
        observed_at=datetime.fromisoformat(str(row["observed_at"])),
        kind=str(row["kind"]),
        source=str(row["source"]),
        status=str(row["status"]),
        summary=str(row["summary"]),
        payload=payload,
        correlation_id=(
            str(row["correlation_id"]) if row["correlation_id"] is not None else None
        ),
        created_at=datetime.fromisoformat(str(row["created_at"])),
    )
