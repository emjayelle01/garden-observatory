"""Operator command line for MGO database backups.

Four subcommands, each doing exactly one thing:

``backup``        take, validate and publish a backup, then apply retention;
``verify``        check a backup against its manifest (read-only);
``restore-test``  restore into an isolated directory and prove it works;
``list``          show the complete backup sets in a directory.

There is deliberately **no** ``restore`` command. Restoring over a live
production database is a disaster-recovery decision with consequences a script
cannot weigh: it needs the service stopped, a check that the failure is really
corruption rather than a full disk, and a copy of the damaged database kept for
analysis. ``restore-test`` proves a backup *can* be restored; the production
procedure stays in ``docs/Operations.md`` where an operator reads it and decides.

The module is a thin boundary. It parses arguments, resolves paths, calls
:mod:`mgo.operations.backup`, prints one JSON summary and maps the outcome to an
exit status. All business logic lives behind it, which is what lets the whole of
it be exercised on Windows with a temporary database.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import IO, Any

from mgo.core.config import (
    SYSTEM_BACKUP_DIRECTORY,
    load_config,
)
from mgo.operations.backup import (
    BACKUP_SERVICE,
    DEFAULT_RETENTION_COUNT,
    create_backup,
    list_backups,
    restore_test,
    verify_backup,
)
from mgo.operations.errors import ErrorCode, OperationError
from mgo.operations.events import EventEmitter, Severity

#: Exit statuses. ``2`` is reserved for a usage error so it cannot be confused
#: with an operational failure, matching ``argparse``'s own convention.
EXIT_SUCCESS = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2


def _resolve_database_path(
    explicit: Path | None, config_path: Path | None
) -> Path:
    """Resolve the database to back up.

    Precedence is ``--database`` then the selected MGO configuration. There is
    no third source and no second configuration system: the application already
    has one authority for where its database lives, and a backup tool that
    disagreed with it would faithfully back up the wrong file.
    """
    if explicit is not None:
        return explicit

    try:
        config = load_config(config_path)
    except FileNotFoundError as exc:
        raise OperationError(
            ErrorCode.CONFIGURATION_UNAVAILABLE,
            f"The configuration file could not be found: {exc}.",
        ) from exc
    except (OSError, ValueError, KeyError) as exc:
        raise OperationError(
            ErrorCode.CONFIGURATION_UNAVAILABLE,
            f"The configuration could not be loaded: {exc}.",
        ) from exc

    return config.storage.database_path


def _build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for every subcommand."""
    parser = argparse.ArgumentParser(
        prog="mgo-backup",
        description=(
            "Take, verify and restore-test consistent backups of the MGO "
            "SQLite database. Safe to run while the API service is serving."
        ),
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    take = subcommands.add_parser(
        "backup",
        help="Take, validate and publish a backup, then apply retention.",
    )
    take.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Configuration file to read the database path from.",
    )
    take.add_argument(
        "--database",
        type=Path,
        default=None,
        help="Database file to back up (overrides the configuration).",
    )
    take.add_argument(
        "--output-directory",
        type=Path,
        default=None,
        help=(
            "Directory to write the backup into "
            f"(default: {SYSTEM_BACKUP_DIRECTORY.as_posix()})."
        ),
    )
    take.add_argument(
        "--keep",
        type=int,
        default=DEFAULT_RETENTION_COUNT,
        help=(
            "Number of complete backup sets to retain "
            f"(default: {DEFAULT_RETENTION_COUNT})."
        ),
    )

    check = subcommands.add_parser(
        "verify",
        help="Verify a backup against its manifest. Read-only.",
    )
    check.add_argument("backup", type=Path, help="Backup file to verify.")

    rehearse = subcommands.add_parser(
        "restore-test",
        help="Restore a backup into an isolated directory and verify it.",
    )
    rehearse.add_argument("backup", type=Path, help="Backup file to restore.")
    rehearse.add_argument(
        "--work-directory",
        type=Path,
        default=None,
        help=(
            "Directory to restore into (default: a new temporary directory). "
            "Production data locations are refused."
        ),
    )
    rehearse.add_argument(
        "--preserve",
        action="store_true",
        help="Keep the restored copy for inspection instead of removing it.",
    )
    rehearse.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Configuration file, used only to protect its database directory.",
    )

    catalogue = subcommands.add_parser(
        "list", help="List the complete backup sets in a directory."
    )
    catalogue.add_argument(
        "--output-directory",
        type=Path,
        default=None,
        help=(
            "Directory to list "
            f"(default: {SYSTEM_BACKUP_DIRECTORY.as_posix()})."
        ),
    )
    catalogue.add_argument(
        "--no-verify",
        action="store_true",
        help="List without verifying each set (faster, less informative).",
    )

    return parser


def _default_output_directory(explicit: Path | None) -> Path:
    """Return the backup directory, defaulting to the deployment constant."""
    if explicit is not None:
        return explicit
    return Path(SYSTEM_BACKUP_DIRECTORY.as_posix())


def _print_summary(payload: dict[str, Any], stream: IO[str]) -> None:
    """Write the machine-readable command summary."""
    stream.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    stream.flush()


def _run_backup(
    arguments: argparse.Namespace, events: EventEmitter, stream: IO[str]
) -> int:
    """Execute the ``backup`` subcommand."""
    database = _resolve_database_path(arguments.database, arguments.config)
    destination = _default_output_directory(arguments.output_directory)

    result = create_backup(
        database_path=database,
        destination=destination,
        keep=arguments.keep,
        emitter=events,
    )

    summary = result.as_dict()
    retention_ok = result.retention is None or result.retention.succeeded
    summary["result"] = "success" if retention_ok else "retention_failed"
    _print_summary(summary, stream)

    # A published, valid backup with a failed retention pass is still a backup,
    # but the operator must be told the directory is growing. Non-zero is the
    # only signal a timer-driven run can give.
    return EXIT_SUCCESS if retention_ok else EXIT_FAILURE


def _run_verify(
    arguments: argparse.Namespace, events: EventEmitter, stream: IO[str]
) -> int:
    """Execute the ``verify`` subcommand."""
    result = verify_backup(arguments.backup)
    _print_summary(result.as_dict(), stream)

    if result.ok:
        events.info(
            "verify.completed",
            "The backup matches its manifest and passed every check.",
            backup_filename=result.backup_filename,
        )
        return EXIT_SUCCESS

    events.error(
        "verify.failed",
        result.detail,
        error_code=result.error_code or ErrorCode.UNEXPECTED_ERROR,
        backup_filename=result.backup_filename,
    )
    return EXIT_FAILURE


def _run_restore_test(
    arguments: argparse.Namespace, events: EventEmitter, stream: IO[str]
) -> int:
    """Execute the ``restore-test`` subcommand."""
    database: Path | None
    try:
        database = _resolve_database_path(None, arguments.config)
    except OperationError:
        # A restore test does not need the configuration; it is read only so the
        # configured database directory can be added to the protected list.
        # Without it the canonical production paths still apply.
        database = None

    result = restore_test(
        arguments.backup,
        work_directory=arguments.work_directory,
        preserve=arguments.preserve,
        database_path=database,
        emitter=events,
    )
    _print_summary(result.as_dict(), stream)
    return EXIT_SUCCESS if result.ok else EXIT_FAILURE


def _run_list(
    arguments: argparse.Namespace, events: EventEmitter, stream: IO[str]
) -> int:
    """Execute the ``list`` subcommand."""
    directory = _default_output_directory(arguments.output_directory)
    listing = list_backups(directory, verify=not arguments.no_verify)
    _print_summary(listing.as_dict(), stream)

    unverified = [
        name
        for name, verification in listing.verifications.items()
        if not verification.ok
    ]
    if unverified:
        events.error(
            "list.verification_failed",
            "One or more backup sets failed verification.",
            error_code=ErrorCode.BACKUP_INTEGRITY_FAILED,
            failed=unverified,
        )
        return EXIT_FAILURE

    events.info(
        "list.completed",
        "Listed the complete backup sets.",
        complete_sets=len(listing.sets),
        orphan_backups=len(listing.orphan_backups),
        orphan_manifests=len(listing.orphan_manifests),
    )
    return EXIT_SUCCESS


_COMMANDS = {
    "backup": _run_backup,
    "verify": _run_verify,
    "restore-test": _run_restore_test,
    "list": _run_list,
}


def main(
    argv: Sequence[str] | None = None,
    *,
    stdout: IO[str] | None = None,
    stderr: IO[str] | None = None,
) -> int:
    """Run the backup CLI and return its exit status.

    Structured events go to ``stderr`` and the machine-readable summary to
    ``stdout``, so a caller can pipe the summary into ``jq`` while the journal
    still receives the event stream. ``systemd`` captures both.

    Every exception is caught here and nowhere else. An expected failure keeps
    its stable error code; an unexpected one becomes
    :attr:`~mgo.operations.errors.ErrorCode.UNEXPECTED_ERROR` so the two remain
    distinguishable in the journal. No traceback is printed: it would be the
    only part of the output that could carry a filesystem path or a value from
    the database into a log an operator forwards elsewhere.
    """
    out = stdout if stdout is not None else sys.stdout
    err = stderr if stderr is not None else sys.stderr
    events = EventEmitter(BACKUP_SERVICE, stream=err)

    parser = _build_parser()
    try:
        arguments = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else EXIT_USAGE

    try:
        return _COMMANDS[arguments.command](arguments, events, out)
    except OperationError as exc:
        events.emit(
            Severity.ERROR,
            f"{arguments.command}.failed".replace("-", "_"),
            exc.message,
            error_code=exc.code,
        )
        _print_summary(
            {"result": "failed", "error_code": exc.code.value, "detail": exc.message},
            out,
        )
        return EXIT_FAILURE
    except Exception as exc:
        message = f"An unexpected error ended the operation: {type(exc).__name__}."
        events.error(
            f"{arguments.command}.failed".replace("-", "_"),
            message,
            error_code=ErrorCode.UNEXPECTED_ERROR,
        )
        _print_summary(
            {
                "result": "failed",
                "error_code": ErrorCode.UNEXPECTED_ERROR.value,
                "detail": message,
            },
            out,
        )
        return EXIT_FAILURE


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())


__all__ = ["EXIT_FAILURE", "EXIT_SUCCESS", "EXIT_USAGE", "main"]
