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

import errno
import io
import json
import os
import sqlite3
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

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
    MAX_CONFIGURATION_BYTES,
    TEMPORARY_PREFIX,
    BackupManifest,
    apply_retention,
    capture_configuration,
    configuration_path_for,
    create_backup,
    file_sha256,
    list_backups,
    manifest_path_for,
    restore_test,
    verify_backup,
)
from mgo.operations.errors import ErrorCode, OperationError
from mgo.operations.events import EventEmitter
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


#: A **complete, loadable** configuration — a recovery set must contain one the
#: application can actually load, so ``capture_configuration`` parses it.
#:
#: Deliberately contains a comment, a blank line and a secret-looking value.
#: The comment and blank line prove the snapshot preserves exact bytes rather
#: than round-tripping through a TOML parser; the value proves configuration
#: content never reaches an event, a manifest or a summary.
CONFIGURATION_TEXT = """\
# Matt's Garden Observatory — production configuration.

[application]
name = "MGO"
environment = "production"
host = "0.0.0.0"
port = 8080

[storage]
data_directory = "data"
log_directory = "logs"
database_path = "data/mgo.db"

[camera]
enabled = false
backend = "null"
detection_interval_seconds = 60
capture_directory = "data/captures"

[health]
enabled = true
collection_interval_seconds = 60
temperature_warning_celsius = 70.0
temperature_critical_celsius = 80.0
disk_warning_percent = 80.0
disk_critical_percent = 90.0
memory_warning_percent = 85.0
memory_critical_percent = 95.0

[future_transport]
bot_token = "SECRET-TOKEN-MUST-NOT-LEAK"
"""


@pytest.fixture
def source_configuration(tmp_path: Path) -> Path:
    """A production-shaped configuration file to snapshot."""
    path = tmp_path / "source" / "mgo.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(CONFIGURATION_TEXT, encoding="utf-8")
    return path


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


def _configuration_beside(source: Path) -> Path:
    """Return a configuration file created next to a database source.

    Tests that build their own database outside the fixtures still need a
    configuration for the recovery set, and it must not be shared with another
    test's temporary tree.
    """
    path = source.parent / "mgo.toml"
    if not path.exists():
        path.write_text(CONFIGURATION_TEXT, encoding="utf-8")
    return path


def _backup(
    source: Path,
    target: Path,
    configuration: Path | None = None,
    **kwargs: object,
) -> Any:
    """Take a complete recovery set, defaulting the configuration.

    Captures the configuration the way the CLI does — securely, once — so the
    tests exercise the real capture path rather than a shortcut around it.
    """
    snapshot = capture_configuration(
        configuration if configuration is not None else _configuration_beside(source)
    )
    return create_backup(
        database_path=source,
        configuration=snapshot,
        destination=target,
        **kwargs,  # type: ignore[arg-type]
    )


def _make_backup(source: Path, target: Path, **kwargs: object) -> Path:
    """Take a recovery set and return the published database file."""
    return _backup(source, target, **kwargs).backup_path


def _manifest_body(
    backup: Path, configuration: Path, **overrides: Any
) -> dict[str, Any]:
    """Build a structurally valid manifest body for a hand-made set.

    Defaults describe the files actually on disk, so a test only has to state
    the one field it wants to be wrong.
    """
    body: dict[str, Any] = {
        "format_version": BACKUP_FORMAT_VERSION,
        "created_at": "2026-01-01T00:00:00+00:00",
        "application": "garden-observatory",
        "application_version": "0.1.0",
        "source_database_name": "mgo.db",
        "backup_filename": backup.name,
        "backup_size_bytes": backup.stat().st_size,
        "sha256": file_sha256(backup),
        "schema_version": CURRENT_SCHEMA_VERSION,
        "expected_schema_version": CURRENT_SCHEMA_VERSION,
        "integrity": "ok",
        "journal_mode_of_backup": "delete",
        "table_row_counts": dict.fromkeys(EXPECTED_TABLES, 0),
        "configuration_source_name": "mgo.toml",
        "configuration_filename": configuration.name,
        "configuration_size_bytes": configuration.stat().st_size,
        "configuration_sha256": file_sha256(configuration),
    }
    body.update(overrides)
    return body


def _rewrite_manifest(backup: Path, **overrides: Any) -> None:
    """Rewrite a real backup's manifest with the given field overrides."""
    manifest = manifest_path_for(backup)
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload.update(overrides)
    manifest.write_text(json.dumps(payload), encoding="utf-8")


# --- taking a backup --------------------------------------------------------


def test_a_wal_database_is_backed_up_successfully(
    source_database: Path, destination: Path
) -> None:
    """The ordinary case: a live WAL database yields a published backup."""
    result = _backup(source_database, destination)

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

        result = _backup(source_database, destination)
    finally:
        writer.close()

    assert _row_count(result.backup_path, "observations") == 3


def test_the_source_database_is_not_modified(
    source_database: Path, destination: Path
) -> None:
    """A backup must be a read: the production database is never touched."""
    before = file_sha256(source_database)
    before_mode = _journal_mode(source_database)

    _backup(source_database, destination)

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
    assert sorted(item.name for item in destination.iterdir()) == sorted(
        [
            backup.name,
            configuration_path_for(backup).name,
            manifest_path_for(backup).name,
        ]
    )

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
    result = _backup(source_database, destination)
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
    result = _backup(source_database, destination)
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
    result = _backup(source_database, destination)
    text = result.manifest_path.read_text(encoding="utf-8")
    payload = json.loads(text)

    assert payload["source_database_name"] == "mgo.db"
    assert str(source_database.parent) not in text
    assert str(source_database) not in text


def test_the_manifest_timestamp_is_utc(
    source_database: Path, destination: Path
) -> None:
    """Local time would be ambiguous across a DST change."""
    result = _backup(source_database, destination)
    payload = json.loads(result.manifest_path.read_text(encoding="utf-8"))

    moment = datetime.fromisoformat(payload["created_at"])
    assert moment.tzinfo is not None
    assert moment.utcoffset() is not None
    assert moment.utcoffset().total_seconds() == 0  # type: ignore[union-attr]


def test_the_manifest_is_deterministic_json(
    source_database: Path, destination: Path
) -> None:
    """Deterministic output is what lets a test assert on content."""
    result = _backup(source_database, destination)
    text = result.manifest_path.read_text(encoding="utf-8")

    reparsed = BackupManifest.from_dict(json.loads(text))
    assert reparsed.to_json() == text


def test_the_manifest_carries_no_database_row_content(
    source_database: Path, destination: Path
) -> None:
    """Counts are permitted; contents are not."""
    result = _backup(source_database, destination)
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
    result = _backup(source_database, destination)

    assert manifest_path_for(result.backup_path) == result.manifest_path


# --- atomicity and failure --------------------------------------------------


def test_no_temporary_file_survives_a_successful_backup(
    source_database: Path, destination: Path
) -> None:
    """A leftover temporary would accumulate on every run."""
    _backup(source_database, destination)

    assert not [
        item for item in destination.iterdir() if item.name.startswith(TEMPORARY_PREFIX)
    ]


def test_a_failed_backup_publishes_nothing(
    source_database: Path, destination: Path
) -> None:
    """The core safety property: failure must not look like success."""
    _corrupt(source_database)

    with pytest.raises(OperationError):
        _backup(source_database, destination)

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
        _backup(source_database, destination)

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
        _backup(source_database, destination)

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
        _backup(source_database, destination, now=moment)

    assert caught.value.code is ErrorCode.BACKUP_ALREADY_EXISTS
    assert first.read_bytes() == original


def test_a_missing_source_is_reported(tmp_path: Path, destination: Path) -> None:
    """A clear code beats an SQLite "unable to open database file"."""
    with pytest.raises(OperationError) as caught:
        _backup(tmp_path / "absent.db", destination)

    assert caught.value.code is ErrorCode.BACKUP_SOURCE_UNAVAILABLE


def test_a_source_that_is_not_a_file_is_reported(
    tmp_path: Path, destination: Path
) -> None:
    """A directory where a database was expected is a configuration error."""
    directory = tmp_path / "not-a-database"
    directory.mkdir()

    with pytest.raises(OperationError) as caught:
        _backup(directory, destination)

    assert caught.value.code is ErrorCode.BACKUP_SOURCE_UNAVAILABLE


def test_an_in_memory_source_is_refused(
    destination: Path, tmp_path: Path
) -> None:
    """There is nothing persistent to back up."""
    with pytest.raises(OperationError) as caught:
        _backup(
            Path(":memory:"),
            destination,
            _configuration_beside(tmp_path / "placeholder"),
        )

    assert caught.value.code is ErrorCode.BACKUP_SOURCE_UNAVAILABLE


def test_an_unwritable_destination_is_reported(
    source_database: Path, tmp_path: Path
) -> None:
    """A destination that cannot be created must fail before any work."""
    blocker = tmp_path / "blocked"
    blocker.write_text("I am a file, not a directory", encoding="utf-8")

    with pytest.raises(OperationError) as caught:
        _backup(source_database, blocker)

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
        _backup(source_database, destination)

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
        _backup(link, destination)

    assert caught.value.code is ErrorCode.BACKUP_SOURCE_UNAVAILABLE
    assert "symbolic link" in caught.value.message


def test_paths_containing_spaces_are_handled(tmp_path: Path) -> None:
    """Deployment paths are quoted, but the Python must cope regardless."""
    source = tmp_path / "a directory with spaces" / "mgo.db"
    apply_migrations(source)
    _insert_observation(source, "spaced")
    target = tmp_path / "backup output dir"

    result = _backup(source, target)

    assert result.backup_path.is_file()
    assert _row_count(result.backup_path, "observations") == 1


def test_unicode_in_row_content_does_not_break_a_backup(tmp_path: Path) -> None:
    """Observation summaries are free text and may be non-ASCII."""
    source = tmp_path / "unicode" / "mgo.db"
    apply_migrations(source)
    _insert_observation(source, "café — 日本語 🐦")
    target = tmp_path / "backups"

    result = _backup(source, target)

    assert "café — 日本語 🐦" in _summaries(result.backup_path)
    # ...and none of it reaches the manifest.
    assert "café" not in result.manifest_path.read_text(encoding="utf-8")


@POSIX_ONLY
def test_published_files_are_not_world_readable(
    source_database: Path, destination: Path
) -> None:
    """A backup holds the whole observation history; it is not public."""
    result = _backup(source_database, destination)

    for path in (result.backup_path, result.manifest_path):
        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode == BACKUP_FILE_MODE
        assert not mode & 0o007


# --- retention --------------------------------------------------------------


def _fabricate_set(directory: Path, stamp: str) -> tuple[Path, Path, Path]:
    """Create a name-shaped three-file set without running a backup.

    Retention and listing recognise a set by its *filenames*, so placeholder
    contents are enough to exercise them — and using placeholders keeps these
    tests fast and independent of SQLite.
    """
    backup = directory / f"mgo-{stamp}.db"
    configuration = directory / f"mgo-{stamp}.config.toml"
    manifest = directory / f"mgo-{stamp}.manifest.json"
    backup.write_bytes(b"placeholder")
    configuration.write_text("# placeholder\n", encoding="utf-8")
    manifest.write_text("{}", encoding="utf-8")
    return backup, configuration, manifest


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
    assert len(result.removed) == 9  # three sets, three files each


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

    result = _backup(source_database, destination, keep=1)

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
        _backup(source_database, destination, keep=1)

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
            _backup(source_database, destination)
    finally:
        holder.release()

    assert caught.value.code is ErrorCode.BACKUP_LOCKED


def test_the_lock_is_released_after_a_backup(
    source_database: Path, destination: Path
) -> None:
    """Two sequential backups must both succeed."""
    _backup(source_database, destination)

    assert not (destination / LOCK_FILENAME).exists()

    later = datetime.now(UTC).replace(microsecond=0)
    second = _backup(
        source_database, destination, now=later.replace(year=later.year + 1)
    )
    assert second.backup_path.is_file()


def test_the_lock_is_released_after_a_failed_backup(
    source_database: Path, destination: Path
) -> None:
    """A crashed backup must not lock out the next scheduled run."""
    _corrupt(source_database)

    with pytest.raises(OperationError):
        _backup(source_database, destination)

    assert not (destination / LOCK_FILENAME).exists()


def test_the_lock_file_is_not_mistaken_for_a_backup(
    source_database: Path, destination: Path
) -> None:
    """Retention must never remove the lock guarding it."""
    _backup(source_database, destination)
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
    _backup(source_database, destination)

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
        "set_complete",
        "manifest_structure",
        "filenames",
        "size",
        "sha256",
        "integrity",
        "schema",
        "expected_tables",
        "journal_mode",
        "row_counts",
    ):
        assert result.checks[check] == "ok", check


def test_verification_is_read_only(
    source_database: Path, destination: Path
) -> None:
    """Verification must never repair, re-checksum or rewrite anything."""
    backup = _make_backup(source_database, destination)
    manifest = manifest_path_for(backup)
    configuration = configuration_path_for(backup)
    before = (
        file_sha256(backup),
        manifest.read_bytes(),
        configuration.read_bytes(),
    )

    verify_backup(backup)

    assert (
        file_sha256(backup),
        manifest.read_bytes(),
        configuration.read_bytes(),
    ) == before
    assert sorted(item.name for item in destination.iterdir()) == sorted(
        [backup.name, configuration.name, manifest.name]
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
    assert result.error_code is ErrorCode.BACKUP_SET_INCOMPLETE


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
    configuration = configuration_path_for(target)
    configuration.write_text(CONFIGURATION_TEXT, encoding="utf-8")
    manifest_path_for(target).write_text(
        json.dumps(
            _manifest_body(
                target,
                configuration,
                schema_version=1,
                table_row_counts={
                    "schema_migrations": 1,
                    "observations": 0,
                    "captures": 0,
                },
            )
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


#: The production locations a restore test must never write into.
PROTECTED_RESTORE_TARGETS = [
    "/var/lib/garden-observatory/db",
    "/var/lib/garden-observatory/db/nested",
    "/var/lib/garden-observatory",
]


class ProtectedTargetWatch:
    """Record whether a protected path is created or stat-ed, without doing it.

    The point of this class is what it *refuses to do*. The obvious way to
    prove "the refusal happened before the directory was created" is to look at
    the directory before and after -- which is what this test used to do, and
    which fails on a correctly configured Raspberry Pi. ``/var/lib/garden-
    observatory`` is owned ``mgo:mgo`` with mode ``0750``, the suite runs as an
    unprivileged account, and so ``Path.exists()`` raises ``PermissionError``
    (``EACCES`` is not in pathlib's ignored-errno set) before the guard is ever
    reached. The restrictive permissions are correct and desirable; the test was
    wrong to depend on being able to see through them.

    The proof is therefore behavioural rather than observational: intercept
    ``mkdir`` and ``exists`` for the protected path only, and assert neither was
    ever called for it. A test of "this code never touches production" must not
    itself touch production to find out.
    """

    def __init__(self, protected: Path) -> None:
        self.protected = protected
        self.created: list[Path] = []
        self.probed: list[Path] = []

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Patch ``Path`` -- only ever *after* the recovery set exists.

        The replacements are plain functions rather than bound methods on
        purpose. A bound method is not a descriptor, so assigning one to
        ``Path.exists`` would make ``some_path.exists()`` call it with **no**
        arguments -- including from pytest's own traceback machinery, which
        turns any genuine failure here into an unreadable INTERNALERROR.
        """
        original_mkdir = Path.mkdir
        original_exists = Path.exists
        watch = self

        def guarded_mkdir(path: Path, *args: Any, **kwargs: Any) -> None:
            if path == watch.protected:
                watch.created.append(path)
                raise AssertionError(
                    f"restore_test tried to create the protected {path}"
                )
            original_mkdir(path, *args, **kwargs)

        def guarded_exists(path: Path, *args: Any, **kwargs: Any) -> bool:
            if path == watch.protected:
                watch.probed.append(path)
                # Exactly what the Pi does, made deterministic: if anything
                # depends on stat-ing a protected path, this surfaces it
                # everywhere rather than only on a locked-down host.
                raise PermissionError(errno.EACCES, "Permission denied")
            return original_exists(path, *args, **kwargs)

        monkeypatch.setattr(Path, "mkdir", guarded_mkdir)
        monkeypatch.setattr(Path, "exists", guarded_exists)

    def assert_untouched(self) -> None:
        """Assert the protected path was neither created nor inspected."""
        assert self.created == [], f"{self.protected} was created"
        assert self.probed == [], f"{self.protected} was stat-ed"


@pytest.mark.parametrize("target", PROTECTED_RESTORE_TARGETS)
def test_a_production_data_location_is_refused_as_a_restore_target(
    source_database: Path,
    destination: Path,
    target: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A restore test writing to the live database directory is unthinkable.

    The refusal must happen *before* the directory is created, and the proof of
    that must not require permission to look at the directory -- see
    :class:`ProtectedTargetWatch`. The recovery set is built before the patches
    are installed so that ordinary temporary-directory work is unaffected.
    """
    backup = _make_backup(source_database, destination)
    protected = Path(target)
    watch = ProtectedTargetWatch(protected)
    watch.install(monkeypatch)

    with pytest.raises(OperationError) as caught:
        restore_test(backup, work_directory=protected)

    assert caught.value.code is ErrorCode.RESTORE_TARGET_REJECTED
    assert protected not in watch.created
    watch.assert_untouched()


@pytest.mark.parametrize("target", PROTECTED_RESTORE_TARGETS)
def test_a_protected_target_is_refused_when_it_cannot_be_resolved(
    source_database: Path,
    destination: Path,
    target: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_comparable`` falls back to the lexical path when ``resolve`` fails.

    That ``except OSError`` branch exists precisely for a locked-down host, and
    an unexercised fallback in a refusal guard is the worst place to have one:
    if it were wrong, the guard would let a protected target through on exactly
    the machine whose permissions were strictest. Simulating ``EACCES`` from
    ``Path.resolve()`` makes the branch deterministic on both platforms.
    """
    backup = _make_backup(source_database, destination)
    protected = Path(target)
    watch = ProtectedTargetWatch(protected)
    resolved: list[Path] = []
    original_resolve = Path.resolve

    def guarded_resolve(path: Path, *args: Any, **kwargs: Any) -> Path:
        if path == protected:
            resolved.append(path)
            raise PermissionError(errno.EACCES, "Permission denied")
        return original_resolve(path, *args, **kwargs)

    watch.install(monkeypatch)
    monkeypatch.setattr(Path, "resolve", guarded_resolve)

    with pytest.raises(OperationError) as caught:
        restore_test(backup, work_directory=protected)

    assert caught.value.code is ErrorCode.RESTORE_TARGET_REJECTED
    # The fallback was genuinely taken, not skipped past.
    assert resolved == [protected]
    assert protected not in watch.created
    watch.assert_untouched()


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
    # Corruption is caught by the set verification that now runs first, so
    # nothing is ever copied.
    assert result.error_code is ErrorCode.BACKUP_CHECKSUM_MISMATCH
    assert "failed verification" in result.detail


def test_a_restore_test_of_a_missing_backup_fails(tmp_path: Path) -> None:
    """A clear result rather than an exception out of the CLI."""
    result = restore_test(tmp_path / "absent.db")

    assert not result.ok
    assert result.error_code is ErrorCode.BACKUP_NOT_FOUND


# --- regression: the configuration is part of every recovery set ------------
#
# The authoritative requirement is "back up the database and production
# configuration". The first implementation backed up only the database, so a
# restore would have recovered the observation history onto a machine whose
# configuration was gone. These tests exist so that omission cannot return.


def test_a_recovery_set_contains_three_files(
    source_database: Path, source_configuration: Path, destination: Path
) -> None:
    """Database, configuration and manifest -- not two of the three."""
    result = _backup(source_database, destination, source_configuration)

    assert result.backup_path.is_file()
    assert result.configuration_path.is_file()
    assert result.manifest_path.is_file()
    assert len(list(destination.iterdir())) == 3


def test_the_configuration_snapshot_is_byte_identical(
    source_database: Path, source_configuration: Path, destination: Path
) -> None:
    """Exact bytes, never a parse-and-rewrite.

    A configuration round-tripped through a TOML parser would lose its comments
    and its ordering, and a restore would hand the operator something merely
    equivalent rather than identical.
    """
    result = _backup(source_database, destination, source_configuration)

    assert result.configuration_path.read_bytes() == (
        source_configuration.read_bytes()
    )
    text = result.configuration_path.read_text(encoding="utf-8")
    assert text.startswith("# Matt's Garden Observatory")
    assert "\n\n" in text, "blank lines must survive"


def test_the_manifest_records_the_configuration(
    source_database: Path, source_configuration: Path, destination: Path
) -> None:
    """Four configuration fields, describing the snapshot on disk."""
    result = _backup(source_database, destination, source_configuration)
    payload = json.loads(result.manifest_path.read_text(encoding="utf-8"))

    assert payload["configuration_source_name"] == "mgo.toml"
    assert payload["configuration_filename"] == result.configuration_path.name
    assert payload["configuration_size_bytes"] == (
        result.configuration_path.stat().st_size
    )
    assert payload["configuration_sha256"] == file_sha256(result.configuration_path)


def test_the_configuration_source_name_is_a_basename(
    source_database: Path, source_configuration: Path, destination: Path
) -> None:
    """A manifest must leak no filesystem layout, for either artefact."""
    result = _backup(source_database, destination, source_configuration)
    text = result.manifest_path.read_text(encoding="utf-8")

    assert str(source_configuration.parent) not in text
    assert str(source_configuration) not in text


def test_configuration_content_never_reaches_the_manifest(
    source_database: Path, source_configuration: Path, destination: Path
) -> None:
    """The configuration may hold credentials; only its checksum is recorded."""
    result = _backup(source_database, destination, source_configuration)

    assert "SECRET-TOKEN-MUST-NOT-LEAK" not in result.manifest_path.read_text(
        encoding="utf-8"
    )


def test_configuration_content_never_reaches_the_event_stream(
    source_database: Path, source_configuration: Path, destination: Path
) -> None:
    """Events go to the journal, which is read by more people than the file."""
    stream = io.StringIO()

    _backup(
        source_database,
        destination,
        source_configuration,
        emitter=EventEmitter("mgo-backup", stream=stream),
    )

    assert "SECRET-TOKEN-MUST-NOT-LEAK" not in stream.getvalue()
    assert "bot_token" not in stream.getvalue()
    # ...while the safe descriptors are reported.
    assert "configuration_sha256" in stream.getvalue()


def test_configuration_content_never_reaches_the_command_summary(
    source_database: Path, source_configuration: Path, destination: Path
) -> None:
    """The JSON summary is what an operator pipes into another tool."""
    result = _backup(source_database, destination, source_configuration)

    assert "SECRET-TOKEN-MUST-NOT-LEAK" not in json.dumps(result.as_dict())


def test_a_missing_configuration_fails_the_backup(
    source_database: Path, destination: Path, tmp_path: Path
) -> None:
    """A set without the configuration is incomplete, so nothing is published."""
    with pytest.raises(OperationError) as caught:
        _backup(source_database, destination, tmp_path / "absent.toml")

    assert caught.value.code is ErrorCode.BACKUP_CONFIGURATION_UNAVAILABLE
    assert list(destination.iterdir()) == []


def test_a_configuration_directory_is_refused(
    source_database: Path, destination: Path, tmp_path: Path
) -> None:
    """A path that is not a regular file cannot be a configuration."""
    directory = tmp_path / "config-dir"
    directory.mkdir()

    with pytest.raises(OperationError) as caught:
        _backup(source_database, destination, directory)

    assert caught.value.code is ErrorCode.BACKUP_CONFIGURATION_UNAVAILABLE


def test_a_symlinked_configuration_is_refused(
    source_database: Path, source_configuration: Path, destination: Path, tmp_path: Path
) -> None:
    """The link target could be changed between unattended runs."""
    link = tmp_path / "linked.toml"
    try:
        link.symlink_to(source_configuration)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not creatable in this environment")

    with pytest.raises(OperationError) as caught:
        _backup(source_database, destination, link)

    assert caught.value.code is ErrorCode.BACKUP_CONFIGURATION_UNAVAILABLE
    assert "symbolic link" in caught.value.message


def test_an_oversized_configuration_is_refused(
    source_database: Path, destination: Path, tmp_path: Path
) -> None:
    """An unattended job must not read an unbounded file into memory."""
    oversized = tmp_path / "huge.toml"
    oversized.write_bytes(b"x" * (MAX_CONFIGURATION_BYTES + 1))

    with pytest.raises(OperationError) as caught:
        _backup(source_database, destination, oversized)

    assert caught.value.code is ErrorCode.BACKUP_CONFIGURATION_UNAVAILABLE
    assert list(destination.iterdir()) == []


def test_a_configuration_at_the_size_limit_is_accepted(
    source_database: Path, destination: Path, tmp_path: Path
) -> None:
    """The bound is inclusive; an off-by-one would reject a legal file.

    Padded with a comment rather than arbitrary bytes: the configuration is now
    parsed as part of capture, so a file at the limit must still be loadable.
    """
    limit = tmp_path / "exact.toml"
    body = CONFIGURATION_TEXT.encode("utf-8")
    padding = MAX_CONFIGURATION_BYTES - len(body) - len("\n# \n")
    limit.write_bytes(body + b"\n# " + (b"x" * padding) + b"\n")

    assert limit.stat().st_size == MAX_CONFIGURATION_BYTES

    result = _backup(source_database, destination, limit)

    assert result.manifest.configuration_size_bytes == MAX_CONFIGURATION_BYTES


def test_a_configuration_changed_during_the_copy_is_refused(
    source_database: Path, destination: Path, tmp_path: Path
) -> None:
    """A snapshot must be one file, not half the old one and half the new."""
    changing = tmp_path / "changing.toml"
    changing.write_text(CONFIGURATION_TEXT, encoding="utf-8")

    real_fstat = os.fstat
    calls = {"count": 0}

    def shifting(fileno: int) -> os.stat_result:
        result = real_fstat(fileno)
        calls["count"] += 1
        if calls["count"] > 1:
            # The second fstat is the after-read check; report a changed file.
            return os.stat_result(
                (
                    result.st_mode,
                    result.st_ino,
                    result.st_dev,
                    result.st_nlink,
                    result.st_uid,
                    result.st_gid,
                    result.st_size + 10,
                    result.st_atime,
                    result.st_mtime + 5,
                    result.st_ctime,
                )
            )
        return result

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(os, "fstat", shifting)
        with pytest.raises(OperationError) as caught:
            _backup(source_database, destination, changing)

    assert caught.value.code is ErrorCode.BACKUP_CONFIGURATION_UNAVAILABLE
    assert "changed while it was being read" in caught.value.message


@POSIX_ONLY
def test_the_configuration_snapshot_is_not_world_readable(
    source_database: Path, source_configuration: Path, destination: Path
) -> None:
    """It may contain credentials; it is the most sensitive file in the set."""
    result = _backup(source_database, destination, source_configuration)

    mode = stat.S_IMODE(result.configuration_path.stat().st_mode)
    assert mode == BACKUP_FILE_MODE
    assert not mode & 0o007


# --- regression: publication is all-or-nothing ------------------------------


def test_a_failed_manifest_write_removes_the_published_recovery_files(
    source_database: Path,
    source_configuration: Path,
    destination: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No manifest may survive claiming a set that is not there.

    The manifest is the completion marker, so a set whose manifest failed must
    leave nothing behind that looks restorable.
    """
    import mgo.operations.backup as backup_module

    def refuse(target: Path, manifest: object) -> None:
        raise OperationError(
            ErrorCode.BACKUP_MANIFEST_FAILED, "manifest write refused"
        )

    monkeypatch.setattr(backup_module, "_write_manifest", refuse)

    with pytest.raises(OperationError) as caught:
        _backup(source_database, destination, source_configuration)

    assert caught.value.code is ErrorCode.BACKUP_MANIFEST_FAILED
    assert list(destination.iterdir()) == []


def test_a_failed_configuration_write_removes_the_published_database(
    source_database: Path,
    source_configuration: Path,
    destination: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A database snapshot with no configuration is not a recovery set."""
    import mgo.operations.backup as backup_module

    def refuse(target: Path, payload: bytes) -> None:
        raise OperationError(
            ErrorCode.BACKUP_CONFIGURATION_UNAVAILABLE, "config write refused"
        )

    monkeypatch.setattr(backup_module, "_write_configuration_snapshot", refuse)

    with pytest.raises(OperationError):
        _backup(source_database, destination, source_configuration)

    assert list(destination.iterdir()) == []


def test_a_rollback_failure_is_reported_rather_than_hidden(
    source_database: Path,
    source_configuration: Path,
    destination: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The operator must be told a stray file was left behind."""
    import mgo.operations.backup as backup_module

    def refuse_manifest(target: Path, manifest: object) -> None:
        raise OperationError(ErrorCode.BACKUP_MANIFEST_FAILED, "refused")

    real_unlink = Path.unlink

    def refuse_unlink(self: Path, *args: object, **kwargs: object) -> None:
        if self.suffix == ".db":
            raise OSError(13, "Permission denied")
        real_unlink(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(backup_module, "_write_manifest", refuse_manifest)
    monkeypatch.setattr(Path, "unlink", refuse_unlink)

    stream = io.StringIO()
    with pytest.raises(OperationError):
        _backup(
            source_database,
            destination,
            source_configuration,
            emitter=EventEmitter("mgo-backup", stream=stream),
        )

    events = [json.loads(line) for line in stream.getvalue().splitlines()]
    rollback = [e for e in events if e["event_id"] == "backup.rollback_failed"]
    assert rollback, "a failed cleanup must be reported"
    assert rollback[0]["error_code"] == "BACKUP_SET_INCOMPLETE"


# --- regression: manifest structural validation -----------------------------
#
# The first implementation coerced fields with str()/int() and caught only
# KeyError/TypeError/ValueError, so a manifest could be "parseable" while being
# meaningless -- booleans as sizes, negative versions, a checksum that was not a
# checksum, a filename that was a path. Each case below is now refused.


def _valid_body(destination: Path) -> dict[str, Any]:
    """A structurally valid manifest body, for mutation by the tests below."""
    backup = destination / "mgo-20260101T000000Z.db"
    configuration = destination / "mgo-20260101T000000Z.config.toml"
    backup.write_bytes(b"placeholder")
    configuration.write_text("x = 1\n", encoding="utf-8")
    return _manifest_body(backup, configuration)


def test_a_valid_manifest_body_parses(destination: Path) -> None:
    """The baseline the mutation tests below depart from."""
    manifest = BackupManifest.from_dict(_valid_body(destination))

    assert manifest.format_version == BACKUP_FORMAT_VERSION
    assert manifest.table_row_counts.keys() == set(EXPECTED_TABLES)


@pytest.mark.parametrize(
    "field",
    [
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
        "configuration_source_name",
        "configuration_filename",
        "configuration_size_bytes",
        "configuration_sha256",
    ],
)
def test_every_required_manifest_field_is_required(
    destination: Path, field: str
) -> None:
    """Each field is load-bearing for a restore decision."""
    body = _valid_body(destination)
    del body[field]

    with pytest.raises(OperationError) as caught:
        BackupManifest.from_dict(body)

    assert caught.value.code is ErrorCode.BACKUP_MANIFEST_FAILED
    assert field in caught.value.message


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("backup_size_bytes", True),
        ("configuration_size_bytes", False),
        ("schema_version", True),
        ("format_version", True),
    ],
)
def test_booleans_are_refused_where_integers_belong(
    destination: Path, field: str, value: object
) -> None:
    """``bool`` subclasses ``int``, so an unchecked field would accept ``true``."""
    body = _valid_body(destination)
    body[field] = value

    with pytest.raises(OperationError) as caught:
        BackupManifest.from_dict(body)

    assert caught.value.code is ErrorCode.BACKUP_MANIFEST_FAILED


@pytest.mark.parametrize(
    "field",
    [
        "backup_size_bytes",
        "configuration_size_bytes",
        "schema_version",
        "expected_schema_version",
    ],
)
def test_negative_numbers_are_refused(destination: Path, field: str) -> None:
    """A negative size or version describes nothing that can exist."""
    body = _valid_body(destination)
    body[field] = -1

    with pytest.raises(OperationError) as caught:
        BackupManifest.from_dict(body)

    assert caught.value.code is ErrorCode.BACKUP_MANIFEST_FAILED


@pytest.mark.parametrize("field", ["sha256", "configuration_sha256"])
@pytest.mark.parametrize(
    "value",
    ["", "not-a-digest", "ABCDEF" * 10, "a" * 63, "a" * 65, "z" * 64],
)
def test_malformed_checksums_are_refused(
    destination: Path, field: str, value: str
) -> None:
    """A field a later comparison treats as a checksum must look like one."""
    body = _valid_body(destination)
    body[field] = value

    with pytest.raises(OperationError) as caught:
        BackupManifest.from_dict(body)

    assert caught.value.code is ErrorCode.BACKUP_MANIFEST_FAILED


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("backup_filename", "/etc/passwd"),
        ("backup_filename", "../mgo-20260101T000000Z.db"),
        ("backup_filename", "sub/mgo-20260101T000000Z.db"),
        ("backup_filename", "C:\\mgo-20260101T000000Z.db"),
        ("backup_filename", "not-a-backup.db"),
        ("configuration_filename", "/etc/garden-observatory/mgo.toml"),
        ("configuration_filename", "../mgo-20260101T000000Z.config.toml"),
        ("configuration_filename", "arbitrary.toml"),
        ("source_database_name", "/var/lib/garden-observatory/db/mgo.db"),
        ("source_database_name", "db/mgo.db"),
        ("source_database_name", ".."),
        ("configuration_source_name", "/etc/garden-observatory/mgo.toml"),
    ],
)
def test_path_like_name_fields_are_refused(
    destination: Path, field: str, value: str
) -> None:
    """A description must not be usable as a traversal."""
    body = _valid_body(destination)
    body[field] = value

    with pytest.raises(OperationError) as caught:
        BackupManifest.from_dict(body)

    assert caught.value.code is ErrorCode.BACKUP_MANIFEST_FAILED


@pytest.mark.parametrize("value", ["", "not a timestamp", "2026-13-45"])
def test_an_invalid_created_at_is_refused(destination: Path, value: str) -> None:
    """A timestamp that cannot be parsed cannot order a set of backups."""
    body = _valid_body(destination)
    body["created_at"] = value

    with pytest.raises(OperationError) as caught:
        BackupManifest.from_dict(body)

    assert caught.value.code is ErrorCode.BACKUP_MANIFEST_FAILED


@pytest.mark.parametrize("version", [0, 2, 99])
def test_only_the_exact_supported_format_version_is_accepted(
    destination: Path, version: int
) -> None:
    """No compatibility policy is implemented, so none is pretended."""
    body = _valid_body(destination)
    body["format_version"] = version

    with pytest.raises(OperationError) as caught:
        BackupManifest.from_dict(body)

    assert caught.value.code is ErrorCode.BACKUP_MANIFEST_FAILED


@pytest.mark.parametrize(
    "counts",
    [
        {},
        {"observations": 1},
        {"schema_migrations": 2, "observations": 1},
        {"schema_migrations": 2, "observations": 1, "captures": 0, "extra": 3},
        {"schema_migrations": 2, "observations": -1, "captures": 0},
        {"schema_migrations": 2, "observations": True, "captures": 0},
    ],
)
def test_row_counts_must_cover_exactly_the_expected_tables(
    destination: Path, counts: dict[str, object]
) -> None:
    """An empty or partial map would have verified against any database at all."""
    body = _valid_body(destination)
    body["table_row_counts"] = counts

    with pytest.raises(OperationError) as caught:
        BackupManifest.from_dict(body)

    assert caught.value.code is ErrorCode.BACKUP_MANIFEST_FAILED


def test_a_non_object_manifest_is_refused(destination: Path) -> None:
    """A JSON array or scalar is not a manifest."""
    for payload in ([], "text", 5, None):
        with pytest.raises(OperationError):
            BackupManifest.from_dict(payload)


# --- regression: verification binds the manifest to the artefacts ------------
#
# Structural validity is not enough: a tidy manifest that describes a different
# set must not verify. Each test below leaves the manifest internally valid and
# changes exactly one value so that it no longer matches reality.


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        (
            "backup_filename",
            "mgo-20200101T000000Z.db",
            ErrorCode.BACKUP_SET_INCOMPLETE,
        ),
        (
            "configuration_filename",
            "mgo-20200101T000000Z.config.toml",
            ErrorCode.BACKUP_SET_INCOMPLETE,
        ),
        ("backup_size_bytes", 12, ErrorCode.BACKUP_CHECKSUM_MISMATCH),
        ("configuration_size_bytes", 12, ErrorCode.BACKUP_CHECKSUM_MISMATCH),
        ("sha256", "b" * 64, ErrorCode.BACKUP_CHECKSUM_MISMATCH),
        ("configuration_sha256", "c" * 64, ErrorCode.BACKUP_CHECKSUM_MISMATCH),
        ("schema_version", 1, ErrorCode.BACKUP_SCHEMA_INCOMPATIBLE),
        ("expected_schema_version", 99, ErrorCode.BACKUP_SCHEMA_INCOMPATIBLE),
        ("integrity", "suspicious", ErrorCode.BACKUP_INTEGRITY_FAILED),
        ("journal_mode_of_backup", "wal", ErrorCode.BACKUP_INTEGRITY_FAILED),
    ],
)
def test_a_manifest_that_describes_a_different_set_fails_verification(
    source_database: Path,
    destination: Path,
    field: str,
    value: object,
    code: ErrorCode,
) -> None:
    """Every recorded value is compared against the artefact it describes."""
    backup = _make_backup(source_database, destination)
    _rewrite_manifest(backup, **{field: value})

    result = verify_backup(backup)

    assert not result.ok
    assert result.error_code is code


def test_a_partial_row_count_manifest_fails_verification(
    source_database: Path, destination: Path
) -> None:
    """The defect this correction exists for: an empty map verified anything."""
    backup = _make_backup(source_database, destination)
    _rewrite_manifest(backup, table_row_counts={})

    result = verify_backup(backup)

    assert not result.ok
    assert result.error_code is ErrorCode.BACKUP_MANIFEST_FAILED


def test_a_missing_configuration_snapshot_fails_verification(
    source_database: Path, destination: Path
) -> None:
    """Two of three files is not a recovery set."""
    backup = _make_backup(source_database, destination)
    configuration_path_for(backup).unlink()

    result = verify_backup(backup)

    assert not result.ok
    assert result.error_code is ErrorCode.BACKUP_SET_INCOMPLETE


def test_a_changed_configuration_snapshot_fails_verification(
    source_database: Path, destination: Path
) -> None:
    """The configuration is checksummed exactly as the database is."""
    backup = _make_backup(source_database, destination)
    configuration_path_for(backup).write_text("tampered = true\n", encoding="utf-8")

    result = verify_backup(backup)

    assert not result.ok
    assert result.error_code is ErrorCode.BACKUP_CHECKSUM_MISMATCH


def test_a_foreign_filename_is_not_verified_as_a_backup(
    destination: Path
) -> None:
    """Only names this tooling produces are part of a recovery set."""
    stray = destination / "some-other-database.db"
    stray.write_bytes(b"placeholder")

    result = verify_backup(stray)

    assert not result.ok
    assert result.error_code is ErrorCode.BACKUP_NOT_FOUND


# --- regression: restore-test requires a complete verified set ---------------


def test_restore_test_requires_a_manifest(
    source_database: Path, destination: Path
) -> None:
    """The "skipped (no manifest)" path must no longer exist."""
    backup = _make_backup(source_database, destination)
    manifest_path_for(backup).unlink()

    result = restore_test(backup)

    assert not result.ok
    assert result.error_code is ErrorCode.BACKUP_SET_INCOMPLETE
    assert "skipped" not in json.dumps(result.as_dict())


def test_restore_test_requires_a_configuration_snapshot(
    source_database: Path, destination: Path
) -> None:
    """A recovery rehearsal must rehearse the whole recovery."""
    backup = _make_backup(source_database, destination)
    configuration_path_for(backup).unlink()

    result = restore_test(backup)

    assert not result.ok
    assert result.error_code is ErrorCode.BACKUP_SET_INCOMPLETE


def test_restore_test_never_reports_a_skipped_row_count_check(
    source_database: Path, destination: Path
) -> None:
    """Its most important assertion may never be quietly skipped."""
    backup = _make_backup(source_database, destination)

    result = restore_test(backup)

    assert result.checks["row_counts"] == "ok"
    assert "skipped" not in json.dumps(result.checks)


def test_restore_test_copies_and_checks_the_configuration(
    source_database: Path, source_configuration: Path, destination: Path, tmp_path: Path
) -> None:
    """Both artefacts are restored; the configuration is checksummed."""
    backup = _make_backup(source_database, destination)
    work = tmp_path / "rehearsal"

    result = restore_test(backup, work_directory=work, preserve=True)

    assert result.ok
    assert result.checks["restored_configuration"] == "ok"
    assert (work / "restored.db").is_file()
    assert (work / "restored-mgo.toml").is_file()
    assert (work / "restored-mgo.toml").read_bytes() == (
        configuration_path_for(backup).read_bytes()
    )


def test_restore_test_does_not_activate_the_restored_configuration(
    source_database: Path, destination: Path, tmp_path: Path
) -> None:
    """It is checked, never applied: nothing is pointed at it."""
    backup = _make_backup(source_database, destination)
    work = tmp_path / "rehearsal"

    before = os.environ.get("MGO_CONFIG_PATH")
    result = restore_test(backup, work_directory=work, preserve=True)

    assert result.ok
    assert os.environ.get("MGO_CONFIG_PATH") == before
    # Only the two restored files exist; nothing was migrated or initialised.
    assert sorted(item.name for item in work.iterdir()) == [
        "restored-mgo.toml",
        "restored.db",
    ]


@pytest.mark.parametrize("existing", ["restored.db", "restored-mgo.toml"])
def test_restore_test_refuses_to_overwrite_an_existing_target(
    source_database: Path, destination: Path, tmp_path: Path, existing: str
) -> None:
    """An operator-supplied directory may hold anything; never clobber it."""
    backup = _make_backup(source_database, destination)
    work = tmp_path / "occupied"
    work.mkdir()
    (work / existing).write_text("do not destroy me", encoding="utf-8")

    with pytest.raises(OperationError) as caught:
        restore_test(backup, work_directory=work)

    assert caught.value.code is ErrorCode.RESTORE_TARGET_EXISTS
    assert (work / existing).read_text(encoding="utf-8") == "do not destroy me"


def test_restore_test_cleans_up_both_artefacts(
    source_database: Path, destination: Path, tmp_path: Path
) -> None:
    """Neither restored file may be left behind in a caller's directory."""
    backup = _make_backup(source_database, destination)
    work = tmp_path / "rehearsal"

    result = restore_test(backup, work_directory=work)

    assert result.ok
    assert list(work.iterdir()) == []


# --- regression: three-file listing and retention ---------------------------


def test_listing_requires_all_three_files(destination: Path) -> None:
    """A two-file remnant is an orphan set, not a backup."""
    _fabricate_set(destination, "20260103T000000Z")
    (destination / "mgo-20260102T000000Z.db").write_bytes(b"x")
    (destination / "mgo-20260102T000000Z.manifest.json").write_text(
        "{}", encoding="utf-8"
    )

    listing = list_backups(destination)

    assert [item.timestamp for item in listing.sets] == ["20260103T000000Z"]
    assert listing.orphan_backups == ("mgo-20260102T000000Z.db",)
    assert listing.orphan_manifests == ("mgo-20260102T000000Z.manifest.json",)


def test_listing_reports_orphan_configurations(destination: Path) -> None:
    """A configuration with no database is its own diagnosable state."""
    (destination / "mgo-20260101T000000Z.config.toml").write_text(
        "x = 1\n", encoding="utf-8"
    )

    listing = list_backups(destination)

    assert listing.sets == ()
    assert listing.orphan_configurations == ("mgo-20260101T000000Z.config.toml",)


def test_retention_removes_all_three_files(destination: Path) -> None:
    """A set is removed whole, or its remains are recognisable orphans."""
    for stamp in ("20260101T000000Z", "20260102T000000Z"):
        _fabricate_set(destination, stamp)

    apply_retention(destination, keep=1)

    for suffix in (".db", ".config.toml", ".manifest.json"):
        assert not (destination / f"mgo-20260101T000000Z{suffix}").exists()
    for suffix in (".db", ".config.toml", ".manifest.json"):
        assert (destination / f"mgo-20260102T000000Z{suffix}").exists()


def test_retention_removes_the_manifest_first(
    destination: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Interrupted deletion must not leave a manifest advertising a gone set."""
    for stamp in ("20260101T000000Z", "20260102T000000Z"):
        _fabricate_set(destination, stamp)

    order: list[str] = []
    real_unlink = Path.unlink

    def record(self: Path, *args: object, **kwargs: object) -> None:
        order.append(self.name)
        real_unlink(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "unlink", record)
    apply_retention(destination, keep=1)

    assert order[0] == "mgo-20260101T000000Z.manifest.json"


def test_retention_reports_a_partial_deletion(
    destination: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A half-removed set must be reported, not silently left behind."""
    for stamp in ("20260101T000000Z", "20260102T000000Z"):
        _fabricate_set(destination, stamp)

    real_unlink = Path.unlink

    def refuse_configuration(self: Path, *args: object, **kwargs: object) -> None:
        if self.name.endswith(".config.toml"):
            raise OSError(13, "Permission denied")
        real_unlink(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "unlink", refuse_configuration)
    result = apply_retention(destination, keep=1)

    assert not result.succeeded
    assert any(".config.toml" in failure for failure in result.failures)
    assert (destination / "mgo-20260101T000000Z.config.toml").exists()


def test_a_row_count_disagreement_fails_the_restore_test(
    source_database: Path, destination: Path
) -> None:
    """The restored copy is compared against the recorded counts."""
    backup = _make_backup(source_database, destination)
    _rewrite_manifest(
        backup,
        table_row_counts={
            "schema_migrations": 2,
            "observations": 4242,
            "captures": 0,
        },
    )

    result = restore_test(backup)

    assert not result.ok
    # Caught by the set verification that runs before anything is copied.
    assert result.error_code is ErrorCode.BACKUP_INTEGRITY_FAILED
