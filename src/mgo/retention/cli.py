"""Operator command line for capture-media retention.

Two subcommands, and deliberately only two:

``plan``      show what the configured policy would select. Read-only: it
              changes no MGO-managed state and issues no SQL write.
``run-once``  execute exactly one bounded retention run. Potentially destructive,
              and gated three separate ways before it can remove anything.

This is the first supported way to invoke the Task 14.1 retention engine. It is
a thin boundary: it parses arguments, checks preconditions, calls the retention
domain service, prints one JSON document and maps the outcome to an exit status.
Every policy decision, every safety refusal and every lifecycle transition still
happens behind it, in code that was reviewed as Task 14.1.

Four things this command deliberately does **not** do.

* **It does not schedule anything.** There is no daemon, no interval, no loop
  and no retry. ``run-once`` runs once and exits, even when the result reports
  that more eligible work remains -- a second run is a second operator decision,
  and making it automatic here would be a scheduler by another name.
* **It does not accept policy.** There is no ``--max-age-days``, no
  ``--max-managed-bytes``, no ``--capture-id`` and no date range. The command
  executes the *configured, reviewed* policy or it executes nothing. An operator
  who could retune the policy at the prompt could turn a reviewed retention
  policy into an unreviewed deletion at the moment of deletion.
* **It does not delete a named file.** There is no ``delete``, ``purge`` or
  ``rm``. The only thing exposed is the existing policy engine.
* **It does not migrate.** Schema migration belongs to application startup. An
  operator asking to *look at* retention must never silently upgrade a database
  as a side effect, so a database that is not already at the expected version is
  refused rather than repaired.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import IO, Any

from mgo.core.config import CONFIG_PATH_ENV, MGOConfig, load_config
from mgo.core.database import (
    CURRENT_SCHEMA_VERSION,
    DatabaseError,
    read_schema_version,
)
from mgo.retention.models import RetentionPlan, RetentionRuntimeState
from mgo.retention.repository import (
    RetentionCatalogueError,
    RetentionRepository,
    RetentionRepositoryError,
)
from mgo.retention.service import RetentionService

#: The console command this module backs.
COMMAND_NAME = "mgo-retention"

#: Exit statuses.
#:
#: ``0`` and ``2`` carry the same meaning they already carry in the backup and
#: support-bundle commands -- success, and a refusal the operator can fix -- so an
#: operator reading exit codes across MGO's tools is not learning two systems.
#: The remaining values separate the three ways this command can decline or fail,
#: because "the database is at the wrong schema version" and "retention ran and
#: hit a safety refusal" call for very different responses.
#:
#: ``1`` is deliberately unused: the other MGO commands use it for a generic
#: operational failure, and every failure here is either bounded and categorised
#: or genuinely unexpected. Nothing about a capture -- no id, filename or policy
#: reason -- is ever encoded into an exit code.
EXIT_SUCCESS = 0
EXIT_REFUSED = 2
EXIT_SCHEMA = 3
EXIT_RETENTION_ERROR = 4
EXIT_UNEXPECTED = 5

#: The one public sentence each refusal is allowed to say. Fixed text, so no
#: configuration path, database location, capture root or exception message can
#: ride out on an operator-facing refusal.
REFUSAL_MISSING_EXECUTE = (
    "Refusing to run: run-once requires the explicit --execute flag."
)
REFUSAL_RETENTION_DISABLED = (
    "Refusing to run: retention is disabled in the selected configuration."
)
REFUSAL_NO_EXPLICIT_CONFIG = (
    f"Refusing to run: {CONFIG_PATH_ENV} must be set to the configuration this "
    "run should act on."
)
REFUSAL_RELATIVE_CONFIG = (
    f"Refusing to run: {CONFIG_PATH_ENV} must be an absolute path, so the "
    "configuration this run acts on does not depend on the working directory."
)
REFUSAL_CONFIGURATION_INVALID = (
    "Refusing to run: the selected configuration could not be loaded."
)
SCHEMA_REFUSAL = (
    "Refusing to proceed: the database is not at the schema version this build "
    f"expects ({CURRENT_SCHEMA_VERSION})."
)
CATALOGUE_REFUSAL = (
    "Refusing to proceed: the capture catalogue could not be read safely."
)
UNEXPECTED_FAILURE = "The command failed unexpectedly."


class _Refusal(Exception):
    """A precondition the operator can see and fix. Carries a fixed message."""

    def __init__(self, message: str, code: int) -> None:
        super().__init__(message)
        self.message = message
        self.code = code


def _emit(payload: dict[str, Any], stream: IO[str]) -> None:
    """Write one JSON document to ``stream``.

    Machine-readable by default because the audience is an operator piping this
    into something. Sorted keys and a trailing newline keep the output stable
    enough to diff between runs.
    """
    stream.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _load_configuration() -> MGOConfig:
    """Load configuration through the application's own loader and no other.

    There is deliberately no ``--config`` here, unlike the backup command. A
    retention run deletes media, and configuration identity decides *which*
    media: allowing an arbitrary path on the command line would make the most
    dangerous input the easiest one to get wrong.

    The exception list is the set the *existing* loader can legitimately raise
    for a malformed configuration, established by reading it rather than by
    guessing:

    * ``OSError`` -- the file is missing or unreadable (``FileNotFoundError``);
    * ``ValueError`` -- ``tomllib`` cannot parse it (``TOMLDecodeError``), a
      numeric conversion fails, or a validator rejects a value;
    * ``KeyError`` -- a required section or key is absent;
    * ``TypeError`` -- a value has the wrong container type, so ``int(...)`` or
      ``float(...)`` is handed a list, or a string is subscripted as a table;
    * ``AttributeError`` -- a section is a scalar rather than a table, so
      ``.get()`` does not exist on it.

    The last two were missing, and a syntactically valid file with a wrongly
    typed value therefore reported an *unexpected internal failure* rather than
    the configuration problem it is. That is the wrong answer twice over: it
    tells the operator to look at MGO instead of at their file, and it spends the
    exit code reserved for genuine defects on an ordinary mistake.

    ``Exception`` is deliberately not caught wholesale. Turning every programmer
    defect inside the loader into "bad configuration" would hide real bugs behind
    a message blaming the operator.
    """
    try:
        return load_config()
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        AttributeError,
    ) as exc:
        raise _Refusal(REFUSAL_CONFIGURATION_INVALID, EXIT_REFUSED) from exc


def _require_explicit_configuration() -> None:
    """Require the destructive path to name its configuration deliberately.

    ``plan`` may use the ordinary resolution rules, because looking is safe. A
    destructive run may not. Without this, an operator standing in the
    repository could run ``run-once --execute`` and have it resolve the tracked
    *development* configuration -- and the command would then be pointed at
    whichever database and capture directory that file happens to name.

    Requiring ``MGO_CONFIG_PATH`` makes the operator state which deployment they
    mean before anything is deleted, not after.

    It must also be **absolute**. The application's own resolution rules accept a
    relative value and resolve it against the current working directory, which is
    correct for general configuration but too weak for this gate: the whole point
    is that the operator has identified *one deployment*, and
    ``MGO_CONFIG_PATH=config/mgo.toml`` names a different file after a ``cd``.
    The same environment value would then select a different database and a
    different capture directory to delete from.

    A relative path is refused rather than resolved on the operator's behalf.
    Silently making it absolute would produce exactly the outcome this gate
    exists to prevent -- a deletion against whatever that path happened to mean
    at that moment -- while looking like it had been checked. The operator states
    the stable identity themselves.

    This is a CLI-only destructive gate. It changes nothing about
    :func:`~mgo.core.config.resolve_config_path`, and nothing about ``plan``,
    which is read-only and may use the ordinary rules.
    """
    raw = os.environ.get(CONFIG_PATH_ENV)
    if raw is None or not raw.strip():
        raise _Refusal(REFUSAL_NO_EXPLICIT_CONFIG, EXIT_REFUSED)

    # The supplied value is never echoed: it is operator-supplied text that may
    # itself be a path worth keeping out of a forwarded log.
    if not Path(raw.strip()).expanduser().is_absolute():
        raise _Refusal(REFUSAL_RELATIVE_CONFIG, EXIT_REFUSED)


def _require_current_schema(database_path: Path) -> None:
    """Refuse unless the database already records exactly the expected version.

    This command never migrates. Schema migration is the application's job at
    startup, where it is transactional, logged and part of a deployment that was
    reviewed. An operator asking to inspect or execute retention must not be able
    to upgrade a database as a side effect of looking at it -- and an *older*
    database is precisely the case where a silent upgrade would be most
    tempting and least safe.

    Every non-matching case is refused identically: missing, unreadable,
    unversioned, older and newer. The read is done through the read-only
    schema-version helper, so a missing database is not created by the check.
    """
    try:
        version = read_schema_version(database_path)
    except (DatabaseError, sqlite3.Error, OSError) as exc:
        # ``sqlite3.Error`` matters as much as the others: a *missing* database
        # surfaces as ``sqlite3.OperationalError`` from the read-only open, and
        # letting it escape would report the most ordinary operator mistake --
        # pointing the command at the wrong path -- as an unexpected internal
        # failure instead of the refusal it is.
        raise _Refusal(SCHEMA_REFUSAL, EXIT_SCHEMA) from exc

    if version != CURRENT_SCHEMA_VERSION:
        raise _Refusal(SCHEMA_REFUSAL, EXIT_SCHEMA)


def _build_service(config: MGOConfig) -> RetentionService:
    """Construct the retention service from configuration. Inert.

    Creating these objects opens no connection, reads no catalogue and touches
    no file: the repository holds a path, and the service holds a lock.
    """
    return RetentionService(
        config.retention,
        RetentionRepository(config.storage.database_path),
        RetentionRuntimeState(enabled=config.retention.enabled),
        config.camera.capture_directory,
        database_path=config.storage.database_path,
    )


def _operator_plan(
    plan: RetentionPlan, *, retention_enabled: bool
) -> dict[str, Any]:
    """Project a plan for operator output, without the catalogue filename.

    ``RetentionPlan.as_dict()`` includes each candidate's ``filename``, and that
    value has been validated only as *a non-empty string*. It has not passed the
    path/filename safety boundary, because that boundary belongs to destructive
    execution and running it here would make a preview disagree with the pure
    policy selection it is supposed to report.

    So a damaged or hand-edited catalogue can hold a "filename" like
    ``/var/lib/garden-observatory/db/mgo.db`` or ``../../private/file.jpg``, and
    publishing it would put that string into operator output that the contract
    says carries no path of any kind.

    The filename is therefore **omitted**, not sanitised and not reduced to a
    basename: anything derived from an unverified value is still derived from it,
    and the capture id identifies the record completely. The candidate keeps
    every fact a planning decision actually rests on.

    This is an output projection only. It re-implements no policy and no
    catalogue interpretation, and ``RetentionCandidate.filename`` is untouched --
    destructive execution still needs it, and still validates it before unlink.
    """
    payload = plan.as_dict()
    payload["candidates"] = [
        {key: value for key, value in candidate.items() if key != "filename"}
        for candidate in payload["candidates"]
    ]
    payload["retention_enabled"] = retention_enabled
    return payload


def _plan(arguments: argparse.Namespace, stream: IO[str]) -> int:
    """Show what the configured policy would select. Mutates nothing.

    Runs whether or not retention is enabled -- previewing a policy is how an
    operator decides whether to enable it. No filesystem validation happens
    here: the filesystem is only ever a veto during destructive execution, and
    stat-ing candidates during a preview would make ``plan`` report a different
    set from the one the policy actually chose.
    """
    config = _load_configuration()
    _require_current_schema(config.storage.database_path)

    try:
        plan = _build_service(config).dry_run()
    except RetentionCatalogueError as exc:
        raise _Refusal(CATALOGUE_REFUSAL, EXIT_SCHEMA) from exc
    except RetentionRepositoryError as exc:
        raise _Refusal(CATALOGUE_REFUSAL, EXIT_SCHEMA) from exc

    payload = _operator_plan(plan, retention_enabled=config.retention.enabled)
    _emit(payload, stream)
    return EXIT_SUCCESS


def _run_once(arguments: argparse.Namespace, stream: IO[str]) -> int:
    """Execute exactly one bounded retention run.

    Three gates, checked before anything is opened or read:

    1. the exact ``--execute`` flag;
    2. ``MGO_CONFIG_PATH`` naming the configuration deliberately;
    3. ``retention.enabled = true`` in that configuration.

    Then the schema gate, then one -- exactly one -- call into the retention
    service. The result is reported and the command exits, including when it
    says more eligible work remains: that is a fact for the operator, not a
    trigger.
    """
    if not arguments.execute:
        raise _Refusal(REFUSAL_MISSING_EXECUTE, EXIT_REFUSED)

    _require_explicit_configuration()
    config = _load_configuration()

    if not config.retention.enabled:
        raise _Refusal(REFUSAL_RETENTION_DISABLED, EXIT_REFUSED)

    _require_current_schema(config.storage.database_path)

    result = _build_service(config).run_once()

    _emit(
        {
            "executed": result.executed,
            "enabled": result.enabled,
            "candidate_count": result.candidate_count,
            "deleted_count": result.deleted_count,
            "bytes_reclaimed": result.bytes_reclaimed,
            "recovered_count": result.recovered_count,
            "more_work_remains": result.more_work_remains,
            "error_category": (
                result.error_category.value
                if result.error_category is not None
                else None
            ),
            "error_message": result.error_message,
        },
        stream,
    )
    return (
        EXIT_RETENTION_ERROR
        if result.error_category is not None
        else EXIT_SUCCESS
    )


_COMMANDS = {
    "plan": _plan,
    "run-once": _run_once,
}


def build_parser() -> argparse.ArgumentParser:
    """Return the argument parser. Exposed so tests can inspect the contract."""
    parser = argparse.ArgumentParser(
        prog=COMMAND_NAME,
        description=(
            "Preview or execute capture-media retention. 'plan' is read-only. "
            "'run-once --execute' can permanently delete capture media."
        ),
        epilog=(
            "Neither command schedules future work: run-once executes exactly "
            "one run and exits, even when more eligible work remains. "
            "Retention policy is not supplied on the command line -- the "
            f"configured policy is executed as reviewed. Set {CONFIG_PATH_ENV} "
            "to choose the configuration a destructive run acts on."
        ),
    )
    subcommands = parser.add_subparsers(dest="command", metavar="COMMAND")
    subcommands.required = True

    subcommands.add_parser(
        "plan",
        help="Show what the configured policy would select. Read-only.",
        description=(
            "Read-only. Evaluates the configured retention policy and prints "
            "the candidates it would select. Creates no lifecycle row, deletes "
            "no file, records no observation and applies no migration."
        ),
    )

    run_once = subcommands.add_parser(
        "run-once",
        help="Execute exactly one bounded retention run. Can delete media.",
        description=(
            "Executes exactly one retention run, which may permanently delete "
            "capture media. Requires the --execute flag, requires "
            f"{CONFIG_PATH_ENV} to be set, and requires retention.enabled = "
            "true in that configuration. Runs once and exits; it never repeats "
            "because more work remains."
        ),
    )
    run_once.add_argument(
        "--execute",
        action="store_true",
        help=(
            "Required. Confirms that this invocation may delete capture media. "
            "Without it the command refuses before touching anything."
        ),
    )

    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    stdout: IO[str] | None = None,
    stderr: IO[str] | None = None,
) -> int:
    """Run one command and return its exit status.

    Nothing escapes as a traceback. An unexpected exception is reduced to one
    fixed sentence and :data:`EXIT_UNEXPECTED`: a traceback is the one part of
    the output that could carry a capture path, a database location or a
    configuration value into a log an operator forwards somewhere else.
    """
    out = stdout if stdout is not None else sys.stdout
    err = stderr if stderr is not None else sys.stderr

    parser = build_parser()
    try:
        arguments = parser.parse_args(argv)
    except SystemExit as exc:
        # argparse already reported the usage problem; map its status onto this
        # command's refusal code so an operator sees one vocabulary.
        return EXIT_SUCCESS if exc.code == 0 else EXIT_REFUSED

    try:
        return _COMMANDS[arguments.command](arguments, out)
    except _Refusal as refusal:
        err.write(refusal.message + "\n")
        return refusal.code
    except Exception:
        err.write(UNEXPECTED_FAILURE + "\n")
        return EXIT_UNEXPECTED


if __name__ == "__main__":  # pragma: no cover - console entry point
    raise SystemExit(main())


__all__ = [
    "COMMAND_NAME",
    "EXIT_REFUSED",
    "EXIT_RETENTION_ERROR",
    "EXIT_SCHEMA",
    "EXIT_SUCCESS",
    "EXIT_UNEXPECTED",
    "REFUSAL_RELATIVE_CONFIG",
    "build_parser",
    "main",
]
