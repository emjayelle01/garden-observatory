"""Consistent SQLite backup, verification and restore testing for MGO.

The production database runs in WAL mode with the API service writing to it
continuously. That rules out the obvious approach: copying ``mgo.db`` with
``cp`` captures the main database file but not the committed pages still sitting
in the write-ahead log, so the result is a file that *looks* like a database and
is missing recent transactions. It also rules out the other obvious approach --
stopping the service -- because a backup must not cost availability.

SQLite's own online backup API solves both. ``Connection.backup()`` copies a
transactionally consistent snapshot while other connections keep reading and
writing, and it is driven from a ``mode=ro`` source connection here so this
module cannot modify, checkpoint, vacuum or re-mode the production database even
by accident.

One subtlety is load-bearing and was measured rather than assumed: the *copy*
inherits WAL journalling from the copied header, so closing it leaves
``-wal``/``-shm`` sidecars next to the backup. A single published ``.db`` file
would then not be the whole database, and its checksum would not describe its
contents. The destination is therefore switched to ``journal_mode=DELETE``
before it is closed, which checkpoints everything into one self-contained file.
That happens entirely on the copy; the source is never touched.

Publication is atomic: build into a temporary file, validate it as a database in
its own right, checksum it, then ``os.replace()`` it into its final name. A
backup that fails at any point leaves no file whose name claims it succeeded.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
from collections.abc import Iterable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from mgo.core.config import (
    SYSTEM_DATABASE_DIRECTORY,
    SYSTEM_STATE_DIRECTORY,
)
from mgo.core.database import (
    CURRENT_SCHEMA_VERSION,
    connect_readonly,
    is_memory_database,
    journal_mode,
    schema_version,
)
from mgo.core.identity import get_application_version
from mgo.operations.errors import ErrorCode, OperationError
from mgo.operations.events import EventEmitter
from mgo.operations.locking import operation_lock

#: The manifest schema this build writes and understands. A manifest recording a
#: *higher* version was written by a newer build and is refused rather than
#: interpreted optimistically -- exactly the rule the database schema follows.
BACKUP_FORMAT_VERSION = 1

#: Logical application name recorded in every manifest.
BACKUP_APPLICATION = "garden-observatory"

#: Service name stamped on this module's structured events.
BACKUP_SERVICE = "mgo-backup"

#: UTC, second precision, no separators that a filesystem or shell would find
#: awkward. The format sorts lexically in chronological order, which is what
#: makes retention a simple "keep the last N after sorting by name".
BACKUP_TIMESTAMP_FORMAT = "%Y%m%dT%H%M%SZ"

BACKUP_NAME_PREFIX = "mgo-"
BACKUP_SUFFIX = ".db"
MANIFEST_SUFFIX = ".manifest.json"

#: The *only* filename shape recognised as an MGO backup. Retention deletes
#: nothing that fails this pattern, so an operator's note, an unrelated archive
#: or a support bundle sharing the directory can never be removed by a backup
#: run.
BACKUP_NAME_PATTERN = re.compile(r"\Amgo-(\d{8}T\d{6}Z)\.db\Z")

#: Prefix for in-progress files. Leading dot plus a distinctive stem: it is
#: recognisably ours, and it can never match :data:`BACKUP_NAME_PATTERN`.
TEMPORARY_PREFIX = ".mgo-backup-"
TEMPORARY_SUFFIX = ".tmp"

#: Lock file guarding the destination directory against overlapping runs.
LOCK_FILENAME = ".mgo-backup.lock"

#: Completed backup sets retained by default. Fourteen daily backups covers a
#: fortnight's absence -- long enough to notice and act on a problem, short
#: enough that fourteen copies of a small database do not crowd an SD card.
DEFAULT_RETENTION_COUNT = 14

#: Upper bound accepted for ``--keep``. Retention is a bounded positive integer,
#: not an invitation to keep every backup ever taken.
MAX_RETENTION_COUNT = 3650

#: Backups and manifests: owner read/write, group read, no world access.
BACKUP_FILE_MODE = 0o640

#: Tables every usable MGO backup must contain. ``schema_migrations`` is the
#: version authority; the other two hold the data a restore exists to recover.
EXPECTED_TABLES: tuple[str, ...] = (
    "schema_migrations",
    "observations",
    "captures",
)

#: Read size for checksumming. Large enough to be efficient, small enough that
#: memory use stays flat regardless of how big the database grows.
_CHECKSUM_CHUNK_BYTES = 1024 * 1024


# --- manifest ---------------------------------------------------------------


@dataclass(frozen=True)
class BackupManifest:
    """The recorded description of one completed backup.

    Everything here is either a logical label, a measurement of the backup file
    or a count. There is deliberately **no** absolute path, no configuration
    value, no environment value, no username, no hostname and no database row
    content: a manifest travels with a backup, and a backup may be copied
    somewhere less private than the Pi.

    ``source_database_name`` is the source file's *name* only, for the same
    reason ``/database/status`` reports a name rather than a path.
    """

    format_version: int
    created_at: str
    application: str
    application_version: str
    source_database_name: str
    backup_filename: str
    backup_size_bytes: int
    sha256: str
    schema_version: int
    expected_schema_version: int
    integrity: str
    journal_mode_of_backup: str
    table_row_counts: dict[str, int]

    def as_dict(self) -> dict[str, Any]:
        """Return the serialisable manifest body."""
        return {
            "format_version": self.format_version,
            "created_at": self.created_at,
            "application": self.application,
            "application_version": self.application_version,
            "source_database_name": self.source_database_name,
            "backup_filename": self.backup_filename,
            "backup_size_bytes": self.backup_size_bytes,
            "sha256": self.sha256,
            "schema_version": self.schema_version,
            "expected_schema_version": self.expected_schema_version,
            "integrity": self.integrity,
            "journal_mode_of_backup": self.journal_mode_of_backup,
            "table_row_counts": dict(sorted(self.table_row_counts.items())),
        }

    def to_json(self) -> str:
        """Return the manifest as deterministic JSON text.

        Sorted keys and a fixed indent mean two manifests describing the same
        backup are byte-identical, which is what lets a test assert on content
        rather than on a parsed subset.
        """
        return (
            json.dumps(
                self.as_dict(), indent=2, sort_keys=True, ensure_ascii=False
            )
            + "\n"
        )

    @classmethod
    def from_dict(cls, payload: object) -> BackupManifest:
        """Parse a manifest body, rejecting anything malformed.

        Raises :class:`OperationError` with
        :attr:`~mgo.operations.errors.ErrorCode.BACKUP_MANIFEST_FAILED` rather
        than a ``KeyError`` or ``TypeError``: a hand-edited or truncated
        manifest is an expected operational condition, not a programming error.
        """
        if not isinstance(payload, dict):
            raise OperationError(
                ErrorCode.BACKUP_MANIFEST_FAILED,
                "The backup manifest is not a JSON object.",
            )

        raw_version = payload.get("format_version")
        if not isinstance(raw_version, int) or isinstance(raw_version, bool):
            raise OperationError(
                ErrorCode.BACKUP_MANIFEST_FAILED,
                "The backup manifest has no integer 'format_version'.",
            )
        if raw_version > BACKUP_FORMAT_VERSION:
            raise OperationError(
                ErrorCode.BACKUP_MANIFEST_FAILED,
                f"The backup manifest uses format version {raw_version}, which "
                f"is newer than the version this build understands "
                f"({BACKUP_FORMAT_VERSION}).",
            )

        try:
            counts_raw = payload["table_row_counts"]
            if not isinstance(counts_raw, dict):
                raise TypeError("table_row_counts")
            counts = {str(name): int(value) for name, value in counts_raw.items()}

            return cls(
                format_version=raw_version,
                created_at=str(payload["created_at"]),
                application=str(payload["application"]),
                application_version=str(payload["application_version"]),
                source_database_name=str(payload["source_database_name"]),
                backup_filename=str(payload["backup_filename"]),
                backup_size_bytes=int(payload["backup_size_bytes"]),
                sha256=str(payload["sha256"]),
                schema_version=int(payload["schema_version"]),
                expected_schema_version=int(payload["expected_schema_version"]),
                integrity=str(payload["integrity"]),
                journal_mode_of_backup=str(payload["journal_mode_of_backup"]),
                table_row_counts=counts,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise OperationError(
                ErrorCode.BACKUP_MANIFEST_FAILED,
                f"The backup manifest is missing or malformed: {exc}.",
            ) from exc


# --- results ----------------------------------------------------------------


@dataclass(frozen=True)
class RetentionResult:
    """What a retention pass kept and removed."""

    keep: int
    kept: tuple[str, ...]
    removed: tuple[str, ...]
    removed_bytes: int
    failures: tuple[str, ...] = ()

    @property
    def succeeded(self) -> bool:
        """Whether every intended removal completed."""
        return not self.failures

    def as_dict(self) -> dict[str, Any]:
        """Return the serialisable summary."""
        return {
            "keep": self.keep,
            "kept": list(self.kept),
            "removed": list(self.removed),
            "removed_bytes": self.removed_bytes,
            "failures": list(self.failures),
        }


@dataclass(frozen=True)
class BackupResult:
    """A completed, validated and published backup."""

    backup_path: Path
    manifest_path: Path
    manifest: BackupManifest
    duration_ms: int
    retention: RetentionResult | None = None

    def as_dict(self) -> dict[str, Any]:
        """Return the serialisable summary (names only, never full paths)."""
        return {
            "backup_filename": self.backup_path.name,
            "manifest_filename": self.manifest_path.name,
            "backup_size_bytes": self.manifest.backup_size_bytes,
            "sha256": self.manifest.sha256,
            "schema_version": self.manifest.schema_version,
            "duration_ms": self.duration_ms,
            "retention": (
                self.retention.as_dict() if self.retention is not None else None
            ),
        }


@dataclass(frozen=True)
class VerificationResult:
    """The outcome of verifying a backup against its manifest."""

    backup_filename: str
    ok: bool
    checks: dict[str, str]
    error_code: ErrorCode | None = None
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        """Return the serialisable summary."""
        return {
            "backup_filename": self.backup_filename,
            "ok": self.ok,
            "checks": dict(sorted(self.checks.items())),
            "error_code": self.error_code.value if self.error_code else None,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class RestoreTestResult:
    """The outcome of restoring a backup into an isolated directory."""

    backup_filename: str
    ok: bool
    checks: dict[str, str]
    row_counts: dict[str, int]
    work_directory: Path | None
    preserved: bool
    error_code: ErrorCode | None = None
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        """Return the serialisable summary."""
        return {
            "backup_filename": self.backup_filename,
            "ok": self.ok,
            "checks": dict(sorted(self.checks.items())),
            "row_counts": dict(sorted(self.row_counts.items())),
            "preserved": self.preserved,
            "error_code": self.error_code.value if self.error_code else None,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class BackupSet:
    """A backup file paired with its manifest."""

    timestamp: str
    backup_path: Path
    manifest_path: Path


@dataclass(frozen=True)
class BackupListing:
    """Every complete backup set in a directory, plus what was ignored.

    Orphans are reported rather than hidden. A ``.db`` with no manifest or a
    manifest with no ``.db`` is not a usable backup, but it *is* something an
    operator should know about -- silently omitting it would make a directory
    holding seven half-backups look empty.
    """

    directory: Path
    sets: tuple[BackupSet, ...] = ()
    orphan_backups: tuple[str, ...] = ()
    orphan_manifests: tuple[str, ...] = ()
    temporary_files: tuple[str, ...] = ()
    verifications: dict[str, VerificationResult] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        """Return the serialisable summary."""
        return {
            "complete_sets": [
                {
                    "timestamp": item.timestamp,
                    "backup_filename": item.backup_path.name,
                    "manifest_filename": item.manifest_path.name,
                    "verification": (
                        self.verifications[item.backup_path.name].as_dict()
                        if item.backup_path.name in self.verifications
                        else None
                    ),
                }
                for item in self.sets
            ],
            "orphan_backups": list(self.orphan_backups),
            "orphan_manifests": list(self.orphan_manifests),
            "temporary_files": list(self.temporary_files),
        }


# --- shared helpers ---------------------------------------------------------


def backup_timestamp(moment: datetime | None = None) -> str:
    """Return the UTC timestamp component of a backup filename."""
    now = moment if moment is not None else datetime.now(UTC)
    return now.astimezone(UTC).strftime(BACKUP_TIMESTAMP_FORMAT)


def backup_filename(timestamp: str) -> str:
    """Return the backup filename for a timestamp component."""
    return f"{BACKUP_NAME_PREFIX}{timestamp}{BACKUP_SUFFIX}"


def manifest_filename(timestamp: str) -> str:
    """Return the manifest filename for a timestamp component."""
    return f"{BACKUP_NAME_PREFIX}{timestamp}{MANIFEST_SUFFIX}"


def manifest_path_for(backup_path: Path) -> Path:
    """Return the manifest path that belongs to a backup file."""
    return backup_path.with_name(
        backup_path.name[: -len(BACKUP_SUFFIX)] + MANIFEST_SUFFIX
    )


def file_sha256(path: Path) -> str:
    """Return the SHA-256 of a file, read in bounded chunks."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_CHECKSUM_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def _set_file_mode(path: Path, mode: int) -> None:
    """Apply restrictive permissions where the platform supports them.

    Windows has no POSIX mode bits, so ``chmod`` is largely a no-op there. That
    is acceptable: the permission requirement is a property of the Raspberry Pi
    deployment, and the development machine never holds a production backup.
    A failure is ignored rather than raised -- a backup that is byte-correct but
    could not be chmod-ed is still a good backup.
    """
    with suppress(OSError):
        os.chmod(path, mode)


def _fsync_file(path: Path) -> None:
    """Flush a completed file to storage, best effort."""
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    """Flush a directory entry so a rename survives power loss, best effort.

    Only meaningful on POSIX. Windows cannot open a directory as a file
    descriptor and raises, which is expected and ignored.
    """
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _cleanup(path: Path) -> None:
    """Remove a partial artefact, ignoring a failure to do so."""
    with suppress(OSError):
        path.unlink()


@dataclass(frozen=True)
class _DatabaseFacts:
    """Everything read from a candidate database file in one pass."""

    integrity: str
    schema: int | None
    journal: str
    tables: frozenset[str]
    row_counts: dict[str, int]


def _inspect_database(path: Path) -> _DatabaseFacts:
    """Open a database read-only and gather the facts a backup is judged on.

    Read-only via ``mode=ro``: inspection can neither create the file nor alter
    the thing it is measuring, which matters because this runs against a
    just-written backup *and* against an operator-supplied file that may be
    anything at all.
    """
    connection = connect_readonly(path)
    try:
        rows = connection.execute("PRAGMA quick_check(1)").fetchall()
        integrity = str(rows[0][0]) if rows else "unknown"

        mode = journal_mode(connection)
        version = schema_version(connection)

        table_rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
        tables = frozenset(str(row[0]) for row in table_rows)

        counts: dict[str, int] = {}
        for table in EXPECTED_TABLES:
            if table not in tables:
                continue
            # The table name comes from this module's own constant, never from
            # an operator or a file, so the identifier interpolation SQLite
            # requires here carries no injection risk.
            count_row = connection.execute(
                f'SELECT COUNT(*) FROM "{table}"'
            ).fetchone()
            counts[table] = int(count_row[0]) if count_row is not None else 0

        return _DatabaseFacts(
            integrity=integrity,
            schema=version,
            journal=mode,
            tables=tables,
            row_counts=counts,
        )
    finally:
        connection.close()


def _require_sound_database(facts: _DatabaseFacts, subject: str) -> int:
    """Raise unless ``facts`` describe a usable, compatible MGO database.

    Returns the validated schema version, so a caller that needs it has no
    reason to re-derive it (or to assert that a checked value is not ``None``).
    """
    if facts.integrity.lower() != "ok":
        raise OperationError(
            ErrorCode.BACKUP_INTEGRITY_FAILED,
            f"{subject} failed its SQLite integrity check: {facts.integrity}.",
        )

    if facts.schema is None:
        raise OperationError(
            ErrorCode.BACKUP_SCHEMA_INCOMPATIBLE,
            f"{subject} has no schema-migration history, so its schema version "
            "cannot be established.",
        )

    if facts.schema > CURRENT_SCHEMA_VERSION:
        raise OperationError(
            ErrorCode.BACKUP_SCHEMA_INCOMPATIBLE,
            f"{subject} records schema version {facts.schema}, which is newer "
            f"than the version this build supports ({CURRENT_SCHEMA_VERSION}).",
        )

    missing = sorted(set(EXPECTED_TABLES) - facts.tables)
    if missing:
        raise OperationError(
            ErrorCode.BACKUP_SCHEMA_INCOMPATIBLE,
            f"{subject} is missing expected table(s): {', '.join(missing)}.",
        )

    return facts.schema


def _validated_keep(keep: int) -> int:
    """Return a validated retention count."""
    if not isinstance(keep, int) or isinstance(keep, bool):
        raise OperationError(
            ErrorCode.INVALID_ARGUMENT,
            "The retention count must be an integer.",
        )
    if keep < 1:
        raise OperationError(
            ErrorCode.INVALID_ARGUMENT,
            "The retention count must be at least 1: retention must never be "
            "able to delete every backup.",
        )
    if keep > MAX_RETENTION_COUNT:
        raise OperationError(
            ErrorCode.INVALID_ARGUMENT,
            f"The retention count must not exceed {MAX_RETENTION_COUNT}.",
        )
    return keep


def _validated_source(database_path: Path) -> Path:
    """Return the source database path, refusing anything unsafe to read.

    A **symlinked** source is refused outright and there is no override. The
    backup runs unattended as a service account with write access to the backup
    root; if the configured database path could be a symlink, replacing it would
    redirect the read to any file the account can see and publish the result
    into a directory an operator later copies elsewhere. Refusing costs a
    deployment nothing -- the canonical path is a real file -- and removes the
    whole class of problem.
    """
    if is_memory_database(database_path):
        raise OperationError(
            ErrorCode.BACKUP_SOURCE_UNAVAILABLE,
            "The configured database is in-memory; there is nothing "
            "persistent to back up.",
        )

    if database_path.is_symlink():
        raise OperationError(
            ErrorCode.BACKUP_SOURCE_UNAVAILABLE,
            f"The source database {database_path.name} is a symbolic link. "
            "Backing up through a link is refused: the link target could be "
            "changed between runs. Point the configuration at the real file.",
        )

    if not database_path.exists():
        raise OperationError(
            ErrorCode.BACKUP_SOURCE_UNAVAILABLE,
            f"The source database {database_path.name} does not exist.",
        )

    if not database_path.is_file():
        raise OperationError(
            ErrorCode.BACKUP_SOURCE_UNAVAILABLE,
            f"The source database path {database_path.name} is not a file.",
        )

    return database_path


def _prepared_destination(destination: Path) -> Path:
    """Create and check the destination directory, or explain why not."""
    try:
        destination.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise OperationError(
            ErrorCode.BACKUP_DESTINATION_UNWRITABLE,
            f"The backup directory could not be created: {exc.strerror or exc}.",
        ) from exc

    if not os.access(destination, os.W_OK):
        raise OperationError(
            ErrorCode.BACKUP_DESTINATION_UNWRITABLE,
            "The backup directory is not writable by this account.",
        )

    return destination


# --- backup -----------------------------------------------------------------


def _copy_database(source: Path, target: Path) -> str:
    """Copy ``source`` into ``target`` as one consistent snapshot.

    Returns the journal mode the finished copy reports.

    The source connection is read-only, so nothing here can modify, checkpoint
    or vacuum the production database. The whole copy is taken in a single step
    (``pages=0``) rather than incrementally: the observation database on a
    Raspberry Pi is small, one step yields the strongest consistency guarantee,
    and it removes the possibility of a busy writer restarting the copy
    repeatedly.

    The copy is then switched out of WAL. Without this the finished backup would
    depend on ``-wal``/``-shm`` sidecars that the single published file does not
    include.
    """
    try:
        source_connection = connect_readonly(source)
    except sqlite3.DatabaseError as exc:
        raise OperationError(
            ErrorCode.BACKUP_SOURCE_UNAVAILABLE,
            f"The source database could not be opened: {exc}.",
        ) from exc
    except OSError as exc:
        raise OperationError(
            ErrorCode.BACKUP_SOURCE_UNAVAILABLE,
            f"The source database path is not usable: {exc.strerror or exc}.",
        ) from exc

    try:
        target_connection = sqlite3.connect(target)
        try:
            source_connection.backup(target_connection)
            target_connection.execute("PRAGMA journal_mode = DELETE")
            mode = journal_mode(target_connection)
        finally:
            target_connection.close()
    except sqlite3.Error as exc:
        raise OperationError(
            ErrorCode.BACKUP_SQLITE_FAILED,
            f"The SQLite online backup failed: {exc}.",
        ) from exc
    except OSError as exc:
        raise OperationError(
            ErrorCode.BACKUP_DESTINATION_UNWRITABLE,
            f"The backup file could not be written: {exc.strerror or exc}.",
        ) from exc
    finally:
        source_connection.close()

    return mode


def create_backup(
    *,
    database_path: Path,
    destination: Path,
    keep: int = DEFAULT_RETENTION_COUNT,
    emitter: EventEmitter | None = None,
    now: datetime | None = None,
) -> BackupResult:
    """Take, validate and publish one consistent backup.

    The sequence is deliberate and each step guards the next:

    1. validate the retention count and the source *before* anything is created;
    2. take the destination lock, so no second run can interleave;
    3. refuse if the final names already exist -- a completed backup is never
       overwritten;
    4. copy into a temporary file in the destination filesystem, so the final
       rename is atomic rather than a cross-device copy;
    5. open the copy independently and prove it is a sound, compatible MGO
       database;
    6. checksum it, flush it, then rename it into place;
    7. write the manifest the same way;
    8. only then apply retention.

    Any failure before step 6 leaves nothing but a temporary file, which is
    removed. Retention failure is reported but does not invalidate the backup
    that was just published.
    """
    events = emitter if emitter is not None else EventEmitter(BACKUP_SERVICE)
    started = datetime.now(UTC)

    keep = _validated_keep(keep)
    source = _validated_source(database_path)
    destination = _prepared_destination(destination)

    events.info(
        "backup.started",
        "Starting a consistent SQLite backup.",
        source_database_name=source.name,
        keep=keep,
    )

    with operation_lock(destination / LOCK_FILENAME, operation="backup") as lock:
        if lock.reclaimed_stale_lock:
            events.warning(
                "backup.stale_lock_reclaimed",
                "An abandoned backup lock was older than the stale threshold "
                "and has been reclaimed.",
            )

        stamp = backup_timestamp(now)
        final_backup = destination / backup_filename(stamp)
        final_manifest = destination / manifest_filename(stamp)

        if final_backup.exists() or final_manifest.exists():
            raise OperationError(
                ErrorCode.BACKUP_ALREADY_EXISTS,
                f"A backup named {final_backup.name} already exists. An "
                "existing backup is never overwritten; retry in a moment.",
            )

        descriptor, temporary_name = tempfile.mkstemp(
            dir=destination, prefix=TEMPORARY_PREFIX, suffix=TEMPORARY_SUFFIX
        )
        os.close(descriptor)
        temporary = Path(temporary_name)

        try:
            mode = _copy_database(source, temporary)

            facts = _inspect_database(temporary)
            backup_schema = _require_sound_database(facts, "The backup just taken")

            size = temporary.stat().st_size
            checksum = file_sha256(temporary)

            manifest = BackupManifest(
                format_version=BACKUP_FORMAT_VERSION,
                created_at=started.isoformat(),
                application=BACKUP_APPLICATION,
                application_version=get_application_version(),
                source_database_name=source.name,
                backup_filename=final_backup.name,
                backup_size_bytes=size,
                sha256=checksum,
                schema_version=backup_schema,
                expected_schema_version=CURRENT_SCHEMA_VERSION,
                integrity=facts.integrity,
                journal_mode_of_backup=mode,
                table_row_counts=facts.row_counts,
            )

            _set_file_mode(temporary, BACKUP_FILE_MODE)
            _fsync_file(temporary)
            os.replace(temporary, final_backup)
            _fsync_directory(destination)
        except OperationError:
            _cleanup(temporary)
            raise
        except (OSError, sqlite3.Error) as exc:
            _cleanup(temporary)
            raise OperationError(
                ErrorCode.BACKUP_SQLITE_FAILED,
                f"The backup could not be completed: {exc}.",
            ) from exc

        try:
            _write_manifest(final_manifest, manifest)
        except OperationError:
            # The database file is published but undescribed, which would look
            # like a complete backup to nothing and an orphan to `list`. Remove
            # it so the failure is unambiguous.
            _cleanup(final_backup)
            raise

        duration_ms = int(
            (datetime.now(UTC) - started).total_seconds() * 1000
        )
        events.info(
            "backup.completed",
            "The backup was validated and published.",
            backup_filename=final_backup.name,
            backup_size_bytes=size,
            sha256=checksum,
            schema_version=backup_schema,
            duration_ms=duration_ms,
        )

        retention = _run_retention(destination, keep, events)

    return BackupResult(
        backup_path=final_backup,
        manifest_path=final_manifest,
        manifest=manifest,
        duration_ms=duration_ms,
        retention=retention,
    )


def _write_manifest(target: Path, manifest: BackupManifest) -> None:
    """Write a manifest atomically with restrictive permissions."""
    descriptor, temporary_name = tempfile.mkstemp(
        dir=target.parent, prefix=TEMPORARY_PREFIX, suffix=TEMPORARY_SUFFIX
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(manifest.to_json())
        _set_file_mode(temporary, BACKUP_FILE_MODE)
        _fsync_file(temporary)
        os.replace(temporary, target)
        _fsync_directory(target.parent)
    except OSError as exc:
        _cleanup(temporary)
        raise OperationError(
            ErrorCode.BACKUP_MANIFEST_FAILED,
            f"The backup manifest could not be written: {exc.strerror or exc}.",
        ) from exc


def _run_retention(
    destination: Path, keep: int, events: EventEmitter
) -> RetentionResult:
    """Apply retention, reporting failure without raising.

    Retention runs *after* a backup has been published, so a retention problem
    must never be able to fail the backup itself: the new copy already exists
    and is valid. The failure is reported truthfully in the result and in the
    command's exit status instead.
    """
    try:
        result = apply_retention(destination, keep)
    except OperationError as exc:
        events.error(
            "backup.retention_failed",
            exc.message,
            error_code=ErrorCode.BACKUP_RETENTION_FAILED,
        )
        return RetentionResult(
            keep=keep, kept=(), removed=(), removed_bytes=0, failures=(exc.message,)
        )

    if result.removed:
        events.info(
            "backup.retention_applied",
            "Retention removed the oldest complete backup set(s).",
            removed=list(result.removed),
            removed_bytes=result.removed_bytes,
            kept_count=len(result.kept),
        )
    if result.failures:
        events.error(
            "backup.retention_failed",
            "Retention could not remove every expired backup set.",
            error_code=ErrorCode.BACKUP_RETENTION_FAILED,
            failures=list(result.failures),
        )
    return result


# --- listing and retention --------------------------------------------------


def list_backups(directory: Path, *, verify: bool = False) -> BackupListing:
    """Return every complete backup set in ``directory``, newest first.

    A set is complete only when both the ``.db`` and its matching manifest are
    present and the filename matches :data:`BACKUP_NAME_PATTERN`. Orphans and
    in-progress temporary files are reported separately and are never treated as
    backups -- neither by this listing nor by retention.
    """
    if not directory.is_dir():
        raise OperationError(
            ErrorCode.BACKUP_NOT_FOUND,
            f"The backup directory {directory.name} does not exist.",
        )

    backups: dict[str, Path] = {}
    manifests: dict[str, Path] = {}
    temporary: list[str] = []

    for entry in sorted(directory.iterdir()):
        if not entry.is_file():
            continue
        name = entry.name
        if name.startswith(TEMPORARY_PREFIX):
            temporary.append(name)
            continue
        match = BACKUP_NAME_PATTERN.match(name)
        if match:
            backups[match.group(1)] = entry
            continue
        if name.startswith(BACKUP_NAME_PREFIX) and name.endswith(MANIFEST_SUFFIX):
            stamp = name[len(BACKUP_NAME_PREFIX) : -len(MANIFEST_SUFFIX)]
            manifests[stamp] = entry

    complete = sorted(set(backups) & set(manifests), reverse=True)
    sets = tuple(
        BackupSet(
            timestamp=stamp,
            backup_path=backups[stamp],
            manifest_path=manifests[stamp],
        )
        for stamp in complete
    )

    verifications: dict[str, VerificationResult] = {}
    if verify:
        for item in sets:
            verifications[item.backup_path.name] = verify_backup(item.backup_path)

    return BackupListing(
        directory=directory,
        sets=sets,
        orphan_backups=tuple(
            sorted(backups[stamp].name for stamp in set(backups) - set(manifests))
        ),
        orphan_manifests=tuple(
            sorted(manifests[stamp].name for stamp in set(manifests) - set(backups))
        ),
        temporary_files=tuple(sorted(temporary)),
        verifications=verifications,
    )


def apply_retention(directory: Path, keep: int) -> RetentionResult:
    """Keep the newest ``keep`` complete backup sets and remove the rest.

    Only complete sets are candidates, and a set's two files are removed
    together: deleting a ``.db`` while leaving its manifest would manufacture an
    orphan. Nothing outside :data:`BACKUP_NAME_PATTERN` is ever touched, so
    support bundles, operator notes and unrelated files sharing the directory
    are safe. The newest set can never be removed, because ``keep`` is validated
    to be at least one.
    """
    keep = _validated_keep(keep)
    listing = list_backups(directory)

    kept = listing.sets[:keep]
    expiring = listing.sets[keep:]

    removed: list[str] = []
    failures: list[str] = []
    removed_bytes = 0

    for item in expiring:
        with suppress(OSError):
            removed_bytes += item.backup_path.stat().st_size
        for path in (item.backup_path, item.manifest_path):
            try:
                path.unlink()
                removed.append(path.name)
            except OSError as exc:
                failures.append(f"{path.name}: {exc.strerror or exc}")

    return RetentionResult(
        keep=keep,
        kept=tuple(item.backup_path.name for item in kept),
        removed=tuple(removed),
        removed_bytes=removed_bytes,
        failures=tuple(failures),
    )


# --- verification -----------------------------------------------------------


def _load_manifest(manifest_path: Path) -> BackupManifest:
    """Read and parse a manifest file."""
    try:
        raw = manifest_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise OperationError(
            ErrorCode.BACKUP_MANIFEST_FAILED,
            f"The backup manifest could not be read: {exc.strerror or exc}.",
        ) from exc

    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise OperationError(
            ErrorCode.BACKUP_MANIFEST_FAILED,
            f"The backup manifest is not valid JSON: {exc}.",
        ) from exc

    return BackupManifest.from_dict(payload)


def verify_backup(backup_path: Path) -> VerificationResult:
    """Verify a backup against its manifest. Read-only; never raises.

    Checks the manifest's structure and version, the recorded size, the
    SHA-256, SQLite integrity, schema compatibility, the presence of every
    expected table, and the recorded row counts. Every failure is returned as a
    result with a stable error code rather than thrown, because ``verify`` is
    frequently run over a whole directory and one bad backup must not abort the
    sweep.

    Nothing is written, moved, repaired or re-checksummed on disk.
    """
    name = backup_path.name
    checks: dict[str, str] = {}

    def failed(code: ErrorCode, detail: str) -> VerificationResult:
        return VerificationResult(
            backup_filename=name,
            ok=False,
            checks=checks,
            error_code=code,
            detail=detail,
        )

    if backup_path.is_symlink():
        return failed(
            ErrorCode.BACKUP_NOT_FOUND,
            "The backup path is a symbolic link; verification is refused.",
        )
    if not backup_path.is_file():
        return failed(
            ErrorCode.BACKUP_NOT_FOUND, f"The backup {name} does not exist."
        )
    checks["backup_present"] = "ok"

    manifest_path = manifest_path_for(backup_path)
    if not manifest_path.is_file():
        return failed(
            ErrorCode.BACKUP_MANIFEST_FAILED,
            f"The backup {name} has no manifest at {manifest_path.name}.",
        )

    try:
        manifest = _load_manifest(manifest_path)
    except OperationError as exc:
        return failed(exc.code, exc.message)
    checks["manifest_readable"] = "ok"

    try:
        actual_size = backup_path.stat().st_size
    except OSError as exc:
        return failed(
            ErrorCode.BACKUP_NOT_FOUND,
            f"The backup could not be read: {exc.strerror or exc}.",
        )

    if actual_size != manifest.backup_size_bytes:
        return failed(
            ErrorCode.BACKUP_CHECKSUM_MISMATCH,
            f"The backup is {actual_size} bytes but its manifest records "
            f"{manifest.backup_size_bytes}.",
        )
    checks["size"] = "ok"

    try:
        actual_checksum = file_sha256(backup_path)
    except OSError as exc:
        return failed(
            ErrorCode.BACKUP_NOT_FOUND,
            f"The backup could not be read for checksumming: "
            f"{exc.strerror or exc}.",
        )

    if actual_checksum != manifest.sha256:
        return failed(
            ErrorCode.BACKUP_CHECKSUM_MISMATCH,
            "The backup's SHA-256 does not match its manifest; the file has "
            "changed since it was written.",
        )
    checks["sha256"] = "ok"

    try:
        facts = _inspect_database(backup_path)
    except sqlite3.DatabaseError as exc:
        return failed(
            ErrorCode.BACKUP_INTEGRITY_FAILED,
            f"The backup is not a readable SQLite database: {exc}.",
        )
    except OSError as exc:
        return failed(
            ErrorCode.BACKUP_NOT_FOUND,
            f"The backup could not be opened: {exc.strerror or exc}.",
        )

    try:
        _require_sound_database(facts, f"The backup {name}")
    except OperationError as exc:
        checks["integrity"] = (
            "ok" if facts.integrity.lower() == "ok" else facts.integrity
        )
        return failed(exc.code, exc.message)

    checks["integrity"] = "ok"
    checks["schema"] = "ok"
    checks["expected_tables"] = "ok"

    mismatched = sorted(
        table
        for table, count in manifest.table_row_counts.items()
        if facts.row_counts.get(table) != count
    )
    if mismatched:
        return failed(
            ErrorCode.BACKUP_INTEGRITY_FAILED,
            "The backup's row counts disagree with its manifest for: "
            f"{', '.join(mismatched)}.",
        )
    checks["row_counts"] = "ok"

    return VerificationResult(backup_filename=name, ok=True, checks=checks)


# --- restore testing --------------------------------------------------------

#: Directories a restore test must never write into. The production database
#: directory heads the list: a restore test that could write there would be one
#: typo away from overwriting the live database, which is the single most
#: destructive thing this tooling could do.
_PROTECTED_ROOTS: tuple[str, ...] = (
    SYSTEM_DATABASE_DIRECTORY.as_posix(),
    SYSTEM_STATE_DIRECTORY.as_posix(),
)


def _comparable(path: Path) -> PurePosixPath:
    """Return a drive-agnostic POSIX form for protected-location comparison.

    The protected roots are Linux deployment paths, but the guard has to be
    *exercised* on the Windows development machine, and Windows resolves an
    anchored path like ``/var/lib/garden-observatory`` against the current drive
    (``C:/var/lib/...``). Comparing the two forms directly therefore never
    matched, which silently disabled the guard in every test that ran here.

    Normalising both sides to a drive-less POSIX path makes the comparison mean
    the same thing on both platforms. Dropping the drive letter can only ever
    make the guard *stricter*, which is the right direction for a check whose
    job is to refuse.
    """
    try:
        resolved = path.resolve()
    except OSError:
        resolved = path

    text = resolved.as_posix()
    drive = resolved.drive
    if drive and text[: len(drive)].lower() == drive.lower():
        text = text[len(drive) :]
    if not text.startswith("/"):
        text = "/" + text
    return PurePosixPath(text)


def _reject_protected_target(
    work_directory: Path, extra_protected: Iterable[Path] = ()
) -> None:
    """Raise if a restore test would write into a protected location."""
    candidate = _comparable(work_directory)

    protected: list[PurePosixPath] = [
        PurePosixPath(root) for root in _PROTECTED_ROOTS
    ]
    protected.extend(_comparable(path) for path in extra_protected)

    for root in protected:
        if candidate == root or candidate.is_relative_to(root):
            raise OperationError(
                ErrorCode.RESTORE_TARGET_REJECTED,
                "A restore test may not write into the production data "
                f"location {root.as_posix()}. Restore testing is isolated by "
                "design; production restore is a separate, explicit operator "
                "procedure.",
            )


def restore_test(
    backup_path: Path,
    *,
    work_directory: Path | None = None,
    preserve: bool = False,
    database_path: Path | None = None,
    emitter: EventEmitter | None = None,
) -> RestoreTestResult:
    """Restore a backup into an isolated directory and prove it is usable.

    This is the step that turns "a backup exists" into "a backup works". The
    file is copied into a temporary directory of its own, opened there as an
    independent database, and subjected to the same integrity, schema, table and
    row-count checks a fresh backup passes. The source backup is checksummed
    before and after, so the test cannot quietly damage the thing it validates.

    It never writes into the production database directory, never replaces a
    configured database, never stops the service and never modifies the backup
    or the configuration. ``preserve`` keeps the restored copy for inspection
    after a failure; without it the directory is always removed.
    """
    events = emitter if emitter is not None else EventEmitter(BACKUP_SERVICE)
    name = backup_path.name
    checks: dict[str, str] = {}

    if backup_path.is_symlink() or not backup_path.is_file():
        return RestoreTestResult(
            backup_filename=name,
            ok=False,
            checks=checks,
            row_counts={},
            work_directory=None,
            preserved=False,
            error_code=ErrorCode.BACKUP_NOT_FOUND,
            detail=f"The backup {name} does not exist or is not a regular file.",
        )

    extra_protected = (
        [database_path.parent] if database_path is not None else []
    )

    if work_directory is not None:
        _reject_protected_target(work_directory, extra_protected)
        try:
            work_directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise OperationError(
                ErrorCode.RESTORE_TEST_FAILED,
                f"The restore-test directory could not be created: "
                f"{exc.strerror or exc}.",
            ) from exc
        root = work_directory
    else:
        root = Path(tempfile.mkdtemp(prefix="mgo-restore-test-"))
        _reject_protected_target(root, extra_protected)

    events.info(
        "restore_test.started",
        "Restoring a backup into an isolated directory for verification.",
        backup_filename=name,
    )

    try:
        before = file_sha256(backup_path)
        restored = root / "restored.db"
        shutil.copyfile(backup_path, restored)
        checks["restored_copy"] = "ok"

        facts = _inspect_database(restored)
        _require_sound_database(facts, "The restored database")
        checks["integrity"] = "ok"
        checks["schema"] = "ok"
        checks["expected_tables"] = "ok"

        manifest_path = manifest_path_for(backup_path)
        if manifest_path.is_file():
            manifest = _load_manifest(manifest_path)
            mismatched = sorted(
                table
                for table, count in manifest.table_row_counts.items()
                if facts.row_counts.get(table) != count
            )
            if mismatched:
                raise OperationError(
                    ErrorCode.RESTORE_TEST_FAILED,
                    "The restored database's row counts disagree with the "
                    f"backup manifest for: {', '.join(mismatched)}.",
                )
            checks["row_counts"] = "ok"
        else:
            checks["row_counts"] = "skipped (no manifest)"

        if file_sha256(backup_path) != before:
            raise OperationError(
                ErrorCode.RESTORE_TEST_FAILED,
                "The source backup changed during the restore test.",
            )
        checks["source_unchanged"] = "ok"

    except OperationError as exc:
        preserved = _finish_restore_directory(root, work_directory, preserve)
        events.error(
            "restore_test.failed",
            exc.message,
            error_code=exc.code,
            backup_filename=name,
        )
        return RestoreTestResult(
            backup_filename=name,
            ok=False,
            checks=checks,
            row_counts={},
            work_directory=root if preserved else None,
            preserved=preserved,
            error_code=exc.code,
            detail=exc.message,
        )
    except (OSError, sqlite3.Error) as exc:
        preserved = _finish_restore_directory(root, work_directory, preserve)
        detail = f"The restore test could not be completed: {exc}."
        events.error(
            "restore_test.failed",
            detail,
            error_code=ErrorCode.RESTORE_TEST_FAILED,
            backup_filename=name,
        )
        return RestoreTestResult(
            backup_filename=name,
            ok=False,
            checks=checks,
            row_counts={},
            work_directory=root if preserved else None,
            preserved=preserved,
            error_code=ErrorCode.RESTORE_TEST_FAILED,
            detail=detail,
        )

    preserved = _finish_restore_directory(root, work_directory, preserve)
    events.info(
        "restore_test.completed",
        "The backup restored cleanly and passed every check.",
        backup_filename=name,
        schema_version=facts.schema,
        preserved=preserved,
    )
    return RestoreTestResult(
        backup_filename=name,
        ok=True,
        checks=checks,
        row_counts=facts.row_counts,
        work_directory=root if preserved else None,
        preserved=preserved,
    )


def _finish_restore_directory(
    root: Path, requested: Path | None, preserve: bool
) -> bool:
    """Remove the restore-test directory unless preservation was requested.

    Returns whether the directory was kept. A caller-supplied directory is never
    deleted outright -- only the restored copy inside it -- because the operator
    chose that location and may have put something else there.
    """
    if preserve:
        return True
    if requested is not None:
        _cleanup(root / "restored.db")
        return False
    shutil.rmtree(root, ignore_errors=True)
    return False


__all__ = [
    "BACKUP_APPLICATION",
    "BACKUP_FILE_MODE",
    "BACKUP_FORMAT_VERSION",
    "BACKUP_NAME_PATTERN",
    "BACKUP_NAME_PREFIX",
    "BACKUP_SERVICE",
    "BACKUP_SUFFIX",
    "BACKUP_TIMESTAMP_FORMAT",
    "DEFAULT_RETENTION_COUNT",
    "EXPECTED_TABLES",
    "LOCK_FILENAME",
    "MANIFEST_SUFFIX",
    "MAX_RETENTION_COUNT",
    "TEMPORARY_PREFIX",
    "BackupListing",
    "BackupManifest",
    "BackupResult",
    "BackupSet",
    "RestoreTestResult",
    "RetentionResult",
    "VerificationResult",
    "apply_retention",
    "backup_filename",
    "backup_timestamp",
    "create_backup",
    "file_sha256",
    "list_backups",
    "manifest_filename",
    "manifest_path_for",
    "restore_test",
    "verify_backup",
]
