"""Backup/retention mutual exclusion (Task 14.5A, brief §9-§10).

The invariant:

    A destructive retention execution and a database backup cannot both
    enter their critical sections, regardless of start order.

The mechanism is one atomic primitive -- the backup's own ``O_CREAT|O_EXCL``
lock file -- which a retention run now *holds* for its whole duration instead
of reading its age. These tests take each row of the brief's interleaving
table and prove it: the backup starting first, retention starting first, the
backup starting *inside* retention's critical section, both from separate
processes, a process killed while holding either lock, a stale lock, and a
lock whose metadata cannot be read. The backup command itself is run against
a temporary database to prove the refusal from its side.

Every path is under ``tmp_path``. Nothing here names a production location.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import mgo
import mgo.operations.backup_cli as backup_cli
import mgo.retention.cli as retention_cli
from mgo.api.app import retention_status
from mgo.core.config import RetentionConfig
from mgo.core.database import apply_migrations, database_connection
from mgo.operations.backup import LOCK_FILENAME as BACKUP_LOCK_FILENAME
from mgo.operations.errors import ErrorCode, OperationError
from mgo.operations.locking import OperationLock
from mgo.retention.models import RetentionErrorCategory, RetentionRuntimeState
from mgo.retention.repository import RetentionRepository
from mgo.retention.service import RETENTION_LOCK_FILENAME, RetentionService

NOW = datetime(2026, 9, 7, 4, 5, tzinfo=UTC)
PAYLOAD = b"jpeg-bytes-stand-in"
SRC = Path(mgo.__file__).resolve().parents[1]


def _config(*, enabled: bool = True) -> RetentionConfig:
    return RetentionConfig(
        enabled=enabled,
        max_age_days=30,
        max_managed_bytes=None,
        minimum_keep_count=0,
        max_deletions_per_run=25,
    )


class _Harness:
    def __init__(
        self,
        tmp_path: Path,
        *,
        backup_lock_path: Path | None | str = "default",
        stale_after: float | None = None,
        repository: RetentionRepository | None = None,
    ) -> None:
        self.root = tmp_path / "captures"
        self.root.mkdir(exist_ok=True)
        self.database_directory = tmp_path / "db"
        self.database_directory.mkdir(exist_ok=True)
        self.database_path = self.database_directory / "mgo.db"
        if not self.database_path.exists():
            apply_migrations(self.database_path)
        self.backup_directory = tmp_path / "backups"
        self.backup_directory.mkdir(exist_ok=True)
        self.backup_lock_path = self.backup_directory / BACKUP_LOCK_FILENAME
        self.state = RetentionRuntimeState(enabled=True)
        extra: dict[str, Any] = {}
        if stale_after is not None:
            extra["stale_lock_after_seconds"] = stale_after
        self.service = RetentionService(
            _config(),
            repository or RetentionRepository(self.database_path, clock=lambda: NOW),
            self.state,
            self.root,
            clock=lambda: NOW,
            database_path=self.database_path,
            backup_lock_path=(
                self.backup_lock_path
                if backup_lock_path == "default"
                else backup_lock_path
            ),
            **extra,
        )

    @property
    def retention_lock_path(self) -> Path:
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


def _write_configuration(tmp_path: Path, harness: _Harness) -> Path:
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
log_directory = "{(tmp_path / "logs").as_posix()}"
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
enabled = true
max_age_days = 30
minimum_keep_count = 1
""",
        encoding="utf-8",
    )
    return path


# --- in-process interleavings --------------------------------------------------------


def test_a_running_backup_excludes_retention(tmp_path: Path) -> None:
    """Row 4: retention starts while the backup holds its lock."""
    harness = _Harness(tmp_path)
    media = harness.add_managed("old")
    backup = OperationLock(harness.backup_lock_path, operation="backup")
    backup.acquire()
    try:
        result = harness.service.run_once()
    finally:
        backup.release()

    assert result.executed is False
    assert result.error_category is RetentionErrorCategory.BACKUP_IN_PROGRESS
    assert media.exists()
    assert not harness.retention_lock_path.exists()
    assert harness.backup_lock_path.exists() is False  # the backup's own release


def test_a_backup_cannot_start_while_retention_runs(tmp_path: Path) -> None:
    """Rows 2, 3 and 5: whenever the backup tries to start during the run --
    including the instant after any check retention could have made -- its
    acquisition fails, because retention holds the very file it needs."""
    outcomes: list[str] = []

    class _Repository(RetentionRepository):
        def list_lifecycle_records(self) -> Any:
            # Retention is inside its critical section right now.
            backup = OperationLock(
                tmp_path / "backups" / BACKUP_LOCK_FILENAME, operation="backup"
            )
            try:
                backup.acquire()
            except OperationError as error:
                outcomes.append(f"refused:{error.code.value}:{error.message}")
            else:
                outcomes.append("acquired")
                backup.release()
            return super().list_lifecycle_records()

    harness = _Harness(
        tmp_path,
        repository=_Repository(tmp_path / "db" / "mgo.db", clock=lambda: NOW),
    )
    media = harness.add_managed("old")

    result = harness.service.run_once()

    assert result.executed is True
    assert result.deleted_count == 1
    assert not media.exists()
    assert len(outcomes) == 1
    assert outcomes[0].startswith(f"refused:{ErrorCode.BACKUP_LOCKED.value}:")
    assert "retention" in outcomes[0]  # the refusal names the holder
    # Both locks are gone once the run ends, so the backup may proceed.
    assert not harness.backup_lock_path.exists()
    assert not harness.retention_lock_path.exists()
    OperationLock(harness.backup_lock_path, operation="backup").acquire()


def test_the_backup_command_refuses_while_retention_holds_the_lock(
    tmp_path: Path,
) -> None:
    """The backup's side of the protocol, through its real command."""
    harness = _Harness(tmp_path)
    harness.add_managed("old")
    config = _write_configuration(tmp_path, harness)
    holder = OperationLock(harness.backup_lock_path, operation="retention")
    holder.acquire()
    out, err = io.StringIO(), io.StringIO()
    try:
        code = backup_cli.main(
            [
                "backup",
                "--config",
                str(config),
                "--database",
                str(harness.database_path),
                "--output-directory",
                str(harness.backup_directory),
                "--keep",
                "2",
            ],
            stdout=out,
            stderr=err,
        )
    finally:
        holder.release()

    assert code == backup_cli.EXIT_FAILURE
    summary = json.loads(out.getvalue())
    assert summary["result"] == "failed"
    assert summary["error_code"] == ErrorCode.BACKUP_LOCKED.value
    assert "retention" in summary["detail"]
    # Nothing was published: the directory holds no recovery set.
    assert [p.name for p in harness.backup_directory.iterdir()] == []


def test_a_stale_backup_lock_is_reclaimed_by_retention(tmp_path: Path) -> None:
    """Row 6: a lock older than the threshold is an abandoned job."""
    harness = _Harness(tmp_path)
    harness.add_managed("old")
    harness.backup_lock_path.write_text(
        json.dumps({"token": "x", "operation": "backup"})
    )
    stamp = time.time() - 7 * 60 * 60
    os.utime(harness.backup_lock_path, (stamp, stamp))

    result = harness.service.run_once()

    assert result.executed is True
    assert result.deleted_count == 1
    assert not harness.backup_lock_path.exists()


def test_a_backup_lock_with_unreadable_metadata_still_excludes(tmp_path: Path) -> None:
    """Row 7: exclusion is the file's existence, never its content."""
    harness = _Harness(tmp_path)
    media = harness.add_managed("old")
    harness.backup_lock_path.write_bytes(b"\x00\xff not json")

    result = harness.service.run_once()

    assert result.error_category is RetentionErrorCategory.BACKUP_IN_PROGRESS
    assert media.exists()
    assert harness.backup_lock_path.read_bytes() == b"\x00\xff not json"


def test_a_backup_lock_whose_age_cannot_be_read_excludes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _Harness(tmp_path)
    media = harness.add_managed("old")
    harness.backup_lock_path.write_text("{}")
    monkeypatch.setattr("mgo.operations.locking._age_seconds", lambda path: None)

    result = harness.service.run_once()

    assert result.error_category is RetentionErrorCategory.BACKUP_IN_PROGRESS
    assert media.exists()


def test_a_service_without_a_backup_lock_location_refuses_to_run(
    tmp_path: Path,
) -> None:
    harness = _Harness(tmp_path, backup_lock_path=None)
    media = harness.add_managed("old")

    result = harness.service.run_once()

    assert result.executed is False
    assert result.error_category is RetentionErrorCategory.LOCK_UNAVAILABLE
    assert media.exists()
    assert not harness.retention_lock_path.exists()


def test_an_absent_backup_directory_refuses_to_run(tmp_path: Path) -> None:
    """Retention never creates the backup directory; with none, it declines."""
    harness = _Harness(
        tmp_path, backup_lock_path=tmp_path / "nowhere" / BACKUP_LOCK_FILENAME
    )
    media = harness.add_managed("old")

    result = harness.service.run_once()

    assert result.executed is False
    assert result.error_category is RetentionErrorCategory.LOCK_UNAVAILABLE
    assert media.exists()
    assert not (tmp_path / "nowhere").exists()


def test_both_locks_are_released_after_a_run_that_fails(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    harness.service._capture_directory = tmp_path / "missing"

    result = harness.service.run_once()

    assert result.error_category is RetentionErrorCategory.UNSAFE_PATH
    assert not harness.backup_lock_path.exists()
    assert not harness.retention_lock_path.exists()


def test_the_locks_are_taken_retention_first_and_released_backup_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    order: list[str] = []
    original_acquire = OperationLock.acquire
    original_release = OperationLock.release

    def acquire(self: OperationLock) -> Any:
        order.append(f"acquire:{self.path.name}")
        return original_acquire(self)

    def release(self: OperationLock) -> None:
        order.append(f"release:{self.path.name}")
        original_release(self)

    monkeypatch.setattr(OperationLock, "acquire", acquire)
    monkeypatch.setattr(OperationLock, "release", release)
    harness = _Harness(tmp_path)

    harness.service.run_once()

    assert order == [
        f"acquire:{RETENTION_LOCK_FILENAME}",
        f"acquire:{BACKUP_LOCK_FILENAME}",
        f"release:{BACKUP_LOCK_FILENAME}",
        f"release:{RETENTION_LOCK_FILENAME}",
    ]


# --- separate processes ---------------------------------------------------------------


_HOLDER = """
import pathlib, sys, time
from mgo.operations.locking import OperationLock
lock = OperationLock(pathlib.Path(sys.argv[1]), operation=sys.argv[2])
lock.acquire()
pathlib.Path(sys.argv[3]).write_text("ready")
go = pathlib.Path(sys.argv[4])
for _ in range(6000):
    if go.exists():
        break
    time.sleep(0.01)
lock.release()
"""


def _hold_in_another_process(
    tmp_path: Path, lock_path: Path, operation: str
) -> tuple[subprocess.Popen[bytes], Path]:
    ready = tmp_path / f"ready-{operation}"
    go = tmp_path / f"go-{operation}"
    process = subprocess.Popen(
        [sys.executable, "-c", _HOLDER, str(lock_path), operation, str(ready), str(go)],
        env={**os.environ, "PYTHONPATH": str(SRC)},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 30
    while not ready.exists():
        if process.poll() is not None:
            raise AssertionError("the holder process exited before taking the lock")
        assert time.monotonic() < deadline, "the holder process never became ready"
        time.sleep(0.02)
    return process, go


def test_another_process_holding_the_backup_lock_excludes_retention_until_it_exits(
    tmp_path: Path,
) -> None:
    """Rows 1 and 5 across a real process boundary."""
    harness = _Harness(tmp_path)
    media = harness.add_managed("old")
    process, go = _hold_in_another_process(tmp_path, harness.backup_lock_path, "backup")
    try:
        while_held = harness.service.run_once()
    finally:
        go.write_text("go")
        process.wait(timeout=30)

    after = harness.service.run_once()

    assert while_held.error_category is RetentionErrorCategory.BACKUP_IN_PROGRESS
    assert after.executed is True and after.deleted_count == 1
    assert not media.exists()


def test_another_retention_process_holding_the_retention_lock_excludes_this_one(
    tmp_path: Path,
) -> None:
    harness = _Harness(tmp_path)
    media = harness.add_managed("old")
    process, go = _hold_in_another_process(
        tmp_path, harness.retention_lock_path, "retention"
    )
    try:
        result = harness.service.run_once()
    finally:
        go.write_text("go")
        process.wait(timeout=30)

    assert result.error_category is RetentionErrorCategory.LOCK_UNAVAILABLE
    assert media.exists()


@pytest.mark.parametrize("which", ["backup", "retention"])
def test_a_process_killed_while_holding_a_lock_is_respected_then_reclaimed(
    tmp_path: Path, which: str
) -> None:
    """Row 9: death while holding either lock. The lock file survives the
    process; a fresh one excludes the next run, and once it is older than
    the stale threshold the next run reclaims it and proceeds."""
    harness = _Harness(tmp_path)
    media = harness.add_managed("old")
    lock_path = (
        harness.backup_lock_path if which == "backup" else harness.retention_lock_path
    )
    process, _ = _hold_in_another_process(tmp_path, lock_path, which)
    process.kill()
    process.wait(timeout=30)
    assert lock_path.exists()

    fresh = harness.service.run_once()

    assert fresh.executed is False
    assert fresh.error_category is (
        RetentionErrorCategory.BACKUP_IN_PROGRESS
        if which == "backup"
        else RetentionErrorCategory.LOCK_UNAVAILABLE
    )
    assert media.exists()

    # The same host, later: the lock is now older than the threshold.
    stamp = time.time() - 7 * 60 * 60
    os.utime(lock_path, (stamp, stamp))
    reclaimed = _Harness(tmp_path).service.run_once()

    assert reclaimed.executed is True
    assert reclaimed.deleted_count == 1
    assert not lock_path.exists()


# --- the commands ---------------------------------------------------------------------


def _run_cli(argv: list[str], config: Path) -> tuple[int, dict[str, Any] | None]:
    out, err = io.StringIO(), io.StringIO()
    previous = os.environ.get(retention_cli.CONFIG_PATH_ENV)
    os.environ[retention_cli.CONFIG_PATH_ENV] = str(config)
    try:
        code = retention_cli.main(argv, stdout=out, stderr=err)
    finally:
        if previous is None:
            os.environ.pop(retention_cli.CONFIG_PATH_ENV, None)
        else:
            os.environ[retention_cli.CONFIG_PATH_ENV] = previous
    text = out.getvalue()
    return code, (json.loads(text) if text.strip() else None)


def test_run_once_takes_the_backup_lock_it_is_pointed_at(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    media = harness.add_managed("old")
    harness.add_managed("recent", days_old=1)  # the preservation floor keeps one
    config = _write_configuration(tmp_path, harness)
    backup = OperationLock(harness.backup_lock_path, operation="backup")
    backup.acquire()
    try:
        code, payload = _run_cli(
            [
                "run-once",
                "--execute",
                "--backup-directory",
                str(harness.backup_directory),
            ],
            config,
        )
    finally:
        backup.release()

    assert code == retention_cli.EXIT_RETENTION_ERROR
    assert payload is not None
    assert payload["executed"] is False
    assert payload["error_category"] == "backup_in_progress"
    assert media.exists()

    code, payload = _run_cli(
        ["run-once", "--execute", "--backup-directory", str(harness.backup_directory)],
        config,
    )
    assert code == retention_cli.EXIT_SUCCESS
    assert payload is not None and payload["deleted_count"] == 1


def test_run_once_refuses_a_relative_backup_directory(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    config = _write_configuration(tmp_path, harness)
    out, err = io.StringIO(), io.StringIO()
    code = retention_cli.main(
        ["run-once", "--execute", "--backup-directory", "backups"],
        stdout=out,
        stderr=err,
    )
    assert code == retention_cli.EXIT_REFUSED
    assert err.getvalue().strip() == retention_cli.REFUSAL_RELATIVE_BACKUP_DIRECTORY
    assert config.exists()


def test_run_once_without_a_backup_directory_never_deletes_without_the_canonical_one(
    tmp_path: Path,
) -> None:
    """With the canonical backup directory absent on this host the run declines;
    with it present (the Pi) the run holds the real backup lock. Either way it
    is coordinated, never unguarded."""
    harness = _Harness(tmp_path)
    media = harness.add_managed("old")
    config = _write_configuration(tmp_path, harness)

    code, payload = _run_cli(["run-once", "--execute"], config)

    assert payload is not None
    if Path("/var/backups/garden-observatory").is_dir():
        pytest.skip("the canonical backup directory exists on this host")
    assert code == retention_cli.EXIT_RETENTION_ERROR
    assert payload["error_category"] == "lock_unavailable"
    assert media.exists()


def test_scheduled_run_reports_a_held_backup_lock_as_a_skip(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    media = harness.add_managed("old")
    config = _write_configuration(tmp_path, harness)
    backup = OperationLock(harness.backup_lock_path, operation="backup")
    backup.acquire()
    try:
        code, payload = _run_cli(
            [
                "scheduled-run",
                "--execute",
                "--backup-directory",
                str(harness.backup_directory),
            ],
            config,
        )
    finally:
        backup.release()

    assert code == retention_cli.EXIT_SUCCESS
    assert payload == {
        "outcome": "skipped",
        "reason": "backup_in_progress",
        "executed": False,
        "enabled": True,
        "deleted_count": 0,
        "bytes_reclaimed": 0,
    }
    assert media.exists()


# --- reporting: idle, busy, unknown -------------------------------------------------


def test_the_lock_state_is_idle_busy_or_unknown(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    assert harness.service.cross_process_lock_state() == "idle"

    holder = OperationLock(harness.retention_lock_path, operation="retention")
    holder.acquire()
    try:
        assert harness.service.cross_process_lock_state() == "busy"
    finally:
        holder.release()
    assert harness.service.cross_process_lock_state() == "idle"

    harness.retention_lock_path.write_text("{}")
    stamp = time.time() - 7 * 60 * 60
    os.utime(harness.retention_lock_path, (stamp, stamp))
    assert harness.service.cross_process_lock_state() == "idle"  # abandoned

    nowhere = RetentionService(
        _config(),
        RetentionRepository(harness.database_path),
        RetentionRuntimeState(enabled=True),
        harness.root,
    )
    assert nowhere.cross_process_lock_state() == "unknown"


def test_the_lock_state_is_unknown_when_the_age_cannot_be_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _Harness(tmp_path)
    harness.retention_lock_path.write_text("{}")
    monkeypatch.setattr("mgo.retention.service.lock_age_seconds", lambda path: None)

    assert harness.service.cross_process_lock_state() == "unknown"


def test_the_status_endpoint_reports_the_lock_state(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                retention_state=harness.state, retention_service=harness.service
            )
        )
    )

    assert retention_status(request).scheduled_lock_state == "idle"  # type: ignore[arg-type]

    holder = OperationLock(harness.retention_lock_path, operation="retention")
    holder.acquire()
    try:
        assert retention_status(request).scheduled_lock_state == "busy"  # type: ignore[arg-type]
    finally:
        holder.release()


def test_the_status_endpoint_reports_unknown_without_a_service() -> None:
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(retention_state=RetentionRuntimeState(enabled=True))
        )
    )
    assert retention_status(request).scheduled_lock_state == "unknown"  # type: ignore[arg-type]
