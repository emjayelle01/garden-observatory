"""Tests for scheduled retention execution (Task 14.5).

The service side: the cross-process retention lock beside the database, the
backup lock the run *holds* for its duration (Task 14.5A; the interleaving
proofs live in ``test_retention_mutual_exclusion.py``), and the structured
outcomes a run reports when it correctly declines to start. The command side:
``scheduled-run`` and how it reports skips, executions and refusals.

Every test operates on a temporary database and capture root under
``tmp_path``. Nothing here references a production path, and the destructive
path is exercised only against files the test itself created.
"""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

import mgo.retention.cli as cli
from mgo.core.config import RetentionConfig
from mgo.core.database import apply_migrations, database_connection
from mgo.operations.backup import LOCK_FILENAME as BACKUP_LOCK_FILENAME
from mgo.operations.locking import OperationLock, lock_age_seconds
from mgo.retention.models import (
    RetentionErrorCategory,
    RetentionRuntimeState,
    RetentionState,
    safe_error_message,
)
from mgo.retention.repository import RetentionRepository
from mgo.retention.service import RETENTION_LOCK_FILENAME, RetentionService

NOW = datetime(2026, 9, 7, 4, 5, tzinfo=UTC)
PAYLOAD = b"jpeg-bytes-stand-in"


def _config(*, enabled: bool = True, max_age_days: int | None = 30) -> RetentionConfig:
    return RetentionConfig(
        enabled=enabled,
        max_age_days=max_age_days,
        max_managed_bytes=None,
        minimum_keep_count=1,
        max_deletions_per_run=25,
    )


class _Harness:
    def __init__(
        self,
        tmp_path: Path,
        *,
        config: RetentionConfig | None = None,
        backup_directory: Path | None = None,
    ) -> None:
        self.root = tmp_path / "captures"
        self.root.mkdir()
        self.database_directory = tmp_path / "db"
        self.database_directory.mkdir()
        self.database_path = self.database_directory / "mgo.db"
        apply_migrations(self.database_path)
        self.backup_directory = (
            backup_directory if backup_directory is not None else tmp_path / "backups"
        )
        self.backup_directory.mkdir(exist_ok=True)
        self.config = config or _config()
        self.state = RetentionRuntimeState(enabled=self.config.enabled)
        self.service = RetentionService(
            self.config,
            RetentionRepository(self.database_path, clock=lambda: NOW),
            self.state,
            self.root,
            clock=lambda: NOW,
            database_path=self.database_path,
            backup_lock_path=self.backup_directory / BACKUP_LOCK_FILENAME,
        )

    @property
    def lock_path(self) -> Path:
        return self.database_directory / RETENTION_LOCK_FILENAME

    def add_managed(self, identifier: str, *, days_old: float = 900.0) -> Path:
        media = self.root / f"{identifier}.jpg"
        media.write_bytes(PAYLOAD)
        stamp = (NOW - timedelta(days=days_old)).isoformat()
        with database_connection(self.database_path) as connection:
            connection.execute(
                """
                INSERT INTO captures (
                    id, filename, absolute_path, captured_at_utc, width, height,
                    filesize_bytes, camera_backend, created_at_utc, extra_metadata
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    identifier,
                    media.name,
                    str(media),
                    stamp,
                    4608,
                    2592,
                    len(PAYLOAD),
                    "simulator",
                    stamp,
                    json.dumps({"origin": "motion"}),
                ),
            )
        return media

    def hold_backup_lock(self, *, age_seconds: float = 0.0) -> Path:
        path = self.backup_directory / BACKUP_LOCK_FILENAME
        path.write_text(json.dumps({"token": "x", "pid": 1, "operation": "backup"}))
        if age_seconds:
            stamp = time.time() - age_seconds
            os.utime(path, (stamp, stamp))
        return path


# --- the retention lock ---------------------------------------------------------


def test_the_lock_lives_beside_the_database_by_default(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    harness.add_managed("old")
    harness.add_managed("recent", days_old=1)  # the preservation floor keeps one

    result = harness.service.run_once()

    assert result.executed is True
    assert result.deleted_count == 1
    # Released after the run: nothing is left behind.
    assert not harness.lock_path.exists()


def test_a_held_lock_declines_the_run_without_touching_anything(
    tmp_path: Path,
) -> None:
    harness = _Harness(tmp_path)
    media = harness.add_managed("old")
    other = OperationLock(harness.lock_path, operation="retention")
    other.acquire()
    try:
        result = harness.service.run_once()
    finally:
        other.release()

    assert result.executed is False
    assert result.error_category is RetentionErrorCategory.LOCK_UNAVAILABLE
    assert result.deleted_count == 0
    assert media.exists()
    assert harness.state.snapshot().state is RetentionState.IDLE
    assert harness.state.snapshot().total_runs == 0


def test_the_lock_is_released_even_when_the_run_fails(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    # A capture root that is not a directory makes the run stop immediately.
    harness.service._capture_directory = tmp_path / "missing"

    result = harness.service.run_once()

    assert result.executed is True
    assert result.error_category is RetentionErrorCategory.UNSAFE_PATH
    assert not harness.lock_path.exists()


def test_a_stale_lock_is_reclaimed(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    harness.add_managed("old")
    harness.add_managed("recent", days_old=1)
    harness.lock_path.write_text(json.dumps({"token": "abandoned"}))
    stamp = time.time() - 7 * 60 * 60
    os.utime(harness.lock_path, (stamp, stamp))

    result = harness.service.run_once()

    assert result.executed is True
    assert result.deleted_count == 1


def test_a_service_without_a_lock_location_refuses_to_run(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    harness.add_managed("old")
    service = RetentionService(
        harness.config,
        RetentionRepository(harness.database_path, clock=lambda: NOW),
        RetentionRuntimeState(enabled=True),
        harness.root,
        clock=lambda: NOW,
    )

    result = service.run_once()

    assert result.executed is False
    assert result.error_category is RetentionErrorCategory.LOCK_UNAVAILABLE
    assert (harness.root / "old.jpg").exists()


def test_an_unwritable_lock_location_refuses_to_run(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    harness.add_managed("old")
    blocker = tmp_path / "blocker"
    blocker.write_bytes(b"a file where a directory should be")
    service = RetentionService(
        harness.config,
        RetentionRepository(harness.database_path, clock=lambda: NOW),
        RetentionRuntimeState(enabled=True),
        harness.root,
        clock=lambda: NOW,
        database_path=harness.database_path,
        lock_path=blocker / "lock",
    )

    result = service.run_once()

    assert result.executed is False
    assert result.error_category is RetentionErrorCategory.LOCK_UNAVAILABLE
    assert (harness.root / "old.jpg").exists()


# --- backup exclusion ---------------------------------------------------------------


def test_a_fresh_backup_lock_skips_the_run(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    media = harness.add_managed("old")
    harness.hold_backup_lock()

    result = harness.service.run_once()

    assert result.executed is False
    assert result.error_category is RetentionErrorCategory.BACKUP_IN_PROGRESS
    assert result.error_message == safe_error_message(
        RetentionErrorCategory.BACKUP_IN_PROGRESS
    )
    assert media.exists()
    assert not harness.lock_path.exists()
    assert harness.state.snapshot().total_runs == 0


def test_a_stale_backup_lock_does_not_block(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    harness.add_managed("old")
    harness.add_managed("recent", days_old=1)
    harness.hold_backup_lock(age_seconds=7 * 60 * 60)

    result = harness.service.run_once()

    assert result.executed is True
    assert result.deleted_count == 1


def test_no_backup_lock_location_means_no_run(tmp_path: Path) -> None:
    """Task 14.5A: a service with nothing to exclude against declines."""
    harness = _Harness(tmp_path)
    media = harness.add_managed("old")
    service = RetentionService(
        harness.config,
        RetentionRepository(harness.database_path, clock=lambda: NOW),
        RetentionRuntimeState(enabled=True),
        harness.root,
        clock=lambda: NOW,
        database_path=harness.database_path,
    )

    result = service.run_once()

    assert result.executed is False
    assert result.error_category is RetentionErrorCategory.LOCK_UNAVAILABLE
    assert media.exists()


def test_a_backup_lock_whose_age_cannot_be_read_blocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _Harness(tmp_path)
    harness.add_managed("old")
    harness.hold_backup_lock()
    monkeypatch.setattr("mgo.operations.locking._age_seconds", lambda path: None)

    result = harness.service.run_once()

    assert result.error_category is RetentionErrorCategory.BACKUP_IN_PROGRESS


def test_the_run_holds_the_backup_lock_for_its_whole_duration(tmp_path: Path) -> None:
    """Task 14.5A: not a check, a hold. While the run executes, the backup's
    lock file exists and names retention; a backup starting at any instant
    inside the run finds it taken. Both locks are gone afterwards."""
    harness = _Harness(tmp_path)
    media = harness.add_managed("old")
    harness.add_managed("recent", days_old=1)  # the preservation floor keeps one
    seen: list[dict[str, Any]] = []
    original = harness.service._execute

    def spying_execute() -> Any:
        seen.append(
            json.loads(
                (harness.backup_directory / BACKUP_LOCK_FILENAME).read_text(
                    encoding="utf-8"
                )
            )
        )
        return original()

    harness.service._execute = spying_execute  # type: ignore[method-assign]

    result = harness.service.run_once()

    assert result.executed is True
    assert not media.exists()
    assert len(seen) == 1
    assert seen[0]["operation"] == "retention"
    assert not (harness.backup_directory / BACKUP_LOCK_FILENAME).exists()
    assert not harness.lock_path.exists()


def test_lock_age_is_readable_without_acquiring(tmp_path: Path) -> None:
    path = tmp_path / "lock"
    assert lock_age_seconds(path) is None
    path.write_text("{}")
    age = lock_age_seconds(path)
    assert age is not None
    assert 0 <= age < 60


def test_disabled_retention_never_touches_the_locks(tmp_path: Path) -> None:
    harness = _Harness(tmp_path, config=_config(enabled=False))
    harness.add_managed("old")
    harness.hold_backup_lock()

    result = harness.service.run_once()

    assert result.executed is False
    assert result.enabled is False
    assert result.error_category is None
    assert not harness.lock_path.exists()


# --- the scheduled-run command ------------------------------------------------


def _write_configuration(
    tmp_path: Path, harness: _Harness, *, retention: str
) -> Path:
    path = tmp_path / "mgo.toml"
    path.write_text(
        f"""
[application]
name = "MGO"
environment = "test"
host = "127.0.0.1"
port = 8080

[storage]
data_directory = "{tmp_path.as_posix()}"
log_directory = "{(tmp_path / 'logs').as_posix()}"
database_path = "{harness.database_path.as_posix()}"

[camera]
enabled = false
backend = "simulator"
detection_interval_seconds = 60
capture_directory = "{harness.root.as_posix()}"

[health]
enabled = true
collection_interval_seconds = 60
temperature_warning_celsius = 70.0
temperature_critical_celsius = 80.0
disk_warning_percent = 80.0
disk_critical_percent = 90.0
memory_warning_percent = 85.0
memory_critical_percent = 95.0

[retention]
{retention}
""",
        encoding="utf-8",
    )
    return path


def _run(
    argv: list[str], *, environment: dict[str, str] | None = None
) -> tuple[int, dict[str, Any] | None, str]:
    import io

    out, err = io.StringIO(), io.StringIO()
    previous = {key: os.environ.get(key) for key in (environment or {})}
    try:
        for key, value in (environment or {}).items():
            os.environ[key] = value
        code = cli.main(argv, stdout=out, stderr=err)
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    text = out.getvalue()
    payload = json.loads(text) if text.strip() else None
    return code, payload, err.getvalue()


def test_scheduled_run_requires_the_execute_flag(tmp_path: Path) -> None:
    code, payload, err = _run(["scheduled-run"])

    assert code == cli.EXIT_REFUSED
    assert payload is None
    assert err.strip() == cli.REFUSAL_MISSING_EXECUTE_SCHEDULED


def test_scheduled_run_requires_an_explicit_configuration(tmp_path: Path) -> None:
    code, _, err = _run(["scheduled-run", "--execute"], environment={})
    # With no MGO_CONFIG_PATH the explicit-configuration gate refuses.
    if os.environ.get(cli.CONFIG_PATH_ENV) is None:
        assert code == cli.EXIT_REFUSED
        assert err.strip() == cli.REFUSAL_NO_EXPLICIT_CONFIG


def test_scheduled_run_refuses_a_relative_backup_directory(tmp_path: Path) -> None:
    code, _, err = _run(
        ["scheduled-run", "--execute", "--backup-directory", "backups"],
    )

    assert code == cli.EXIT_REFUSED
    assert err.strip() == cli.REFUSAL_RELATIVE_BACKUP_DIRECTORY


def test_scheduled_run_skips_when_retention_is_disabled(tmp_path: Path) -> None:
    """The production configuration today: a nightly run that deletes nothing."""
    harness = _Harness(tmp_path, config=_config(enabled=False))
    media = harness.add_managed("old")
    config = _write_configuration(tmp_path, harness, retention="enabled = false")

    code, payload, err = _run(
        ["scheduled-run", "--execute"],
        environment={cli.CONFIG_PATH_ENV: str(config)},
    )

    assert code == cli.EXIT_SUCCESS
    assert err == ""
    assert payload == {
        "outcome": "skipped",
        "reason": "retention_disabled",
        "executed": False,
        "enabled": False,
        "deleted_count": 0,
        "bytes_reclaimed": 0,
    }
    assert media.exists()


def test_scheduled_run_skips_while_a_backup_runs(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    media = harness.add_managed("old")
    harness.hold_backup_lock()
    config = _write_configuration(
        tmp_path, harness, retention="enabled = true\nmax_age_days = 30"
    )

    code, payload, _ = _run(
        [
            "scheduled-run",
            "--execute",
            "--backup-directory",
            str(harness.backup_directory),
        ],
        environment={cli.CONFIG_PATH_ENV: str(config)},
    )

    assert code == cli.EXIT_SUCCESS
    assert payload is not None
    assert payload["outcome"] == "skipped"
    assert payload["reason"] == "backup_in_progress"
    assert payload["deleted_count"] == 0
    assert media.exists()


def test_scheduled_run_skips_when_another_run_holds_the_lock(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    media = harness.add_managed("old")
    config = _write_configuration(
        tmp_path, harness, retention="enabled = true\nmax_age_days = 30"
    )
    other = OperationLock(harness.lock_path, operation="retention")
    other.acquire()
    try:
        code, payload, _ = _run(
            [
                "scheduled-run",
                "--execute",
                "--backup-directory",
                str(harness.backup_directory),
            ],
            environment={cli.CONFIG_PATH_ENV: str(config)},
        )
    finally:
        other.release()

    assert code == cli.EXIT_SUCCESS
    assert payload is not None
    assert payload["outcome"] == "skipped"
    assert payload["reason"] == "lock_unavailable"
    assert media.exists()


def test_scheduled_run_executes_the_configured_policy(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    old = harness.add_managed("old", days_old=400)
    recent = harness.add_managed("recent", days_old=1)
    config = _write_configuration(
        tmp_path,
        harness,
        retention="enabled = true\nmax_age_days = 30\nminimum_keep_count = 1",
    )

    code, payload, _ = _run(
        [
            "scheduled-run",
            "--execute",
            "--backup-directory",
            str(harness.backup_directory),
        ],
        environment={cli.CONFIG_PATH_ENV: str(config)},
    )

    assert code == cli.EXIT_SUCCESS
    assert payload is not None
    assert payload["outcome"] == "executed"
    assert payload["deleted_count"] == 1
    assert not old.exists()
    assert recent.exists()


def test_scheduled_run_repeats_cleanly(tmp_path: Path) -> None:
    """A second invocation after a clean run is a normal run with nothing to do."""
    harness = _Harness(tmp_path)
    harness.add_managed("old", days_old=400)
    harness.add_managed("recent", days_old=1)
    config = _write_configuration(
        tmp_path,
        harness,
        retention="enabled = true\nmax_age_days = 30\nminimum_keep_count = 1",
    )
    argv = [
        "scheduled-run",
        "--execute",
        "--backup-directory",
        str(harness.backup_directory),
    ]
    environment = {cli.CONFIG_PATH_ENV: str(config)}

    first = _run(argv, environment=environment)
    second = _run(argv, environment=environment)

    assert first[0] == cli.EXIT_SUCCESS and first[1]["deleted_count"] == 1  # type: ignore[index]
    assert second[0] == cli.EXIT_SUCCESS and second[1]["deleted_count"] == 0  # type: ignore[index]
    assert not harness.lock_path.exists()


def test_scheduled_run_reports_a_safety_refusal_as_an_error(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    harness.add_managed("old", days_old=400)
    config = _write_configuration(
        tmp_path,
        harness,
        retention="enabled = true\nmax_age_days = 30\nminimum_keep_count = 1",
    )
    # Remove the capture root so the run stops with UNSAFE_PATH.
    for child in harness.root.iterdir():
        child.unlink()
    harness.root.rmdir()

    code, payload, _ = _run(
        [
            "scheduled-run",
            "--execute",
            "--backup-directory",
            str(harness.backup_directory),
        ],
        environment={cli.CONFIG_PATH_ENV: str(config)},
    )

    assert code == cli.EXIT_RETENTION_ERROR
    assert payload is not None
    assert payload["outcome"] == "executed"
    assert payload["error_category"] == "unsafe_path"


def test_run_once_also_yields_to_a_backup(tmp_path: Path) -> None:
    """The manual command shares the exclusion; it just reports it differently."""
    harness = _Harness(tmp_path)
    media = harness.add_managed("old")
    config = _write_configuration(
        tmp_path, harness, retention="enabled = true\nmax_age_days = 30"
    )
    harness.hold_backup_lock()

    code, payload, _ = _run(
        [
            "run-once",
            "--execute",
            "--backup-directory",
            str(harness.backup_directory),
        ],
        environment={cli.CONFIG_PATH_ENV: str(config)},
    )

    assert code == cli.EXIT_RETENTION_ERROR
    assert payload is not None
    assert payload["executed"] is False
    assert payload["error_category"] == "backup_in_progress"
    assert media.exists()
