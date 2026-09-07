"""Tests that ``dry_run`` is read-only at the SQLite boundary, not merely in intent.

"Read-only" is easy to claim and easy to get wrong. A method that issues only a
``SELECT`` can still create a database file, create a parent directory tree and
switch a database's journal mode, because those are side effects of *opening*
the connection rather than of the statement. Task 14.1 documented ``dry_run`` as
mutating nothing while it read through the ordinary read-write helper, so all
three of those were reachable from a command an operator was told was safe.

These tests assert the boundary from the outside: they look at the filesystem
and at the database's own settings before and after, rather than reading the
implementation and believing it.

Everything runs against temporary directories. No Raspberry Pi, no real capture
directory and no production database is involved.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from mgo.core.config import RetentionConfig
from mgo.core.database import apply_migrations, database_connection
from mgo.core.observations import list_observations
from mgo.operations.backup import LOCK_FILENAME as BACKUP_LOCK_FILENAME
from mgo.retention.models import RetentionRuntimeState
from mgo.retention.repository import (
    RetentionCatalogueError,
    RetentionRepository,
    RetentionRepositoryError,
)
from mgo.retention.service import RetentionService

NOW = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)
PAYLOAD = b"jpeg-bytes-stand-in"


def _config(
    *,
    enabled: bool = False,
    max_age_days: int | None = 7,
    minimum_keep_count: int = 1,
) -> RetentionConfig:
    """Build a retention configuration for a preview test."""
    return RetentionConfig(
        enabled=enabled,
        max_age_days=max_age_days,
        max_managed_bytes=None,
        minimum_keep_count=minimum_keep_count,
        max_deletions_per_run=25,
    )


def _service(
    database_path: Path,
    capture_root: Path,
    *,
    enabled: bool = False,
    state: RetentionRuntimeState | None = None,
) -> RetentionService:
    """Build a retention service pointed at the given database and root.

    The backup lock location is named but never created: nothing in this
    module runs retention, and a read-only preview must not add a directory.
    """
    backup_directory = capture_root.parent / "backups"
    return RetentionService(
        _config(enabled=enabled),
        RetentionRepository(database_path),
        state if state is not None else RetentionRuntimeState(enabled=enabled),
        capture_root,
        clock=lambda: NOW,
        database_path=database_path,
        backup_lock_path=backup_directory / BACKUP_LOCK_FILENAME,
    )


def _add_capture(
    database_path: Path,
    capture_root: Path,
    identifier: str,
    *,
    days_old: float = 900.0,
    origin: str | None = "motion",
    write_file: bool = True,
) -> Path:
    """Catalogue one capture and (by default) write its media."""
    media = capture_root / f"{identifier}.jpg"
    if write_file:
        media.write_bytes(PAYLOAD)
    stamp = (NOW - timedelta(days=days_old)).isoformat()
    metadata = {} if origin is None else {"origin": origin}
    with database_connection(database_path) as connection:
        connection.execute(
            """
            INSERT INTO captures (
                id, filename, absolute_path, captured_at_utc, width, height,
                filesize_bytes, camera_backend, created_at_utc, extra_metadata
            )
            VALUES (?, ?, ?, ?, 4608, 2592, ?, 'simulator', ?, ?)
            """,
            (
                identifier,
                media.name,
                str(media),
                stamp,
                len(PAYLOAD),
                stamp,
                json.dumps(metadata),
            ),
        )
    return media


def _prepared(tmp_path: Path) -> tuple[Path, Path]:
    """Return a migrated database and a capture root holding two captures."""
    capture_root = tmp_path / "captures"
    capture_root.mkdir()
    database_path = tmp_path / "mgo.db"
    apply_migrations(database_path)
    _add_capture(database_path, capture_root, "cap-old")
    _add_capture(database_path, capture_root, "cap-new", days_old=0.0)
    return database_path, capture_root


def _use_delete_journal(database_path: Path) -> None:
    """Switch a database to DELETE journalling.

    WAL needs a ``-shm`` file to be *read*, so a sidecar assertion against a WAL
    database would be testing SQLite rather than this code. DELETE journalling
    removes that confound: nothing whatsoever should appear beside the file.
    """
    connection = sqlite3.connect(database_path)
    try:
        connection.execute("PRAGMA journal_mode = DELETE")
        connection.commit()
    finally:
        connection.close()
    for suffix in ("-wal", "-shm"):
        sidecar = database_path.with_name(database_path.name + suffix)
        if sidecar.exists():
            sidecar.unlink()


def _journal_mode(database_path: Path) -> str:
    """Return the journal mode the database is actually using."""
    connection = sqlite3.connect(database_path)
    try:
        return str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower()
    finally:
        connection.close()


def _snapshot(database_path: Path) -> dict[str, list[tuple[object, ...]]]:
    """Return every row of every table this test cares about."""
    with database_connection(database_path) as connection:
        return {
            table: [
                tuple(row)
                for row in connection.execute(f"SELECT * FROM {table}")
            ]
            for table in ("captures", "observations", "capture_media_lifecycle")
        }


# --- the plan still works ---------------------------------------------------


def test_a_current_schema_database_can_be_planned_read_only(
    tmp_path: Path,
) -> None:
    """The control: a valid database still produces the expected plan."""
    database_path, capture_root = _prepared(tmp_path)

    plan = _service(database_path, capture_root).dry_run()

    assert [candidate.capture_id for candidate in plan.candidates] == ["cap-old"]
    assert plan.managed_present_count == 2
    assert plan.protected_count == 1


def test_the_read_only_projection_matches_the_read_write_one(
    tmp_path: Path,
) -> None:
    """Both paths decode the same catalogue into the same projection.

    They share the SQL and the decoder precisely so a preview cannot disagree
    with the run it is previewing; this asserts that rather than assuming it.
    """
    database_path, _ = _prepared(tmp_path)
    repository = RetentionRepository(database_path)

    assert repository.read_lifecycle_records() == repository.list_lifecycle_records()


# --- no database mutation ---------------------------------------------------


def test_a_dry_run_adds_or_alters_no_lifecycle_row(tmp_path: Path) -> None:
    """A preview creates no deletion intent and completes none."""
    database_path, capture_root = _prepared(tmp_path)

    _service(database_path, capture_root).dry_run()

    with database_connection(database_path) as connection:
        rows = connection.execute(
            "SELECT COUNT(*) FROM capture_media_lifecycle"
        ).fetchone()[0]
    assert rows == 0


def test_a_dry_run_adds_no_observation(tmp_path: Path) -> None:
    """A preview writes nothing to the immutable timeline."""
    database_path, capture_root = _prepared(tmp_path)

    _service(database_path, capture_root).dry_run()

    assert list_observations(database_path) == []


def test_a_dry_run_alters_no_capture_row(tmp_path: Path) -> None:
    """Every table is byte-identical afterwards."""
    database_path, capture_root = _prepared(tmp_path)
    before = _snapshot(database_path)

    _service(database_path, capture_root).dry_run()

    assert _snapshot(database_path) == before


def test_a_dry_run_moves_no_runtime_counter(tmp_path: Path) -> None:
    """Previewing a policy is not a run and is never counted as one."""
    database_path, capture_root = _prepared(tmp_path)
    state = RetentionRuntimeState(enabled=True)
    service = _service(database_path, capture_root, enabled=True, state=state)
    before = state.snapshot()

    service.dry_run()
    service.dry_run()

    assert state.snapshot() == before


def test_a_dry_run_deletes_no_media(tmp_path: Path) -> None:
    """The capture directory is untouched by a preview."""
    database_path, capture_root = _prepared(tmp_path)
    before = sorted(path.name for path in capture_root.iterdir())

    _service(database_path, capture_root).dry_run()

    assert sorted(path.name for path in capture_root.iterdir()) == before


# --- no filesystem side effects ---------------------------------------------


def test_a_dry_run_against_a_missing_database_creates_nothing(
    tmp_path: Path,
) -> None:
    """A missing database fails cleanly instead of being brought into existence.

    Before the read-only path existed this created an empty database file as a
    side effect of the preview -- and then reported that it could not read the
    captures table it had just finished not creating.
    """
    capture_root = tmp_path / "captures"
    capture_root.mkdir()
    missing = tmp_path / "absent.db"

    with pytest.raises(RetentionRepositoryError):
        _service(missing, capture_root).dry_run()

    assert not missing.exists()
    assert sorted(path.name for path in tmp_path.iterdir()) == ["captures"]


def test_a_dry_run_creates_no_missing_parent_directory(tmp_path: Path) -> None:
    """A preview never brings a directory tree into existence."""
    capture_root = tmp_path / "captures"
    capture_root.mkdir()
    nested = tmp_path / "no" / "such" / "dir" / "mgo.db"

    with pytest.raises(RetentionRepositoryError):
        _service(nested, capture_root).dry_run()

    assert not (tmp_path / "no").exists()


def test_a_dry_run_does_not_change_the_journal_mode(tmp_path: Path) -> None:
    """A database using DELETE journalling is still using it afterwards.

    The read-write helper requests WAL on every connection, so a preview through
    it silently converted a database's journal mode -- a persistent change to a
    database the operator only asked to look at.
    """
    database_path, capture_root = _prepared(tmp_path)
    _use_delete_journal(database_path)
    assert _journal_mode(database_path) == "delete"

    _service(database_path, capture_root).dry_run()

    assert _journal_mode(database_path) == "delete"


def test_a_dry_run_creates_no_wal_sidecars(tmp_path: Path) -> None:
    """No ``-wal`` or ``-shm`` file appears merely because a plan was read."""
    database_path, capture_root = _prepared(tmp_path)
    _use_delete_journal(database_path)

    _service(database_path, capture_root).dry_run()

    for suffix in ("-wal", "-shm"):
        sidecar = database_path.with_name(database_path.name + suffix)
        assert not sidecar.exists(), f"the preview created {sidecar.name}"


def test_a_dry_run_leaves_the_directory_listing_unchanged(
    tmp_path: Path,
) -> None:
    """Nothing at all appears beside a DELETE-journal database."""
    database_path, capture_root = _prepared(tmp_path)
    _use_delete_journal(database_path)
    before = sorted(path.name for path in tmp_path.iterdir())

    _service(database_path, capture_root).dry_run()

    assert sorted(path.name for path in tmp_path.iterdir()) == before


def test_reading_a_wal_database_creates_only_sqlite_s_own_shm(
    tmp_path: Path,
) -> None:
    """A documented limitation, asserted rather than glossed over.

    A WAL database cannot be read at all -- even read-only, even with
    ``mode=ro`` -- without SQLite's shared-memory index, so a ``-shm`` file may
    appear beside it. That is SQLite's own mechanism for reading WAL, not a
    mutation this code chose, and it is why the sidecar test above pins a
    DELETE-journal database instead.

    What matters is asserted here directly: the journal mode is unchanged and
    every table is byte-identical. The preview reads a WAL database without
    altering it.
    """
    database_path, capture_root = _prepared(tmp_path)
    assert _journal_mode(database_path) == "wal"
    before = _snapshot(database_path)

    _service(database_path, capture_root).dry_run()

    assert _journal_mode(database_path) == "wal"
    assert _snapshot(database_path) == before


# --- fail-closed semantics are unchanged ------------------------------------


def test_the_read_only_path_still_fails_closed_on_malformed_metadata(
    tmp_path: Path,
) -> None:
    """The preview refuses exactly what a destructive run would refuse."""
    database_path, capture_root = _prepared(tmp_path)
    with database_connection(database_path) as connection:
        connection.execute("UPDATE captures SET extra_metadata = 'not json'")

    with pytest.raises(RetentionCatalogueError):
        _service(database_path, capture_root).dry_run()


def test_the_read_only_path_still_fails_closed_on_a_corrupt_filesize(
    tmp_path: Path,
) -> None:
    """The hardened decoder applies to the preview path too."""
    database_path, capture_root = _prepared(tmp_path)
    with database_connection(database_path) as connection:
        connection.execute("UPDATE captures SET filesize_bytes = 0")

    with pytest.raises(RetentionCatalogueError):
        _service(database_path, capture_root).dry_run()


def test_the_read_only_path_still_protects_unmanaged_captures(
    tmp_path: Path,
) -> None:
    """Manual and unknown-origin captures are absent from a preview too."""
    capture_root = tmp_path / "captures"
    capture_root.mkdir()
    database_path = tmp_path / "mgo.db"
    apply_migrations(database_path)
    _add_capture(database_path, capture_root, "manual", origin=None)
    _add_capture(database_path, capture_root, "unknown", origin="timelapse")
    _add_capture(database_path, capture_root, "motion-old")
    _add_capture(database_path, capture_root, "motion-new", days_old=0.0)

    plan = _service(database_path, capture_root).dry_run()

    assert [candidate.capture_id for candidate in plan.candidates] == ["motion-old"]
    assert plan.managed_present_count == 2


def test_the_read_only_path_works_while_retention_is_disabled(
    tmp_path: Path,
) -> None:
    """Previewing is available from either side of the enablement switch."""
    database_path, capture_root = _prepared(tmp_path)

    plan = _service(database_path, capture_root, enabled=False).dry_run()

    assert [candidate.capture_id for candidate in plan.candidates] == ["cap-old"]


def test_the_plan_projection_still_carries_no_media_paths(
    tmp_path: Path,
) -> None:
    """The published preview names captures, never locations."""
    database_path, capture_root = _prepared(tmp_path)

    payload = json.dumps(_service(database_path, capture_root).dry_run().as_dict())

    assert str(capture_root) not in payload
    assert str(database_path) not in payload
    assert "absolute_path" not in payload
