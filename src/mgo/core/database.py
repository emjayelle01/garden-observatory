"""SQLite database management for Matt's Garden Observatory."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from mgo.core.config import PROJECT_ROOT

MIGRATIONS_DIRECTORY = PROJECT_ROOT / "migrations"


class DatabaseError(RuntimeError):
    """Raised when an MGO database operation cannot be completed."""


def utc_now_iso() -> str:
    """Return the current UTC timestamp in ISO 8601 format."""
    return datetime.now(UTC).isoformat()


def connect_database(database_path: Path) -> sqlite3.Connection:
    """Open an MGO SQLite database with required safety settings."""
    database_path.parent.mkdir(parents=True, exist_ok=True)

    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA busy_timeout = 5000")

    return connection


@contextmanager
def database_connection(database_path: Path) -> Iterator[sqlite3.Connection]:
    """Provide a transactional SQLite connection."""
    connection = connect_database(database_path)

    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _ensure_migration_table(connection: sqlite3.Connection) -> None:
    """Create the migration history table when it does not yet exist."""
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TEXT NOT NULL
        )
        """
    )


def _migration_version(path: Path) -> int:
    """Extract the numeric migration version from a migration filename."""
    prefix = path.name.split("_", maxsplit=1)[0]

    try:
        return int(prefix)
    except ValueError as exc:
        raise DatabaseError(f"Invalid migration filename: {path.name}") from exc


def apply_migrations(database_path: Path) -> list[int]:
    """Apply all pending SQL migrations in ascending version order."""
    if not MIGRATIONS_DIRECTORY.exists():
        raise DatabaseError(
            f"Migration directory does not exist: {MIGRATIONS_DIRECTORY}"
        )

    applied_versions: list[int] = []

    with database_connection(database_path) as connection:
        _ensure_migration_table(connection)

        rows = connection.execute("SELECT version FROM schema_migrations").fetchall()
        existing_versions = {int(row["version"]) for row in rows}

        migration_paths = sorted(
            MIGRATIONS_DIRECTORY.glob("[0-9][0-9][0-9]_*.sql"),
            key=_migration_version,
        )

        for migration_path in migration_paths:
            version = _migration_version(migration_path)

            if version in existing_versions:
                continue

            sql = migration_path.read_text(encoding="utf-8")

            try:
                connection.executescript(sql)
                connection.execute(
                    """
                    INSERT INTO schema_migrations (
                        version,
                        name,
                        applied_at
                    )
                    VALUES (?, ?, ?)
                    """,
                    (version, migration_path.name, utc_now_iso()),
                )
            except sqlite3.Error as exc:
                raise DatabaseError(
                    f"Failed to apply migration {migration_path.name}: {exc}"
                ) from exc

            applied_versions.append(version)

    return applied_versions
