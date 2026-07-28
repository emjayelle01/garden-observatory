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
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from mgo.core.config import (
    SYSTEM_DATABASE_DIRECTORY,
    SYSTEM_STATE_DIRECTORY,
    MGOConfig,
    parse_config_bytes,
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
from mgo.operations.source_identity import (
    SourceAnchor,
    SourceIdentity,
    anchored_source,
    read_regular_file,
)

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
CONFIGURATION_SUFFIX = ".config.toml"
MANIFEST_SUFFIX = ".manifest.json"

#: The *only* filename shapes recognised as MGO recovery artefacts. Retention
#: deletes nothing that fails these patterns, so an operator's note, an
#: unrelated archive or a support bundle sharing the directory can never be
#: removed by a backup run.
BACKUP_NAME_PATTERN = re.compile(r"\Amgo-(\d{8}T\d{6}Z)\.db\Z")
CONFIGURATION_NAME_PATTERN = re.compile(r"\Amgo-(\d{8}T\d{6}Z)\.config\.toml\Z")
MANIFEST_NAME_PATTERN = re.compile(r"\Amgo-(\d{8}T\d{6}Z)\.manifest\.json\Z")

#: A SHA-256 digest as this tooling writes it: 64 lowercase hex characters.
#: Validated rather than assumed, so a hand-edited manifest cannot smuggle
#: arbitrary text through a field a later comparison treats as a checksum.
_SHA256_PATTERN = re.compile(r"\A[0-9a-f]{64}\Z")

#: Largest configuration file accepted into a recovery set. MGO's configuration
#: is a few kilobytes of TOML; one mebibyte is far beyond any legitimate value
#: and bounds what an unattended job will read into memory and write to the SD
#: card if the path is ever pointed at something unexpected.
MAX_CONFIGURATION_BYTES = 1024 * 1024

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


def _manifest_failure(detail: str) -> OperationError:
    """Return the error raised for any structurally invalid manifest."""
    return OperationError(ErrorCode.BACKUP_MANIFEST_FAILED, detail)


def _require_int(payload: dict[str, Any], key: str, *, minimum: int = 0) -> int:
    """Return an integer field, rejecting booleans and out-of-range values.

    ``bool`` is a subclass of ``int`` in Python, so ``isinstance(True, int)`` is
    true and an unchecked field would accept ``true`` where a size or a version
    belongs. That is exactly the kind of "parseable but meaningless" manifest
    this validation exists to reject.
    """
    if key not in payload:
        raise _manifest_failure(f"The manifest is missing '{key}'.")
    value = payload[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise _manifest_failure(f"The manifest field '{key}' is not an integer.")
    if value < minimum:
        raise _manifest_failure(
            f"The manifest field '{key}' is {value}, below the minimum "
            f"{minimum}."
        )
    return value


def _require_text(payload: dict[str, Any], key: str) -> str:
    """Return a non-empty string field."""
    if key not in payload:
        raise _manifest_failure(f"The manifest is missing '{key}'.")
    value = payload[key]
    if not isinstance(value, str) or not value.strip():
        raise _manifest_failure(
            f"The manifest field '{key}' is not a non-empty string."
        )
    return value


def _require_digest(payload: dict[str, Any], key: str) -> str:
    """Return a well-formed SHA-256 field."""
    value = _require_text(payload, key)
    if not _SHA256_PATTERN.match(value):
        raise _manifest_failure(
            f"The manifest field '{key}' is not a SHA-256 digest."
        )
    return value


def _require_basename(payload: dict[str, Any], key: str) -> str:
    """Return a field that must be a bare filename, never a path.

    A manifest travels with its backup and is read by tooling that may later
    join these values onto a directory. An absolute path or one containing a
    separator would turn a description into a traversal, so both are refused
    here rather than sanitised later.
    """
    value = _require_text(payload, key)
    if "/" in value or "\\" in value:
        raise _manifest_failure(
            f"The manifest field '{key}' contains a directory separator; it "
            "must be a bare filename."
        )
    if PurePosixPath(value).is_absolute() or PureWindowsPath(value).is_absolute():
        raise _manifest_failure(
            f"The manifest field '{key}' is an absolute path; it must be a "
            "bare filename."
        )
    if value in {".", ".."}:
        raise _manifest_failure(f"The manifest field '{key}' is not a filename.")
    return value


def _require_matching_name(
    payload: dict[str, Any], key: str, pattern: re.Pattern[str]
) -> str:
    """Return a filename field that matches the tooling's own naming."""
    value = _require_basename(payload, key)
    if not pattern.match(value):
        raise _manifest_failure(
            f"The manifest field '{key}' ({value!r}) is not a name this "
            "tooling produces."
        )
    return value


def _require_timestamp(payload: dict[str, Any], key: str) -> str:
    """Return an ISO 8601 timestamp field."""
    value = _require_text(payload, key)
    try:
        datetime.fromisoformat(value)
    except ValueError as exc:
        raise _manifest_failure(
            f"The manifest field '{key}' is not an ISO 8601 timestamp."
        ) from exc
    return value


def _require_row_counts(payload: dict[str, Any]) -> dict[str, int]:
    """Return the row-count map, requiring exactly the expected tables.

    An *exact* set is required rather than "whatever keys are present". The
    original implementation compared only the keys the manifest happened to
    carry, so a manifest with an empty ``table_row_counts`` object verified
    successfully against any database at all -- the check silently did nothing.
    """
    if "table_row_counts" not in payload:
        raise _manifest_failure("The manifest is missing 'table_row_counts'.")
    raw = payload["table_row_counts"]
    if not isinstance(raw, dict):
        raise _manifest_failure(
            "The manifest field 'table_row_counts' is not an object."
        )

    counts: dict[str, int] = {}
    for name, value in raw.items():
        if not isinstance(name, str):
            raise _manifest_failure("A row-count key is not a string.")
        if isinstance(value, bool) or not isinstance(value, int):
            raise _manifest_failure(
                f"The row count for {name!r} is not an integer."
            )
        if value < 0:
            raise _manifest_failure(f"The row count for {name!r} is negative.")
        counts[name] = value

    expected = set(EXPECTED_TABLES)
    actual = set(counts)
    if actual != expected:
        missing = sorted(expected - actual)
        unexpected = sorted(actual - expected)
        raise _manifest_failure(
            "The manifest's row counts do not cover exactly the expected "
            f"tables (missing: {missing or 'none'}; "
            f"unexpected: {unexpected or 'none'})."
        )
    return counts


@dataclass(frozen=True)
class BackupManifest:
    """The recorded description of one completed recovery set.

    A recovery set is three files -- the database snapshot, the production
    configuration snapshot and this manifest -- and the manifest is what binds
    them together. It is written **last**, so its presence is the marker that a
    set is complete.

    Everything here is either a logical label, a measurement of a file in the
    set or a count. There is deliberately **no** absolute path, no configuration
    *content*, no environment value, no username, no hostname and no database
    row content: a manifest travels with a backup, and a backup may be copied
    somewhere less private than the Pi.

    ``source_database_name`` and ``configuration_source_name`` are the source
    files' *names* only, for the same reason ``/database/status`` reports a name
    rather than a path.
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
    configuration_source_name: str
    configuration_filename: str
    configuration_size_bytes: int
    configuration_sha256: str

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
            "configuration_source_name": self.configuration_source_name,
            "configuration_filename": self.configuration_filename,
            "configuration_size_bytes": self.configuration_size_bytes,
            "configuration_sha256": self.configuration_sha256,
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
        """Parse and **structurally validate** a manifest body.

        Being parseable is not enough. A manifest is the evidence a restore
        decision rests on, so every field is checked for the right type, the
        right shape and a plausible range before it is trusted: booleans are
        refused where integers belong, sizes and versions may not be negative,
        checksums must look like SHA-256, filenames must match the names this
        tooling produces and must be bare basenames, and the row-count map must
        cover exactly the expected tables.

        Raises :class:`OperationError` with
        :attr:`~mgo.operations.errors.ErrorCode.BACKUP_MANIFEST_FAILED` rather
        than a ``KeyError`` or ``TypeError``: a hand-edited, truncated or
        foreign manifest is an expected operational condition, not a
        programming error.
        """
        if not isinstance(payload, dict):
            raise _manifest_failure("The backup manifest is not a JSON object.")

        version = _require_int(payload, "format_version", minimum=1)
        # Exactly the supported format. A forward-compatibility policy would
        # have to define which fields may be absent and what they default to;
        # none is implemented, so accepting an older or newer manifest would be
        # accepting one whose meaning this build cannot actually know.
        if version != BACKUP_FORMAT_VERSION:
            raise _manifest_failure(
                f"The backup manifest uses format version {version}; this "
                f"build supports exactly version {BACKUP_FORMAT_VERSION}."
            )

        return cls(
            format_version=version,
            created_at=_require_timestamp(payload, "created_at"),
            application=_require_text(payload, "application"),
            application_version=_require_text(payload, "application_version"),
            source_database_name=_require_basename(payload, "source_database_name"),
            backup_filename=_require_matching_name(
                payload, "backup_filename", BACKUP_NAME_PATTERN
            ),
            backup_size_bytes=_require_int(payload, "backup_size_bytes"),
            sha256=_require_digest(payload, "sha256"),
            schema_version=_require_int(payload, "schema_version"),
            expected_schema_version=_require_int(
                payload, "expected_schema_version"
            ),
            integrity=_require_text(payload, "integrity"),
            journal_mode_of_backup=_require_text(
                payload, "journal_mode_of_backup"
            ),
            table_row_counts=_require_row_counts(payload),
            configuration_source_name=_require_basename(
                payload, "configuration_source_name"
            ),
            configuration_filename=_require_matching_name(
                payload, "configuration_filename", CONFIGURATION_NAME_PATTERN
            ),
            configuration_size_bytes=_require_int(
                payload, "configuration_size_bytes"
            ),
            configuration_sha256=_require_digest(payload, "configuration_sha256"),
        )


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
    """A completed, validated and published recovery set (three files)."""

    backup_path: Path
    configuration_path: Path
    manifest_path: Path
    manifest: BackupManifest
    duration_ms: int
    retention: RetentionResult | None = None

    def as_dict(self) -> dict[str, Any]:
        """Return the serialisable summary (names only, never full paths).

        The configuration's checksum and size are reported; its **contents**
        never are.
        """
        return {
            "backup_filename": self.backup_path.name,
            "configuration_filename": self.configuration_path.name,
            "manifest_filename": self.manifest_path.name,
            "backup_size_bytes": self.manifest.backup_size_bytes,
            "sha256": self.manifest.sha256,
            "configuration_size_bytes": self.manifest.configuration_size_bytes,
            "configuration_sha256": self.manifest.configuration_sha256,
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
    """A complete recovery set: database, configuration and manifest."""

    timestamp: str
    backup_path: Path
    configuration_path: Path
    manifest_path: Path


@dataclass(frozen=True)
class BackupListing:
    """Every complete recovery set in a directory, plus what was ignored.

    Orphans are reported rather than hidden, and each kind separately. A
    database with no configuration is a different problem from a manifest with
    no database, and an operator deciding what they can restore needs to know
    which one they have. Silently omitting them would make a directory holding
    seven half-sets look empty.
    """

    directory: Path
    sets: tuple[BackupSet, ...] = ()
    orphan_backups: tuple[str, ...] = ()
    orphan_configurations: tuple[str, ...] = ()
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
                    "configuration_filename": item.configuration_path.name,
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
            "orphan_configurations": list(self.orphan_configurations),
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


def configuration_filename(timestamp: str) -> str:
    """Return the configuration-snapshot filename for a timestamp component."""
    return f"{BACKUP_NAME_PREFIX}{timestamp}{CONFIGURATION_SUFFIX}"


def manifest_filename(timestamp: str) -> str:
    """Return the manifest filename for a timestamp component."""
    return f"{BACKUP_NAME_PREFIX}{timestamp}{MANIFEST_SUFFIX}"


def manifest_path_for(backup_path: Path) -> Path:
    """Return the manifest path that belongs to a backup file."""
    return backup_path.with_name(
        backup_path.name[: -len(BACKUP_SUFFIX)] + MANIFEST_SUFFIX
    )


def configuration_path_for(backup_path: Path) -> Path:
    """Return the configuration-snapshot path that belongs to a backup file."""
    return backup_path.with_name(
        backup_path.name[: -len(BACKUP_SUFFIX)] + CONFIGURATION_SUFFIX
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

    These are the cheap, early checks that produce a clear message for the
    ordinary mistakes: an in-memory database, a missing file, a directory, a
    symlink. They are **not** the security boundary — a path can change between
    this check and SQLite's open of it. That gap is closed separately by the
    identity anchor held across the connection in :func:`_copy_database`.

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


@dataclass(frozen=True, repr=False)
class ConfigurationSnapshot:
    """One securely-read configuration: the bytes, and what they parse to.

    This exists so that a backup run reads the configuration **exactly once**.
    Previously the file was parsed to find the database and then opened again
    later to be snapshotted, which left a window in which an administrator
    replacing the configuration would have paired a database chosen from one
    version with bytes from another — a recovery set that describes a pairing
    that never existed.

    The bytes are authoritative. ``config`` is parsed *from these bytes*, not
    from a second read of the file, so the snapshot in the recovery set is
    provably the configuration that selected the database.

    ``repr`` is overridden. A configuration may hold credentials, and a
    dataclass's generated ``repr`` would print them into any traceback, log
    line or test failure that happened to include this object.
    """

    source_name: str
    data: bytes
    sha256: str
    identity: SourceIdentity
    config: MGOConfig

    @property
    def size_bytes(self) -> int:
        """The size of the captured configuration."""
        return len(self.data)

    def __repr__(self) -> str:
        """Return a description that cannot leak configuration content."""
        return (
            f"ConfigurationSnapshot(source_name={self.source_name!r}, "
            f"size_bytes={self.size_bytes}, sha256={self.sha256!r})"
        )


def capture_configuration(configuration_path: Path) -> ConfigurationSnapshot:
    """Read, validate and parse the production configuration exactly once.

    The bytes are preserved **exactly**: the file is never normalised or
    rewritten. A configuration that round-tripped through a TOML parser would
    lose its comments and its ordering, and a restore would then hand the
    operator something merely equivalent rather than identical.

    Reading is delegated to :func:`~mgo.operations.source_identity.read_regular_file`,
    which refuses a symbolic link **at the open itself** (``O_NOFOLLOW`` on
    Linux) rather than by checking the path first and opening it afterwards,
    and which proves that the path still names the object that was opened. The
    previous check-then-open sequence was correct only for a path that did not
    change underneath it.

    The parsed configuration comes from these same bytes, via the application's
    own parser, so there is no second read and no second schema.

    Contents are never logged, never placed in an event, never put in the
    manifest and never included in a support bundle.
    """
    data, identity = read_regular_file(
        configuration_path,
        max_bytes=MAX_CONFIGURATION_BYTES,
        subject=f"The configuration {configuration_path.name}",
        code=ErrorCode.BACKUP_CONFIGURATION_UNAVAILABLE,
    )

    try:
        parsed = parse_config_bytes(data)
    except UnicodeDecodeError as exc:
        raise OperationError(
            ErrorCode.BACKUP_CONFIGURATION_UNAVAILABLE,
            f"The configuration {configuration_path.name} is not valid UTF-8.",
        ) from exc
    except (ValueError, KeyError, TypeError) as exc:
        # Deliberately does not echo the exception's text: a TOML parse error
        # quotes the offending line, which may be the line holding a secret.
        raise OperationError(
            ErrorCode.BACKUP_CONFIGURATION_UNAVAILABLE,
            f"The configuration {configuration_path.name} could not be parsed "
            f"({type(exc).__name__}); a recovery set must contain a "
            "configuration the application can actually load.",
        ) from exc

    return ConfigurationSnapshot(
        source_name=configuration_path.name,
        data=data,
        sha256=hashlib.sha256(data).hexdigest(),
        identity=identity,
        config=parsed,
    )


def _write_configuration_snapshot(target: Path, payload: bytes) -> None:
    """Write a configuration snapshot atomically with restrictive permissions.

    The temporary file is created by :func:`tempfile.mkstemp`, which opens it
    ``0600`` before any bytes are written, so the contents are never briefly
    readable by the group even in the window before the final ``chmod``.
    """
    descriptor, temporary_name = tempfile.mkstemp(
        dir=target.parent, prefix=TEMPORARY_PREFIX, suffix=TEMPORARY_SUFFIX
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
        _set_file_mode(temporary, BACKUP_FILE_MODE)
        _fsync_file(temporary)
        os.replace(temporary, target)
        _fsync_directory(target.parent)
    except OSError as exc:
        _cleanup(temporary)
        raise OperationError(
            ErrorCode.BACKUP_CONFIGURATION_UNAVAILABLE,
            f"The configuration snapshot could not be written: "
            f"{exc.strerror or exc}.",
        ) from exc


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


def _copy_database(source: Path, target: Path, anchor: SourceAnchor) -> str:
    """Copy ``source`` into ``target`` as one consistent snapshot.

    Returns the journal mode the finished copy reports.

    The source connection is read-only, so nothing here can modify, checkpoint
    or vacuum the production database. The whole copy is taken in a single step
    (``pages=0``) rather than incrementally: the observation database on a
    Raspberry Pi is small, one step yields the strongest consistency guarantee,
    and it removes the possibility of a busy writer restarting the copy
    repeatedly.

    ``anchor`` pins the identity of the file that was validated for this run.
    SQLite opens the database **by name**, so between validation and that open
    the path could be repointed at another file; the anchor is therefore held
    open across the connect and the path re-checked immediately afterwards. A
    substitution in that window fails the run before anything is published.

    ``immutable=1`` is deliberately not used to sidestep this: the production
    database is live and carries WAL state, and telling SQLite it cannot change
    would produce an inconsistent read rather than a safe one.

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

    # SQLite has now opened the path. Prove it opened the file this run
    # validated, before a single page is copied out of it.
    try:
        anchor.verify(
            subject="The source database",
            code=ErrorCode.BACKUP_SOURCE_IDENTITY_CHANGED,
        )
    except OperationError:
        source_connection.close()
        raise

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
    configuration: ConfigurationSnapshot,
    destination: Path,
    keep: int = DEFAULT_RETENTION_COUNT,
    emitter: EventEmitter | None = None,
    now: datetime | None = None,
) -> BackupResult:
    """Take, validate and publish one complete recovery set.

    A recovery set is **three** files: the database snapshot, the production
    configuration snapshot and the manifest that binds them. The plan requires
    both the database *and* the configuration to be backed up, and a database
    restored onto a machine whose configuration was lost is only half a
    recovery.

    The sequence is deliberate and each step guards the next:

    1. validate the retention count and the source database *before* anything
       is created;
    2. take the destination lock, so no second run can interleave;
    3. refuse if any of the three final names already exists;
    4. hold an **identity anchor** on the source database, and keep it open
       across SQLite's own open of that path;
    5. build the database copy in a temporary file **in the destination
       filesystem**, so the final rename is atomic rather than a cross-device
       copy;
    6. open the copy independently and prove it is a sound, compatible MGO
       database;
    7. publish the database, then the configuration, then -- **last** -- the
       manifest.

    The configuration is **not read here**. It arrives already captured, as a
    :class:`ConfigurationSnapshot`, because the same bytes must be the ones that
    chose the database path: reading the file a second time at this point would
    reintroduce exactly the pairing hazard the snapshot exists to remove.

    The manifest is written last on purpose: it is the completion marker, so a
    set is only ever complete once every file it describes is already in place.
    If publication fails part-way, the recovery files published so far are
    removed, so no manifest can survive claiming a set that is not there. A
    cleanup that itself fails is reported truthfully rather than swallowed.

    Retention failure is reported but does not invalidate the set just
    published.
    """
    events = emitter if emitter is not None else EventEmitter(BACKUP_SERVICE)
    started = datetime.now(UTC)

    keep = _validated_keep(keep)
    source = _validated_source(database_path)
    destination = _prepared_destination(destination)

    events.info(
        "backup.started",
        "Starting a consistent SQLite backup with its configuration.",
        source_database_name=source.name,
        configuration_source_name=configuration.source_name,
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
        final_configuration = destination / configuration_filename(stamp)
        final_manifest = destination / manifest_filename(stamp)

        if (
            final_backup.exists()
            or final_configuration.exists()
            or final_manifest.exists()
        ):
            raise OperationError(
                ErrorCode.BACKUP_ALREADY_EXISTS,
                f"A recovery set named {final_backup.name} already exists. An "
                "existing backup is never overwritten; retry in a moment.",
            )

        descriptor, temporary_name = tempfile.mkstemp(
            dir=destination, prefix=TEMPORARY_PREFIX, suffix=TEMPORARY_SUFFIX
        )
        os.close(descriptor)
        temporary = Path(temporary_name)

        try:
            # The anchor is opened before SQLite and held until the copy is
            # finished, so the inode cannot be recycled underneath the check.
            with anchored_source(
                source,
                subject="The source database",
                code=ErrorCode.BACKUP_SOURCE_UNAVAILABLE,
            ) as anchor:
                mode = _copy_database(source, temporary, anchor)

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
                configuration_source_name=configuration.source_name,
                configuration_filename=final_configuration.name,
                configuration_size_bytes=configuration.size_bytes,
                configuration_sha256=configuration.sha256,
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

        # From here the database snapshot is published. Any later failure must
        # roll the set back so no manifest claims a set that is incomplete.
        try:
            _write_configuration_snapshot(final_configuration, configuration.data)
        except OperationError:
            _rollback_partial_set(events, final_backup)
            raise

        try:
            _write_manifest(final_manifest, manifest)
        except OperationError:
            _rollback_partial_set(events, final_backup, final_configuration)
            raise

        duration_ms = int(
            (datetime.now(UTC) - started).total_seconds() * 1000
        )
        events.info(
            "backup.completed",
            "The recovery set was validated and published.",
            backup_filename=final_backup.name,
            backup_size_bytes=size,
            sha256=checksum,
            schema_version=backup_schema,
            configuration_filename=final_configuration.name,
            configuration_size_bytes=manifest.configuration_size_bytes,
            configuration_sha256=manifest.configuration_sha256,
            duration_ms=duration_ms,
        )

        retention = _run_retention(destination, keep, events)

    return BackupResult(
        backup_path=final_backup,
        configuration_path=final_configuration,
        manifest_path=final_manifest,
        manifest=manifest,
        duration_ms=duration_ms,
        retention=retention,
    )


def _rollback_partial_set(events: EventEmitter, *published: Path) -> None:
    """Remove recovery files published before the set could be completed.

    Without this, a failed manifest write would leave a database snapshot (and
    possibly a configuration snapshot) that ``list`` reports as an orphan and
    that an operator could mistake for a usable backup.

    A cleanup failure is reported rather than hidden: the operator needs to know
    that files were left behind, and a rollback that silently fails is how a
    directory fills with half-sets nobody can account for.
    """
    for path in published:
        try:
            path.unlink()
        except OSError as exc:
            events.error(
                "backup.rollback_failed",
                f"A partially published recovery file could not be removed: "
                f"{path.name} ({exc.strerror or exc}). Remove it by hand; it "
                "is not a usable backup.",
                error_code=ErrorCode.BACKUP_SET_INCOMPLETE,
                orphan_filename=path.name,
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
    """Return every complete recovery set in ``directory``, newest first.

    A set is complete only when **all three** files are present with the same
    timestamp: the database snapshot, the configuration snapshot and the
    manifest. Orphans of each kind and in-progress temporary files are reported
    separately and are never treated as backups -- neither by this listing nor
    by retention.
    """
    if not directory.is_dir():
        raise OperationError(
            ErrorCode.BACKUP_NOT_FOUND,
            f"The backup directory {directory.name} does not exist.",
        )

    backups: dict[str, Path] = {}
    configurations: dict[str, Path] = {}
    manifests: dict[str, Path] = {}
    temporary: list[str] = []

    for entry in sorted(directory.iterdir()):
        if not entry.is_file():
            continue
        name = entry.name
        if name.startswith(TEMPORARY_PREFIX):
            temporary.append(name)
            continue
        for pattern, collected in (
            (BACKUP_NAME_PATTERN, backups),
            (CONFIGURATION_NAME_PATTERN, configurations),
            (MANIFEST_NAME_PATTERN, manifests),
        ):
            match = pattern.match(name)
            if match:
                collected[match.group(1)] = entry
                break

    complete = sorted(
        set(backups) & set(configurations) & set(manifests), reverse=True
    )
    sets = tuple(
        BackupSet(
            timestamp=stamp,
            backup_path=backups[stamp],
            configuration_path=configurations[stamp],
            manifest_path=manifests[stamp],
        )
        for stamp in complete
    )
    complete_stamps = set(complete)

    verifications: dict[str, VerificationResult] = {}
    if verify:
        for item in sets:
            verifications[item.backup_path.name] = verify_backup(item.backup_path)

    return BackupListing(
        directory=directory,
        sets=sets,
        orphan_backups=tuple(
            sorted(
                path.name
                for stamp, path in backups.items()
                if stamp not in complete_stamps
            )
        ),
        orphan_configurations=tuple(
            sorted(
                path.name
                for stamp, path in configurations.items()
                if stamp not in complete_stamps
            )
        ),
        orphan_manifests=tuple(
            sorted(
                path.name
                for stamp, path in manifests.items()
                if stamp not in complete_stamps
            )
        ),
        temporary_files=tuple(sorted(temporary)),
        verifications=verifications,
    )


def apply_retention(directory: Path, keep: int) -> RetentionResult:
    """Keep the newest ``keep`` complete recovery sets and remove the rest.

    Only complete three-file sets are candidates, and a set is removed
    **manifest first**. That order matters: the manifest is the completion
    marker, so if deletion is interrupted part-way the remains are recognisable
    orphans rather than a manifest still advertising a set whose database has
    already gone.

    Nothing outside the recovery-artefact name patterns is ever touched, so
    support bundles, operator notes, temporary files and unrelated files sharing
    the directory are safe. The newest set can never be removed, because
    ``keep`` is validated to be at least one.
    """
    keep = _validated_keep(keep)
    listing = list_backups(directory)

    kept = listing.sets[:keep]
    expiring = listing.sets[keep:]

    removed: list[str] = []
    failures: list[str] = []
    removed_bytes = 0

    for item in expiring:
        for path in (item.backup_path, item.configuration_path):
            with suppress(OSError):
                removed_bytes += path.stat().st_size

        # Manifest first: it is what makes the set look complete.
        for path in (item.manifest_path, item.backup_path, item.configuration_path):
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
    """Verify a complete recovery set against its manifest. Read-only.

    Never raises: every failure is returned as a result with a stable error
    code, because ``verify`` is frequently run over a whole directory and one
    bad set must not abort the sweep.

    Verification is **binding**, not merely structural. Beyond checking that the
    manifest parses, every recorded value is compared against the artefact it
    claims to describe: both filenames, both sizes, both checksums, the schema
    version, the expected schema version, the integrity verdict, the backup's
    journal mode and the exact row counts for exactly the expected tables. A
    manifest that is internally tidy but describes a different set is precisely
    the failure this exists to catch.

    Nothing is written, moved, repaired or re-checksummed on disk, and no
    configuration content is read into the result.
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
    if not BACKUP_NAME_PATTERN.match(name):
        return failed(
            ErrorCode.BACKUP_NOT_FOUND,
            f"{name} is not a name this tooling produces, so it is not part of "
            "a recovery set.",
        )
    checks["backup_present"] = "ok"

    manifest_path = manifest_path_for(backup_path)
    configuration_path = configuration_path_for(backup_path)

    if not manifest_path.is_file():
        return failed(
            ErrorCode.BACKUP_SET_INCOMPLETE,
            f"The backup {name} has no manifest at {manifest_path.name}; the "
            "recovery set is incomplete.",
        )
    if configuration_path.is_symlink() or not configuration_path.is_file():
        return failed(
            ErrorCode.BACKUP_SET_INCOMPLETE,
            f"The backup {name} has no configuration snapshot at "
            f"{configuration_path.name}; the recovery set is incomplete.",
        )
    checks["set_complete"] = "ok"

    try:
        manifest = _load_manifest(manifest_path)
    except OperationError as exc:
        return failed(exc.code, exc.message)
    checks["manifest_structure"] = "ok"

    # --- the manifest must describe *these* files ---------------------------
    if manifest.backup_filename != name:
        return failed(
            ErrorCode.BACKUP_SET_INCOMPLETE,
            f"The manifest describes {manifest.backup_filename!r}, not {name!r}.",
        )
    if manifest.configuration_filename != configuration_path.name:
        return failed(
            ErrorCode.BACKUP_SET_INCOMPLETE,
            f"The manifest names configuration {manifest.configuration_filename!r}, "
            f"not {configuration_path.name!r}.",
        )
    checks["filenames"] = "ok"

    if manifest.expected_schema_version != CURRENT_SCHEMA_VERSION:
        return failed(
            ErrorCode.BACKUP_SCHEMA_INCOMPATIBLE,
            f"The manifest records an expected schema version of "
            f"{manifest.expected_schema_version}; this build expects "
            f"{CURRENT_SCHEMA_VERSION}.",
        )

    # --- sizes and checksums ------------------------------------------------
    try:
        actual_size = backup_path.stat().st_size
        actual_configuration_size = configuration_path.stat().st_size
    except OSError as exc:
        return failed(
            ErrorCode.BACKUP_NOT_FOUND,
            f"The recovery set could not be read: {exc.strerror or exc}.",
        )

    if actual_size != manifest.backup_size_bytes:
        return failed(
            ErrorCode.BACKUP_CHECKSUM_MISMATCH,
            f"The backup is {actual_size} bytes but its manifest records "
            f"{manifest.backup_size_bytes}.",
        )
    if actual_configuration_size != manifest.configuration_size_bytes:
        return failed(
            ErrorCode.BACKUP_CHECKSUM_MISMATCH,
            f"The configuration snapshot is {actual_configuration_size} bytes "
            f"but its manifest records {manifest.configuration_size_bytes}.",
        )
    checks["size"] = "ok"

    try:
        actual_checksum = file_sha256(backup_path)
        actual_configuration_checksum = file_sha256(configuration_path)
    except OSError as exc:
        return failed(
            ErrorCode.BACKUP_NOT_FOUND,
            f"The recovery set could not be read for checksumming: "
            f"{exc.strerror or exc}.",
        )

    if actual_checksum != manifest.sha256:
        return failed(
            ErrorCode.BACKUP_CHECKSUM_MISMATCH,
            "The backup's SHA-256 does not match its manifest; the file has "
            "changed since it was written.",
        )
    if actual_configuration_checksum != manifest.configuration_sha256:
        return failed(
            ErrorCode.BACKUP_CHECKSUM_MISMATCH,
            "The configuration snapshot's SHA-256 does not match its manifest; "
            "the file has changed since it was written.",
        )
    checks["sha256"] = "ok"

    # --- the database itself -------------------------------------------------
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
        actual_schema = _require_sound_database(facts, f"The backup {name}")
    except OperationError as exc:
        checks["integrity"] = (
            "ok" if facts.integrity.lower() == "ok" else facts.integrity
        )
        return failed(exc.code, exc.message)

    if facts.integrity != manifest.integrity:
        return failed(
            ErrorCode.BACKUP_INTEGRITY_FAILED,
            f"The backup reports integrity {facts.integrity!r} but its manifest "
            f"records {manifest.integrity!r}.",
        )
    checks["integrity"] = "ok"

    if actual_schema != manifest.schema_version:
        return failed(
            ErrorCode.BACKUP_SCHEMA_INCOMPATIBLE,
            f"The backup is at schema version {actual_schema} but its manifest "
            f"records {manifest.schema_version}.",
        )
    checks["schema"] = "ok"
    checks["expected_tables"] = "ok"

    if facts.journal != manifest.journal_mode_of_backup:
        return failed(
            ErrorCode.BACKUP_INTEGRITY_FAILED,
            f"The backup's journal mode is {facts.journal!r} but its manifest "
            f"records {manifest.journal_mode_of_backup!r}.",
        )
    checks["journal_mode"] = "ok"

    # Exact comparison in both directions. Iterating only the manifest's keys
    # would let an empty or partial map verify against any database at all.
    if facts.row_counts != manifest.table_row_counts:
        differing = sorted(
            table
            for table in set(facts.row_counts) | set(manifest.table_row_counts)
            if facts.row_counts.get(table) != manifest.table_row_counts.get(table)
        )
        return failed(
            ErrorCode.BACKUP_INTEGRITY_FAILED,
            "The backup's row counts disagree with its manifest for: "
            f"{', '.join(differing)}.",
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


#: Fixed names the restore test writes inside its isolated directory. Constants
#: rather than derived names, so the test can never be induced to write
#: somewhere unexpected by an oddly named backup.
RESTORED_DATABASE_NAME = "restored.db"
RESTORED_CONFIGURATION_NAME = "restored-mgo.toml"


def restore_test(
    backup_path: Path,
    *,
    work_directory: Path | None = None,
    preserve: bool = False,
    database_path: Path | None = None,
    emitter: EventEmitter | None = None,
) -> RestoreTestResult:
    """Restore a complete recovery set into isolation and prove it is usable.

    This is the step that turns "a backup exists" into "a backup works", so it
    tests a **complete verified set** rather than an arbitrary SQLite file. Full
    set verification runs *first*: a missing manifest, a missing configuration
    snapshot, a checksum mismatch or a semantically inconsistent manifest all
    fail before a single byte is copied. There is deliberately no "no manifest,
    so skip the row-count check" path -- a restore test that quietly skips its
    most important assertion is worse than no restore test.

    Both artefacts are then copied into an isolated directory under fixed names
    and checked: database integrity, schema compatibility, expected tables,
    exact row counts, and the configuration snapshot's checksum. Both sources
    are checksummed before and after, so the test cannot quietly damage the
    thing it validates.

    The restored configuration is **not activated**: nothing is pointed at it,
    no API is started and no production path is written. It never writes into
    the production database directory, never replaces a configured database and
    never stops the service. ``preserve`` keeps the restored copies for
    inspection; without it they are always removed.
    """
    events = emitter if emitter is not None else EventEmitter(BACKUP_SERVICE)
    name = backup_path.name
    checks: dict[str, str] = {}

    def refused(code: ErrorCode, detail: str) -> RestoreTestResult:
        return RestoreTestResult(
            backup_filename=name,
            ok=False,
            checks=checks,
            row_counts={},
            work_directory=None,
            preserved=False,
            error_code=code,
            detail=detail,
        )

    if backup_path.is_symlink() or not backup_path.is_file():
        return refused(
            ErrorCode.BACKUP_NOT_FOUND,
            f"The backup {name} does not exist or is not a regular file.",
        )

    # Verify the whole set before copying anything. Everything below depends on
    # the manifest being trustworthy, so it is established first.
    verification = verify_backup(backup_path)
    if not verification.ok:
        events.error(
            "restore_test.failed",
            verification.detail,
            error_code=verification.error_code or ErrorCode.RESTORE_TEST_FAILED,
            backup_filename=name,
        )
        return refused(
            verification.error_code or ErrorCode.RESTORE_TEST_FAILED,
            f"The recovery set failed verification, so it was not restored: "
            f"{verification.detail}",
        )
    checks["set_verified"] = "ok"

    configuration_backup = configuration_path_for(backup_path)
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

    restored = root / RESTORED_DATABASE_NAME
    restored_configuration = root / RESTORED_CONFIGURATION_NAME

    # An operator-supplied work directory may hold anything. Refuse before
    # writing rather than overwrite whatever is already there.
    for target in (restored, restored_configuration):
        if target.exists() or target.is_symlink():
            raise OperationError(
                ErrorCode.RESTORE_TARGET_EXISTS,
                f"{target.name} already exists in the restore-test directory. "
                "The restore test never overwrites an existing file; choose an "
                "empty directory or remove it first.",
            )

    events.info(
        "restore_test.started",
        "Restoring a verified recovery set into an isolated directory.",
        backup_filename=name,
        configuration_filename=configuration_backup.name,
    )

    try:
        before = file_sha256(backup_path)
        before_configuration = file_sha256(configuration_backup)

        shutil.copyfile(backup_path, restored)
        shutil.copyfile(configuration_backup, restored_configuration)
        checks["restored_copy"] = "ok"

        facts = _inspect_database(restored)
        _require_sound_database(facts, "The restored database")
        checks["integrity"] = "ok"
        checks["schema"] = "ok"
        checks["expected_tables"] = "ok"

        manifest = _load_manifest(manifest_path_for(backup_path))
        if facts.row_counts != manifest.table_row_counts:
            differing = sorted(
                table
                for table in set(facts.row_counts) | set(manifest.table_row_counts)
                if facts.row_counts.get(table)
                != manifest.table_row_counts.get(table)
            )
            raise OperationError(
                ErrorCode.RESTORE_TEST_FAILED,
                "The restored database's row counts disagree with the backup "
                f"manifest for: {', '.join(differing)}.",
            )
        checks["row_counts"] = "ok"

        # The restored configuration is checked, never parsed, never applied.
        if file_sha256(restored_configuration) != manifest.configuration_sha256:
            raise OperationError(
                ErrorCode.RESTORE_TEST_FAILED,
                "The restored configuration's checksum does not match the "
                "backup manifest.",
            )
        checks["restored_configuration"] = "ok"

        if file_sha256(backup_path) != before:
            raise OperationError(
                ErrorCode.RESTORE_TEST_FAILED,
                "The source backup changed during the restore test.",
            )
        if file_sha256(configuration_backup) != before_configuration:
            raise OperationError(
                ErrorCode.RESTORE_TEST_FAILED,
                "The source configuration snapshot changed during the restore "
                "test.",
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
    """Remove the restore-test artefacts unless preservation was requested.

    Returns whether they were kept. A caller-supplied directory is never deleted
    outright -- only the files this test created inside it -- because the
    operator chose that location and may have put something else there.
    """
    if preserve:
        return True
    if requested is not None:
        _cleanup(root / RESTORED_DATABASE_NAME)
        _cleanup(root / RESTORED_CONFIGURATION_NAME)
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
    "CONFIGURATION_NAME_PATTERN",
    "CONFIGURATION_SUFFIX",
    "DEFAULT_RETENTION_COUNT",
    "EXPECTED_TABLES",
    "LOCK_FILENAME",
    "MANIFEST_NAME_PATTERN",
    "MANIFEST_SUFFIX",
    "MAX_CONFIGURATION_BYTES",
    "MAX_RETENTION_COUNT",
    "RESTORED_CONFIGURATION_NAME",
    "RESTORED_DATABASE_NAME",
    "TEMPORARY_PREFIX",
    "BackupListing",
    "BackupManifest",
    "BackupResult",
    "BackupSet",
    "ConfigurationSnapshot",
    "RestoreTestResult",
    "RetentionResult",
    "VerificationResult",
    "apply_retention",
    "backup_filename",
    "backup_timestamp",
    "capture_configuration",
    "configuration_filename",
    "configuration_path_for",
    "create_backup",
    "file_sha256",
    "list_backups",
    "manifest_filename",
    "manifest_path_for",
    "restore_test",
    "verify_backup",
]
