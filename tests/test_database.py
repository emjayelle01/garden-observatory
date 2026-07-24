"""Tests for MGO database migration management."""

import sqlite3
from pathlib import Path

from mgo.core.database import (
    MIGRATIONS_DIRECTORY,
    apply_migrations,
    database_connection,
    utc_now_iso,
)


def _table_exists(database_path: Path, name: str) -> bool:
    """Return whether a table exists in the SQLite database."""
    connection = sqlite3.connect(database_path)
    try:
        row = connection.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table' AND name = ?
            """,
            (name,),
        ).fetchone()
    finally:
        connection.close()
    return row is not None


def test_initial_migration_creates_observation_table(tmp_path: Path) -> None:
    """The initial migration should create the observation schema."""
    database_path = tmp_path / "test.db"

    applied = apply_migrations(database_path)

    assert applied == [1, 2]
    assert _table_exists(database_path, "observations")


def test_fresh_database_receives_captures_table(tmp_path: Path) -> None:
    """A fresh database gains every table, including ``captures`` (migration 2)."""
    database_path = tmp_path / "test.db"

    applied = apply_migrations(database_path)

    assert 2 in applied
    assert _table_exists(database_path, "observations")
    assert _table_exists(database_path, "captures")


def test_existing_pre_task_2c_database_receives_captures_migration(
    tmp_path: Path,
) -> None:
    """An existing DB with only migration 1 gains ``captures`` without data loss."""
    database_path = tmp_path / "test.db"

    # Reconstruct a pre-Task-2C installation: migration 1 applied, migration 2
    # not yet present in the schema-version history.
    sql_001 = (
        MIGRATIONS_DIRECTORY / "001_initial_observation_engine.sql"
    ).read_text(encoding="utf-8")
    with database_connection(database_path) as connection:
        connection.executescript(sql_001)
        connection.execute(
            """
            INSERT INTO schema_migrations (version, name, applied_at)
            VALUES (1, '001_initial_observation_engine.sql', ?)
            """,
            (utc_now_iso(),),
        )
        connection.execute(
            """
            INSERT INTO observations (
                id, observed_at, kind, source, status, summary,
                payload_json, correlation_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "legacy-observation",
                utc_now_iso(),
                "application_start",
                "legacy",
                "success",
                "Pre-2C row",
                "{}",
                None,
                utc_now_iso(),
            ),
        )

    assert not _table_exists(database_path, "captures")

    applied = apply_migrations(database_path)

    assert applied == [2]
    assert _table_exists(database_path, "captures")
    # Pre-existing observation data survives the migration untouched.
    connection = sqlite3.connect(database_path)
    try:
        row = connection.execute(
            "SELECT summary FROM observations WHERE id = 'legacy-observation'"
        ).fetchone()
    finally:
        connection.close()
    assert row is not None
    assert row[0] == "Pre-2C row"


def test_migrations_are_idempotent(tmp_path: Path) -> None:
    """Already-applied migrations should not run a second time."""
    database_path = tmp_path / "test.db"

    first_result = apply_migrations(database_path)
    second_result = apply_migrations(database_path)

    assert first_result == [1, 2]
    assert second_result == []
