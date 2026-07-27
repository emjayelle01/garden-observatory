"""Tests for the read-only database health check and its monitor.

Every case runs against a temporary SQLite database. The health check must be
truthful, bounded, non-mutating and quiet: these tests pin all four.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from mgo.core.config import MGOConfig, StorageConfig, load_config
from mgo.core.database import (
    CURRENT_SCHEMA_VERSION,
    apply_migrations,
    connect_readonly,
    utc_now_iso,
)
from mgo.core.database_health import (
    DatabaseHealth,
    DatabaseHealthState,
    DatabaseStatus,
    MigrationStatus,
    check_database_health,
    unavailable_health,
)
from mgo.core.database_monitor import (
    is_material_change,
    perform_database_check,
    run_database_monitor,
)
from mgo.core.observations import list_observations

_NOW = datetime(2026, 7, 27, 9, 30, tzinfo=UTC)


def _config(database_path: Path) -> MGOConfig:
    """Return the default configuration pointed at an isolated database."""
    base = load_config()
    return replace(
        base,
        storage=StorageConfig(
            data_directory=database_path.parent,
            log_directory=database_path.parent / "logs",
            database_path=database_path,
        ),
    )


def _migrated(tmp_path: Path, name: str = "health.db") -> MGOConfig:
    """Return a configuration whose database is migrated and current."""
    database_path = tmp_path / name
    apply_migrations(database_path)
    return _config(database_path)


class _Recorder:
    """A fake observation recorder capturing every persisted observation."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, database_path: Path, **kwargs: Any) -> None:
        self.calls.append(kwargs)


def _health(status: DatabaseStatus, **overrides: Any) -> DatabaseHealth:
    """Build a health result with the given status for monitor tests."""
    base = {
        "status": status,
        "accessible": status is not DatabaseStatus.UNHEALTHY,
        "database_name": "health.db",
        "schema_version": CURRENT_SCHEMA_VERSION,
        "expected_schema_version": CURRENT_SCHEMA_VERSION,
        "migration_status": MigrationStatus.CURRENT,
        "journal_mode": "wal",
        "foreign_keys_enabled": True,
        "integrity": "ok",
        "detail": f"Database is {status.value}.",
        "checked_at": _NOW,
    }
    return DatabaseHealth(**{**base, **overrides})


# --- a healthy database -----------------------------------------------------


def test_healthy_database_reports_truthful_metadata(tmp_path: Path) -> None:
    """A migrated database reports healthy with accurate, bounded evidence."""
    config = _migrated(tmp_path)

    result = check_database_health(config, now=_NOW)

    assert result.status is DatabaseStatus.HEALTHY
    assert result.severity == "healthy"
    assert result.accessible is True
    assert result.schema_version == CURRENT_SCHEMA_VERSION
    assert result.expected_schema_version == CURRENT_SCHEMA_VERSION
    assert result.migration_status is MigrationStatus.CURRENT
    assert result.journal_mode == "wal"
    assert result.foreign_keys_enabled is True
    assert result.integrity == "ok"
    assert result.checked_at == _NOW


def test_health_result_serialises_without_leaking_anything(
    tmp_path: Path,
) -> None:
    """The serialised result exposes no path, no SQL and no data."""
    config = _migrated(tmp_path)

    payload = check_database_health(config, now=_NOW).as_dict()

    assert payload["database"] == "health.db"
    # The absolute path is deliberately not exposed; only the file name is.
    assert str(tmp_path) not in repr(payload)
    assert "SELECT" not in repr(payload)
    assert "Traceback" not in repr(payload)
    assert set(payload) == {
        "status",
        "accessible",
        "database",
        "schema_version",
        "expected_schema_version",
        "migration_status",
        "journal_mode",
        "foreign_keys",
        "integrity",
        "detail",
        "checked_at",
    }


# --- unusable databases -----------------------------------------------------


def test_missing_database_file_reports_unhealthy(tmp_path: Path) -> None:
    """An absent database is unhealthy -- and is not created by the check."""
    database_path = tmp_path / "absent.db"
    config = _config(database_path)

    result = check_database_health(config, now=_NOW)

    assert result.status is DatabaseStatus.UNHEALTHY
    assert result.severity == "critical"
    assert result.accessible is False
    assert "could not be opened" in result.detail
    assert not database_path.exists()


def test_missing_parent_directory_reports_unhealthy(tmp_path: Path) -> None:
    """A database under a missing directory is unhealthy, not created."""
    database_path = tmp_path / "nowhere" / "mgo.db"
    config = _config(database_path)

    result = check_database_health(config, now=_NOW)

    assert result.status is DatabaseStatus.UNHEALTHY
    assert result.accessible is False
    assert not database_path.parent.exists()


def test_inaccessible_database_reports_unhealthy(tmp_path: Path) -> None:
    """A path that is a directory, not a database file, is unhealthy."""
    database_path = tmp_path / "mgo.db"
    database_path.mkdir()
    config = _config(database_path)

    result = check_database_health(config, now=_NOW)

    assert result.status is DatabaseStatus.UNHEALTHY
    assert result.accessible is False


def test_corrupt_database_is_never_reported_healthy(tmp_path: Path) -> None:
    """A file that is not a SQLite database is unhealthy."""
    database_path = tmp_path / "corrupt.db"
    database_path.write_bytes(b"not a sqlite database at all" * 64)
    config = _config(database_path)

    result = check_database_health(config, now=_NOW)

    assert result.status is DatabaseStatus.UNHEALTHY
    assert result.integrity != "ok"


def test_database_without_migration_history_is_unhealthy(tmp_path: Path) -> None:
    """A real but unmigrated database is unhealthy, not merely degraded."""
    database_path = tmp_path / "unmigrated.db"
    connection = sqlite3.connect(database_path)
    connection.execute("CREATE TABLE something (x INTEGER)")
    connection.commit()
    connection.close()
    config = _config(database_path)

    result = check_database_health(config, now=_NOW)

    assert result.status is DatabaseStatus.UNHEALTHY
    assert result.migration_status is MigrationStatus.UNKNOWN
    assert result.schema_version is None


def test_in_memory_database_is_reported_unhealthy(tmp_path: Path) -> None:
    """An in-memory database cannot be health-checked and says so."""
    config = _config(Path(":memory:"))

    result = check_database_health(config, now=_NOW)

    assert result.status is DatabaseStatus.UNHEALTHY
    assert "in-memory" in result.detail


# --- schema-version mismatch ------------------------------------------------


def test_schema_version_ahead_is_unhealthy(tmp_path: Path) -> None:
    """A database from a newer build fails closed."""
    config = _migrated(tmp_path)
    connection = sqlite3.connect(config.storage.database_path)
    connection.execute(
        "INSERT INTO schema_migrations (version, name, applied_at) "
        "VALUES (?, ?, ?)",
        (CURRENT_SCHEMA_VERSION + 5, "999_future.sql", utc_now_iso()),
    )
    connection.commit()
    connection.close()

    result = check_database_health(config, now=_NOW)

    assert result.status is DatabaseStatus.UNHEALTHY
    assert result.migration_status is MigrationStatus.AHEAD
    assert result.schema_version == CURRENT_SCHEMA_VERSION + 5


def test_schema_version_behind_is_degraded(tmp_path: Path) -> None:
    """A database still awaiting a migration is usable but degraded."""
    config = _migrated(tmp_path)
    connection = sqlite3.connect(config.storage.database_path)
    connection.execute(
        "DELETE FROM schema_migrations WHERE version = ?",
        (CURRENT_SCHEMA_VERSION,),
    )
    connection.commit()
    connection.close()

    result = check_database_health(config, now=_NOW)

    assert result.status is DatabaseStatus.DEGRADED
    assert result.severity == "warning"
    assert result.migration_status is MigrationStatus.PENDING
    assert result.schema_version == CURRENT_SCHEMA_VERSION - 1
    assert "behind" in result.detail


def test_foreign_key_enforcement_failure_is_surfaced(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A build that cannot enforce foreign keys degrades rather than lies."""
    config = _migrated(tmp_path)
    monkeypatch.setattr(
        "mgo.core.database_health.foreign_keys_enabled", lambda connection: False
    )

    result = check_database_health(config, now=_NOW)

    assert result.status is DatabaseStatus.DEGRADED
    assert result.foreign_keys_enabled is False
    assert "foreign-key enforcement is not active" in result.detail


def test_unexpected_journal_mode_is_surfaced(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A file-backed database not in WAL mode degrades rather than lies."""
    config = _migrated(tmp_path)
    monkeypatch.setattr(
        "mgo.core.database_health.journal_mode", lambda connection: "delete"
    )

    result = check_database_health(config, now=_NOW)

    assert result.status is DatabaseStatus.DEGRADED
    assert result.journal_mode == "delete"
    assert "journal mode" in result.detail


# --- bounds and resource hygiene --------------------------------------------


def test_health_detail_is_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """However long an underlying error is, the reported detail stays bounded."""
    config = _migrated(tmp_path)

    def _explode(connection: sqlite3.Connection) -> str:
        raise sqlite3.DatabaseError("x" * 5000)

    monkeypatch.setattr("mgo.core.database_health._quick_check", _explode)

    result = check_database_health(config, now=_NOW)

    assert result.status is DatabaseStatus.UNHEALTHY
    assert len(result.detail) <= 500


def test_health_checker_closes_every_connection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No connection (and no WAL sidecar handle) survives a health poll."""
    config = _migrated(tmp_path)
    opened: list[sqlite3.Connection] = []

    def _tracking_connect(path: Path, **kwargs: Any) -> sqlite3.Connection:
        connection = connect_readonly(path, **kwargs)
        opened.append(connection)
        return connection

    monkeypatch.setattr(
        "mgo.core.database_health.connect_readonly", _tracking_connect
    )

    check_database_health(config, now=_NOW)

    assert len(opened) == 1
    # A closed sqlite3 connection raises on any further use; that is the
    # portable proof it was closed.
    with pytest.raises(sqlite3.ProgrammingError):
        opened[0].execute("SELECT 1")

    # And on Windows an open handle would prevent deletion outright.
    config.storage.database_path.unlink()


def test_repeated_identical_checks_do_not_flood(tmp_path: Path) -> None:
    """Polling a stable database records exactly one observation."""
    config = _migrated(tmp_path)
    state = DatabaseHealthState()
    recorder = _Recorder()

    for _ in range(5):
        perform_database_check(config, state, recorder=recorder)

    assert len(recorder.calls) == 1
    assert recorder.calls[0]["kind"] == "database_health"
    assert recorder.calls[0]["status"] == DatabaseStatus.HEALTHY.value


def test_healthy_poll_persists_a_real_observation(tmp_path: Path) -> None:
    """The observation genuinely lands in the timeline, once."""
    config = _migrated(tmp_path)
    state = DatabaseHealthState()

    perform_database_check(config, state)
    perform_database_check(config, state)

    observations = list_observations(
        config.storage.database_path, kind="database_health"
    )
    assert len(observations) == 1
    assert observations[0].source == "mgo-database"
    assert observations[0].payload["status"] == DatabaseStatus.HEALTHY.value


# --- transitions ------------------------------------------------------------


def test_material_change_ignores_cosmetic_differences() -> None:
    """Only status and migration status count as a material change."""
    healthy = _health(DatabaseStatus.HEALTHY)

    assert is_material_change(None, healthy) is True
    assert (
        is_material_change(
            healthy,
            dataclasses.replace(
                healthy, detail="reworded", checked_at=datetime.now(UTC)
            ),
        )
        is False
    )
    assert (
        is_material_change(healthy, _health(DatabaseStatus.UNHEALTHY)) is True
    )
    assert (
        is_material_change(
            healthy,
            dataclasses.replace(
                healthy, migration_status=MigrationStatus.PENDING
            ),
        )
        is True
    )


def test_unhealthy_transition_is_logged_but_not_persisted(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An unhealthy database is never written to -- that write would fail."""
    config = _migrated(tmp_path)
    state = DatabaseHealthState()
    recorder = _Recorder()

    with caplog.at_level(logging.WARNING, logger="mgo.core.database_monitor"):
        perform_database_check(
            config,
            state,
            checker=lambda _: _health(DatabaseStatus.UNHEALTHY),
            recorder=recorder,
        )

    assert recorder.calls == []
    assert any(
        "unhealthy" in record.getMessage() for record in caplog.records
    )


def test_recovery_from_unhealthy_is_represented_correctly(
    tmp_path: Path,
) -> None:
    """Recovery is persisted once and records the state it recovered from."""
    config = _migrated(tmp_path)
    state = DatabaseHealthState()
    recorder = _Recorder()
    results = [
        _health(DatabaseStatus.UNHEALTHY),
        _health(DatabaseStatus.UNHEALTHY),
        _health(DatabaseStatus.HEALTHY),
        _health(DatabaseStatus.HEALTHY),
    ]

    for result in results:
        perform_database_check(
            config, state, checker=lambda _, r=result: r, recorder=recorder
        )

    assert len(recorder.calls) == 1
    call = recorder.calls[0]
    assert call["status"] == DatabaseStatus.HEALTHY.value
    assert call["payload"]["previous_status"] == DatabaseStatus.UNHEALTHY.value
    assert state.get() is not None
    assert state.get().status is DatabaseStatus.HEALTHY  # type: ignore[union-attr]


def test_degraded_transition_is_persisted(tmp_path: Path) -> None:
    """A degraded database can still accept the observation, so it is written."""
    config = _migrated(tmp_path)
    state = DatabaseHealthState()
    recorder = _Recorder()

    perform_database_check(
        config,
        state,
        checker=lambda _: _health(DatabaseStatus.DEGRADED),
        recorder=recorder,
    )

    assert len(recorder.calls) == 1
    assert recorder.calls[0]["status"] == DatabaseStatus.DEGRADED.value


def test_observation_failure_never_breaks_the_check(tmp_path: Path) -> None:
    """A recorder that raises is isolated; the check still returns a result."""
    config = _migrated(tmp_path)
    state = DatabaseHealthState()

    def _failing(database_path: Path, **kwargs: Any) -> None:
        raise sqlite3.OperationalError("disk I/O error")

    result = perform_database_check(config, state, recorder=_failing)

    assert result.status is DatabaseStatus.HEALTHY
    assert state.get() is result


def test_unevaluated_state_is_reported_safely(tmp_path: Path) -> None:
    """Before any check has run the state is unhealthy, never optimistic."""
    config = _migrated(tmp_path)

    result = unavailable_health(config, now=_NOW)

    assert result.status is DatabaseStatus.UNHEALTHY
    assert result.schema_version is None
    assert "not been evaluated" in result.detail


# --- the background monitor -------------------------------------------------


def test_monitor_runs_an_initial_check_and_stops_cleanly(
    tmp_path: Path,
) -> None:
    """The monitor checks once, then exits promptly when signalled."""
    config = _migrated(tmp_path)
    state = DatabaseHealthState()
    recorder = _Recorder()

    async def _run() -> None:
        stop_event = asyncio.Event()
        task = asyncio.create_task(
            run_database_monitor(
                config, state, stop_event, recorder=recorder
            )
        )
        # Yield until the initial check has landed, with no sleep involved.
        while state.get() is None:
            await asyncio.sleep(0)
        stop_event.set()
        await asyncio.wait_for(task, timeout=5)
        assert task.done()

    asyncio.run(_run())

    assert state.get() is not None
    assert len(recorder.calls) == 1


def test_monitor_can_skip_the_initial_check(tmp_path: Path) -> None:
    """``run_initial=False`` performs no check before the first interval."""
    config = _migrated(tmp_path)
    state = DatabaseHealthState()
    recorder = _Recorder()

    async def _run() -> None:
        stop_event = asyncio.Event()
        stop_event.set()
        await asyncio.wait_for(
            run_database_monitor(
                config,
                state,
                stop_event,
                recorder=recorder,
                run_initial=False,
            ),
            timeout=5,
        )

    asyncio.run(_run())

    assert state.get() is None
    assert recorder.calls == []


def test_monitor_survives_a_failing_checker(tmp_path: Path) -> None:
    """An exception inside one check never terminates the monitor."""
    config = _migrated(tmp_path)
    state = DatabaseHealthState()

    def _boom(_: MGOConfig) -> DatabaseHealth:
        raise RuntimeError("checker exploded")

    async def _run() -> None:
        stop_event = asyncio.Event()
        task = asyncio.create_task(
            run_database_monitor(
                config, state, stop_event, checker=_boom, run_initial=True
            )
        )
        # The initial check runs in a worker thread; let it complete and be
        # swallowed before signalling shutdown.
        await asyncio.sleep(0)
        stop_event.set()
        await asyncio.wait_for(task, timeout=5)
        assert task.done()
        assert task.exception() is None

    asyncio.run(_run())

    # The check raised, so no result was ever stored -- and nothing propagated.
    assert state.get() is None
