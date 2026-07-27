"""SQLite database management for Matt's Garden Observatory.

This module owns three responsibilities and nothing else:

* opening SQLite connections with the settings the application requires
  (foreign keys, journal mode, a bounded busy timeout);
* an ordered, transactional, idempotent schema-migration runner keyed on the
  ``schema_migrations`` table;
* reporting the recorded schema version.

Migrations are plain numbered SQL files under :data:`MIGRATIONS_DIRECTORY`.
There is no ORM and no migration framework: the application's schema is small,
the deployment is a single Raspberry Pi, and an explicit runner is far easier to
reason about during an incident than a generated one.

Transaction control is deliberately explicit. ``sqlite3.Connection``'s
``executescript`` issues an implicit ``COMMIT`` before running its script, so a
migration that fails part-way would leave its already-executed statements
committed and un-rollbackable. Each migration is therefore split into its
individual statements and executed inside one explicit ``BEGIN IMMEDIATE`` /
``COMMIT``, with a ``ROLLBACK`` on any failure.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

from mgo.core.config import PROJECT_ROOT

LOGGER = logging.getLogger(__name__)

MIGRATIONS_DIRECTORY = PROJECT_ROOT / "migrations"

#: The schema version this build of the application expects. A database
#: recording a *higher* version was written by a newer build and is rejected;
#: a lower one is upgraded by :func:`apply_migrations`.
#:
#: It is an explicit constant rather than "whatever the highest file is" so a
#: stray or half-finished migration file can never silently redefine what
#: "current" means. A test asserts it stays in step with the migration files.
CURRENT_SCHEMA_VERSION = 2

#: Bounded wait for a competing writer's lock before SQLite gives up with
#: ``database is locked``. Five seconds comfortably covers the application's
#: short single-statement transactions on a Raspberry Pi's SD card while still
#: failing fast enough to surface a genuinely stuck writer. It is a finite
#: timeout, never a retry loop.
DEFAULT_BUSY_TIMEOUT_SECONDS = 5.0

#: Upper bound accepted for a configured busy timeout.
MAX_BUSY_TIMEOUT_SECONDS = 60.0

#: The SQLite path that means "a private, transient, in-memory database".
MEMORY_DATABASE = ":memory:"

#: Journal mode requested for every file-backed database. Concurrent readers
#: never block the writer, which matters because the health monitor, the
#: observation writers and the capture archive all touch the same file.
REQUIRED_JOURNAL_MODE = "wal"


class DatabaseError(RuntimeError):
    """Raised when an MGO database operation cannot be completed."""


class IncompatibleSchemaError(DatabaseError):
    """Raised when a database's schema cannot be used by this build.

    Covers both directions of incompatibility: a database recording a schema
    version newer than :data:`CURRENT_SCHEMA_VERSION`, and a legacy unversioned
    database whose tables do not match any shape this build knows how to adopt.
    In both cases the database is left exactly as it was found.
    """


#: Tables introduced by each migration version, used only to recognise a legacy
#: *unversioned* database. It is not a schema definition -- the SQL files remain
#: the single source of truth for what a migration creates.
_VERSION_TABLES: dict[int, tuple[str, ...]] = {
    1: ("observations",),
    2: ("captures",),
}

#: The exact column set each recognisable table must have before an unversioned
#: database may be adopted at that version. A superset or subset is treated as
#: an unknown schema rather than being adopted optimistically.
_VERSION_TABLE_COLUMNS: dict[str, frozenset[str]] = {
    "observations": frozenset(
        {
            "id",
            "observed_at",
            "kind",
            "source",
            "status",
            "summary",
            "payload_json",
            "correlation_id",
            "created_at",
        }
    ),
    "captures": frozenset(
        {
            "id",
            "filename",
            "absolute_path",
            "captured_at_utc",
            "width",
            "height",
            "filesize_bytes",
            "camera_backend",
            "created_at_utc",
            "extra_metadata",
        }
    ),
}


def utc_now_iso() -> str:
    """Return the current UTC timestamp in ISO 8601 format."""
    return datetime.now(UTC).isoformat()


def is_memory_database(database_path: Path) -> bool:
    """Return whether ``database_path`` names SQLite's in-memory database.

    An in-memory database is not a file: it has no parent directory to create,
    it cannot use WAL (SQLite reports ``memory`` instead), and it disappears
    when its connection closes. Callers that care about durability must treat
    it differently, so the check is exposed rather than hidden.
    """
    return str(database_path) == MEMORY_DATABASE


def _validated_busy_timeout(busy_timeout_seconds: float) -> int:
    """Return the busy timeout in milliseconds, rejecting unusable values."""
    if busy_timeout_seconds <= 0:
        raise DatabaseError("Database busy timeout must be positive")
    if busy_timeout_seconds > MAX_BUSY_TIMEOUT_SECONDS:
        raise DatabaseError(
            "Database busy timeout must not exceed "
            f"{MAX_BUSY_TIMEOUT_SECONDS} seconds"
        )
    return int(busy_timeout_seconds * 1000)


def connect_database(
    database_path: Path,
    *,
    busy_timeout_seconds: float = DEFAULT_BUSY_TIMEOUT_SECONDS,
    create_parents: bool = True,
) -> sqlite3.Connection:
    """Open a read-write MGO SQLite database with required safety settings.

    Enables foreign-key enforcement, requests WAL journalling and applies a
    finite busy timeout. The *requested* journal mode is not assumed to have
    been granted -- callers that care read it back with :func:`journal_mode`;
    an in-memory database always reports ``memory``.

    ``create_parents`` exists so read-mostly callers (the health check) can
    refuse to bring a missing directory into existence as a side effect.
    """
    timeout_ms = _validated_busy_timeout(busy_timeout_seconds)

    if not is_memory_database(database_path) and create_parents:
        database_path.parent.mkdir(parents=True, exist_ok=True)

    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute(f"PRAGMA busy_timeout = {timeout_ms}")

    return connection


def connect_readonly(
    database_path: Path,
    *,
    busy_timeout_seconds: float = DEFAULT_BUSY_TIMEOUT_SECONDS,
) -> sqlite3.Connection:
    """Open an existing database read-only, without creating anything.

    Uses SQLite's ``mode=ro`` URI so a missing file fails cleanly instead of
    being created, and so no statement can modify the database. This is what
    makes the health check genuinely non-mutating: it can neither materialise a
    database file, nor create a missing table, nor change the journal mode.

    In-memory databases cannot be opened this way -- each connection to
    ``:memory:`` is a distinct empty database, so a "read-only" open would
    inspect something no other component can see.
    """
    if is_memory_database(database_path):
        raise DatabaseError(
            "An in-memory database cannot be opened read-only; "
            "each connection would address a different empty database"
        )

    timeout_ms = _validated_busy_timeout(busy_timeout_seconds)
    # ``quote`` protects paths containing URI-significant characters ("?", "#",
    # "%"); ":" and "/" stay literal so Windows drive letters survive.
    uri = f"file:{quote(database_path.as_posix(), safe='/:')}?mode=ro"

    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute(f"PRAGMA busy_timeout = {timeout_ms}")

    return connection


@contextmanager
def database_connection(
    database_path: Path,
    *,
    busy_timeout_seconds: float = DEFAULT_BUSY_TIMEOUT_SECONDS,
) -> Iterator[sqlite3.Connection]:
    """Provide a transactional read-write SQLite connection.

    Commits on success, rolls back on any exception, and always closes the
    connection so no handle (and no WAL sidecar lock) is leaked.
    """
    connection = connect_database(
        database_path,
        busy_timeout_seconds=busy_timeout_seconds,
    )

    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def journal_mode(connection: sqlite3.Connection) -> str:
    """Return the journal mode the database is *actually* using."""
    row = connection.execute("PRAGMA journal_mode").fetchone()
    return str(row[0]).lower() if row is not None else "unknown"


def foreign_keys_enabled(connection: sqlite3.Connection) -> bool:
    """Return whether foreign-key enforcement is active on this connection."""
    row = connection.execute("PRAGMA foreign_keys").fetchone()
    return bool(row[0]) if row is not None else False


def _table_names(connection: sqlite3.Connection) -> frozenset[str]:
    """Return every table name present in the database."""
    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    ).fetchall()
    return frozenset(str(row[0]) for row in rows)


def _column_names(connection: sqlite3.Connection, table: str) -> frozenset[str]:
    """Return the column names of ``table``.

    ``table`` is never operator-supplied -- it comes from this module's own
    ``_VERSION_TABLES`` constant -- so the unavoidable identifier interpolation
    required by ``PRAGMA table_info`` carries no injection risk.
    """
    rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
    return frozenset(str(row[1]) for row in rows)


def schema_version(connection: sqlite3.Connection) -> int | None:
    """Return the highest recorded schema version, or ``None`` if untracked.

    ``None`` means the database has no ``schema_migrations`` history at all --
    either it is brand new or it predates schema versioning. It never means
    "empty": an unversioned database may hold real data.
    """
    if "schema_migrations" not in _table_names(connection):
        return None

    row = connection.execute(
        "SELECT MAX(version) FROM schema_migrations"
    ).fetchone()
    if row is None or row[0] is None:
        return None
    return int(row[0])


def read_schema_version(
    database_path: Path,
    *,
    busy_timeout_seconds: float = DEFAULT_BUSY_TIMEOUT_SECONDS,
) -> int | None:
    """Return the recorded schema version of an existing database file.

    Read-only: it never creates the file, a table or a directory.
    """
    connection = connect_readonly(
        database_path,
        busy_timeout_seconds=busy_timeout_seconds,
    )
    try:
        return schema_version(connection)
    finally:
        connection.close()


def _migration_version(path: Path) -> int:
    """Extract the numeric migration version from a migration filename."""
    prefix = path.name.split("_", maxsplit=1)[0]

    try:
        return int(prefix)
    except ValueError as exc:
        raise DatabaseError(f"Invalid migration filename: {path.name}") from exc


def _migration_paths() -> list[Path]:
    """Return every numbered migration file in ascending version order."""
    if not MIGRATIONS_DIRECTORY.exists():
        raise DatabaseError(
            f"Migration directory does not exist: {MIGRATIONS_DIRECTORY}"
        )

    return sorted(
        MIGRATIONS_DIRECTORY.glob("[0-9][0-9][0-9]_*.sql"),
        key=_migration_version,
    )


def _is_ignorable(text: str) -> bool:
    """Return whether trailing script text is only blanks and line comments."""
    return all(
        not line.strip() or line.strip().startswith("--")
        for line in text.splitlines()
    )


def _split_statements(sql: str, *, source: str) -> list[str]:
    """Split migration SQL into individually executable statements.

    Splitting is required because ``executescript`` commits implicitly and would
    defeat per-migration rollback. ``sqlite3.complete_statement`` performs the
    split using SQLite's own notion of statement completeness, so semicolons
    inside string literals and comments do not split a statement incorrectly.
    """
    statements: list[str] = []
    buffer = ""

    for line in sql.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            statement = buffer.strip()
            if statement:
                statements.append(statement)
            buffer = ""

    if buffer.strip() and not _is_ignorable(buffer):
        raise DatabaseError(
            f"Migration {source} ends with an incomplete SQL statement"
        )

    if not statements:
        raise DatabaseError(f"Migration {source} contains no SQL statements")

    return statements


def _ensure_migration_table(connection: sqlite3.Connection) -> None:
    """Create the migration history table when it does not yet exist."""
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TEXT NOT NULL
        )
        """
    )


def _record_version(
    connection: sqlite3.Connection,
    version: int,
    name: str,
) -> None:
    """Insert one schema-migration history row."""
    connection.execute(
        """
        INSERT INTO schema_migrations (version, name, applied_at)
        VALUES (?, ?, ?)
        """,
        (version, name, utc_now_iso()),
    )


def _adoptable_version(existing_tables: frozenset[str]) -> int:
    """Return the highest version an unversioned database already satisfies.

    Walks the known versions in order and stops at the first whose tables are
    absent. Adoption is then refused -- rather than guessed at -- if a version's
    tables are only partly present, or if a *later* version's tables exist
    without the versions beneath them. The latter check matters: without it a
    database holding, say, ``captures`` but no ``observations`` would fall
    through to a normal migration run and be treated as if it were empty.

    A return of ``0`` therefore means "no recognised application table exists",
    which is the only case that may safely be treated as a new database.
    """
    adopted = 0

    for version in sorted(_VERSION_TABLES):
        tables = _VERSION_TABLES[version]
        present = [table for table in tables if table in existing_tables]

        if not present:
            break
        if len(present) != len(tables):
            missing = sorted(set(tables) - set(present))
            raise IncompatibleSchemaError(
                f"Database has a partial version {version} schema "
                f"(missing: {', '.join(missing)}); refusing to adopt it"
            )
        adopted = version

    stray = sorted(
        table
        for version, tables in _VERSION_TABLES.items()
        if version > adopted
        for table in tables
        if table in existing_tables
    )
    if stray:
        raise IncompatibleSchemaError(
            f"Database contains {', '.join(repr(name) for name in stray)} "
            f"without the schema beneath it (recognised up to version "
            f"{adopted}); refusing to adopt it"
        )

    return adopted


def _verify_adoptable_shape(
    connection: sqlite3.Connection,
    adopted_version: int,
) -> None:
    """Confirm every table being adopted has exactly its expected columns."""
    for version in range(1, adopted_version + 1):
        for table in _VERSION_TABLES[version]:
            expected = _VERSION_TABLE_COLUMNS[table]
            actual = _column_names(connection, table)
            if actual != expected:
                missing = sorted(expected - actual)
                unexpected = sorted(actual - expected)
                raise IncompatibleSchemaError(
                    f"Legacy table {table!r} does not match the supported "
                    f"schema (missing columns: {missing or 'none'}; "
                    f"unexpected columns: {unexpected or 'none'}); "
                    "refusing to adopt it"
                )


def _adopt_legacy_schema(
    connection: sqlite3.Connection,
    adopted_version: int,
    migration_names: dict[int, str],
) -> None:
    """Record versions 1..``adopted_version`` for an unversioned database.

    The tables already exist and hold real data, so nothing is executed against
    them: only the missing history rows are written, inside one transaction. If
    anything fails the database keeps its original (unversioned) state.
    """
    connection.execute("BEGIN IMMEDIATE")
    try:
        _ensure_migration_table(connection)
        for version in range(1, adopted_version + 1):
            _record_version(
                connection,
                version,
                migration_names.get(version, f"{version:03d}_adopted.sql"),
            )
    except sqlite3.Error as exc:
        connection.execute("ROLLBACK")
        raise DatabaseError(
            f"Failed to adopt the existing unversioned schema: {exc}"
        ) from exc
    connection.execute("COMMIT")

    LOGGER.warning(
        "Adopted an existing unversioned database as schema version %s; "
        "no table was created or modified",
        adopted_version,
    )


def _apply_one(
    connection: sqlite3.Connection,
    migration_path: Path,
    version: int,
) -> None:
    """Apply a single migration atomically.

    Every statement in the file and the history row that records it share one
    transaction, so the database can only ever be at the version before or the
    version after -- never part-way through.
    """
    statements = _split_statements(
        migration_path.read_text(encoding="utf-8"),
        source=migration_path.name,
    )

    connection.execute("BEGIN IMMEDIATE")
    try:
        for statement in statements:
            connection.execute(statement)
        _record_version(connection, version, migration_path.name)
    except sqlite3.Error as exc:
        connection.execute("ROLLBACK")
        raise DatabaseError(
            f"Failed to apply migration {migration_path.name} "
            f"(version {version}); the database was rolled back to "
            f"version {version - 1}: {exc}"
        ) from exc
    connection.execute("COMMIT")


def apply_migrations(
    database_path: Path,
    *,
    busy_timeout_seconds: float = DEFAULT_BUSY_TIMEOUT_SECONDS,
) -> list[int]:
    """Bring a database up to :data:`CURRENT_SCHEMA_VERSION`.

    Returns the versions applied by this call, so an already-current database
    returns ``[]``. Safe to call repeatedly and safe on all three starting
    points the deployment can present:

    * a brand-new (or absent) database, which receives every migration;
    * a database with a partial migration history, which receives the rest;
    * an existing *unversioned* database carrying the supported schema, whose
      shape is verified and then adopted without touching its data.

    Raises :class:`IncompatibleSchemaError` -- leaving the database unchanged --
    when it records a version this build does not support, or when an
    unversioned schema cannot be recognised unambiguously.
    """
    migration_paths = _migration_paths()
    migration_names = {
        _migration_version(path): path.name for path in migration_paths
    }
    applied_versions: list[int] = []

    # Explicit transaction control: autocommit mode so ``BEGIN IMMEDIATE`` /
    # ``COMMIT`` / ``ROLLBACK`` in the helpers below mean exactly what they say.
    connection = connect_database(
        database_path,
        busy_timeout_seconds=busy_timeout_seconds,
    )
    connection.isolation_level = None

    try:
        existing_tables = _table_names(connection)
        recorded = schema_version(connection)

        if recorded is None:
            # No history. Distinguish "brand new" from "unversioned but
            # populated" *before* writing anything, so an unrecognised legacy
            # schema is rejected with the database untouched.
            adoptable = _adoptable_version(existing_tables)
            if adoptable:
                _verify_adoptable_shape(connection, adoptable)
                _adopt_legacy_schema(connection, adoptable, migration_names)
                recorded = adoptable
            else:
                _ensure_migration_table(connection)
                recorded = None

        if recorded is not None and recorded > CURRENT_SCHEMA_VERSION:
            raise IncompatibleSchemaError(
                f"Database schema version {recorded} is newer than the "
                f"version this application supports "
                f"({CURRENT_SCHEMA_VERSION}); refusing to open it. "
                "Upgrade the application or restore a compatible database."
            )

        rows = connection.execute("SELECT version FROM schema_migrations").fetchall()
        already_applied = {int(row["version"]) for row in rows}

        pending = [
            path
            for path in migration_paths
            if _migration_version(path) not in already_applied
        ]
        if pending:
            LOGGER.info(
                "Applying %s database migration(s) from version %s",
                len(pending),
                recorded if recorded is not None else 0,
            )

        for migration_path in pending:
            version = _migration_version(migration_path)
            _apply_one(connection, migration_path, version)
            applied_versions.append(version)
            LOGGER.info(
                "Applied database migration %s (version %s)",
                migration_path.name,
                version,
            )

        if applied_versions:
            LOGGER.info(
                "Database schema is now at version %s", CURRENT_SCHEMA_VERSION
            )
    finally:
        connection.close()

    return applied_versions
