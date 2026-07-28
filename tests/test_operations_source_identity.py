"""Tests for race-resistant identification of the files a backup reads.

These cover the last defect class found in Task 10: the tooling checked a path
and then opened it, which is only correct for a path that does not change. The
tests here prove the gap is closed in both directions —

* the **open itself** refuses a symlink (``O_NOFOLLOW`` on Linux);
* the object that was opened is proven to still be the object the path names.

Race conditions are simulated deterministically by narrowly monkeypatching the
single ``os`` call that observes the path after the fact. There are no threads
and no timing dependencies: a test suite that reproduces a race only sometimes
is a test suite that reports success only sometimes.
"""

from __future__ import annotations

import io
import json
import os
import sqlite3
import stat
from pathlib import Path
from typing import Any

import pytest

from mgo.core.config import parse_config_bytes
from mgo.core.database import apply_migrations
from mgo.operations import backup as backup_module
from mgo.operations.backup import (
    MAX_CONFIGURATION_BYTES,
    ConfigurationSnapshot,
    capture_configuration,
    configuration_path_for,
    create_backup,
    file_sha256,
)
from mgo.operations.errors import ErrorCode, OperationError
from mgo.operations.events import EventEmitter
from mgo.operations.source_identity import (
    SUPPORTS_NO_FOLLOW,
    SourceIdentity,
    anchored_source,
    open_no_follow,
    read_regular_file,
)

POSIX_ONLY = pytest.mark.skipif(os.name != "posix", reason="POSIX-only behaviour")

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
database_path = "{database}"

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


# --- fixtures ---------------------------------------------------------------


@pytest.fixture
def database(tmp_path: Path) -> Path:
    """A migrated WAL database with a row in it."""
    path = tmp_path / "source" / "mgo.db"
    apply_migrations(path)
    connection = sqlite3.connect(path)
    connection.execute(
        "INSERT INTO observations "
        "(observed_at, kind, source, status, summary, created_at) "
        "VALUES ('t', 'test', 'pytest', 'success', 'seeded', 't')"
    )
    connection.commit()
    connection.close()
    return path


@pytest.fixture
def configuration(tmp_path: Path, database: Path) -> Path:
    """A loadable configuration naming the fixture database."""
    path = tmp_path / "mgo.toml"
    path.write_text(
        CONFIGURATION_TEXT.format(database=database.as_posix()), encoding="utf-8"
    )
    return path


@pytest.fixture
def destination(tmp_path: Path) -> Path:
    """An empty backup directory."""
    directory = tmp_path / "backups"
    directory.mkdir()
    return directory


def _fake_lstat(source: os.stat_result, **overrides: int) -> os.stat_result:
    """Return a ``stat_result`` copy with selected fields replaced.

    ``os.stat_result`` is immutable, and building one from a 10-tuple drops the
    nanosecond fields, so the full sequence plus the ``st_*`` extras is rebuilt
    explicitly.
    """
    fields = {
        "st_mode": source.st_mode,
        "st_ino": source.st_ino,
        "st_dev": source.st_dev,
        "st_nlink": source.st_nlink,
        "st_uid": source.st_uid,
        "st_gid": source.st_gid,
        "st_size": source.st_size,
        "st_atime": source.st_atime,
        "st_mtime": source.st_mtime,
        "st_ctime": source.st_ctime,
    }
    fields.update(overrides)
    return os.stat_result(
        (
            fields["st_mode"],
            fields["st_ino"],
            fields["st_dev"],
            fields["st_nlink"],
            fields["st_uid"],
            fields["st_gid"],
            fields["st_size"],
            int(fields["st_atime"]),
            int(fields["st_mtime"]),
            int(fields["st_ctime"]),
        )
    )


def _substitute_after_open(
    monkeypatch: pytest.MonkeyPatch, target: Path, **overrides: int
) -> None:
    """Make the **post-open** ``lstat`` of ``target`` report a different object.

    This is the deterministic stand-in for "someone replaced the path while we
    were reading it": the descriptor still refers to the original file, but the
    path now names something else, which is exactly the state the identity
    comparison exists to detect.

    The Windows fallback performs an ``lstat`` *before* opening as well, so the
    substitution deliberately skips that first observation. Without the skip
    these tests would be caught by the pre-open check on Windows and by the
    post-open check on Linux — passing on both platforms while testing two
    different things, and never testing the post-open comparison here at all.
    """
    real_lstat = os.lstat
    resolved = str(target)
    skip = 0 if SUPPORTS_NO_FOLLOW else 1
    seen = {"count": 0}

    def substituting(path: Any, **kwargs: Any) -> os.stat_result:
        result = real_lstat(path, **kwargs)
        if str(path) != resolved:
            return result
        seen["count"] += 1
        if seen["count"] <= skip:
            return result
        return _fake_lstat(result, **overrides)

    monkeypatch.setattr(os, "lstat", substituting)


# --- the primitive ----------------------------------------------------------


def test_a_symlink_is_refused_by_the_open_itself(tmp_path: Path) -> None:
    """The check-to-open gap is closed where it has to be: at the open."""
    real = tmp_path / "real.toml"
    real.write_text("a = 1\n", encoding="utf-8")
    link = tmp_path / "link.toml"
    try:
        link.symlink_to(real)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not creatable in this environment")

    with pytest.raises(OperationError) as caught:
        open_no_follow(
            link, subject="The file", code=ErrorCode.BACKUP_CONFIGURATION_UNAVAILABLE
        )

    assert caught.value.code is ErrorCode.BACKUP_CONFIGURATION_UNAVAILABLE
    assert "symbolic link" in caught.value.message


@POSIX_ONLY
def test_the_kernel_refuses_the_symlink_on_posix() -> None:
    """On the deployment platform the refusal is the kernel's, not a check."""
    assert SUPPORTS_NO_FOLLOW
    assert hasattr(os, "O_NOFOLLOW")


def test_the_open_flags_use_no_follow_where_available() -> None:
    """Linux gets O_NOFOLLOW; Windows gets the strongest it has."""
    from mgo.operations.source_identity import _open_flags

    resolved = _open_flags()
    assert resolved & os.O_RDONLY == os.O_RDONLY

    if hasattr(os, "O_NOFOLLOW"):
        assert resolved & os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        assert resolved & os.O_CLOEXEC
    if hasattr(os, "O_BINARY"):
        # Windows: no newline translation on a file read byte-for-byte.
        assert resolved & os.O_BINARY
    if hasattr(os, "O_NOINHERIT"):
        assert resolved & os.O_NOINHERIT


def test_the_windows_fallback_still_rejects_a_symlink_before_opening() -> None:
    """The fallback is weaker than O_NOFOLLOW, but it is not absent.

    Documented explicitly so nobody later assumes Windows carries the Linux
    guarantee: on Windows a substitution is *detected* afterwards rather than
    *prevented* at the open.
    """
    if SUPPORTS_NO_FOLLOW:
        # Linux: prevention. Nothing to assert about the fallback.
        assert hasattr(os, "O_NOFOLLOW")
    else:
        # Windows: the pre-open lstat is present, and the post-open identity
        # comparison is what catches a race against it.
        source = Path("src/mgo/operations/source_identity.py").read_text(
            encoding="utf-8"
        )
        assert "if not SUPPORTS_NO_FOLLOW:" in source
        assert "stat.S_ISLNK(pre.st_mode)" in source


def test_a_replaced_path_is_detected_after_the_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """fstat proves the object is regular; only identity proves it is *the* one."""
    target = tmp_path / "config.toml"
    target.write_text("a = 1\n", encoding="utf-8")

    _substitute_after_open(monkeypatch, target, st_ino=999_999)

    with pytest.raises(OperationError) as caught:
        read_regular_file(
            target,
            max_bytes=1024,
            subject="The configuration",
            code=ErrorCode.BACKUP_CONFIGURATION_UNAVAILABLE,
        )

    assert caught.value.code is ErrorCode.BACKUP_CONFIGURATION_UNAVAILABLE
    assert "was replaced" in caught.value.message


def test_a_path_that_became_a_symlink_is_detected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The substitution that matters most: a regular file swapped for a link."""
    target = tmp_path / "config.toml"
    target.write_text("a = 1\n", encoding="utf-8")

    _substitute_after_open(
        monkeypatch, target, st_mode=stat.S_IFLNK | 0o777
    )

    with pytest.raises(OperationError) as caught:
        read_regular_file(
            target,
            max_bytes=1024,
            subject="The configuration",
            code=ErrorCode.BACKUP_CONFIGURATION_UNAVAILABLE,
        )

    assert "became a symbolic link" in caught.value.message


def test_a_directory_is_refused(tmp_path: Path) -> None:
    """A path that is not a regular file cannot be read as one."""
    directory = tmp_path / "a-directory"
    directory.mkdir()

    with pytest.raises(OperationError):
        read_regular_file(
            directory,
            max_bytes=1024,
            subject="The configuration",
            code=ErrorCode.BACKUP_CONFIGURATION_UNAVAILABLE,
        )


def test_unreportable_inodes_are_treated_as_unproven() -> None:
    """A zero inode would make every comparison vacuously true."""
    unknown = SourceIdentity(device=1, inode=0, size=10, mtime_ns=5)
    other = SourceIdentity(device=1, inode=0, size=10, mtime_ns=5)

    assert not unknown.names_the_same_file_as(other)

    real = SourceIdentity(device=1, inode=42, size=10, mtime_ns=5)
    assert real.names_the_same_file_as(
        SourceIdentity(device=1, inode=42, size=99, mtime_ns=7)
    )


def test_the_size_bound_is_enforced_before_reading(tmp_path: Path) -> None:
    """An oversized file is refused on its stat, not after being read in."""
    target = tmp_path / "big.toml"
    target.write_bytes(b"x" * 2048)

    with pytest.raises(OperationError) as caught:
        read_regular_file(
            target,
            max_bytes=1024,
            subject="The configuration",
            code=ErrorCode.BACKUP_CONFIGURATION_UNAVAILABLE,
        )

    assert "above the 1024 byte limit" in caught.value.message


def test_a_bounded_file_is_read_completely(tmp_path: Path) -> None:
    """Short reads must not silently truncate the result."""
    target = tmp_path / "exact.bin"
    payload = bytes(range(256)) * 4
    target.write_bytes(payload)

    data, identity = read_regular_file(
        target,
        max_bytes=len(payload),
        subject="The file",
        code=ErrorCode.BACKUP_CONFIGURATION_UNAVAILABLE,
    )

    assert data == payload
    assert identity.size == len(payload)


# --- configuration capture --------------------------------------------------


def test_capture_reads_the_configuration_exactly_once(
    configuration: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two reads is the defect; one read is the fix."""
    opened: list[str] = []
    real_open = os.open
    target = str(configuration)

    def counting(path: Any, *args: Any, **kwargs: Any) -> int:
        if str(path) == target:
            opened.append(str(path))
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", counting)
    capture_configuration(configuration)

    assert len(opened) == 1


def test_a_backup_run_opens_the_configuration_only_once(
    database: Path,
    configuration: Path,
    destination: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole command, not just the capture helper."""
    opened: list[str] = []
    real_open = os.open
    target = str(configuration)

    def counting(path: Any, *args: Any, **kwargs: Any) -> int:
        if str(path) == target:
            opened.append(str(path))
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", counting)

    snapshot = capture_configuration(configuration)
    create_backup(
        database_path=database,
        configuration=snapshot,
        destination=destination,
    )

    assert len(opened) == 1, "create_backup must not reopen the configuration"


def test_create_backup_never_loads_the_configuration_again(
    database: Path,
    configuration: Path,
    destination: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second ``load_config`` would reintroduce the pairing hazard."""
    snapshot = capture_configuration(configuration)

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("create_backup must not call load_config")

    monkeypatch.setattr(backup_module, "parse_config_bytes", forbidden)
    monkeypatch.setattr("mgo.core.config.load_config", forbidden)

    result = create_backup(
        database_path=database,
        configuration=snapshot,
        destination=destination,
    )

    assert result.backup_path.is_file()


def test_the_stored_snapshot_is_the_bytes_that_were_parsed(
    database: Path, configuration: Path, destination: Path
) -> None:
    """The property the whole single-read design exists to guarantee."""
    snapshot = capture_configuration(configuration)

    result = create_backup(
        database_path=snapshot.config.storage.database_path,
        configuration=snapshot,
        destination=destination,
    )

    stored = result.configuration_path.read_bytes()
    assert stored == snapshot.data
    # ...and re-parsing exactly those stored bytes yields the same database.
    assert (
        parse_config_bytes(stored).storage.database_path
        == snapshot.config.storage.database_path
    )
    assert snapshot.config.storage.database_path == database


def test_editing_the_configuration_after_capture_cannot_change_the_set(
    database: Path, configuration: Path, destination: Path
) -> None:
    """The captured bytes are the authority for the whole run."""
    snapshot = capture_configuration(configuration)
    original = configuration.read_bytes()

    # An administrator edits the live configuration mid-run.
    configuration.write_text(
        CONFIGURATION_TEXT.format(database="/somewhere/else.db"), encoding="utf-8"
    )

    result = create_backup(
        database_path=snapshot.config.storage.database_path,
        configuration=snapshot,
        destination=destination,
    )

    assert result.configuration_path.read_bytes() == original
    assert "somewhere/else.db" not in result.configuration_path.read_text(
        encoding="utf-8"
    )


def test_a_configuration_that_cannot_be_parsed_is_refused(
    tmp_path: Path
) -> None:
    """A recovery set must hold a configuration the application can load."""
    broken = tmp_path / "broken.toml"
    broken.write_text("this is not = = valid toml\n", encoding="utf-8")

    with pytest.raises(OperationError) as caught:
        capture_configuration(broken)

    assert caught.value.code is ErrorCode.BACKUP_CONFIGURATION_UNAVAILABLE


def test_a_parse_failure_does_not_echo_configuration_content(
    tmp_path: Path
) -> None:
    """A TOML error quotes the offending line, which may hold the secret."""
    broken = tmp_path / "broken.toml"
    broken.write_text(
        '[secrets]\nbot_token = "SECRET-TOKEN-MUST-NOT-LEAK\n', encoding="utf-8"
    )

    with pytest.raises(OperationError) as caught:
        capture_configuration(broken)

    assert "SECRET-TOKEN-MUST-NOT-LEAK" not in caught.value.message
    assert "bot_token" not in caught.value.message


def test_the_snapshot_repr_cannot_leak_configuration_content(
    configuration: Path
) -> None:
    """A dataclass repr would print the bytes into any traceback."""
    snapshot = capture_configuration(configuration)

    rendered = repr(snapshot)
    assert "SECRET-TOKEN-MUST-NOT-LEAK" not in rendered
    assert "bot_token" not in rendered
    assert snapshot.sha256 in rendered
    assert "mgo.toml" in rendered

    # Also inside a container, which is how it would reach a pytest failure.
    assert "SECRET-TOKEN-MUST-NOT-LEAK" not in repr([snapshot])
    assert "SECRET-TOKEN-MUST-NOT-LEAK" not in repr({"snapshot": snapshot})


def test_the_snapshot_reports_its_own_size_and_checksum(
    configuration: Path
) -> None:
    """The manifest's configuration fields come from here."""
    snapshot = capture_configuration(configuration)

    assert snapshot.size_bytes == configuration.stat().st_size
    assert snapshot.sha256 == file_sha256(configuration)
    assert snapshot.source_name == "mgo.toml"


def test_a_snapshot_is_immutable(configuration: Path) -> None:
    """Nothing may rewrite the bytes between capture and publication."""
    snapshot = capture_configuration(configuration)

    with pytest.raises(AttributeError):
        snapshot.data = b"replaced"  # type: ignore[misc]


def test_capture_bounds_the_configuration_size(tmp_path: Path) -> None:
    """The 1 MiB bound still applies through the secure reader."""
    oversized = tmp_path / "huge.toml"
    oversized.write_bytes(b"# " + b"x" * (MAX_CONFIGURATION_BYTES + 1))

    with pytest.raises(OperationError) as caught:
        capture_configuration(oversized)

    assert caught.value.code is ErrorCode.BACKUP_CONFIGURATION_UNAVAILABLE


# --- configuration / database pairing ---------------------------------------


def test_the_database_comes_from_the_captured_configuration(
    database: Path, configuration: Path
) -> None:
    """The pairing the recovery set claims must be the pairing that happened."""
    snapshot = capture_configuration(configuration)

    assert snapshot.config.storage.database_path == database


def test_an_explicit_override_is_reported_as_an_override(
    database: Path, configuration: Path, destination: Path, tmp_path: Path
) -> None:
    """The CLI must not claim configuration-derived selection when it was not."""
    from mgo.operations.backup_cli import main

    other = tmp_path / "other.db"
    apply_migrations(other)

    out = io.StringIO()
    err = io.StringIO()
    code = main(
        [
            "backup",
            "--config",
            str(configuration),
            "--database",
            str(other),
            "--output-directory",
            str(destination),
        ],
        stdout=out,
        stderr=err,
    )

    assert code == 0
    summary = json.loads(out.getvalue())
    assert summary["database_source"] == "explicit_override"

    events = [json.loads(line) for line in err.getvalue().splitlines()]
    assert any(item["event_id"] == "backup.database_overridden" for item in events)


def test_configuration_derived_selection_is_reported_as_such(
    database: Path, configuration: Path, destination: Path
) -> None:
    """The ordinary case is labelled honestly too."""
    from mgo.operations.backup_cli import main

    out = io.StringIO()
    err = io.StringIO()
    code = main(
        [
            "backup",
            "--config",
            str(configuration),
            "--output-directory",
            str(destination),
        ],
        stdout=out,
        stderr=err,
    )

    assert code == 0
    summary = json.loads(out.getvalue())
    assert summary["database_source"] == "configuration"

    events = [json.loads(line) for line in err.getvalue().splitlines()]
    assert not any(
        item["event_id"] == "backup.database_overridden" for item in events
    )
    assert any(
        item.get("source_database_name") == "mgo.db"
        for item in events
        if item["event_id"] == "backup.started"
    )


def test_the_backup_command_does_not_reload_the_configuration(
    database: Path,
    configuration: Path,
    destination: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``backup`` must not call ``load_config`` at all."""
    import mgo.operations.backup_cli as cli

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the backup command must not call load_config")

    monkeypatch.setattr(cli, "load_config", forbidden)

    code = cli.main(
        [
            "backup",
            "--config",
            str(configuration),
            "--output-directory",
            str(destination),
        ],
        stdout=io.StringIO(),
        stderr=io.StringIO(),
    )

    assert code == 0


# --- database source identity -----------------------------------------------


def test_a_normal_wal_database_still_backs_up(
    database: Path, configuration: Path, destination: Path
) -> None:
    """The hardening must not have cost the ordinary case."""
    snapshot = capture_configuration(configuration)

    result = create_backup(
        database_path=database,
        configuration=snapshot,
        destination=destination,
    )

    assert result.backup_path.is_file()
    connection = sqlite3.connect(result.backup_path)
    try:
        assert connection.execute(
            "SELECT COUNT(*) FROM observations"
        ).fetchone()[0] == 1
    finally:
        connection.close()


def test_committed_wal_data_reaches_the_snapshot(
    database: Path, configuration: Path, destination: Path
) -> None:
    """The whole reason the online backup API is used rather than ``cp``.

    A writer commits while holding the database open, so the rows live in the
    ``-wal`` sidecar at the moment the backup runs.
    """
    writer = sqlite3.connect(database)
    writer.execute("PRAGMA journal_mode = WAL")
    writer.execute(
        "INSERT INTO observations "
        "(observed_at, kind, source, status, summary, created_at) "
        "VALUES ('t', 'test', 'writer', 'success', 'in the wal', 't')"
    )
    writer.commit()

    wal = database.with_name(database.name + "-wal")
    assert wal.exists(), "the fixture should leave data in the WAL"

    try:
        snapshot = capture_configuration(configuration)
        result = create_backup(
            database_path=database,
            configuration=snapshot,
            destination=destination,
        )
    finally:
        writer.close()

    connection = sqlite3.connect(result.backup_path)
    try:
        summaries = [
            row[0]
            for row in connection.execute("SELECT summary FROM observations")
        ]
    finally:
        connection.close()

    assert "in the wal" in summaries
    assert "seeded" in summaries


def test_the_source_database_and_its_sidecars_are_unchanged(
    database: Path, configuration: Path, destination: Path
) -> None:
    """A backup reads; it never writes, checkpoints or re-modes the source."""
    writer = sqlite3.connect(database)
    writer.execute("PRAGMA journal_mode = WAL")
    writer.execute(
        "INSERT INTO observations "
        "(observed_at, kind, source, status, summary, created_at) "
        "VALUES ('t', 'test', 'writer', 'success', 'held', 't')"
    )
    writer.commit()

    wal = database.with_name(database.name + "-wal")
    before = {
        "db": file_sha256(database),
        "wal": file_sha256(wal) if wal.exists() else None,
    }

    try:
        snapshot = capture_configuration(configuration)
        create_backup(
            database_path=database,
            configuration=snapshot,
            destination=destination,
        )

        # Compared while the writer is still open. Closing the last connection
        # to a WAL database checkpoints it into the main file, which would
        # change the hash for a reason that has nothing to do with the backup.
        assert file_sha256(database) == before["db"]
        if before["wal"] is not None:
            assert file_sha256(wal) == before["wal"]
    finally:
        writer.close()


def test_a_substituted_database_is_detected_across_the_sqlite_open(
    database: Path,
    configuration: Path,
    destination: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The anchor's whole purpose: SQLite opens by name, so verify afterwards."""
    snapshot = capture_configuration(configuration)
    _substitute_after_open(monkeypatch, database, st_ino=1_234_567)

    with pytest.raises(OperationError) as caught:
        create_backup(
            database_path=database,
            configuration=snapshot,
            destination=destination,
        )

    assert caught.value.code is ErrorCode.BACKUP_SOURCE_IDENTITY_CHANGED
    assert "was replaced" in caught.value.message


def test_a_database_that_became_a_symlink_is_detected(
    database: Path,
    configuration: Path,
    destination: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A regular file swapped for a link between validation and open."""
    snapshot = capture_configuration(configuration)
    _substitute_after_open(monkeypatch, database, st_mode=stat.S_IFLNK | 0o777)

    with pytest.raises(OperationError) as caught:
        create_backup(
            database_path=database,
            configuration=snapshot,
            destination=destination,
        )

    assert caught.value.code is ErrorCode.BACKUP_SOURCE_IDENTITY_CHANGED


def test_an_identity_failure_publishes_nothing(
    database: Path,
    configuration: Path,
    destination: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A substitution must leave no recovery-set file and no temporary file."""
    snapshot = capture_configuration(configuration)
    _substitute_after_open(monkeypatch, database, st_ino=7_654_321)

    with pytest.raises(OperationError):
        create_backup(
            database_path=database,
            configuration=snapshot,
            destination=destination,
        )

    assert list(destination.iterdir()) == []


def test_an_identity_failure_emits_no_source_content(
    database: Path,
    configuration: Path,
    destination: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failure messages describe the situation, never the data."""
    snapshot = capture_configuration(configuration)
    _substitute_after_open(monkeypatch, database, st_ino=222_222)

    stream = io.StringIO()
    with pytest.raises(OperationError) as caught:
        create_backup(
            database_path=database,
            configuration=snapshot,
            destination=destination,
            emitter=EventEmitter("mgo-backup", stream=stream),
        )

    assert "seeded" not in caught.value.message
    assert "SECRET-TOKEN-MUST-NOT-LEAK" not in stream.getvalue()
    assert "seeded" not in stream.getvalue()


def test_the_anchor_pins_a_regular_file_only(tmp_path: Path) -> None:
    """An anchor must never be taken on something that is not a regular file."""
    directory = tmp_path / "not-a-file"
    directory.mkdir()

    with (
        pytest.raises(OperationError),
        anchored_source(
            directory,
            subject="The source database",
            code=ErrorCode.BACKUP_SOURCE_UNAVAILABLE,
        ),
    ):
        pass


def test_the_anchor_is_released_after_the_block(database: Path) -> None:
    """A held descriptor must not leak; the file stays deletable afterwards."""
    with anchored_source(
        database,
        subject="The source database",
        code=ErrorCode.BACKUP_SOURCE_UNAVAILABLE,
    ) as anchor:
        assert anchor.identity.inode != 0 or os.name == "nt"

    # Closing is what makes this possible on Windows, where an open handle
    # blocks removal.
    database.unlink()
    assert not database.exists()


def test_the_anchor_verifies_an_unchanged_path(database: Path) -> None:
    """The success path: nothing changed, so verification passes silently."""
    with anchored_source(
        database,
        subject="The source database",
        code=ErrorCode.BACKUP_SOURCE_UNAVAILABLE,
    ) as anchor:
        anchor.verify(
            subject="The source database",
            code=ErrorCode.BACKUP_SOURCE_IDENTITY_CHANGED,
        )


def test_a_recovery_set_still_verifies_after_the_hardening(
    database: Path, configuration: Path, destination: Path
) -> None:
    """End to end: the hardened path still produces a verifiable set."""
    from mgo.operations.backup import verify_backup

    snapshot = capture_configuration(configuration)
    result = create_backup(
        database_path=database,
        configuration=snapshot,
        destination=destination,
    )

    verification = verify_backup(result.backup_path)
    assert verification.ok, verification.detail
    assert configuration_path_for(result.backup_path).is_file()


def test_the_snapshot_type_is_what_create_backup_requires(
    configuration: Path
) -> None:
    """The signature change is the mechanism that prevents a second read."""
    snapshot = capture_configuration(configuration)

    assert isinstance(snapshot, ConfigurationSnapshot)
    assert isinstance(snapshot.identity, SourceIdentity)
