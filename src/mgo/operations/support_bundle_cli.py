"""Operator command line for MGO diagnostic support bundles.

One command, one artefact. The exit status is the contract:

``0``  a complete bundle -- every source answered;
``1``  a partial bundle -- the file exists and is useful, but something was
       unreachable (very often the API being down, which is usually the thing
       being diagnosed);
``2``  no bundle was created.

The distinction between ``1`` and ``2`` is the point. A support bundle is most
needed when the system is unwell, so "some sources failed" must not be treated
as "generation failed" -- the bundle describing a dead API is exactly the bundle
worth sending.
"""

from __future__ import annotations

import argparse
import json
import sys
import tomllib
from collections.abc import Sequence
from pathlib import Path
from typing import IO, Any

from mgo.core.config import (
    SERVICE_UNIT_NAME,
    SYSTEM_BACKUP_DIRECTORY,
    load_config,
    resolve_config_path,
)
from mgo.operations.errors import ErrorCode, OperationError
from mgo.operations.events import EventEmitter
from mgo.operations.support_bundle import (
    BUNDLE_SERVICE,
    DEFAULT_API_BASE_URL,
    BundleOutcome,
    create_support_bundle,
)

EXIT_COMPLETE = 0
EXIT_PARTIAL = 1
EXIT_FAILED = 2
EXIT_USAGE = 2


def _build_parser() -> argparse.ArgumentParser:
    """Build the argument parser."""
    parser = argparse.ArgumentParser(
        prog="mgo-support-bundle",
        description=(
            "Generate a bounded diagnostic support bundle. Contains no "
            "database, no media, no raw configuration and no credentials."
        ),
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Configuration file to summarise.",
    )
    parser.add_argument(
        "--output-directory",
        type=Path,
        default=Path("."),
        help="Directory to write the bundle into (default: the current one).",
    )
    parser.add_argument(
        "--api-base-url",
        default=DEFAULT_API_BASE_URL,
        help=(
            "Loopback API base URL to collect status from "
            f"(default: {DEFAULT_API_BASE_URL}). Non-loopback is refused."
        ),
    )
    parser.add_argument(
        "--unit",
        default=SERVICE_UNIT_NAME,
        help=f"systemd unit to inspect (default: {SERVICE_UNIT_NAME}).",
    )
    parser.add_argument(
        "--backup-directory",
        type=Path,
        default=None,
        help=(
            "Backup directory to summarise "
            f"(default: {SYSTEM_BACKUP_DIRECTORY.as_posix()})."
        ),
    )
    return parser


def _load_raw_configuration(config_path: Path | None) -> dict[str, Any] | None:
    """Read the configuration TOML for *key names only*.

    The parsed document never enters the bundle. It is used solely to discover
    keys this build does not recognise, whose values are then withheld. A
    failure here is not worth failing a diagnostic run over -- the summary is
    simply built without the unrecognised-key listing.
    """
    try:
        path = resolve_config_path(config_path)
        with path.open("rb") as handle:
            loaded = tomllib.load(handle)
    except (OSError, ValueError, tomllib.TOMLDecodeError):
        return None
    return loaded


def main(
    argv: Sequence[str] | None = None,
    *,
    stdout: IO[str] | None = None,
    stderr: IO[str] | None = None,
) -> int:
    """Run the support-bundle CLI and return its exit status."""
    out = stdout if stdout is not None else sys.stdout
    err = stderr if stderr is not None else sys.stderr
    events = EventEmitter(BUNDLE_SERVICE, stream=err)

    parser = _build_parser()
    try:
        arguments = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else EXIT_USAGE

    try:
        config = load_config(arguments.config)
    except (FileNotFoundError, OSError, ValueError, KeyError) as exc:
        message = f"The configuration could not be loaded: {exc}."
        events.error(
            "bundle.failed",
            message,
            error_code=ErrorCode.CONFIGURATION_UNAVAILABLE,
        )
        _write(
            out,
            {
                "outcome": BundleOutcome.FAILED.value,
                "error_code": ErrorCode.CONFIGURATION_UNAVAILABLE.value,
                "detail": message,
            },
        )
        return EXIT_FAILED

    try:
        result = create_support_bundle(
            config=config,
            destination=arguments.output_directory,
            raw_configuration=_load_raw_configuration(arguments.config),
            backup_directory=arguments.backup_directory,
            api_base_url=arguments.api_base_url,
            unit=arguments.unit,
            emitter=events,
        )
    except OperationError as exc:
        events.error("bundle.failed", exc.message, error_code=exc.code)
        _write(
            out,
            {
                "outcome": BundleOutcome.FAILED.value,
                "error_code": exc.code.value,
                "detail": exc.message,
            },
        )
        return EXIT_FAILED
    except Exception as exc:
        # The process boundary. No traceback is printed: it is the one part of
        # the output that could carry a filesystem path or a configuration value
        # into a log an operator forwards to someone else.
        message = f"An unexpected error ended bundle generation: {type(exc).__name__}."
        events.error(
            "bundle.failed", message, error_code=ErrorCode.UNEXPECTED_ERROR
        )
        _write(
            out,
            {
                "outcome": BundleOutcome.FAILED.value,
                "error_code": ErrorCode.UNEXPECTED_ERROR.value,
                "detail": message,
            },
        )
        return EXIT_FAILED

    _write(out, result.as_dict())
    return result.exit_code


def _write(stream: IO[str], payload: dict[str, Any]) -> None:
    """Write the machine-readable command summary."""
    stream.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    stream.flush()


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())


__all__ = ["EXIT_COMPLETE", "EXIT_FAILED", "EXIT_PARTIAL", "EXIT_USAGE", "main"]
