"""Tests for MGO database migration management."""

import sqlite3
from pathlib import Path

from mgo.core.database import apply_migrations


def test_initial_migration_creates_observation_table(tmp_path: Path) -> None:
    """The initial migration should create the observation schema."""
    database_path = tmp_path / "test.db"

    applied = apply_migrations(database_path)

    assert applied == [1]

    connection = sqlite3.connect(database_path)
    try:
        row = connection.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table' AND name = 'observations'
            """
        ).fetchone()
    finally:
        connection.close()

    assert row is not None


def test_migrations_are_idempotent(tmp_path: Path) -> None:
    """Already-applied migrations should not run a second time."""
    database_path = tmp_path / "test.db"

    first_result = apply_migrations(database_path)
    second_result = apply_migrations(database_path)

    assert first_result == [1]
    assert second_result == []
