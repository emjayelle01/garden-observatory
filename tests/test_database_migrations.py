"""Tests for the Task 7 database foundation: migrations and SQLite runtime.

Everything here runs against temporary SQLite databases in ``tmp_path``. No
Raspberry Pi hardware, no network access and no live/production database path
is involved, so the suite is deterministic on Windows, Linux and CI alike.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from mgo.core.config import PROJECT_ROOT
from mgo.core.database import (
    CURRENT_SCHEMA_VERSION,
    MEMORY_DATABASE,
    MIGRATIONS_DIRECTORY,
    REQUIRED_JOURNAL_MODE,
    DatabaseError,
    IncompatibleSchemaError,
    apply_migrations,
    connect_database,
    connect_readonly,
    database_connection,
    foreign_keys_enabled,
    is_memory_database,
    journal_mode,
    read_schema_version,
    utc_now_iso,
)
from mgo.core.observations import list_observations, record_observation


def _import_in_fresh_interpreter(*modules: str, cwd: Path) -> str:
    """Import ``modules`` in a new interpreter and return its stdout.

    Runs with ``cwd`` set to a temporary directory, so any relative-path
    filesystem side effect during import would be visible there.
    """
    code = (
        "import " + ", ".join(modules) + "\n"
        "from mgo.core.database import CURRENT_SCHEMA_VERSION\n"
        "print(CURRENT_SCHEMA_VERSION)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=cwd,
        env={**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "src")},
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


_OBSERVATION_INDEXES = {
    "idx_observations_observed_at",
    "idx_observations_kind",
    "idx_observations_source",
    "idx_observations_correlation_id",
}


def _objects(database_path: Path, kind: str) -> set[str]:
    """Return the names of every schema object of ``kind`` in the database."""
    connection = sqlite3.connect(database_path)
    try:
        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = ?", (kind,)
        ).fetchall()
    finally:
        connection.close()
    return {str(row[0]) for row in rows}


def _tables(database_path: Path) -> set[str]:
    """Return every table name in the database."""
    return _objects(database_path, "table")


def _write_legacy_observation(connection: sqlite3.Connection, id_: str) -> None:
    """Insert one observation row directly, as an older build would have."""
    connection.execute(
        """
        INSERT INTO observations (
            id, observed_at, kind, source, status, summary,
            payload_json, correlation_id, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            id_,
            utc_now_iso(),
            "application_start",
            "legacy",
            "success",
            "Legacy row",
            '{"legacy": true}',
            None,
            utc_now_iso(),
        ),
    )


# --- a brand-new database ---------------------------------------------------


def test_new_database_migrates_to_the_current_schema(tmp_path: Path) -> None:
    """A brand-new database receives every migration and reaches current."""
    database_path = tmp_path / "new.db"

    applied = apply_migrations(database_path)

    assert applied == [1, 2, 3, 4]
    assert read_schema_version(database_path) == CURRENT_SCHEMA_VERSION


def test_migration_creates_the_expected_tables_and_indexes(
    tmp_path: Path,
) -> None:
    """The migrated schema contains exactly the tables and indexes it should."""
    database_path = tmp_path / "new.db"

    apply_migrations(database_path)

    assert {
        "schema_migrations",
        "observations",
        "captures",
        "capture_media_lifecycle",
        "recognition_jobs",
        "recognition_results",
    } <= _tables(database_path)
    indexes = _objects(database_path, "index")
    assert indexes >= _OBSERVATION_INDEXES
    assert "idx_captures_captured_at_utc" in indexes
    assert "idx_capture_media_lifecycle_state" in indexes
    assert "idx_recognition_jobs_due" in indexes
    assert "idx_recognition_jobs_lease" in indexes


def test_current_schema_version_is_recorded(tmp_path: Path) -> None:
    """Every applied migration leaves a history row naming its file."""
    database_path = tmp_path / "new.db"

    apply_migrations(database_path)

    with database_connection(database_path) as connection:
        rows = connection.execute(
            "SELECT version, name, applied_at FROM schema_migrations "
            "ORDER BY version"
        ).fetchall()

    assert [int(row["version"]) for row in rows] == [1, 2, 3, 4]
    assert str(rows[0]["name"]) == "001_initial_observation_engine.sql"
    assert str(rows[1]["name"]) == "002_capture_archive.sql"
    assert str(rows[2]["name"]) == "003_capture_media_lifecycle.sql"
    assert str(rows[3]["name"]) == "004_recognition_jobs.sql"
    assert all(str(row["applied_at"]) for row in rows)


def test_schema_version_constant_matches_the_migration_files() -> None:
    """The constant must stay in step with the highest numbered migration.

    Guards the one thing an explicit constant can get wrong: a new migration
    file landing without the application's notion of "current" moving with it.
    """
    versions = [
        int(path.name.split("_", maxsplit=1)[0])
        for path in MIGRATIONS_DIRECTORY.glob("[0-9][0-9][0-9]_*.sql")
    ]

    assert max(versions) == CURRENT_SCHEMA_VERSION
    assert sorted(versions) == list(range(1, CURRENT_SCHEMA_VERSION + 1))


def test_migration_is_idempotent(tmp_path: Path) -> None:
    """Re-running migration changes nothing once the schema is current."""
    database_path = tmp_path / "new.db"

    first = apply_migrations(database_path)
    before = _tables(database_path)

    second = apply_migrations(database_path)
    third = apply_migrations(database_path)

    assert first == [1, 2, 3, 4]
    assert second == []
    assert third == []
    assert _tables(database_path) == before
    assert read_schema_version(database_path) == CURRENT_SCHEMA_VERSION


# --- an existing database from current main ---------------------------------


def test_existing_versioned_database_keeps_its_observations(
    tmp_path: Path,
) -> None:
    """A database written by current main upgrades with its data intact."""
    database_path = tmp_path / "existing.db"
    apply_migrations(database_path)
    record_observation(
        database_path,
        kind="application_start",
        source="mgo-api",
        status="success",
        summary="Existing production row",
    )
    before = list_observations(database_path)

    applied = apply_migrations(database_path)

    assert applied == []
    after = list_observations(database_path)
    assert after == before
    assert len(after) == 1
    assert after[0].summary == "Existing production row"


def test_unversioned_legacy_database_is_adopted_without_data_loss(
    tmp_path: Path,
) -> None:
    """A pre-versioning database carrying the supported schema is adopted.

    The tables already exist and hold real data, so adoption records the
    history rows and touches nothing else -- no table is created, dropped or
    rewritten, and every observation survives byte for byte.
    """
    database_path = tmp_path / "legacy.db"
    sql = (MIGRATIONS_DIRECTORY / "001_initial_observation_engine.sql").read_text(
        encoding="utf-8"
    )
    with database_connection(database_path) as connection:
        connection.executescript(sql)
        connection.execute("DROP TABLE schema_migrations")
        _write_legacy_observation(connection, "legacy-1")
        _write_legacy_observation(connection, "legacy-2")

    assert "schema_migrations" not in _tables(database_path)
    before = list_observations(database_path)
    assert len(before) == 2

    applied = apply_migrations(database_path)

    # Version 1 was adopted (not re-run); 2, 3 and 4 were actually applied.
    assert applied == [2, 3, 4]
    assert read_schema_version(database_path) == CURRENT_SCHEMA_VERSION
    after = list_observations(database_path)
    assert after == before
    assert {observation.id for observation in after} == {"legacy-1", "legacy-2"}
    assert after[0].payload == {"legacy": True}


def test_unversioned_version_two_database_is_adopted_then_migrated(
    tmp_path: Path,
) -> None:
    """An unversioned version-2 database adopts at 2, then migrates onward.

    This is the shape a real pre-Task-14.1 deployment presents, and it is the
    reason migration 003 adds a *new table* rather than a column on ``captures``:
    the legacy-adoption logic compares an exact column set, so widening the
    capture table would have made every existing database unrecognisable.
    """
    database_path = tmp_path / "legacy-full.db"
    with database_connection(database_path) as connection:
        for name in ("001_initial_observation_engine", "002_capture_archive"):
            connection.executescript(
                (MIGRATIONS_DIRECTORY / f"{name}.sql").read_text(encoding="utf-8")
            )
        connection.execute("DROP TABLE schema_migrations")
        _write_legacy_observation(connection, "legacy-3")

    applied = apply_migrations(database_path)

    assert applied == [3, 4]
    assert read_schema_version(database_path) == 4
    assert len(list_observations(database_path)) == 1
    assert "capture_media_lifecycle" in _tables(database_path)


def test_fully_unversioned_current_schema_is_adopted_at_the_top_version(
    tmp_path: Path,
) -> None:
    """An unversioned database with every current table adopts the top version.

    Nothing is executed against it: the tables already exist, so adoption only
    writes the missing history rows.
    """
    database_path = tmp_path / "legacy-v4.db"
    with database_connection(database_path) as connection:
        for name in (
            "001_initial_observation_engine",
            "002_capture_archive",
            "003_capture_media_lifecycle",
            "004_recognition_jobs",
        ):
            connection.executescript(
                (MIGRATIONS_DIRECTORY / f"{name}.sql").read_text(encoding="utf-8")
            )
        connection.execute("DROP TABLE schema_migrations")
        _write_legacy_observation(connection, "legacy-4")

    applied = apply_migrations(database_path)

    assert applied == []
    assert read_schema_version(database_path) == 4
    assert len(list_observations(database_path)) == 1


def test_a_partial_version_three_legacy_schema_is_rejected(
    tmp_path: Path,
) -> None:
    """A lifecycle table of the wrong shape is refused, not adopted.

    Adoption records history rows without inspecting data, so a table that only
    *looks* like the lifecycle table would be trusted forever afterwards. An
    exact column-set match is what stops that.
    """
    database_path = tmp_path / "legacy-bad-v3.db"
    with database_connection(database_path) as connection:
        for name in ("001_initial_observation_engine", "002_capture_archive"):
            connection.executescript(
                (MIGRATIONS_DIRECTORY / f"{name}.sql").read_text(encoding="utf-8")
            )
        connection.execute("DROP TABLE schema_migrations")
        connection.execute(
            "CREATE TABLE capture_media_lifecycle "
            "(capture_id TEXT PRIMARY KEY, state TEXT NOT NULL)"
        )

    with pytest.raises(IncompatibleSchemaError):
        apply_migrations(database_path)

    assert "schema_migrations" not in _tables(database_path)


def test_a_lifecycle_table_without_the_schema_beneath_it_is_rejected(
    tmp_path: Path,
) -> None:
    """A version-3 table with no version-2 table beneath it is not adoptable."""
    database_path = tmp_path / "stray-v3.db"
    with database_connection(database_path) as connection:
        connection.executescript(
            (MIGRATIONS_DIRECTORY / "001_initial_observation_engine.sql").read_text(
                encoding="utf-8"
            )
        )
        connection.execute("DROP TABLE schema_migrations")
        connection.execute(
            "CREATE TABLE capture_media_lifecycle ("
            "capture_id TEXT PRIMARY KEY, state TEXT NOT NULL, "
            "requested_at_utc TEXT NOT NULL, deleted_at_utc TEXT, "
            "reason TEXT NOT NULL)"
        )

    with pytest.raises(IncompatibleSchemaError):
        apply_migrations(database_path)

    assert "schema_migrations" not in _tables(database_path)


# --- refusing what cannot be used -------------------------------------------


def test_newer_schema_version_is_rejected(tmp_path: Path) -> None:
    """A database from a newer build fails closed and is left unchanged."""
    database_path = tmp_path / "future.db"
    apply_migrations(database_path)
    with database_connection(database_path) as connection:
        connection.execute(
            "INSERT INTO schema_migrations (version, name, applied_at) "
            "VALUES (?, ?, ?)",
            (CURRENT_SCHEMA_VERSION + 1, "999_from_the_future.sql", utc_now_iso()),
        )

    with pytest.raises(IncompatibleSchemaError) as excinfo:
        apply_migrations(database_path)

    assert "newer than the version this application supports" in str(excinfo.value)
    assert read_schema_version(database_path) == CURRENT_SCHEMA_VERSION + 1


def test_malformed_legacy_schema_is_rejected_and_database_untouched(
    tmp_path: Path,
) -> None:
    """An unversioned ``observations`` table of the wrong shape is refused."""
    database_path = tmp_path / "malformed.db"
    with database_connection(database_path) as connection:
        connection.execute(
            "CREATE TABLE observations (id TEXT PRIMARY KEY, whatever TEXT)"
        )

    with pytest.raises(IncompatibleSchemaError) as excinfo:
        apply_migrations(database_path)

    assert "does not match the supported schema" in str(excinfo.value)
    # Nothing was created: the database is exactly as it was found.
    assert _tables(database_path) == {"observations"}


def test_partial_legacy_schema_without_version_one_is_rejected(
    tmp_path: Path,
) -> None:
    """A ``captures`` table with no ``observations`` table is not adoptable."""
    database_path = tmp_path / "partial.db"
    with database_connection(database_path) as connection:
        connection.executescript(
            (MIGRATIONS_DIRECTORY / "002_capture_archive.sql").read_text(
                encoding="utf-8"
            )
        )

    with pytest.raises(IncompatibleSchemaError):
        apply_migrations(database_path)

    assert "schema_migrations" not in _tables(database_path)


# --- transactional failure --------------------------------------------------


def _install_failing_migrations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    """Point the runner at a good migration 001 and a failing migration 002."""
    directory = tmp_path / "migrations"
    directory.mkdir()
    (directory / "001_initial_observation_engine.sql").write_text(
        (MIGRATIONS_DIRECTORY / "001_initial_observation_engine.sql").read_text(
            encoding="utf-8"
        ),
        encoding="utf-8",
    )
    # The first statement succeeds; the second cannot even be prepared. A
    # runner without real per-migration rollback would leave ``partial_marker``
    # behind.
    (directory / "002_broken.sql").write_text(
        "CREATE TABLE partial_marker (x INTEGER);\n"
        "INSERT INTO no_such_table (y) VALUES (1);\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("mgo.core.database.MIGRATIONS_DIRECTORY", directory)
    return directory


def test_failed_migration_rolls_back_completely(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A migration that fails part-way leaves none of its statements applied."""
    _install_failing_migrations(tmp_path, monkeypatch)
    database_path = tmp_path / "rollback.db"

    with pytest.raises(DatabaseError) as excinfo:
        apply_migrations(database_path)

    message = str(excinfo.value)
    assert "002_broken.sql" in message
    assert "version 2" in message
    assert "partial_marker" not in _tables(database_path)
    assert "observations" in _tables(database_path)


def test_partially_migrated_database_is_not_reported_as_current(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After a failed migration the recorded version stays at the last good one."""
    _install_failing_migrations(tmp_path, monkeypatch)
    database_path = tmp_path / "rollback.db"

    with pytest.raises(DatabaseError):
        apply_migrations(database_path)

    assert read_schema_version(database_path) == 1


def test_failed_migration_preserves_existing_observations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Data written before a failed upgrade survives the rollback."""
    database_path = tmp_path / "rollback.db"
    apply_migrations(database_path)
    record_observation(
        database_path,
        kind="application_start",
        source="mgo-api",
        status="success",
        summary="Row that must survive",
    )
    before = list_observations(database_path)

    directory = _install_failing_migrations(tmp_path, monkeypatch)
    # Renumber so the broken migration sits one version above the current
    # schema, i.e. genuinely pending against the already-current database above,
    # and supply real copies of every migration beneath it.
    (directory / "002_broken.sql").rename(
        directory / f"{CURRENT_SCHEMA_VERSION + 1:03d}_broken.sql"
    )
    for name in (
        "002_capture_archive",
        "003_capture_media_lifecycle",
        "004_recognition_jobs",
    ):
        (directory / f"{name}.sql").write_text(
            (MIGRATIONS_DIRECTORY / f"{name}.sql").read_text(encoding="utf-8"),
            encoding="utf-8",
        )

    with pytest.raises(DatabaseError):
        apply_migrations(database_path)

    assert list_observations(database_path) == before
    assert read_schema_version(database_path) == CURRENT_SCHEMA_VERSION


# --- SQLite runtime configuration -------------------------------------------


def test_operational_connections_enforce_foreign_keys(tmp_path: Path) -> None:
    """Every read-write connection has foreign-key enforcement active."""
    database_path = tmp_path / "fk.db"
    apply_migrations(database_path)

    connection = connect_database(database_path)
    try:
        assert foreign_keys_enabled(connection) is True
    finally:
        connection.close()

    with database_connection(database_path) as transactional:
        assert foreign_keys_enabled(transactional) is True


def test_file_backed_database_uses_the_documented_journal_mode(
    tmp_path: Path,
) -> None:
    """A file-backed database really is in WAL mode, not merely asked to be."""
    database_path = tmp_path / "wal.db"
    apply_migrations(database_path)

    connection = connect_database(database_path)
    try:
        assert journal_mode(connection) == REQUIRED_JOURNAL_MODE
    finally:
        connection.close()


def test_in_memory_database_is_handled_explicitly(tmp_path: Path) -> None:
    """An in-memory database is recognised and never pretends to be WAL."""
    memory_path = Path(MEMORY_DATABASE)

    assert is_memory_database(memory_path) is True
    assert is_memory_database(tmp_path / "file.db") is False

    connection = connect_database(memory_path)
    try:
        # SQLite silently substitutes its own mode; the helper reports the
        # truth rather than the request.
        assert journal_mode(connection) != REQUIRED_JOURNAL_MODE
        assert journal_mode(connection) == "memory"
        assert foreign_keys_enabled(connection) is True
    finally:
        connection.close()

    # The migration runner works against it, but it is per-connection and
    # transient, so it can never be opened read-only for a health check.
    assert apply_migrations(memory_path) == [1, 2, 3, 4]
    with pytest.raises(DatabaseError):
        connect_readonly(memory_path)


def test_connect_database_creates_no_parent_when_asked_not_to(
    tmp_path: Path,
) -> None:
    """``create_parents=False`` refuses to bring a missing directory to life."""
    database_path = tmp_path / "missing" / "mgo.db"

    with pytest.raises(sqlite3.OperationalError):
        connect_database(database_path, create_parents=False)

    assert not database_path.parent.exists()


def test_readonly_connection_never_creates_or_writes(tmp_path: Path) -> None:
    """The read-only connection cannot materialise or modify a database."""
    missing = tmp_path / "absent.db"

    with pytest.raises(sqlite3.OperationalError):
        connect_readonly(missing)
    assert not missing.exists()

    database_path = tmp_path / "ro.db"
    apply_migrations(database_path)
    connection = connect_readonly(database_path)
    try:
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("CREATE TABLE injected (x INTEGER)")
    finally:
        connection.close()

    assert "injected" not in _tables(database_path)


def test_busy_timeout_is_finite_and_bounded(tmp_path: Path) -> None:
    """A competing writer fails after the configured wait, not indefinitely.

    Proves the bound is real in both directions: the writer waits at least
    roughly its timeout (so the setting is applied at all) and gives up far
    inside the default, so a stuck writer surfaces instead of hanging.
    """
    database_path = tmp_path / "busy.db"
    apply_migrations(database_path)

    blocker = connect_database(database_path)
    contender = connect_database(database_path, busy_timeout_seconds=0.25)
    try:
        blocker.execute("BEGIN EXCLUSIVE")
        blocker.execute(
            "INSERT INTO observations (id, observed_at, kind, source, status, "
            "summary, payload_json, correlation_id, created_at) "
            "VALUES ('blocker', ?, 'k', 's', 'ok', 'held', '{}', NULL, ?)",
            (utc_now_iso(), utc_now_iso()),
        )

        started = time.monotonic()
        with pytest.raises(sqlite3.OperationalError) as excinfo:
            contender.execute("BEGIN IMMEDIATE")
        elapsed = time.monotonic() - started
    finally:
        contender.close()
        blocker.rollback()
        blocker.close()

    assert "locked" in str(excinfo.value).lower()
    assert elapsed >= 0.2
    assert elapsed < 5.0


def test_invalid_busy_timeout_is_rejected(tmp_path: Path) -> None:
    """A non-positive or absurd busy timeout fails clearly."""
    database_path = tmp_path / "timeout.db"

    with pytest.raises(DatabaseError):
        connect_database(database_path, busy_timeout_seconds=0)
    with pytest.raises(DatabaseError):
        connect_database(database_path, busy_timeout_seconds=600)


def test_importing_the_database_module_creates_nothing(tmp_path: Path) -> None:
    """Importing the module in a fresh interpreter has no filesystem effect.

    A subprocess is used deliberately: reloading the module in-process would
    rebind its exception and enum classes and break every other test's identity
    checks, and re-importing an already-imported module proves nothing.
    """
    result = _import_in_fresh_interpreter("mgo.core.database", cwd=tmp_path)

    assert result == str(CURRENT_SCHEMA_VERSION)
    assert list(tmp_path.iterdir()) == []


# --- version-3 lifecycle schema safety (Task 14.1 correction round) ---------
#
# The lifecycle table governs the deletion of media, so "this database is at
# version 3" has to mean the constraints are really there. Adoption writes
# history rows and then trusts the tables forever afterwards: a table with the
# right five column names and none of the guarantees would be adopted once and
# believed permanently.

#: The canonical lifecycle DDL, and the variants that each drop exactly one
#: safety property while keeping the same five column names. Every variant must
#: be refused.
_CANONICAL_LIFECYCLE = """
    CREATE TABLE capture_media_lifecycle (
        capture_id TEXT NOT NULL PRIMARY KEY REFERENCES captures(id),
        state TEXT NOT NULL CHECK (state IN ('pending_delete', 'deleted')),
        requested_at_utc TEXT NOT NULL,
        deleted_at_utc TEXT,
        reason TEXT NOT NULL
            CHECK (reason IN ('age', 'managed_bytes', 'age_and_managed_bytes')),
        CHECK (
            (state = 'pending_delete' AND deleted_at_utc IS NULL)
            OR (state = 'deleted' AND deleted_at_utc IS NOT NULL)
        )
    )
"""

_NO_CONSTRAINTS_AT_ALL = """
    CREATE TABLE capture_media_lifecycle (
        capture_id TEXT,
        state TEXT,
        requested_at_utc TEXT,
        deleted_at_utc TEXT,
        reason TEXT
    )
"""

_NO_PRIMARY_KEY = """
    CREATE TABLE capture_media_lifecycle (
        capture_id TEXT NOT NULL REFERENCES captures(id),
        state TEXT NOT NULL CHECK (state IN ('pending_delete', 'deleted')),
        requested_at_utc TEXT NOT NULL,
        deleted_at_utc TEXT,
        reason TEXT NOT NULL
            CHECK (reason IN ('age', 'managed_bytes', 'age_and_managed_bytes')),
        CHECK (
            (state = 'pending_delete' AND deleted_at_utc IS NULL)
            OR (state = 'deleted' AND deleted_at_utc IS NOT NULL)
        )
    )
"""

_NO_FOREIGN_KEY = """
    CREATE TABLE capture_media_lifecycle (
        capture_id TEXT NOT NULL PRIMARY KEY,
        state TEXT NOT NULL CHECK (state IN ('pending_delete', 'deleted')),
        requested_at_utc TEXT NOT NULL,
        deleted_at_utc TEXT,
        reason TEXT NOT NULL
            CHECK (reason IN ('age', 'managed_bytes', 'age_and_managed_bytes')),
        CHECK (
            (state = 'pending_delete' AND deleted_at_utc IS NULL)
            OR (state = 'deleted' AND deleted_at_utc IS NOT NULL)
        )
    )
"""

_NO_STATE_CHECK = """
    CREATE TABLE capture_media_lifecycle (
        capture_id TEXT NOT NULL PRIMARY KEY REFERENCES captures(id),
        state TEXT NOT NULL,
        requested_at_utc TEXT NOT NULL,
        deleted_at_utc TEXT,
        reason TEXT NOT NULL
            CHECK (reason IN ('age', 'managed_bytes', 'age_and_managed_bytes')),
        CHECK (
            (state = 'pending_delete' AND deleted_at_utc IS NULL)
            OR (state = 'deleted' AND deleted_at_utc IS NOT NULL)
        )
    )
"""

_WIDENED_STATE_CHECK = """
    CREATE TABLE capture_media_lifecycle (
        capture_id TEXT NOT NULL PRIMARY KEY REFERENCES captures(id),
        state TEXT NOT NULL
            CHECK (state IN ('pending_delete', 'deleted', 'quarantined')),
        requested_at_utc TEXT NOT NULL,
        deleted_at_utc TEXT,
        reason TEXT NOT NULL
            CHECK (reason IN ('age', 'managed_bytes', 'age_and_managed_bytes')),
        CHECK (
            (state = 'pending_delete' AND deleted_at_utc IS NULL)
            OR (state = 'deleted' AND deleted_at_utc IS NOT NULL)
        )
    )
"""

_NO_REASON_CHECK = """
    CREATE TABLE capture_media_lifecycle (
        capture_id TEXT NOT NULL PRIMARY KEY REFERENCES captures(id),
        state TEXT NOT NULL CHECK (state IN ('pending_delete', 'deleted')),
        requested_at_utc TEXT NOT NULL,
        deleted_at_utc TEXT,
        reason TEXT NOT NULL,
        CHECK (
            (state = 'pending_delete' AND deleted_at_utc IS NULL)
            OR (state = 'deleted' AND deleted_at_utc IS NOT NULL)
        )
    )
"""

_WIDENED_REASON_CHECK = """
    CREATE TABLE capture_media_lifecycle (
        capture_id TEXT NOT NULL PRIMARY KEY REFERENCES captures(id),
        state TEXT NOT NULL CHECK (state IN ('pending_delete', 'deleted')),
        requested_at_utc TEXT NOT NULL,
        deleted_at_utc TEXT,
        reason TEXT NOT NULL CHECK (reason IN ('age', 'operator')),
        CHECK (
            (state = 'pending_delete' AND deleted_at_utc IS NULL)
            OR (state = 'deleted' AND deleted_at_utc IS NOT NULL)
        )
    )
"""

_NO_TIMESTAMP_COHERENCE = """
    CREATE TABLE capture_media_lifecycle (
        capture_id TEXT NOT NULL PRIMARY KEY REFERENCES captures(id),
        state TEXT NOT NULL CHECK (state IN ('pending_delete', 'deleted')),
        requested_at_utc TEXT NOT NULL,
        deleted_at_utc TEXT,
        reason TEXT NOT NULL
            CHECK (reason IN ('age', 'managed_bytes', 'age_and_managed_bytes'))
    )
"""

_NULLABLE_CAPTURE_ID = """
    CREATE TABLE capture_media_lifecycle (
        capture_id TEXT PRIMARY KEY REFERENCES captures(id),
        state TEXT NOT NULL CHECK (state IN ('pending_delete', 'deleted')),
        requested_at_utc TEXT NOT NULL,
        deleted_at_utc TEXT,
        reason TEXT NOT NULL
            CHECK (reason IN ('age', 'managed_bytes', 'age_and_managed_bytes')),
        CHECK (
            (state = 'pending_delete' AND deleted_at_utc IS NULL)
            OR (state = 'deleted' AND deleted_at_utc IS NOT NULL)
        )
    )
"""

_MISSING_NOT_NULL = """
    CREATE TABLE capture_media_lifecycle (
        capture_id TEXT NOT NULL PRIMARY KEY REFERENCES captures(id),
        state TEXT CHECK (state IN ('pending_delete', 'deleted')),
        requested_at_utc TEXT NOT NULL,
        deleted_at_utc TEXT,
        reason TEXT NOT NULL
            CHECK (reason IN ('age', 'managed_bytes', 'age_and_managed_bytes')),
        CHECK (
            (state = 'pending_delete' AND deleted_at_utc IS NULL)
            OR (state = 'deleted' AND deleted_at_utc IS NOT NULL)
        )
    )
"""


def _unversioned_v3(tmp_path: Path, lifecycle_ddl: str, name: str) -> Path:
    """Build an unversioned database whose lifecycle table uses ``lifecycle_ddl``."""
    database_path = tmp_path / f"{name}.db"
    with database_connection(database_path) as connection:
        for migration in ("001_initial_observation_engine", "002_capture_archive"):
            connection.executescript(
                (MIGRATIONS_DIRECTORY / f"{migration}.sql").read_text(
                    encoding="utf-8"
                )
            )
        connection.execute("DROP TABLE schema_migrations")
        connection.execute(lifecycle_ddl)
        _write_legacy_observation(connection, "legacy-v3")
    return database_path


def test_a_canonical_unversioned_version_three_database_is_adopted(
    tmp_path: Path,
) -> None:
    """The real schema, written by hand rather than by the runner, is accepted.

    This is the control for the rejection cases below: without it they would
    only prove that the verifier refuses things, not that it still recognises
    the schema it is meant to recognise. Adopted at version 3, the database
    then receives migration 004 like any other version-3 database.
    """
    database_path = _unversioned_v3(tmp_path, _CANONICAL_LIFECYCLE, "canonical")

    applied = apply_migrations(database_path)

    assert applied == [4]
    assert read_schema_version(database_path) == 4
    assert len(list_observations(database_path)) == 1


@pytest.mark.parametrize(
    ("label", "lifecycle_ddl"),
    [
        ("no constraints at all", _NO_CONSTRAINTS_AT_ALL),
        ("no primary key", _NO_PRIMARY_KEY),
        ("no foreign key", _NO_FOREIGN_KEY),
        ("no state CHECK", _NO_STATE_CHECK),
        ("widened state CHECK", _WIDENED_STATE_CHECK),
        ("no reason CHECK", _NO_REASON_CHECK),
        ("widened reason CHECK", _WIDENED_REASON_CHECK),
        ("no timestamp coherence CHECK", _NO_TIMESTAMP_COHERENCE),
        ("missing NOT NULL", _MISSING_NOT_NULL),
        ("nullable capture_id", _NULLABLE_CAPTURE_ID),
    ],
)
def test_an_unconstrained_unversioned_version_three_table_is_rejected(
    tmp_path: Path, label: str, lifecycle_ddl: str
) -> None:
    """The same five column names are not enough to be adopted as version 3.

    Each variant drops exactly one safety property. A table adopted without it
    would go on to accept a lifecycle row for a capture that does not exist, in
    a state that does not exist, for a reason nobody defined, or a completed
    deletion with no deletion time -- and nothing would ever check again.
    """
    database_path = _unversioned_v3(
        tmp_path, lifecycle_ddl, label.replace(" ", "-")
    )

    with pytest.raises(IncompatibleSchemaError):
        apply_migrations(database_path)


def test_a_rejected_version_three_adoption_fabricates_no_history(
    tmp_path: Path,
) -> None:
    """A refused database is left exactly as it was found.

    No ``schema_migrations`` table is created, so nothing later reads the
    refusal as a partially adopted database.
    """
    database_path = _unversioned_v3(
        tmp_path, _NO_CONSTRAINTS_AT_ALL, "no-history"
    )

    with pytest.raises(IncompatibleSchemaError):
        apply_migrations(database_path)

    assert "schema_migrations" not in _tables(database_path)
    assert read_schema_version(database_path) is None


def test_a_pre_existing_lifecycle_table_fails_migration_003(
    tmp_path: Path,
) -> None:
    """A version-2 database carrying a shadow lifecycle table must not migrate.

    ``CREATE TABLE IF NOT EXISTS`` would have made this statement a no-op, and
    the runner would then have recorded version 3 over a table it never created.
    The shadow table below deliberately carries a ``state`` column, so the index
    creation that follows would have succeeded and nothing else would have
    noticed.
    """
    database_path = tmp_path / "shadow.db"
    with database_connection(database_path) as connection:
        for migration in ("001_initial_observation_engine", "002_capture_archive"):
            connection.executescript(
                (MIGRATIONS_DIRECTORY / f"{migration}.sql").read_text(
                    encoding="utf-8"
                )
            )
        connection.execute("DELETE FROM schema_migrations WHERE version > 2")
        connection.execute(
            "INSERT OR REPLACE INTO schema_migrations (version, name, applied_at) "
            "VALUES (1, '001.sql', ?), (2, '002.sql', ?)",
            (utc_now_iso(), utc_now_iso()),
        )
        connection.execute(
            "CREATE TABLE capture_media_lifecycle (capture_id TEXT, state TEXT)"
        )
        _write_legacy_observation(connection, "legacy-shadow")

    with pytest.raises(DatabaseError):
        apply_migrations(database_path)

    # Rolled back to the version it really is, with its data intact.
    assert read_schema_version(database_path) == 2
    assert len(list_observations(database_path)) == 1


def test_a_failed_migration_003_leaves_the_shadow_table_untouched(
    tmp_path: Path,
) -> None:
    """The failed migration creates nothing and rewrites nothing."""
    database_path = tmp_path / "shadow-intact.db"
    with database_connection(database_path) as connection:
        for migration in ("001_initial_observation_engine", "002_capture_archive"):
            connection.executescript(
                (MIGRATIONS_DIRECTORY / f"{migration}.sql").read_text(
                    encoding="utf-8"
                )
            )
        connection.execute("DELETE FROM schema_migrations WHERE version > 2")
        connection.execute(
            "INSERT OR REPLACE INTO schema_migrations (version, name, applied_at) "
            "VALUES (1, '001.sql', ?), (2, '002.sql', ?)",
            (utc_now_iso(), utc_now_iso()),
        )
        connection.execute(
            "CREATE TABLE capture_media_lifecycle (capture_id TEXT, state TEXT)"
        )

    with pytest.raises(DatabaseError):
        apply_migrations(database_path)

    with database_connection(database_path) as connection:
        columns = [
            str(row[1])
            for row in connection.execute(
                "PRAGMA table_info(capture_media_lifecycle)"
            )
        ]
    assert columns == ["capture_id", "state"]
    assert "idx_capture_media_lifecycle_state" not in _objects(
        database_path, "index"
    )


# --- lifecycle capture identity (Task 14.1 final correction) ----------------
#
# Every lifecycle row belongs to exactly one real capture. SQLite does not give
# that for free: a PRIMARY KEY column that is not INTEGER PRIMARY KEY stays
# nullable -- a documented legacy quirk -- and a NULL foreign key is never
# checked, because NULL means there is no referenced value to check. Before
# capture_id was declared NOT NULL the table admitted rows bound to no capture,
# and admitted more than one of them, because the primary key index treats NULLs
# as distinct.


def test_the_lifecycle_capture_id_is_declared_not_null(tmp_path: Path) -> None:
    """The migrated table reports the identity column as NOT NULL and PK."""
    database_path = tmp_path / "identity.db"
    apply_migrations(database_path)

    with database_connection(database_path) as connection:
        columns = {
            str(row[1]): {"notnull": int(row[3]), "pk": int(row[5])}
            for row in connection.execute(
                "PRAGMA table_info(capture_media_lifecycle)"
            )
        }

    assert columns["capture_id"] == {"notnull": 1, "pk": 1}
    # The rest of the shape is unchanged by this correction.
    assert columns["state"]["notnull"] == 1
    assert columns["requested_at_utc"]["notnull"] == 1
    assert columns["reason"]["notnull"] == 1
    assert columns["deleted_at_utc"]["notnull"] == 0


def test_a_null_lifecycle_capture_id_is_rejected(tmp_path: Path) -> None:
    """A lifecycle row bound to no capture cannot exist."""
    database_path = tmp_path / "null-identity.db"
    apply_migrations(database_path)

    with (
        pytest.raises(sqlite3.IntegrityError, match="NOT NULL"),
        database_connection(database_path) as connection,
    ):
        connection.execute(
            "INSERT INTO capture_media_lifecycle VALUES "
            "(NULL, 'pending_delete', ?, NULL, 'age')",
            (utc_now_iso(),),
        )


def test_multiple_null_lifecycle_capture_ids_cannot_accumulate(
    tmp_path: Path,
) -> None:
    """Not even a second NULL row can be created.

    The primary key alone would not have stopped this: SQLite's index treats
    NULLs as distinct, so a nullable identity column admits *many* orphan rows
    rather than one. Only NOT NULL closes it.
    """
    database_path = tmp_path / "many-null.db"
    apply_migrations(database_path)

    for state, deleted_at in (
        ("pending_delete", None),
        ("deleted", utc_now_iso()),
    ):
        with (
            pytest.raises(sqlite3.IntegrityError),
            database_connection(database_path) as connection,
        ):
            connection.execute(
                "INSERT INTO capture_media_lifecycle VALUES "
                "(NULL, ?, ?, ?, 'age')",
                (state, utc_now_iso(), deleted_at),
            )

    with database_connection(database_path) as connection:
        remaining = connection.execute(
            "SELECT COUNT(*) FROM capture_media_lifecycle"
        ).fetchone()[0]
    assert remaining == 0


def _insert_lifecycle_capture(database_path: Path, identifier: str) -> None:
    """Insert one minimal capture row so a lifecycle row has something to bind to."""
    stamp = utc_now_iso()
    with database_connection(database_path) as connection:
        connection.execute(
            """
            INSERT INTO captures (
                id, filename, absolute_path, captured_at_utc, width, height,
                filesize_bytes, camera_backend, created_at_utc, extra_metadata
            )
            VALUES (?, ?, ?, ?, 4608, 2592, 1000, 'simulator', ?, '{}')
            """,
            (identifier, f"{identifier}.jpg", f"/captures/{identifier}.jpg",
             stamp, stamp),
        )


def test_a_lifecycle_row_for_a_real_capture_still_succeeds(
    tmp_path: Path,
) -> None:
    """The control: an ordinary bound lifecycle row is unaffected."""
    database_path = tmp_path / "valid-identity.db"
    apply_migrations(database_path)
    _insert_lifecycle_capture(database_path, "cap-1")

    with database_connection(database_path) as connection:
        connection.execute(
            "INSERT INTO capture_media_lifecycle VALUES "
            "(?, 'pending_delete', ?, NULL, 'age')",
            ("cap-1", utc_now_iso()),
        )

    with database_connection(database_path) as connection:
        rows = [
            tuple(row)
            for row in connection.execute(
                "SELECT capture_id, state FROM capture_media_lifecycle"
            )
        ]
    assert rows == [("cap-1", "pending_delete")]


def test_a_non_null_unknown_lifecycle_capture_id_still_fails(
    tmp_path: Path,
) -> None:
    """The foreign key still catches a present-but-unreal identity.

    NOT NULL and the foreign key close different halves of the same invariant:
    NOT NULL refuses "no capture at all", the foreign key refuses "a capture
    that does not exist".
    """
    database_path = tmp_path / "unknown-identity.db"
    apply_migrations(database_path)

    with (
        pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"),
        database_connection(database_path) as connection,
    ):
        connection.execute(
            "INSERT INTO capture_media_lifecycle VALUES "
            "('no-such-capture', 'pending_delete', ?, NULL, 'age')",
            (utc_now_iso(),),
        )


def test_a_nullable_capture_id_unversioned_schema_is_rejected(
    tmp_path: Path,
) -> None:
    """An otherwise perfect unversioned v3 table with a nullable identity fails.

    Every other property matches canonical exactly -- same columns, primary key,
    foreign key and all three CHECK constraints. Only the NOT NULL on the
    identity column is missing, and that alone is enough to refuse adoption.
    """
    database_path = _unversioned_v3(
        tmp_path, _NULLABLE_CAPTURE_ID, "nullable-identity"
    )

    with pytest.raises(IncompatibleSchemaError, match="capture_id"):
        apply_migrations(database_path)


def test_a_rejected_nullable_identity_adoption_fabricates_no_history(
    tmp_path: Path,
) -> None:
    """The refused database keeps no trace of the attempt."""
    database_path = _unversioned_v3(
        tmp_path, _NULLABLE_CAPTURE_ID, "nullable-no-history"
    )

    with pytest.raises(IncompatibleSchemaError):
        apply_migrations(database_path)

    assert "schema_migrations" not in _tables(database_path)
    assert read_schema_version(database_path) is None
    assert len(list_observations(database_path)) == 1
