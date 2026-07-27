"""Truthful database health for Matt's Garden Observatory.

The check answers one question -- *can the application rely on its database
right now?* -- and reports the evidence behind the answer. It is deliberately
**read-only**: it opens the database through SQLite's ``mode=ro`` URI, so it
can neither create a missing database file, nor create a missing table, nor
change the journal mode, nor repair anything. A health request must never be
able to alter the thing it is measuring.

Integrity is measured with ``PRAGMA quick_check(1)`` rather than
``PRAGMA integrity_check``. ``quick_check`` verifies page structure and record
sanity but skips the index-content cross-checks, which are the expensive part
and grow with the database; capping it at one error keeps a corrupt database
from producing an unbounded report. That is the right trade for a check that
runs on a schedule on a Raspberry Pi. A full ``integrity_check`` belongs to
operator-invoked diagnostics, which is a later task.

The module never raises: every failure becomes an ``unhealthy`` result with
bounded detail text, so a database problem can never take the application down
or leak a stack trace through the API.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from mgo.core.config import MGOConfig
from mgo.core.database import (
    CURRENT_SCHEMA_VERSION,
    REQUIRED_JOURNAL_MODE,
    connect_readonly,
    foreign_keys_enabled,
    is_memory_database,
    journal_mode,
    schema_version,
)

LOGGER = logging.getLogger(__name__)

_MAX_DETAIL_LENGTH = 500


class DatabaseStatus(StrEnum):
    """Truthful database health states.

    ``healthy``    -- reachable, structurally sound, at the expected schema
                      version, with foreign keys enforced and the documented
                      journal mode in use. Normal operation.
    ``degraded``   -- reachable and structurally sound, but running with a
                      documented deviation: the schema is behind the
                      application, foreign keys are not being enforced, or a
                      file-backed database is not in WAL mode. Reads and writes
                      still work; an operator should look.
    ``unhealthy``  -- not usable: unreachable, corrupt, carrying no schema
                      history at all, or recording a schema version newer than
                      this build supports. The application must not treat its
                      data as trustworthy.
    """

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"


class MigrationStatus(StrEnum):
    """How the database's schema version relates to this build's."""

    CURRENT = "current"
    PENDING = "pending"
    AHEAD = "ahead"
    UNKNOWN = "unknown"


#: How a database status maps onto the severity vocabulary the existing
#: ``/health`` aggregation already speaks (``healthy``/``warning``/
#: ``critical``). A degraded database is a warning, not an outage; an unhealthy
#: one is critical because nothing the application persists can be trusted.
_SEVERITY: dict[DatabaseStatus, str] = {
    DatabaseStatus.HEALTHY: "healthy",
    DatabaseStatus.DEGRADED: "warning",
    DatabaseStatus.UNHEALTHY: "critical",
}


def _bounded(detail: str) -> str:
    """Trim detail text to a safe, log-friendly length."""
    collapsed = " ".join(detail.split())
    if len(collapsed) <= _MAX_DETAIL_LENGTH:
        return collapsed
    return collapsed[: _MAX_DETAIL_LENGTH - 1].rstrip() + "…"


@dataclass(frozen=True)
class DatabaseHealth:
    """A typed, truthful snapshot of database health.

    ``database_name`` is deliberately the database file's *name* and not its
    absolute path: ``/health`` exposes no filesystem layout today, and the
    configured path is already available to an operator from the configuration
    file and the service logs. Nothing here echoes database contents, SQL
    values, configuration secrets or exception tracebacks.
    """

    status: DatabaseStatus
    accessible: bool
    database_name: str
    schema_version: int | None
    expected_schema_version: int
    migration_status: MigrationStatus
    journal_mode: str | None
    foreign_keys_enabled: bool
    integrity: str
    detail: str
    checked_at: datetime

    @property
    def severity(self) -> str:
        """This result expressed in the application's severity vocabulary."""
        return _SEVERITY[self.status]

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable representation of the result."""
        return {
            "status": self.status.value,
            "accessible": self.accessible,
            "database": self.database_name,
            "schema_version": self.schema_version,
            "expected_schema_version": self.expected_schema_version,
            "migration_status": self.migration_status.value,
            "journal_mode": self.journal_mode,
            "foreign_keys": self.foreign_keys_enabled,
            "integrity": self.integrity,
            "detail": self.detail,
            "checked_at": self.checked_at.isoformat(),
        }

    def health_dict(self) -> dict[str, Any]:
        """Return the compact projection embedded in ``GET /health``.

        Mirrors the preview service's compact projection: enough for a monitor
        to alert on, without duplicating the full status payload.
        """
        return {
            "status": self.status.value,
            "accessible": self.accessible,
            "schema_version": self.schema_version,
            "expected_schema_version": self.expected_schema_version,
            "migration_status": self.migration_status.value,
            "integrity": self.integrity,
        }


def _now(now: datetime | None) -> datetime:
    """Return a timezone-aware UTC timestamp for a health result."""
    return now if now is not None else datetime.now(UTC)


def _unhealthy(
    config: MGOConfig,
    detail: str,
    *,
    now: datetime | None = None,
    schema: int | None = None,
    migration: MigrationStatus = MigrationStatus.UNKNOWN,
    journal: str | None = None,
    integrity: str = "unknown",
    accessible: bool = False,
) -> DatabaseHealth:
    """Build an ``unhealthy`` result with bounded detail."""
    return DatabaseHealth(
        status=DatabaseStatus.UNHEALTHY,
        accessible=accessible,
        database_name=config.storage.database_path.name,
        schema_version=schema,
        expected_schema_version=CURRENT_SCHEMA_VERSION,
        migration_status=migration,
        journal_mode=journal,
        foreign_keys_enabled=False,
        integrity=integrity,
        detail=_bounded(detail),
        checked_at=_now(now),
    )


def _quick_check(connection: sqlite3.Connection) -> str:
    """Return the bounded integrity verdict: ``ok`` or a bounded description."""
    rows = connection.execute("PRAGMA quick_check(1)").fetchall()
    if not rows:
        return "unknown"
    first = str(rows[0][0])
    return "ok" if first.lower() == "ok" else _bounded(first)


def check_database_health(
    config: MGOConfig,
    *,
    now: datetime | None = None,
) -> DatabaseHealth:
    """Run one read-only database health check.

    Never raises and never mutates: an unreachable, corrupt or unsupported
    database becomes an ``unhealthy`` result rather than an exception. The
    connection is always closed, including on failure.
    """
    database_path = config.storage.database_path
    checked_at = _now(now)

    if is_memory_database(database_path):
        # An in-memory database is per-connection and vanishes on close, so
        # there is nothing a separate health connection could truthfully
        # inspect. Say so rather than reporting a fabricated result.
        return _unhealthy(
            config,
            "The configured database is in-memory; it is not persistent and "
            "cannot be health-checked independently.",
            now=checked_at,
        )

    try:
        connection = connect_readonly(
            database_path,
            busy_timeout_seconds=config.database.busy_timeout_seconds,
        )
    except sqlite3.DatabaseError as exc:
        return _unhealthy(
            config, f"Database could not be opened: {exc}", now=checked_at
        )
    except OSError as exc:
        return _unhealthy(
            config, f"Database path is not usable: {exc}", now=checked_at
        )

    try:
        return _inspect(config, connection, checked_at)
    except sqlite3.DatabaseError as exc:
        return _unhealthy(
            config,
            f"Database inspection failed: {exc}",
            now=checked_at,
            accessible=True,
        )
    finally:
        # Deterministic close: no connection (and no WAL sidecar handle) is
        # ever left behind by a health poll.
        connection.close()


def _inspect(
    config: MGOConfig,
    connection: sqlite3.Connection,
    checked_at: datetime,
) -> DatabaseHealth:
    """Gather evidence from an open read-only connection and classify it."""
    integrity = _quick_check(connection)
    if integrity != "ok":
        return _unhealthy(
            config,
            f"Database integrity check failed: {integrity}",
            now=checked_at,
            accessible=True,
            integrity=integrity,
        )

    mode = journal_mode(connection)
    # Enforcement is a per-connection setting, so the check verifies that an
    # operational connection *can* enable it -- exactly what every writer does.
    connection.execute("PRAGMA foreign_keys = ON")
    foreign_keys = foreign_keys_enabled(connection)

    version = schema_version(connection)

    if version is None:
        return _unhealthy(
            config,
            "Database has no schema-migration history; it has not been "
            "migrated by this application.",
            now=checked_at,
            accessible=True,
            journal=mode,
            integrity=integrity,
            migration=MigrationStatus.UNKNOWN,
        )

    if version > CURRENT_SCHEMA_VERSION:
        return _unhealthy(
            config,
            f"Database schema version {version} is newer than the supported "
            f"version {CURRENT_SCHEMA_VERSION}.",
            now=checked_at,
            accessible=True,
            schema=version,
            journal=mode,
            integrity=integrity,
            migration=MigrationStatus.AHEAD,
        )

    deviations: list[str] = []
    migration = MigrationStatus.CURRENT

    if version < CURRENT_SCHEMA_VERSION:
        migration = MigrationStatus.PENDING
        deviations.append(
            f"schema version {version} is behind the expected "
            f"{CURRENT_SCHEMA_VERSION}"
        )

    if not foreign_keys:
        deviations.append("foreign-key enforcement is not active")

    if mode != REQUIRED_JOURNAL_MODE:
        deviations.append(
            f"journal mode is {mode!r}, not {REQUIRED_JOURNAL_MODE!r}"
        )

    if deviations:
        return DatabaseHealth(
            status=DatabaseStatus.DEGRADED,
            accessible=True,
            database_name=config.storage.database_path.name,
            schema_version=version,
            expected_schema_version=CURRENT_SCHEMA_VERSION,
            migration_status=migration,
            journal_mode=mode,
            foreign_keys_enabled=foreign_keys,
            integrity=integrity,
            detail=_bounded("Database is usable but " + "; ".join(deviations) + "."),
            checked_at=checked_at,
        )

    return DatabaseHealth(
        status=DatabaseStatus.HEALTHY,
        accessible=True,
        database_name=config.storage.database_path.name,
        schema_version=version,
        expected_schema_version=CURRENT_SCHEMA_VERSION,
        migration_status=migration,
        journal_mode=mode,
        foreign_keys_enabled=foreign_keys,
        integrity=integrity,
        detail=(
            f"Database is at schema version {version} with "
            f"{mode} journalling and foreign keys enforced."
        ),
        checked_at=checked_at,
    )


def unavailable_health(
    config: MGOConfig,
    *,
    now: datetime | None = None,
) -> DatabaseHealth:
    """Return the safe result to report before any check has run."""
    return _unhealthy(
        config,
        "Database health has not been evaluated yet.",
        now=now,
    )


class DatabaseHealthState:
    """Holds the most recently checked database-health result.

    A single asyncio event loop reads and writes this holder, so a plain
    attribute is sufficient; no locking is required (mirrors
    :class:`mgo.core.camera.CameraState`).
    """

    def __init__(self) -> None:
        self._latest: DatabaseHealth | None = None

    def get(self) -> DatabaseHealth | None:
        """Return the latest result, or ``None`` if none recorded yet."""
        return self._latest

    def set(self, health: DatabaseHealth) -> None:
        """Replace the latest result."""
        self._latest = health
