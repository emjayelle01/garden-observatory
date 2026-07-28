"""Tests for consistent SQLite backup, verification and restore testing.

The property that matters most is not "a file was produced" but "the file that
was produced is a database that can be restored". Almost every test here
therefore ends by *opening* the artefact rather than by asserting on its name or
size.

The second property is that failure leaves nothing misleading behind. A backup
that fails must not leave a file whose name says it succeeded, because the next
operator to look at the directory will believe the name.

Everything runs on the development machine: temporary directories, temporary
SQLite databases, no Raspberry Pi, no ``systemd``, no camera, no network, no
production path and no root.
"""

from __future__ import annotations

import json
import os
import sqlite3
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest

from mgo.core.database import (
    CURRENT_SCHEMA_VERSION,
    apply_migrations,
    connect_readonly,
)
from mgo.operations.backup import (
    BACKUP_FILE_MODE,
    BACKUP_FORMAT_VERSION,
    BACKUP_NAME_PATTERN,
    DEFAULT_RETENTION_COUNT,
    EXPECTED_TABLES,
    LOCK_FILENAME,
    TEMPORARY_PREFIX,
    BackupManifest,
    apply_retention,
    create_backup,
    file_sha256,
    list_backups,
    manifest_path_for,
    restore_test,
    verify_backup,
)
from mgo.operations.errors import ErrorCode, OperationError
from mgo.operations.locking import OperationLock

POSIX_ONLY = pytest.mark.skipif(os.name != "posix", reason="POSIX mode bits")


# --- fixtures ---------------------------------------------------------------


def _insert_observation(database: Path, summary: str) -> None:
    """Add one observation row using the real schema."""
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            """
            INSERT INTO observations
                (observed_at, kind, source, status, summary, created_at)
            VALUES (?, 'test', 'pytest', 'success', ?, ?)
            """,
            (datetime.now(UTC).isoformat(), summary, datetime.now(UTC).isoformat()),
        )
        connection.commit()
    finally:
        connection.close()


@pytest.fixture
def source_database(tmp_path: Path) -> Path:
    """A migrated WAL database holding real rows, left open-able by others."""
    database = tmp_path / "source" / "mgo.db"
    apply_migrations(database)
    _insert_observation(database, "first observation")
    _insert_observation(database, "second observation")
    return database


@pytest.fixture
def destination(tmp_path: Path) -> Path:
    """An empty backup directory."""
    directory = tmp_path / "backups"
    directory.mkdir()
    return directory


def _row_count(database: Path, table: str) -> int:
    """Count rows in a table of a database, read-only."""
    connection = connect_readonly(database)
    try:
        return int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
    finally:
        connection.close()


def _make_backup(source: Path, target: Path, **kwargs: object) -> Path:
    """Take a backup and return the published file."""
    result = create_backup(
        database_path=source,
        destination=target,
        **kwargs,  # type: ignore[arg-type]
    )
    return result.backup_path


# --- taking a backup --------------------------------------------------------


def test_a_wal_database_is_backed_up_successfully(
    source_database: Path, destination: Path
) -> None:
    """The ordinary case: a live WAL database yields a published backup."""
    result = create_backup(database_path=source_database, destination=destination)

    assert result.backup_path.is_file()
    assert result.manifest_path.is_file()
    assert BACKUP_NAME_PATTERN.match(result.backup_path.name)


def test_a_backup_succeeds_while_the_source_is_open_for_writing(
    source_database: Path, destination: Path
) -> None:
    """The point of the online backup API: no service stop is required.

    A second connection holds the database open and has written to it, exactly
    as the API service does, while the backup runs.
    """
    writer = sqlite3.connect(source_database)
    try:
        writer.execute("PRAGMA journal_mode = WAL")
        writer.execute(
            """
            INSERT INTO observations
                (observed_at, kind, source, status, summary, created_at)
            VALUES ('t', 'test', 'writer', 'success', 'held open', 't')
            """
        )
        writer.commit()

        result = create_backup(
            database_path=source_database, destination=destination
        )
    finally:
        writer.close()

    assert _row_count(result.backup_path, "observations") == 3


def test_the_source_database_is_not_modified(
    source_database: Path, destination: Path
) -> None:
    """A backup must be a read: the production database is never touched."""
    before = file_sha256(source_database)
    before_mode = _journal_mode(source_database)

    create_backup(database_path=source_database, destination=destination)

    assert file_sha256(source_database) == before
    assert _journal_mode(source_database) == before_mode == "wal"


def _journal_mode(database: Path) -> str:
    """Return a database's journal mode, read-only."""
    connection = connect_readonly(database)
    try:
        return str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower()
    finally:
        connection.close()


def test_the_backup_contains_the_expected_rows(
    source_database: Path, destination: Path
) -> None:
    """A backup that loses rows is not a backup."""
    backup = _make_backup(source_database, destination)

    assert _row_count(backup, "observations") == 2
    summaries = _summaries(backup)
    assert "first observation" in summaries
    assert "second observation" in summaries


def _summaries(database: Path) -> list[str]:
    """Return every observation summary in a database."""
    connection = connect_readonly(database)
    try:
        return [
            str(row[0])
            for row in connection.execute("SELECT summary FROM observations")
        ]
    finally:
        connection.close()


def test_the_backup_passes_its_own_integrity_check(
    source_database: Path, destination: Path
) -> None:
    """The artefact is a sound SQLite database in its own right."""
    backup = _make_backup(source_database, destination)

    connection = connect_readonly(backup)
    try:
        assert connection.execute("PRAGMA quick_check(1)").fetchone()[0] == "ok"
    finally:
        connection.close()


def test_the_backup_is_a_single_self_contained_file(
    source_database: Path, destination: Path
) -> None:
    """No ``-wal``/``-shm`` sidecar may be needed to read the backup.

    The copy inherits WAL from the source's header, so without collapsing it the
    published ``.db`` would be an incomplete database whose checksum described
    only part of its content. This is the test that would fail if that step were
    ever removed.
    """
    backup = _make_backup(source_database, destination)

    assert not (destination / f"{backup.name}-wal").exists()
    assert not (destination / f"{backup.name}-shm").exists()
    assert sorted(item.name for item in destination.iterdir()) == [
        backup.name,
        manifest_path_for(backup).name,
    ]

    # Readable with the sidecars provably absent.
    assert _row_count(backup, "observations") == 2


def test_every_expected_table_survives_the_backup(
    source_database: Path, destination: Path
) -> None:
    """A restore needs the version authority as well as the data."""
    backup = _make_backup(source_database, destination)

    connection = connect_readonly(backup)
    try:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    finally:
        connection.close()

    for table in EXPECTED_TABLES:
        assert table in tables


# --- the manifest -----------------------------------------------------------


def test_the_manifest_records_every_required_field(
    source_database: Path, destination: Path
) -> None:
    """The manifest is the backup's description; it must be complete."""
    result = create_backup(database_path=source_database, destination=destination)
    payload = json.loads(result.manifest_path.read_text(encoding="utf-8"))

    for name in (
        "format_version",
        "created_at",
        "application",
        "application_version",
        "source_database_name",
        "backup_filename",
        "backup_size_bytes",
        "sha256",
        "schema_version",
        "expected_schema_version",
        "integrity",
        "journal_mode_of_backup",
        "table_row_counts",
    ):
        assert name in payload, name


def test_the_manifest_values_describe_the_published_backup(
    source_database: Path, destination: Path
) -> None:
    """Every recorded measurement must match the file on disk."""
    result = create_backup(database_path=source_database, destination=destination)
    payload = json.loads(result.manifest_path.read_text(encoding="utf-8"))

    assert payload["format_version"] == BACKUP_FORMAT_VERSION
    assert payload["backup_filename"] == result.backup_path.name
    assert payload["backup_size_bytes"] == result.backup_path.stat().st_size
    assert payload["sha256"] == file_sha256(result.backup_path)
    assert payload["schema_version"] == CURRENT_SCHEMA_VERSION
    assert payload["expected_schema_version"] == CURRENT_SCHEMA_VERSION
    assert payload["integrity"] == "ok"
    assert payload["table_row_counts"]["observations"] == 2


def test_the_manifest_records_a_name_not_a_path(
    source_database: Path, destination: Path
) -> None:
    """A manifest travels with its backup; it must leak no filesystem layout."""
    result = create_backup(database_path=source_database, destination=destination)
    text = result.manifest_path.read_text(encoding="utf-8")
    payload = json.loads(text)

    assert payload["source_database_name"] == "mgo.db"
    assert str(source_database.parent) not in text
    assert str(source_database) not in text


def test_the_manifest_timestamp_is_utc(
    source_database: Path, destination: Path
) -> None:
    """Local time would be ambiguous across a DST change."""
    result = create_backup(database_path=source_database, destination=destination)
    payload = json.loads(result.manifest_path.read_text(encoding="utf-8"))

    moment = datetime.fromisoformat(payload["created_at"])
    assert moment.tzinfo is not None
    assert moment.utcoffset() is not None
    assert moment.utcoffset().total_seconds() == 0  # type: ignore[union-attr]


def test_the_manifest_is_deterministic_json(
    source_database: Path, destination: Path
) -> None:
    """Deterministic output is what lets a test assert on content."""
    result = create_backup(database_path=source_database, destination=destination)
    text = result.manifest_path.read_text(encoding="utf-8")

    reparsed = BackupManifest.from_dict(json.loads(text))
    assert reparsed.to_json() == text


def test_the_manifest_carries_no_database_row_content(
    source_database: Path, destination: Path
) -> None:
    """Counts are permitted; contents are not."""
    result = create_backup(database_path=source_database, destination=destination)
    text = result.manifest_path.read_text(encoding="utf-8")

    assert "first observation" not in text
    assert "second observation" not in text


# --- naming -----------------------------------------------------------------


def test_backup_names_use_a_sortable_utc_timestamp(
    source_database: Path, destination: Path
) -> None:
    """Sortable names are what make retention a simple "keep the newest N"."""
    backup = _make_backup(source_database, destination)
    match = BACKUP_NAME_PATTERN.match(backup.name)

    assert match is not None
    stamp = match.group(1)
    parsed = datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    assert abs((datetime.now(UTC) - parsed).total_seconds()) < 300


def test_a_backup_name_carries_no_username_or_host_path(
    source_database: Path, destination: Path
) -> None:
    """Names appear in logs and in listings sent to other people."""
    backup = _make_backup(source_database, destination)

    assert backup.name.startswith("mgo-")
    for fragment in ("/", "\\", os.environ.get("USERNAME", "\0"), "home"):
        assert fragment not in backup.name


def test_the_manifest_sits_beside_its_backup(
    source_database: Path, destination: Path
) -> None:
    """The pairing rule retention depends on."""
    result = create_backup(database_path=source_database, destination=destination)

    assert manifest_path_for(result.backup_path) == result.manifest_path


# --- atomicity and failure --------------------------------------------------


def test_no_temporary_file_survives_a_successful_backup(
    source_database: Path, destination: Path
) -> None:
    """A leftover temporary would accumulate on every run."""
    create_backup(database_path=source_database, destination=destination)

    assert not [
        item for item in destination.iterdir() if item.name.startswith(TEMPORARY_PREFIX)
    ]


def test_a_failed_backup_publishes_nothing(
    source_database: Path, destination: Path
) -> None:
    """The core safety property: failure must not look like success."""
    _corrupt(source_database)

    with pytest.raises(OperationError):
        create_backup(database_path=source_database, destination=destination)

    published = [
        item for item in destination.iterdir() if BACKUP_NAME_PATTERN.match(item.name)
    ]
    assert published == []


def test_a_failed_backup_leaves_no_temporary_file(
    source_database: Path, destination: Path
) -> None:
    """A failed run cleans up after itself."""
    _corrupt(source_database)

    with pytest.raises(OperationError):
        create_backup(database_path=source_database, destination=destination)

    leftovers = [
        item.name
        for item in destination.iterdir()
        if item.name.startswith(TEMPORARY_PREFIX)
    ]
    assert leftovers == []


def _corrupt(database: Path) -> None:
    """Replace a database's contents with bytes that are not a database."""
    for suffix in ("-wal", "-shm"):
        sidecar = database.with_name(database.name + suffix)
        if sidecar.exists():
            sidecar.unlink()
    database.write_bytes(b"this is definitely not a SQLite database" * 64)


def test_a_corrupt_source_is_reported_and_not_published(
    source_database: Path, destination: Path
) -> None:
    """Corruption must be detected rather than faithfully copied."""
    _corrupt(source_database)

    with pytest.raises(OperationError) as caught:
        create_backup(database_path=source_database, destination=destination)

    assert caught.value.code in {
        ErrorCode.BACKUP_SQLITE_FAILED,
        ErrorCode.BACKUP_SOURCE_UNAVAILABLE,
        ErrorCode.BACKUP_INTEGRITY_FAILED,
    }


def test_an_existing_backup_name_is_never_overwritten(
    source_database: Path, destination: Path
) -> None:
    """A completed backup is immutable once published."""
    first = _make_backup(source_database, destination)
    original = first.read_bytes()

    # Force the same name by pinning the timestamp.
    moment = datetime.strptime(
        BACKUP_NAME_PATTERN.match(first.name).group(1),  # type: ignore[union-attr]
        "%Y%m%dT%H%M%SZ",
    ).replace(tzinfo=UTC)

    with pytest.raises(OperationError) as caught:
        create_backup(
            database_path=source_database, destination=destination, now=moment
        )

    assert caught.value.code is ErrorCode.BACKUP_ALREADY_EXISTS
    assert first.read_bytes() == original


def test_a_missing_source_is_reported(tmp_path: Path, destination: Path) -> None:
    """A clear code beats an SQLite "unable to open database file"."""
    with pytest.raises(OperationError) as caught:
        create_backup(
            database_path=tmp_path / "absent.db", destination=destination
        )

    assert caught.value.code is ErrorCode.BACKUP_SOURCE_UNAVAILABLE


def test_a_source_that_is_not_a_file_is_reported(
    tmp_path: Path, destination: Path
) -> None:
    """A directory where a database was expected is a configuration error."""
    directory = tmp_path / "not-a-database"
    directory.mkdir()

    with pytest.raises(OperationError) as caught:
        create_backup(database_path=directory, destination=destination)

    assert caught.value.code is ErrorCode.BACKUP_SOURCE_UNAVAILABLE


def test_an_in_memory_source_is_refused(destination: Path) -> None:
    """There is nothing persistent to back up."""
    with pytest.raises(OperationError) as caught:
        create_backup(database_path=Path(":memory:"), destination=destination)

    assert caught.value.code is ErrorCode.BACKUP_SOURCE_UNAVAILABLE


def test_an_unwritable_destination_is_reported(
    source_database: Path, tmp_path: Path
) -> None:
    """A destination that cannot be created must fail before any work."""
    blocker = tmp_path / "blocked"
    blocker.write_text("I am a file, not a directory", encoding="utf-8")

    with pytest.raises(OperationError) as caught:
        create_backup(database_path=source_database, destination=blocker)

    assert caught.value.code is ErrorCode.BACKUP_DESTINATION_UNWRITABLE


def test_a_source_newer_than_this_build_is_refused(
    source_database: Path, destination: Path
) -> None:
    """Restoring a future schema into an older build would corrupt meaning."""
    connection = sqlite3.connect(source_database)
    try:
        connection.execute(
            "INSERT INTO schema_migrations (version, name, applied_at) "
            "VALUES (?, 'future.sql', 't')",
            (CURRENT_SCHEMA_VERSION + 5,),
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(OperationError) as caught:
        create_backup(database_path=source_database, destination=destination)

    assert caught.value.code is ErrorCode.BACKUP_SCHEMA_INCOMPATIBLE
    assert not [
        item for item in destination.iterdir() if BACKUP_NAME_PATTERN.match(item.name)
    ]


def test_a_symlinked_source_is_refused(
    source_database: Path, destination: Path, tmp_path: Path
) -> None:
    """The recorded policy: a link target can change between runs.

    Creating a symlink needs privilege or Developer Mode on Windows; the test
    skips where it cannot be created rather than asserting nothing.
    """
    link = tmp_path / "linked.db"
    try:
        link.symlink_to(source_database)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not creatable in this environment")

    with pytest.raises(OperationError) as caught:
        create_backup(database_path=link, destination=destination)

    assert caught.value.code is ErrorCode.BACKUP_SOURCE_UNAVAILABLE
    assert "symbolic link" in caught.value.message


def test_paths_containing_spaces_are_handled(tmp_path: Path) -> None:
    """Deployment paths are quoted, but the Python must cope regardless."""
    source = tmp_path / "a directory with spaces" / "mgo.db"
    apply_migrations(source)
    _insert_observation(source, "spaced")
    target = tmp_path / "backup output dir"

    result = create_backup(database_path=source, destination=target)

    assert result.backup_path.is_file()
    assert _row_count(result.backup_path, "observations") == 1


def test_unicode_in_row_content_does_not_break_a_backup(tmp_path: Path) -> None:
    """Observation summaries are free text and may be non-ASCII."""
    source = tmp_path / "unicode" / "mgo.db"
    apply_migrations(source)
    _insert_observation(source, "café — 日本語 🐦")
    target = tmp_path / "backups"

    result = create_backup(database_path=source, destination=target)

    assert "café — 日本語 🐦" in _summaries(result.backup_path)
    # ...and none of it reaches the manifest.
    assert "café" not in result.manifest_path.read_text(encoding="utf-8")


@POSIX_ONLY
def test_published_files_are_not_world_readable(
    source_database: Path, destination: Path
) -> None:
    """A backup holds the whole observation history; it is not public."""
    result = create_backup(database_path=source_database, destination=destination)

    for path in (result.backup_path, result.manifest_path):
        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode == BACKUP_FILE_MODE
        assert not mode & 0o007


# --- retention --------------------------------------------------------------


def _fabricate_set(directory: Path, stamp: str) -> tuple[Path, Path]:
    """Create a syntactically valid backup set without running a backup."""
    backup = directory / f"mgo-{stamp}.db"
    manifest = directory / f"mgo-{stamp}.manifest.json"
    backup.write_bytes(b"placeholder")
    manifest.write_text("{}", encoding="utf-8")
    return backup, manifest


def test_retention_keeps_the_newest_complete_sets(destination: Path) -> None:
    """Retention is "keep the newest N", by name."""
    stamps = [f"2026010{index}T000000Z" for index in range(1, 6)]
    for stamp in stamps:
        _fabricate_set(destination, stamp)

    result = apply_retention(destination, keep=2)

    remaining = sorted(
        item.name for item in destination.iterdir() if item.suffix == ".db"
    )
    assert remaining == ["mgo-20260104T000000Z.db", "mgo-20260105T000000Z.db"]
    assert result.keep == 2
    assert len(result.removed) == 6  # three sets, two files each


def test_retention_removes_a_set_as_one_unit(destination: Path) -> None:
    """Removing a ``.db`` but leaving its manifest would manufacture an orphan."""
    for stamp in ("20260101T000000Z", "20260102T000000Z"):
        _fabricate_set(destination, stamp)

    apply_retention(destination, keep=1)

    assert not (destination / "mgo-20260101T000000Z.db").exists()
    assert not (destination / "mgo-20260101T000000Z.manifest.json").exists()


def test_retention_never_removes_the_newest_backup(destination: Path) -> None:
    """``keep`` is validated to be at least one, so this cannot happen."""
    _fabricate_set(destination, "20260101T000000Z")

    with pytest.raises(OperationError) as caught:
        apply_retention(destination, keep=0)

    assert caught.value.code is ErrorCode.INVALID_ARGUMENT
    assert (destination / "mgo-20260101T000000Z.db").exists()


@pytest.mark.parametrize("keep", [-1, 0, 100_000])
def test_retention_counts_are_bounded(destination: Path, keep: int) -> None:
    """A bounded positive integer, validated before anything is deleted."""
    with pytest.raises(OperationError) as caught:
        apply_retention(destination, keep=keep)

    assert caught.value.code is ErrorCode.INVALID_ARGUMENT


def test_retention_ignores_unrelated_files(destination: Path) -> None:
    """A backup directory may legitimately hold other things."""
    for stamp in ("20260101T000000Z", "20260102T000000Z", "20260103T000000Z"):
        _fabricate_set(destination, stamp)

    bystanders = [
        destination / "README.txt",
        destination / "mgo-support-20260101T000000Z.tar.gz",
        destination / "old-backup.db",
        destination / "mgo-notes.md",
    ]
    for path in bystanders:
        path.write_text("keep me", encoding="utf-8")

    apply_retention(destination, keep=1)

    for path in bystanders:
        assert path.exists(), path.name


def test_retention_does_not_delete_support_bundles(destination: Path) -> None:
    """Bundles share the naming prefix but are not backups."""
    for stamp in ("20260101T000000Z", "20260102T000000Z"):
        _fabricate_set(destination, stamp)
    bundle = destination / "mgo-support-20260101T000000Z.tar.gz"
    bundle.write_bytes(b"bundle")

    apply_retention(destination, keep=1)

    assert bundle.exists()


def test_retention_ignores_orphans_and_temporary_files(destination: Path) -> None:
    """An incomplete set is not a backup and must not be counted as one."""
    _fabricate_set(destination, "20260105T000000Z")
    (destination / "mgo-20260104T000000Z.db").write_bytes(b"orphan db")
    (destination / "mgo-20260103T000000Z.manifest.json").write_text(
        "{}", encoding="utf-8"
    )
    (destination / f"{TEMPORARY_PREFIX}abcd.tmp").write_bytes(b"in progress")

    apply_retention(destination, keep=1)

    assert (destination / "mgo-20260104T000000Z.db").exists()
    assert (destination / "mgo-20260103T000000Z.manifest.json").exists()
    assert (destination / f"{TEMPORARY_PREFIX}abcd.tmp").exists()


def test_retention_failure_is_reported_without_raising(
    destination: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A retention problem must not be silent, and must not raise mid-sweep."""
    for stamp in ("20260101T000000Z", "20260102T000000Z"):
        _fabricate_set(destination, stamp)

    real_unlink = Path.unlink

    def refuse(self: Path, *args: object, **kwargs: object) -> None:
        if self.name.startswith("mgo-2026"):
            raise OSError(13, "Permission denied")
        real_unlink(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "unlink", refuse)

    result = apply_retention(destination, keep=1)

    assert not result.succeeded
    assert result.failures
    assert (destination / "mgo-20260101T000000Z.db").exists()


def test_retention_failure_does_not_invalidate_the_new_backup(
    source_database: Path, destination: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The backup already exists and is valid; retention is a separate concern."""
    for stamp in ("20200101T000000Z", "20200102T000000Z"):
        _fabricate_set(destination, stamp)

    real_unlink = Path.unlink

    def refuse(self: Path, *args: object, **kwargs: object) -> None:
        if self.name.startswith("mgo-2020"):
            raise OSError(13, "Permission denied")
        real_unlink(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "unlink", refuse)

    result = create_backup(
        database_path=source_database, destination=destination, keep=1
    )

    assert result.backup_path.is_file()
    assert verify_backup(result.backup_path).ok
    assert result.retention is not None
    assert not result.retention.succeeded


def test_retention_runs_only_after_a_published_backup(
    source_database: Path, destination: Path
) -> None:
    """A failed backup must not expire an older good one."""
    for index in range(1, 4):
        _fabricate_set(destination, f"2026010{index}T000000Z")
    _corrupt(source_database)

    with pytest.raises(OperationError):
        create_backup(
            database_path=source_database, destination=destination, keep=1
        )

    survivors = sorted(
        item.name for item in destination.iterdir() if item.suffix == ".db"
    )
    assert len(survivors) == 3


def test_the_default_retention_is_fourteen() -> None:
    """The documented operations policy, asserted so it cannot drift."""
    assert DEFAULT_RETENTION_COUNT == 14


# --- concurrency ------------------------------------------------------------


def test_a_second_backup_is_refused_while_one_holds_the_lock(
    source_database: Path, destination: Path
) -> None:
    """Overlapping runs would compete for the same directory."""
    holder = OperationLock(destination / LOCK_FILENAME, operation="backup")
    holder.acquire()
    try:
        with pytest.raises(OperationError) as caught:
            create_backup(
                database_path=source_database, destination=destination
            )
    finally:
        holder.release()

    assert caught.value.code is ErrorCode.BACKUP_LOCKED


def test_the_lock_is_released_after_a_backup(
    source_database: Path, destination: Path
) -> None:
    """Two sequential backups must both succeed."""
    create_backup(database_path=source_database, destination=destination)

    assert not (destination / LOCK_FILENAME).exists()

    later = datetime.now(UTC).replace(microsecond=0)
    second = create_backup(
        database_path=source_database,
        destination=destination,
        now=later.replace(year=later.year + 1),
    )
    assert second.backup_path.is_file()


def test_the_lock_is_released_after_a_failed_backup(
    source_database: Path, destination: Path
) -> None:
    """A crashed backup must not lock out the next scheduled run."""
    _corrupt(source_database)

    with pytest.raises(OperationError):
        create_backup(database_path=source_database, destination=destination)

    assert not (destination / LOCK_FILENAME).exists()


def test_the_lock_file_is_not_mistaken_for_a_backup(
    source_database: Path, destination: Path
) -> None:
    """Retention must never remove the lock guarding it."""
    create_backup(database_path=source_database, destination=destination)
    listing = list_backups(destination)

    assert all(LOCK_FILENAME not in item.backup_path.name for item in listing.sets)


# --- listing ----------------------------------------------------------------


def test_listing_returns_complete_sets_newest_first(destination: Path) -> None:
    """Newest first is what an operator wants to see."""
    for stamp in ("20260101T000000Z", "20260103T000000Z", "20260102T000000Z"):
        _fabricate_set(destination, stamp)

    listing = list_backups(destination)

    assert [item.timestamp for item in listing.sets] == [
        "20260103T000000Z",
        "20260102T000000Z",
        "20260101T000000Z",
    ]


def test_listing_reports_orphans_separately(destination: Path) -> None:
    """An orphan is not a backup, but hiding it would misrepresent the state."""
    _fabricate_set(destination, "20260101T000000Z")
    (destination / "mgo-20260102T000000Z.db").write_bytes(b"orphan")
    (destination / "mgo-20260103T000000Z.manifest.json").write_text(
        "{}", encoding="utf-8"
    )
    (destination / f"{TEMPORARY_PREFIX}x.tmp").write_bytes(b"tmp")

    listing = list_backups(destination)

    assert len(listing.sets) == 1
    assert listing.orphan_backups == ("mgo-20260102T000000Z.db",)
    assert listing.orphan_manifests == ("mgo-20260103T000000Z.manifest.json",)
    assert listing.temporary_files == (f"{TEMPORARY_PREFIX}x.tmp",)


def test_listing_can_report_verification_state(
    source_database: Path, destination: Path
) -> None:
    """"Is my backup good?" is the question a listing exists to answer."""
    create_backup(database_path=source_database, destination=destination)

    listing = list_backups(destination, verify=True)

    assert len(listing.verifications) == 1
    assert all(result.ok for result in listing.verifications.values())


def test_listing_a_missing_directory_is_reported(tmp_path: Path) -> None:
    """A clear code beats a bare ``FileNotFoundError``."""
    with pytest.raises(OperationError) as caught:
        list_backups(tmp_path / "absent")

    assert caught.value.code is ErrorCode.BACKUP_NOT_FOUND


# --- verification -----------------------------------------------------------


def test_a_good_backup_verifies(source_database: Path, destination: Path) -> None:
    """The happy path, with every individual check recorded."""
    backup = _make_backup(source_database, destination)

    result = verify_backup(backup)

    assert result.ok
    assert result.error_code is None
    for check in (
        "backup_present",
        "manifest_readable",
        "size",
        "sha256",
        "integrity",
        "schema",
        "expected_tables",
        "row_counts",
    ):
        assert result.checks[check] == "ok", check


def test_verification_is_read_only(
    source_database: Path, destination: Path
) -> None:
    """Verification must never repair, re-checksum or rewrite anything."""
    backup = _make_backup(source_database, destination)
    manifest = manifest_path_for(backup)
    before = (file_sha256(backup), manifest.read_bytes())

    verify_backup(backup)

    assert (file_sha256(backup), manifest.read_bytes()) == before
    assert sorted(item.name for item in destination.iterdir()) == sorted(
        [backup.name, manifest.name]
    )


def test_a_changed_backup_fails_its_checksum(
    source_database: Path, destination: Path
) -> None:
    """Bit rot on an SD card is exactly what a checksum exists to catch."""
    backup = _make_backup(source_database, destination)
    data = bytearray(backup.read_bytes())
    data[len(data) // 2] ^= 0xFF
    backup.write_bytes(bytes(data))

    result = verify_backup(backup)

    assert not result.ok
    assert result.error_code is ErrorCode.BACKUP_CHECKSUM_MISMATCH


def test_a_truncated_backup_fails_on_size(
    source_database: Path, destination: Path
) -> None:
    """A short write is caught before the more expensive checksum."""
    backup = _make_backup(source_database, destination)
    backup.write_bytes(backup.read_bytes()[: -4096])

    result = verify_backup(backup)

    assert not result.ok
    assert result.error_code is ErrorCode.BACKUP_CHECKSUM_MISMATCH
    assert "size" not in result.checks


def test_a_missing_manifest_fails_verification(
    source_database: Path, destination: Path
) -> None:
    """A backup with no description cannot be verified against anything."""
    backup = _make_backup(source_database, destination)
    manifest_path_for(backup).unlink()

    result = verify_backup(backup)

    assert not result.ok
    assert result.error_code is ErrorCode.BACKUP_MANIFEST_FAILED


def test_a_malformed_manifest_fails_verification(
    source_database: Path, destination: Path
) -> None:
    """Truncated JSON is an expected condition, not a crash."""
    backup = _make_backup(source_database, destination)
    manifest_path_for(backup).write_text('{"format_version": 1,', encoding="utf-8")

    result = verify_backup(backup)

    assert not result.ok
    assert result.error_code is ErrorCode.BACKUP_MANIFEST_FAILED


def test_a_manifest_from_a_newer_build_is_refused(
    source_database: Path, destination: Path
) -> None:
    """The same rule the database schema follows."""
    backup = _make_backup(source_database, destination)
    manifest = manifest_path_for(backup)
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["format_version"] = BACKUP_FORMAT_VERSION + 1
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    result = verify_backup(backup)

    assert not result.ok
    assert result.error_code is ErrorCode.BACKUP_MANIFEST_FAILED


def test_a_manifest_missing_a_required_field_is_refused(
    source_database: Path, destination: Path
) -> None:
    """Every recorded field is load-bearing for a verification decision."""
    backup = _make_backup(source_database, destination)
    manifest = manifest_path_for(backup)
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    del payload["sha256"]
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    result = verify_backup(backup)

    assert not result.ok
    assert result.error_code is ErrorCode.BACKUP_MANIFEST_FAILED


def test_a_corrupt_backup_file_fails_verification(
    source_database: Path, destination: Path
) -> None:
    """A file that checksums correctly can still be a broken database.

    The manifest is rewritten to match the corrupt bytes, so the checksum
    passes and the SQLite check is genuinely what catches the problem.
    """
    backup = _make_backup(source_database, destination)
    backup.write_bytes(b"not a database" * 512)
    manifest = manifest_path_for(backup)
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["sha256"] = file_sha256(backup)
    payload["backup_size_bytes"] = backup.stat().st_size
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    result = verify_backup(backup)

    assert not result.ok
    assert result.error_code in {
        ErrorCode.BACKUP_INTEGRITY_FAILED,
        ErrorCode.BACKUP_SCHEMA_INCOMPATIBLE,
    }


def test_a_row_count_mismatch_fails_verification(
    source_database: Path, destination: Path
) -> None:
    """The manifest's counts are checked against the database, not trusted."""
    backup = _make_backup(source_database, destination)
    manifest = manifest_path_for(backup)
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["table_row_counts"]["observations"] = 999
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    result = verify_backup(backup)

    assert not result.ok
    assert result.error_code is ErrorCode.BACKUP_INTEGRITY_FAILED


def test_a_backup_missing_an_expected_table_fails_verification(
    tmp_path: Path, destination: Path
) -> None:
    """A database without ``captures`` is not a restorable MGO backup."""
    partial = tmp_path / "partial.db"
    connection = sqlite3.connect(partial)
    connection.execute(
        "CREATE TABLE schema_migrations "
        "(version INTEGER PRIMARY KEY, name TEXT, applied_at TEXT)"
    )
    connection.execute(
        "INSERT INTO schema_migrations VALUES (1, 'x', 't')"
    )
    connection.commit()
    connection.close()

    target = destination / "mgo-20260101T000000Z.db"
    target.write_bytes(partial.read_bytes())
    manifest_path_for(target).write_text(
        json.dumps(
            {
                "format_version": 1,
                "created_at": "2026-01-01T00:00:00+00:00",
                "application": "garden-observatory",
                "application_version": "0.1.0",
                "source_database_name": "mgo.db",
                "backup_filename": target.name,
                "backup_size_bytes": target.stat().st_size,
                "sha256": file_sha256(target),
                "schema_version": 1,
                "expected_schema_version": CURRENT_SCHEMA_VERSION,
                "integrity": "ok",
                "journal_mode_of_backup": "delete",
                "table_row_counts": {},
            }
        ),
        encoding="utf-8",
    )

    result = verify_backup(target)

    assert not result.ok
    assert result.error_code is ErrorCode.BACKUP_SCHEMA_INCOMPATIBLE


def test_verifying_a_missing_backup_is_reported(tmp_path: Path) -> None:
    """A typo in a filename must not look like corruption."""
    result = verify_backup(tmp_path / "absent.db")

    assert not result.ok
    assert result.error_code is ErrorCode.BACKUP_NOT_FOUND


# --- restore testing --------------------------------------------------------


def test_a_backup_restores_and_verifies(
    source_database: Path, destination: Path
) -> None:
    """The step that turns "a backup exists" into "a backup works"."""
    backup = _make_backup(source_database, destination)

    result = restore_test(backup)

    assert result.ok
    assert result.checks["integrity"] == "ok"
    assert result.checks["row_counts"] == "ok"
    assert result.row_counts["observations"] == 2


def test_a_restore_test_cleans_up_after_itself(
    source_database: Path, destination: Path, tmp_path: Path
) -> None:
    """A restore test that left copies behind would fill the SD card."""
    backup = _make_backup(source_database, destination)
    work = tmp_path / "restore-work"

    result = restore_test(backup, work_directory=work)

    assert result.ok
    assert not (work / "restored.db").exists()
    assert not result.preserved


def test_a_restore_test_can_preserve_its_directory_when_asked(
    source_database: Path, destination: Path, tmp_path: Path
) -> None:
    """Preservation is opt-in, for diagnosing a failure."""
    backup = _make_backup(source_database, destination)
    work = tmp_path / "kept"

    result = restore_test(backup, work_directory=work, preserve=True)

    assert result.preserved
    assert (work / "restored.db").is_file()


def test_a_restore_test_does_not_change_the_source_backup(
    source_database: Path, destination: Path
) -> None:
    """Testing a backup must not damage the thing being tested."""
    backup = _make_backup(source_database, destination)
    before = file_sha256(backup)

    result = restore_test(backup)

    assert result.checks["source_unchanged"] == "ok"
    assert file_sha256(backup) == before


def test_a_restore_test_never_touches_the_production_database(
    source_database: Path, destination: Path
) -> None:
    """The most destructive thing this tooling could do, made impossible."""
    backup = _make_backup(source_database, destination)
    before = file_sha256(source_database)

    restore_test(backup, database_path=source_database)

    assert file_sha256(source_database) == before


@pytest.mark.parametrize(
    "target",
    [
        "/var/lib/garden-observatory/db",
        "/var/lib/garden-observatory/db/nested",
        "/var/lib/garden-observatory",
    ],
)
def test_a_production_data_location_is_refused_as_a_restore_target(
    source_database: Path, destination: Path, target: str
) -> None:
    """A restore test writing to the live database directory is unthinkable.

    The refusal must happen *before* the directory is created. Existence is
    compared before and after rather than asserted absolutely: the test must
    neither depend on the state of the machine running it nor leave anything
    behind on one where the path happens to exist.
    """
    backup = _make_backup(source_database, destination)
    existed_before = Path(target).exists()

    with pytest.raises(OperationError) as caught:
        restore_test(backup, work_directory=Path(target))

    assert caught.value.code is ErrorCode.RESTORE_TARGET_REJECTED
    assert Path(target).exists() == existed_before, (
        "the refusal must happen before the target directory is created"
    )


def test_the_configured_database_directory_is_also_refused(
    source_database: Path, destination: Path
) -> None:
    """Protection follows the configuration, not only the canonical constant."""
    backup = _make_backup(source_database, destination)

    with pytest.raises(OperationError) as caught:
        restore_test(
            backup,
            work_directory=source_database.parent,
            database_path=source_database,
        )

    assert caught.value.code is ErrorCode.RESTORE_TARGET_REJECTED


def test_a_restore_test_of_a_corrupt_backup_fails(
    source_database: Path, destination: Path
) -> None:
    """A backup that cannot be restored must be reported as such."""
    backup = _make_backup(source_database, destination)
    backup.write_bytes(b"not a database" * 512)

    result = restore_test(backup)

    assert not result.ok
    assert result.error_code in {
        ErrorCode.RESTORE_TEST_FAILED,
        ErrorCode.BACKUP_INTEGRITY_FAILED,
        ErrorCode.BACKUP_SCHEMA_INCOMPATIBLE,
    }


def test_a_restore_test_of_a_missing_backup_fails(tmp_path: Path) -> None:
    """A clear result rather than an exception out of the CLI."""
    result = restore_test(tmp_path / "absent.db")

    assert not result.ok
    assert result.error_code is ErrorCode.BACKUP_NOT_FOUND


def test_a_row_count_disagreement_fails_the_restore_test(
    source_database: Path, destination: Path
) -> None:
    """The restored copy is compared against the recorded counts."""
    backup = _make_backup(source_database, destination)
    manifest = manifest_path_for(backup)
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["table_row_counts"]["observations"] = 4242
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    result = restore_test(backup)

    assert not result.ok
    assert result.error_code is ErrorCode.RESTORE_TEST_FAILED
