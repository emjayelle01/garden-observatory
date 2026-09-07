"""Operator command line for capture-media retention.

Two subcommands, and deliberately only two:

``plan``      show what the configured policy would select. Read-only: it
              changes no MGO-managed state and issues no SQL write.
``run-once``  execute exactly one bounded retention run. Potentially destructive,
              and gated three separate ways before it can remove anything.

Two properties of the argument surface are part of the safety contract rather
than presentation. Long-option **abbreviation is disabled**, so ``--execute``
means ``--execute`` and nothing shorter authorises a deletion; and an invalid
invocation is refused through this command's own bounded message rather than
argparse's, which would otherwise echo the operator's own arguments -- paths
included -- into stderr before anything could intercept them.

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
from typing import IO, Any, NoReturn

from mgo.core.config import (
    CONFIG_PATH_ENV,
    SYSTEM_BACKUP_DIRECTORY,
    MGOConfig,
    load_config,
)
from mgo.core.database import (
    CURRENT_SCHEMA_VERSION,
    DatabaseError,
    read_schema_version,
)
from mgo.operations.backup import LOCK_FILENAME as BACKUP_LOCK_FILENAME
from mgo.retention.models import (
    RetentionErrorCategory,
    RetentionPlan,
    RetentionRuntimeState,
)
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
REFUSAL_MISSING_EXECUTE_SCHEDULED = (
    "Refusing to run: scheduled-run requires the explicit --execute flag."
)
REFUSAL_RELATIVE_BACKUP_DIRECTORY = (
    "Refusing to run: --backup-directory must be an absolute path."
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
REFUSAL_INVALID_ARGUMENTS = "Refusing to run: invalid command arguments."
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


class _BoundedParser(argparse.ArgumentParser):
    """An ``ArgumentParser`` with two of this command's safety rules built in.

    **Abbreviation is off.** ``argparse`` accepts unambiguous prefixes of long
    options by default, so ``--exe``, ``--exec`` and even ``--e`` were all
    accepted as ``--execute`` -- which turned the one deliberate destructive
    authorisation spelling into a family of them, and let a typo satisfy the
    gate. Setting ``allow_abbrev=False`` on the top-level parser alone is not
    enough: ``add_subparsers`` builds each subparser through this class but with
    ``argparse``'s own constructor defaults, so the flag has to default off
    *here*, where every parser and subparser inherits it.

    **Argument errors are this command's refusals.** ``ArgumentParser.error()``
    writes its own diagnostic -- a usage dump plus the offending argument text --
    straight to ``sys.stderr`` and only then raises ``SystemExit``. Catching
    ``SystemExit`` afterwards is too late: the text is already written, it names
    whatever the operator typed (``--config /etc/garden-observatory/mgo.toml``
    rides out verbatim), and it bypasses the stream the caller injected. Raising
    the bounded refusal instead means one fixed sentence, on the caller's stream,
    with nothing operator-supplied in it.

    ``--help`` is untouched: it exits through ``parser.exit()``, not
    ``error()``, and still prints the ordinary static help text successfully.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("allow_abbrev", False)
        super().__init__(*args, **kwargs)

    def error(self, message: str) -> NoReturn:
        """Refuse with a fixed sentence instead of reporting ``message``.

        ``message`` is deliberately discarded rather than wrapped or truncated:
        it is built from the operator's own argument text, and a truncated path
        is still a path.
        """
        raise _Refusal(REFUSAL_INVALID_ARGUMENTS, EXIT_REFUSED)


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
      ``.get()`` does not exist on it;
    * ``OverflowError`` -- an ``int(...)`` conversion is handed a floating-point
      infinity. TOML has a literal ``inf``, so ``collection_interval_seconds =
      inf`` is *syntactically valid*, parses to ``float('inf')`` and reaches
      ``int(...)``, which raises ``OverflowError``. That is not a ``ValueError``
      subclass, so it escaped the boundary entirely.

    Each of these was missing at some point, and a syntactically valid file with
    a wrongly typed value therefore reported an *unexpected internal failure*
    rather than the configuration problem it is. That is the wrong answer twice
    over: it tells the operator to look at MGO instead of at their file, and it
    spends the exit code reserved for genuine defects on an ordinary mistake.

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
        OverflowError,
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

    It must also be **absolute as supplied**. The application's own resolution
    rules accept a relative value and resolve it against the current working
    directory, which is correct for general configuration but too weak for this
    gate: the whole point is that the operator has identified *one deployment*,
    and ``MGO_CONFIG_PATH=config/mgo.toml`` names a different file after a
    ``cd``. The same environment value would then select a different database
    and a different capture directory to delete from.

    ``~`` is not expanded before that decision, and that is the whole difference
    between a stable identity and a contextual one. ``~/mgo.toml`` is not an
    absolute path; it is an instruction to look in *the executing account's*
    home directory, so the same string names a different configuration under a
    different account -- exactly the context-dependence this gate exists to
    remove, merely a different context from the working directory. The test is
    therefore on the value as supplied, not on what it happens to expand to for
    whoever is running.

    A relative value -- ``~`` forms included -- is refused rather than resolved
    on the operator's behalf. Silently making it absolute would produce exactly
    the outcome this gate exists to prevent, a deletion against whatever that
    path happened to mean at that moment, while looking like it had been
    checked. The operator states the stable identity themselves.

    An already-expanded absolute path is of course accepted: a home directory is
    not the problem, the *deferred interpretation* of one is.

    This is a CLI-only destructive gate. It changes nothing about
    :func:`~mgo.core.config.resolve_config_path`, and nothing about ``plan``,
    which is read-only and may use the ordinary rules.
    """
    raw = os.environ.get(CONFIG_PATH_ENV)
    if raw is None or not raw.strip():
        raise _Refusal(REFUSAL_NO_EXPLICIT_CONFIG, EXIT_REFUSED)

    # The supplied value is never echoed: it is operator-supplied text that may
    # itself be a path worth keeping out of a forwarded log.
    if not Path(raw.strip()).is_absolute():
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


def _build_service(
    config: MGOConfig, *, backup_directory: Path | None = None
) -> RetentionService:
    """Construct the retention service from configuration. Inert.

    Creating these objects opens no connection, reads no catalogue and touches
    no file: the repository holds a path, and the service holds a lock.

    ``backup_directory`` names where a running backup announces itself. The
    scheduled command passes the directory the timer was installed with; the
    manual command uses the canonical production location, so an operator's
    ``run-once`` also yields to the nightly backup.
    """
    backup_root = (
        backup_directory
        if backup_directory is not None
        else Path(str(SYSTEM_BACKUP_DIRECTORY))
    )
    return RetentionService(
        config.retention,
        RetentionRepository(config.storage.database_path),
        RetentionRuntimeState(enabled=config.retention.enabled),
        config.camera.capture_directory,
        database_path=config.storage.database_path,
        backup_lock_path=backup_root / BACKUP_LOCK_FILENAME,
    )


#: The only candidate fields the operator command publishes, listed in the
#: order it builds them.
#:
#: An **allow-list**, deliberately, rather than a filename deny-list. A
#: deny-list publishes everything it has not been told to withhold, so a field
#: added to :class:`~mgo.retention.models.RetentionCandidate` later reaches
#: operator output by default -- and whoever adds it has to remember, a second
#: time and in a different file, that this projection exists. An allow-list
#: makes silence the default: a new field is published only when someone
#: decides to publish it here.
#:
#: JSON output is emitted with sorted keys regardless, so this order governs
#: construction rather than presentation; it is fixed so the projection is
#: deterministic either way.
_OPERATOR_CANDIDATE_FIELDS = (
    "capture_id",
    "captured_at",
    "filesize_bytes",
    "policy_reason",
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

    It is omitted by *not being selected*, not by being removed. The projection
    names the fields it publishes -- :data:`_OPERATOR_CANDIDATE_FIELDS` -- so
    anything else in ``as_dict()`` is dropped whether or not this code has heard
    of it. The earlier spelling excluded ``"filename"`` by name, which meant a
    path-bearing field added to the domain model later would have been published
    by default, and the omission would have had to be remembered again in
    another file. Here the default is silence.

    A *missing* safe field is a different matter and is deliberately not
    tolerated: indexing raises ``KeyError``, which reaches
    :data:`EXIT_UNEXPECTED` as the internal defect it is. Quietly emitting a
    short candidate would hide a real bug behind output that still looked
    plausible.

    This is an output projection only. It re-implements no policy and no
    catalogue interpretation, and ``RetentionCandidate.filename`` is untouched --
    destructive execution still needs it, and still validates it before unlink.
    """
    payload = plan.as_dict()
    payload["candidates"] = [
        {field: candidate[field] for field in _OPERATOR_CANDIDATE_FIELDS}
        for candidate in payload["candidates"]
    ]
    payload["retention_enabled"] = retention_enabled
    return payload


def _plan(arguments: argparse.Namespace, stream: IO[str]) -> int:
    """Show what the configured policy would select.

    No MGO-managed state changes. Specifically: no SQL write, no lifecycle row
    created or modified, no media deleted, no observation recorded, no database
    or parent directory created, and no journal-mode change. The one thing not
    claimed is that no byte moves on disk -- reading a live WAL database
    requires SQLite to create or use its own ``-shm`` shared-memory index, which
    is SQLite's documented read mechanism rather than anything this command
    chooses. ``immutable=1`` would avoid the sidecar and is deliberately not
    used: it asserts a live database cannot change, which would licence SQLite
    to ignore concurrent WAL state.

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

    Three gates, in this order:

    1. **Gate A** -- the exact ``--execute`` flag, checked before any file is
       read. Abbreviation is disabled parser-wide, so ``--exe`` is not it;
    2. **Gate B** -- ``MGO_CONFIG_PATH`` set and absolute *as supplied*, also
       checked before any file is read;
    3. **Gate C** -- ``retention.enabled = true``, necessarily checked *after*
       the configuration has been read, because that is where the value lives.

    Gates A and B are the ones that hold before anything is opened; gate C
    cannot be, and describing all three that way was simply untrue. Nothing
    destructive happens in between: reading the operator's own configuration
    file is the only step, and the database and capture directory are still
    untouched when gate C is evaluated.

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


#: The declined outcomes a scheduled run reports as a *skip* rather than a
#: failure. Each is a deliberate refusal to start -- nothing was touched -- and
#: a timer that fires every night must not turn "correctly did nothing" into
#: a failed unit. Everything else keeps its non-zero exit code.
SCHEDULED_SKIP_REASONS: dict[RetentionErrorCategory | None, str] = {
    RetentionErrorCategory.BUSY: "retention_busy",
    RetentionErrorCategory.BACKUP_IN_PROGRESS: "backup_in_progress",
    RetentionErrorCategory.LOCK_UNAVAILABLE: "lock_unavailable",
}


def _absolute_backup_directory(raw: str | None) -> Path | None:
    """Validate the optional ``--backup-directory`` as supplied.

    Absolute as written, for the same reason the configuration path must be:
    the timer runs from a working directory an operator did not choose, and a
    relative value would name a different place from a different shell.
    """
    if raw is None:
        return None
    if not Path(raw).is_absolute():
        raise _Refusal(REFUSAL_RELATIVE_BACKUP_DIRECTORY, EXIT_REFUSED)
    return Path(raw)


def _scheduled_run(arguments: argparse.Namespace, stream: IO[str]) -> int:
    """Execute one retention run the way a timer needs it executed (Task 14.5).

    The same gates as ``run-once`` -- the explicit ``--execute`` flag and an
    absolute ``MGO_CONFIG_PATH`` -- and the same single call into the service.
    What differs is the *reporting* of a run that correctly declined to start:

    * ``retention.enabled = false`` is an ``outcome`` of ``skipped`` with reason
      ``retention_disabled`` and exit ``0``. With the production configuration
      as deployed, this is the nightly result, and it deletes nothing;
    * a held retention lock, a running backup or an unavailable lock are
      ``skipped`` with their reason and exit ``0``;
    * a run that started is ``executed`` (exit ``0``, or the retention error
      code if it stopped on a safety refusal); a wrong schema is still refused.

    Skipping is not silent: the structured document says exactly why, and the
    journal carries it. It is merely not a failure.
    """
    if not arguments.execute:
        raise _Refusal(REFUSAL_MISSING_EXECUTE_SCHEDULED, EXIT_REFUSED)

    backup_directory = _absolute_backup_directory(arguments.backup_directory)
    _require_explicit_configuration()
    config = _load_configuration()

    if not config.retention.enabled:  # the scheduled gate
        _emit(
            {
                "outcome": "skipped",
                "reason": "retention_disabled",
                "executed": False,
                "enabled": False,
                "deleted_count": 0,
                "bytes_reclaimed": 0,
            },
            stream,
        )
        return EXIT_SUCCESS

    _require_current_schema(config.storage.database_path)

    result = _build_service(config, backup_directory=backup_directory).run_once()

    if not result.executed:
        reason = SCHEDULED_SKIP_REASONS.get(result.error_category)
        if reason is not None:
            _emit(
                {
                    "outcome": "skipped",
                    "reason": reason,
                    "executed": False,
                    "enabled": True,
                    "deleted_count": 0,
                    "bytes_reclaimed": 0,
                },
                stream,
            )
            return EXIT_SUCCESS

    _emit(
        {
            "outcome": "executed" if result.executed else "declined",
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
    "scheduled-run": _scheduled_run,
}


def build_parser() -> argparse.ArgumentParser:
    """Return the argument parser. Exposed so tests can inspect the contract.

    Every parser in the tree is a :class:`_BoundedParser`, subparsers included:
    ``parser_class`` is passed explicitly rather than relying on
    ``add_subparsers`` defaulting it to ``type(self)``, so the guarantee survives
    someone later constructing the subparsers differently.
    """
    parser = _BoundedParser(
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
    subcommands = parser.add_subparsers(
        dest="command", metavar="COMMAND", parser_class=_BoundedParser
    )
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

    scheduled = subcommands.add_parser(
        "scheduled-run",
        help=(
            "Execute one bounded retention run for a timer. Skips cleanly "
            "when retention is disabled, busy or a backup is running."
        ),
        description=(
            "The subcommand mgo-retention.service invokes. Identical gates to "
            "run-once and the same single run, but a run that correctly "
            "declines to start -- retention disabled, another run holding "
            "the lock, a backup in progress -- is reported as a structured "
            "skip with exit 0 rather than as a failed unit. It never repeats "
            "because more work remains, and never runs when retention is "
            "disabled in the configuration."
        ),
    )
    scheduled.add_argument(
        "--execute",
        action="store_true",
        help=(
            "Required. Confirms that this invocation may delete capture media. "
            "Without it the command refuses before touching anything."
        ),
    )
    scheduled.add_argument(
        "--backup-directory",
        default=None,
        help=(
            "Absolute path of the backup directory whose lock a running backup "
            "holds. Retention skips while that lock is fresh. Defaults to the "
            "canonical production backup location."
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
    except _Refusal as refusal:
        # An invalid invocation. The parser raised this instead of writing
        # argparse's own diagnostic, so the offending argument text was never
        # produced at all, and the refusal goes to the caller's stream rather
        # than to the process's ``sys.stderr`` behind the caller's back.
        err.write(refusal.message + "\n")
        return refusal.code
    except SystemExit as exc:
        # ``--help`` exits this way, having already printed the static help
        # text. The non-zero mapping is retained defensively: any argparse path
        # that exits without going through ``error()`` should still land in this
        # command's refusal vocabulary rather than escape as a traceback.
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
    "REFUSAL_CONFIGURATION_INVALID",
    "REFUSAL_INVALID_ARGUMENTS",
    "REFUSAL_MISSING_EXECUTE_SCHEDULED",
    "REFUSAL_RELATIVE_BACKUP_DIRECTORY",
    "REFUSAL_RELATIVE_CONFIG",
    "SCHEDULED_SKIP_REASONS",
    "build_parser",
    "main",
]
