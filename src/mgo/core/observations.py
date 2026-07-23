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
    if not kind.strip():
        raise ValueError("Observation kind cannot be empty")

    if not source.strip():
        raise ValueError("Observation source cannot be empty")

    if not status.strip():
        raise ValueError("Observation status cannot be empty")

    if not summary.strip():
        raise ValueError("Observation summary cannot be empty")

    observation_time = _utc_datetime(observed_at)
    creation_time = datetime.now(UTC)
    observation_id = str(uuid4())
    observation_payload = payload or {}

    with database_connection(database_path) as connection:
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
                observation_id,
                observation_time.isoformat(),
                kind,
                source,
                status,
                summary,
                json.dumps(observation_payload, sort_keys=True),
                correlation_id,
                creation_time.isoformat(),
            ),
        )

    return Observation(
        id=observation_id,
        observed_at=observation_time,
        kind=kind,
        source=source,
        status=status,
        summary=summary,
        payload=observation_payload,
        correlation_id=correlation_id,
        created_at=creation_time,
    )


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
