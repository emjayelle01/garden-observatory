"""Bounded, privacy-safe diagnostic support bundles for MGO.

A support bundle answers "why is the observatory misbehaving?" for someone who
is not standing next to the Raspberry Pi. It is therefore a file that leaves the
machine, and that single fact drives the whole design.

**Every member is generated in memory.** Nothing is ever read from disk and
copied into the archive. That is not a stylistic choice: it is what makes the
privacy guarantees structural instead of aspirational. A bundle built by
*copying* files needs an exclusion filter that is perfect forever -- one that
remembers ``mgo.db`` and also ``mgo.db-wal``, ``mgo.db-shm``, every image
extension, ``.ssh``, ``.git``, the raw TOML, and whatever the next task adds. A
bundle built only from values this module computes cannot include those things
at all. For the same reason no archive member can be a symlink, an absolute path
or contain ``..``: member names are constants in this file.

**Everything is bounded.** Endpoint responses, journal lines, journal bytes,
command output, subprocess runtime, member count and total archive size all have
explicit limits. A bundle is generated on a device whose storage is an SD card,
sometimes while that device is already unwell, and a diagnostic tool that can
fill the disk is a fault amplifier.

**Partial results are still useful.** A failed source produces a truthful error
entry and the rest of the bundle is still built. The outcome vocabulary is
``complete`` / ``partial`` / ``failed``, mapped to exit codes 0 / 1 / 2, so a
caller can tell "everything answered" from "something is missing" from "there is
no bundle".

Nothing here requires the Raspberry Pi. ``systemctl`` and ``journalctl`` are
absent on the development machine, the API is usually not running, and the
production paths do not exist -- each is recorded as an ``unavailable`` result
rather than raising.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from mgo.core.config import (
    SERVICE_UNIT_NAME,
    SYSTEM_BACKUP_DIRECTORY,
    SYSTEM_CAPTURE_DIRECTORY,
    SYSTEM_CONFIG_DIRECTORY,
    SYSTEM_CONFIG_PATH,
    SYSTEM_DATABASE_DIRECTORY,
    SYSTEM_DATABASE_PATH,
    SYSTEM_LOG_DIRECTORY,
    SYSTEM_MEDIA_DIRECTORY,
    SYSTEM_QUEUE_DIRECTORY,
    SYSTEM_RUNTIME_STATE_DIRECTORY,
    SYSTEM_STATE_DIRECTORY,
    MGOConfig,
)
from mgo.core.identity import get_application_version
from mgo.operations.errors import ErrorCode, OperationError
from mgo.operations.events import (
    REDACTED,
    EventEmitter,
    Severity,
    redact_mapping,
)

#: Service name stamped on this module's structured events.
BUNDLE_SERVICE = "mgo-support-bundle"

#: Bundle naming. The ``mgo-support-`` prefix cannot match the backup name
#: pattern, so backup retention will never delete a bundle sharing a directory.
BUNDLE_NAME_PREFIX = "mgo-support-"
BUNDLE_SUFFIX = ".tar.gz"
BUNDLE_TIMESTAMP_FORMAT = "%Y%m%dT%H%M%SZ"

#: The manifest schema this build writes.
BUNDLE_FORMAT_VERSION = 1

#: A bundle is meant to be handed to a person. Owner-only: it is the most
#: restrictive mode that still lets the operator who generated it copy it.
BUNDLE_FILE_MODE = 0o600

#: Members are owner read/write, group read inside the archive.
MEMBER_MODE = 0o640

#: The API is read over the loopback interface only. A bundle must never reach
#: the network, so the host is validated rather than assumed.
DEFAULT_API_BASE_URL = "http://127.0.0.1:8080"

#: Literal loopback addresses, and *only* literal addresses. ``localhost`` is
#: deliberately excluded: resolving it is a DNS lookup, and what it resolves to
#: is decided by ``/etc/hosts``, ``nsswitch.conf`` and the resolver -- none of
#: which this tooling controls. A name that "is obviously loopback" is exactly
#: the kind of assumption a privacy boundary must not rest on. A literal IP
#: needs no resolution at all, so there is nothing to redirect.
ALLOWED_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1"})

#: Per-request timeout. Short and explicit: a struggling API must not stall a
#: diagnostic run, and there are no retries -- a retry loop is exactly how a
#: "bounded" tool becomes unbounded.
ENDPOINT_TIMEOUT_SECONDS = 5.0

#: Largest endpoint response accepted. The real payloads are a few kilobytes.
MAX_ENDPOINT_RESPONSE_BYTES = 256 * 1024

#: Subprocess bounds for ``systemctl`` / ``journalctl``.
SUBPROCESS_TIMEOUT_SECONDS = 15.0
MAX_COMMAND_OUTPUT_BYTES = 256 * 1024

#: Journal collection bounds. One day is the window in which a fault an operator
#: has just noticed will have happened.
JOURNAL_SINCE = "-24h"
MAX_JOURNAL_LINES = 2000
MAX_JOURNAL_BYTES = 2 * 1024 * 1024

#: Archive bounds. Neither can be reached by the sources below; they exist so
#: that a source which misbehaves in an unforeseen way still cannot fill the
#: SD card.
MAX_ARCHIVE_MEMBERS = 64
MAX_ARCHIVE_BYTES = 8 * 1024 * 1024

#: Read-only endpoints collected, as ``(member name, path)``. Every one is a
#: GET against a status route. No mutation endpoint appears here: no capture, no
#: preview start or stop, no stream, no notification publication.
COLLECTED_ENDPOINTS: tuple[tuple[str, str], ...] = (
    ("application-identity.json", "/"),
    ("application-version.json", "/version"),
    ("health.json", "/health"),
    ("database-status.json", "/database/status"),
    ("camera-status.json", "/camera/status"),
    ("preview-status.json", "/camera/preview/status"),
    ("motion-status.json", "/motion/status"),
    ("notifications-status.json", "/notifications/status"),
)

#: ``systemctl show`` properties collected. A closed list: ``systemctl show``
#: with no ``--property`` would dump the unit's entire environment block, which
#: is exactly the sort of thing that must never enter a bundle.
SERVICE_PROPERTIES: tuple[str, ...] = (
    "ActiveState",
    "SubState",
    "MainPID",
    "NRestarts",
    "ExecMainStatus",
    "Result",
    "ActiveEnterTimestamp",
    "FragmentPath",
    "User",
    "Group",
)

#: Deployment paths that may appear verbatim. They are public constants in this
#: repository, documented in ``docs/Service-Identity.md``, and knowing them tells
#: a reader nothing they could not read here. Any *other* path is reported by
#: role only.
_PUBLIC_PATHS: frozenset[str] = frozenset(
    path.as_posix()
    for path in (
        SYSTEM_CONFIG_DIRECTORY,
        SYSTEM_CONFIG_PATH,
        SYSTEM_STATE_DIRECTORY,
        SYSTEM_DATABASE_DIRECTORY,
        SYSTEM_DATABASE_PATH,
        SYSTEM_MEDIA_DIRECTORY,
        SYSTEM_CAPTURE_DIRECTORY,
        SYSTEM_QUEUE_DIRECTORY,
        SYSTEM_RUNTIME_STATE_DIRECTORY,
        SYSTEM_LOG_DIRECTORY,
        SYSTEM_BACKUP_DIRECTORY,
    )
)

#: Placeholder for a path that is not a canonical deployment location.
NON_CANONICAL_PATH = "<non-canonical path>"

#: Configuration keys this build knows and has decided are safe to report. A key
#: absent from this map is reported by *name* with its value redacted, so a
#: setting added by a future task defaults to withheld rather than exposed.
_KNOWN_CONFIG_KEYS: Mapping[str, frozenset[str]] = {
    "application": frozenset({"name", "environment", "host", "port"}),
    "storage": frozenset({"data_directory", "log_directory", "database_path"}),
    "camera": frozenset(
        {
            "enabled",
            "backend",
            "device_index",
            "detection_interval_seconds",
            "capture_directory",
        }
    ),
    "preview": frozenset(
        {
            "enabled",
            "width",
            "height",
            "fps",
            "startup_timeout_seconds",
            "shutdown_timeout_seconds",
        }
    ),
    "motion": frozenset(
        {
            "enabled",
            "analysis_interval_seconds",
            "analysis_width",
            "analysis_height",
            "pixel_difference_threshold",
            "changed_pixel_ratio_threshold",
            "cooldown_seconds",
        }
    ),
    "notifications": frozenset({"enabled", "provider"}),
    "database": frozenset({"health_check_interval_seconds", "busy_timeout_seconds"}),
    "health": frozenset(
        {
            "enabled",
            "collection_interval_seconds",
            "temperature_warning_celsius",
            "temperature_critical_celsius",
            "disk_warning_percent",
            "disk_critical_percent",
            "memory_warning_percent",
            "memory_critical_percent",
        }
    ),
}


class BundleOutcome(StrEnum):
    """How completely a bundle was generated."""

    COMPLETE = "complete"
    PARTIAL = "partial"
    FAILED = "failed"


@dataclass(frozen=True)
class CommandResult:
    """The bounded outcome of running one system command."""

    available: bool
    returncode: int | None = None
    stdout: str = ""
    stderr: str = ""
    truncated: bool = False
    error: str = ""


@dataclass(frozen=True)
class SourceError:
    """One source that could not be collected."""

    source: str
    error_code: ErrorCode
    detail: str

    def as_dict(self) -> dict[str, str]:
        """Return the serialisable form."""
        return {
            "source": self.source,
            "error_code": self.error_code.value,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class BundleResult:
    """A generated (or not generated) support bundle."""

    outcome: BundleOutcome
    bundle_path: Path | None
    members: tuple[str, ...] = ()
    errors: tuple[SourceError, ...] = ()
    size_bytes: int = 0

    @property
    def exit_code(self) -> int:
        """The documented exit contract: 0 complete, 1 partial, 2 failed."""
        return {
            BundleOutcome.COMPLETE: 0,
            BundleOutcome.PARTIAL: 1,
            BundleOutcome.FAILED: 2,
        }[self.outcome]

    def as_dict(self) -> dict[str, Any]:
        """Return the serialisable summary (a name, never a full path)."""
        return {
            "outcome": self.outcome.value,
            "bundle_filename": (
                self.bundle_path.name if self.bundle_path is not None else None
            ),
            "size_bytes": self.size_bytes,
            "files_included": list(self.members),
            "files_skipped": [error.source for error in self.errors],
            "errors": [error.as_dict() for error in self.errors],
        }


# --- collection primitives --------------------------------------------------

Fetcher = Callable[[str], bytes]
Runner = Callable[[Sequence[str]], CommandResult]


def _validated_base_url(base_url: str) -> str:
    """Return the API base URL, refusing anything that is not literal loopback.

    A support bundle must never reach the network. Validating the base URL here
    means an operator (or a future caller) cannot point bundle generation at a
    remote address and post the Pi's status to it.

    Validation is deliberately strict, because every rejected form is a way the
    request could have left the machine or carried something it should not:

    * only ``http`` -- ``https`` to loopback would be a misconfiguration, and
      accepting other schemes invites ``file://`` and ``ftp://``;
    * only a **literal** loopback address, never a name (see
      :data:`ALLOWED_LOOPBACK_HOSTS`);
    * no user information, so credentials cannot be smuggled into a URL that
      ends up in a log;
    * no query string or fragment, because the endpoint paths are fixed by the
      reviewed table and a base URL is not a place to add parameters.
    """
    parsed = urlparse(base_url)

    if parsed.scheme != "http":
        raise OperationError(
            ErrorCode.INVALID_ARGUMENT,
            "The API base URL must use http:// on the loopback interface.",
        )
    if parsed.username is not None or parsed.password is not None:
        raise OperationError(
            ErrorCode.INVALID_ARGUMENT,
            "The API base URL must not contain user information.",
        )
    if parsed.query or parsed.fragment:
        raise OperationError(
            ErrorCode.INVALID_ARGUMENT,
            "The API base URL must not contain a query string or fragment; "
            "endpoint paths are fixed by the reviewed endpoint table.",
        )
    if (parsed.hostname or "") not in ALLOWED_LOOPBACK_HOSTS:
        allowed = ", ".join(sorted(ALLOWED_LOOPBACK_HOSTS))
        raise OperationError(
            ErrorCode.INVALID_ARGUMENT,
            f"The API base URL must address a literal loopback address "
            f"({allowed}); a support bundle never reaches the network, and a "
            "hostname would require a DNS lookup this tooling does not control.",
        )
    return base_url.rstrip("/")


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """A redirect handler that refuses to follow anything.

    ``urllib``'s default follows 3xx responses automatically, which would let a
    compromised or merely misconfigured local service redirect a diagnostic
    request to an external host -- and the validation of the *initial* URL would
    have passed. Returning ``None`` from every redirect hook makes ``urllib``
    surface the 3xx as an :class:`~urllib.error.HTTPError` instead, which the
    caller records as a failed source.
    """

    def redirect_request(self, *args: object, **kwargs: object) -> None:
        """Never produce a follow-up request."""
        return None


#: The opener used for every diagnostic request.
#:
#: Built explicitly rather than using ``urllib.request.urlopen``'s global
#: opener, because the default installs a ``ProxyHandler`` that reads
#: ``HTTP_PROXY``/``ALL_PROXY`` from the environment. On a machine with a proxy
#: configured, a "loopback only" request would have been sent to that proxy --
#: off the machine entirely. ``ProxyHandler({})`` disables proxying outright.
_OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({}),
    _NoRedirects,
)


def _http_get(url: str) -> bytes:
    """Fetch a loopback URL with an explicit timeout and a bounded read.

    One attempt, no retries, no proxy, no redirect, and at most
    :data:`MAX_ENDPOINT_RESPONSE_BYTES` plus one byte read -- the extra byte is
    how an over-long response is detected rather than silently truncated into
    invalid JSON.
    """
    request = urllib.request.Request(url, method="GET")
    with _OPENER.open(request, timeout=ENDPOINT_TIMEOUT_SECONDS) as response:
        return bytes(response.read(MAX_ENDPOINT_RESPONSE_BYTES + 1))


def _run_command(command: Sequence[str]) -> CommandResult:
    """Run a system command safely and bounded.

    An argument array, never ``shell=True``: nothing here can be turned into a
    shell injection by a value from the environment. A missing executable is the
    normal case on the development machine and is reported as ``available=False``
    rather than raised.
    """
    try:
        completed = subprocess.run(
            list(command),
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT_SECONDS,
            check=False,
        )
    except FileNotFoundError:
        return CommandResult(
            available=False, error=f"{command[0]} is not installed on this host."
        )
    except subprocess.TimeoutExpired:
        return CommandResult(
            available=True,
            error=(
                f"{command[0]} did not finish within "
                f"{SUBPROCESS_TIMEOUT_SECONDS:.0f} seconds."
            ),
        )
    except OSError as exc:
        return CommandResult(
            available=False, error=f"{command[0]} could not be run: {exc}."
        )

    stdout = completed.stdout or ""
    truncated = False
    if len(stdout.encode("utf-8", "replace")) > MAX_COMMAND_OUTPUT_BYTES:
        stdout = stdout.encode("utf-8", "replace")[
            :MAX_COMMAND_OUTPUT_BYTES
        ].decode("utf-8", "replace")
        truncated = True

    return CommandResult(
        available=True,
        returncode=completed.returncode,
        stdout=stdout,
        stderr=(completed.stderr or "")[:4096],
        truncated=truncated,
    )


def _fsync_path(path: Path) -> None:
    """Flush a file or directory to storage, best effort.

    Durability matters here for the same reason it does for a backup: a bundle
    generated moments before a Pi is power-cycled should survive the reboot.
    Windows cannot open a directory as a file descriptor and raises, which is
    expected and ignored.
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


def _json_bytes(payload: object) -> bytes:
    """Serialise a member deterministically as UTF-8 JSON."""
    return (
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    ).encode("utf-8")


# --- individual sources -----------------------------------------------------


def collect_endpoint(
    base_url: str, path: str, fetch: Fetcher
) -> tuple[object, SourceError | None]:
    """Collect one read-only API endpoint.

    A failure -- the API not running, a timeout, a non-JSON body, an over-long
    response -- becomes a truthful error payload *and* a recorded
    :class:`SourceError`, so the member still exists in the bundle and the
    overall outcome becomes ``partial``.
    """
    url = f"{base_url}{path}"
    try:
        raw = fetch(url)
    except urllib.error.HTTPError as exc:
        detail = f"{path} returned HTTP {exc.code}."
        return {"error": detail, "endpoint": path}, SourceError(
            path, ErrorCode.DIAGNOSTIC_SOURCE_UNAVAILABLE, detail
        )
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        detail = f"{path} could not be reached: {type(exc).__name__}."
        return {"error": detail, "endpoint": path}, SourceError(
            path, ErrorCode.DIAGNOSTIC_SOURCE_UNAVAILABLE, detail
        )

    if len(raw) > MAX_ENDPOINT_RESPONSE_BYTES:
        detail = (
            f"{path} returned more than {MAX_ENDPOINT_RESPONSE_BYTES} bytes; "
            "the response was discarded rather than stored."
        )
        return {"error": detail, "endpoint": path}, SourceError(
            path, ErrorCode.DIAGNOSTIC_LIMIT_EXCEEDED, detail
        )

    try:
        return json.loads(raw.decode("utf-8")), None
    except (ValueError, UnicodeDecodeError):
        detail = f"{path} did not return valid JSON."
        return {"error": detail, "endpoint": path}, SourceError(
            path, ErrorCode.DIAGNOSTIC_SOURCE_UNAVAILABLE, detail
        )


#: Longest error detail retained from a failed command. stderr is attacker- and
#: accident-adjacent text that ends up in a file leaving the machine, so it is
#: bounded and whitespace-collapsed rather than copied through wholesale.
MAX_COMMAND_ERROR_DETAIL = 300


def _command_failure_detail(name: str, result: CommandResult) -> str:
    """Return a bounded, sanitised description of a failed command.

    A command that exits non-zero usually explains itself on stderr, and that
    explanation is genuinely useful ("Unit mgo.service could not be found").
    What must not happen is unbounded stderr flowing into the bundle, so the
    text is collapsed to one line and truncated.
    """
    detail = f"{name} exited {result.returncode}"
    message = " ".join((result.stderr or "").split())
    if message:
        if len(message) > MAX_COMMAND_ERROR_DETAIL:
            message = message[: MAX_COMMAND_ERROR_DETAIL - 1].rstrip() + "…"
        detail = f"{detail}: {message}"
    return detail + "."


def collect_service_status(
    unit: str, run: Runner
) -> tuple[dict[str, Any], SourceError | None]:
    """Collect a bounded set of ``systemctl show`` properties for a unit."""
    command = [
        "systemctl",
        "show",
        unit,
        *(f"--property={name}" for name in SERVICE_PROPERTIES),
    ]
    result = run(command)

    if not result.available:
        detail = result.error or "systemctl is unavailable on this host."
        return (
            {"unit": unit, "available": False, "error": detail},
            SourceError("systemctl", ErrorCode.DIAGNOSTIC_SOURCE_UNAVAILABLE, detail),
        )
    if result.error:
        return (
            {"unit": unit, "available": False, "error": result.error},
            SourceError(
                "systemctl", ErrorCode.DIAGNOSTIC_TIMEOUT, result.error
            ),
        )

    # A non-zero exit means systemctl did not answer the question -- an unknown
    # unit, a refused request, no running systemd. Reporting ``available: true``
    # with an empty property set would read as "the service exists and told us
    # nothing", which is a different and much more alarming fact.
    if result.returncode not in (0, None):
        detail = _command_failure_detail("systemctl", result)
        return (
            {"unit": unit, "available": False, "error": detail},
            SourceError(
                "systemctl", ErrorCode.DIAGNOSTIC_SOURCE_UNAVAILABLE, detail
            ),
        )

    allowed = set(SERVICE_PROPERTIES)
    properties: dict[str, str] = {}
    for line in result.stdout.splitlines():
        key, separator, value = line.partition("=")
        # Only the whitelisted properties are kept, even though only they were
        # requested: a future systemd could add unrequested output, and the
        # bundle must never carry a property nobody reviewed.
        if separator and key in allowed:
            properties[key] = value

    if not properties:
        detail = (
            f"systemctl returned no recognised property for {unit}; the unit "
            "may not exist on this host."
        )
        return (
            {"unit": unit, "available": False, "error": detail},
            SourceError(
                "systemctl", ErrorCode.DIAGNOSTIC_SOURCE_UNAVAILABLE, detail
            ),
        )

    summary: dict[str, Any] = {
        "unit": unit,
        "available": True,
        "properties": properties,
    }
    if result.truncated:
        summary["truncated"] = True
    return summary, None


def collect_journal(unit: str, run: Runner) -> tuple[str, SourceError | None]:
    """Collect a bounded slice of one unit's journal.

    Scoped to the MGO unit, the last day and a line cap. The whole system
    journal is never collected: it would carry other services' logs, other
    users' activity and authentication records into a file that leaves the Pi.
    """
    result = run(
        [
            "journalctl",
            "--unit",
            unit,
            "--since",
            JOURNAL_SINCE,
            "--lines",
            str(MAX_JOURNAL_LINES),
            "--no-pager",
        ]
    )

    if not result.available:
        detail = result.error or "journalctl is unavailable on this host."
        return (
            f"[journal unavailable] {detail}\n",
            SourceError(
                "journalctl", ErrorCode.DIAGNOSTIC_SOURCE_UNAVAILABLE, detail
            ),
        )
    if result.error:
        return (
            f"[journal unavailable] {result.error}\n",
            SourceError("journalctl", ErrorCode.DIAGNOSTIC_TIMEOUT, result.error),
        )
    if result.returncode not in (0, None):
        detail = (
            f"{_command_failure_detail('journalctl', result)} Access may be "
            "denied to this account."
        )
        return (
            f"[journal unavailable] {detail}\n",
            SourceError(
                "journalctl", ErrorCode.DIAGNOSTIC_SOURCE_UNAVAILABLE, detail
            ),
        )

    lines = result.stdout.splitlines()[:MAX_JOURNAL_LINES]
    text = "\n".join(lines)
    encoded = text.encode("utf-8", "replace")
    if len(encoded) > MAX_JOURNAL_BYTES:
        text = encoded[:MAX_JOURNAL_BYTES].decode("utf-8", "replace")
        text += "\n[truncated: journal exceeded the collection byte limit]"
    if result.truncated:
        text += "\n[truncated: journal exceeded the command output limit]"
    return text + "\n", None


def collect_journal_disk_usage(run: Runner) -> tuple[str, SourceError | None]:
    """Collect ``journalctl --disk-usage``.

    This is how an operator sees whether host journald retention is bounded, and
    it is the one journald fact Task 10 reports without changing anything.
    """
    result = run(["journalctl", "--disk-usage"])
    if not result.available or result.error:
        detail = result.error or "journalctl is unavailable on this host."
        code = (
            ErrorCode.DIAGNOSTIC_TIMEOUT
            if result.available
            else ErrorCode.DIAGNOSTIC_SOURCE_UNAVAILABLE
        )
        return (
            f"[unavailable] {detail}\n",
            SourceError("journalctl --disk-usage", code, detail),
        )

    # A non-zero exit means the figure was not obtained. Returning whatever
    # happened to be on stdout would put an empty (or partial) line in the
    # bundle and call it a disk-usage reading.
    if result.returncode not in (0, None):
        detail = _command_failure_detail("journalctl --disk-usage", result)
        return (
            f"[unavailable] {detail}\n",
            SourceError(
                "journalctl --disk-usage",
                ErrorCode.DIAGNOSTIC_SOURCE_UNAVAILABLE,
                detail,
            ),
        )

    return result.stdout.strip() + "\n", None


def _describe_path(path: Path, role: str) -> dict[str, Any]:
    """Describe a configured location without necessarily naming it.

    A canonical deployment path is public and is reported verbatim, because an
    operator diagnosing a layout problem needs to see it and it is already
    documented. Anything else -- a developer's checkout, a relocated data
    directory, a path under a home directory -- is reported by role and state
    only.
    """
    posix = path.as_posix()
    exists = False
    is_directory = False
    writable = False
    with suppress(OSError):
        exists = path.exists()
        is_directory = path.is_dir()
        writable = os.access(path, os.W_OK)

    return {
        "role": role,
        "path": posix if posix in _PUBLIC_PATHS else NON_CANONICAL_PATH,
        "is_canonical": posix in _PUBLIC_PATHS,
        "exists": exists,
        "is_directory": is_directory,
        "writable": writable,
    }


def collect_configuration_summary(
    config: MGOConfig, raw_configuration: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """Summarise the effective configuration safely.

    The raw TOML is never copied. This is built from the *typed* configuration,
    so only fields this build knows about can appear, and each is chosen by
    hand. ``raw_configuration`` is used solely to *name* keys the build does not
    recognise, with their values withheld: a setting introduced by a later task
    is then visible as "present but not reviewed" rather than silently exposed.
    """
    summary: dict[str, Any] = {
        "application": {
            "name": config.application.name,
            "environment": config.application.environment,
            "host": config.application.host,
            "port": config.application.port,
        },
        "camera": {
            "enabled": config.camera.enabled,
            "backend": config.camera.backend,
            "device_index": config.camera.device_index,
            "detection_interval_seconds": config.camera.detection_interval_seconds,
        },
        "preview": {
            "enabled": config.preview.enabled,
            "width": config.preview.width,
            "height": config.preview.height,
            "fps": config.preview.fps,
            "startup_timeout_seconds": config.preview.startup_timeout_seconds,
            "shutdown_timeout_seconds": config.preview.shutdown_timeout_seconds,
        },
        "motion": {
            "enabled": config.motion.enabled,
            "analysis_interval_seconds": config.motion.analysis_interval_seconds,
            "analysis_width": config.motion.analysis_width,
            "analysis_height": config.motion.analysis_height,
            "pixel_difference_threshold": config.motion.pixel_difference_threshold,
            "changed_pixel_ratio_threshold": (
                config.motion.changed_pixel_ratio_threshold
            ),
            "cooldown_seconds": config.motion.cooldown_seconds,
        },
        "notifications": {
            "enabled": config.notifications.enabled,
            "provider": config.notifications.provider,
        },
        "database": {
            "health_check_interval_seconds": (
                config.database.health_check_interval_seconds
            ),
            "busy_timeout_seconds": config.database.busy_timeout_seconds,
        },
        "health": {
            "enabled": config.health.enabled,
            "collection_interval_seconds": config.health.collection_interval_seconds,
            "temperature_warning_celsius": config.health.temperature_warning_celsius,
            "temperature_critical_celsius": (
                config.health.temperature_critical_celsius
            ),
            "disk_warning_percent": config.health.disk_warning_percent,
            "disk_critical_percent": config.health.disk_critical_percent,
            "memory_warning_percent": config.health.memory_warning_percent,
            "memory_critical_percent": config.health.memory_critical_percent,
        },
        "storage_locations": [
            _describe_path(config.storage.data_directory, "data_directory"),
            _describe_path(config.storage.log_directory, "log_directory"),
            _describe_path(config.storage.database_path, "database_path"),
            _describe_path(config.camera.capture_directory, "capture_directory"),
        ],
        "unrecognised_settings": _unrecognised_settings(raw_configuration),
    }

    # A final defensive pass. Nothing above should produce a sensitive key, and
    # today nothing does -- but this is the layer that would catch it if a
    # future field named ``*_token`` were added to the summary by mistake.
    return redact_mapping(summary)


def _unrecognised_settings(
    raw_configuration: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Name configuration keys this build does not know, withholding values."""
    if raw_configuration is None:
        return {}

    unknown: dict[str, Any] = {}
    for section, values in raw_configuration.items():
        known = _KNOWN_CONFIG_KEYS.get(str(section))
        if known is None:
            unknown[str(section)] = REDACTED
            continue
        if not isinstance(values, Mapping):
            continue
        extra = {
            str(key): REDACTED for key in values if str(key) not in known
        }
        if extra:
            unknown[str(section)] = extra
    return unknown


#: Bounds for storage aggregation. ``Path.rglob("*")`` walks an entire tree with
#: no limit, which is fine for today's flat captures directory and is exactly
#: the kind of assumption that stops being true later. A diagnostic tool must
#: not be able to spend minutes stat-ing a media archive on a device that is
#: already unwell, so traversal stops and says so instead.
MAX_STORAGE_ENTRIES = 20_000
MAX_STORAGE_DEPTH = 8


def _directory_totals(
    directory: Path,
    *,
    max_entries: int | None = None,
    max_depth: int | None = None,
) -> dict[str, Any]:
    """Return a bounded entry count and total byte size for a directory.

    Counts and bytes only -- **never a filename**. That is what makes this safe
    to run over the media archive: the diagnostic question is "is the SD card
    filling up?", which needs no image name to answer.

    Traversal is explicitly bounded and iterative:

    * at most ``max_entries`` filesystem entries are inspected;
    * at most ``max_depth`` levels below the root are descended;
    * **symlinked directories are never descended**, so a link into ``/`` cannot
      turn a capture-directory scan into a whole-filesystem walk, and a link
      loop cannot make it run forever;
    * symlinked *files* are counted as entries but not stat-ed through, so the
      size of a target outside the approved directory is never read;
    * entries that vanish or cannot be read mid-walk are skipped rather than
      failing the whole bundle.

    When a limit is reached the counts gathered so far are returned with
    ``truncated: true`` and the limit that stopped it, so a partial figure is
    never mistaken for a complete one.

    The limits default to :data:`MAX_STORAGE_ENTRIES` and
    :data:`MAX_STORAGE_DEPTH`, resolved **at call time** rather than bound as
    default argument values, so the module constants remain the single
    authority and a caller (or a test) can lower them.
    """
    max_entries = MAX_STORAGE_ENTRIES if max_entries is None else max_entries
    max_depth = MAX_STORAGE_DEPTH if max_depth is None else max_depth

    if not directory.is_dir():
        return {"exists": False, "entries": 0, "total_bytes": 0, "truncated": False}

    entries = 0
    total = 0
    truncated_by: str | None = None
    # Iterative breadth-first walk with an explicit queue: no recursion to blow
    # the stack, and the depth is a value rather than a call count.
    queue: list[tuple[Path, int]] = [(directory, 0)]

    while queue and truncated_by is None:
        current, depth = queue.pop(0)
        try:
            children = list(current.iterdir())
        except OSError:
            # Unreadable directory: skip it, keep the totals gathered so far.
            continue

        for item in children:
            if entries >= max_entries:
                truncated_by = "max_entries"
                break

            try:
                is_symlink = item.is_symlink()
                is_directory = item.is_dir()
            except OSError:
                continue

            if is_directory:
                if is_symlink:
                    # Never descend a link: it may point outside the approved
                    # root, or back into this tree.
                    continue
                if depth + 1 > max_depth:
                    truncated_by = "max_depth"
                    break
                queue.append((item, depth + 1))
                continue

            entries += 1
            if is_symlink:
                # Counted, but its target is not measured.
                continue
            try:
                total += item.stat().st_size
            except OSError:
                # Disappeared between listing and stat: normal on a live system.
                continue

    summary: dict[str, Any] = {
        "exists": True,
        "entries": entries,
        "total_bytes": total,
        "truncated": truncated_by is not None,
    }
    if truncated_by is not None:
        summary["truncated_by"] = truncated_by
        summary["limit"] = (
            max_entries if truncated_by == "max_entries" else max_depth
        )
    return summary


def collect_storage_summary(
    config: MGOConfig, backup_directory: Path
) -> tuple[dict[str, Any], SourceError | None]:
    """Summarise storage as aggregates only, with bounded traversal.

    Sizes and counts, never contents and never names. The media directory is
    reported as "how many files and how many bytes", which is the diagnostic
    question ("is the SD card filling up?") without any of the imagery.

    Returns the summary and, when any directory hit a traversal bound, a
    :class:`SourceError` so the bundle outcome becomes ``partial``. Truncation
    is a *reported* condition, never a silent one and never a fatal one: a
    figure that stopped early is still worth having, provided the reader knows
    it stopped.
    """
    summary: dict[str, Any] = {}

    with suppress(OSError):
        usage = shutil.disk_usage(
            config.storage.data_directory
            if config.storage.data_directory.exists()
            else Path(config.storage.data_directory.anchor or ".")
        )
        summary["filesystem"] = {
            "total_bytes": usage.total,
            "free_bytes": usage.free,
            "used_percent": round((usage.used / usage.total) * 100, 1)
            if usage.total
            else 0.0,
        }

    database = config.storage.database_path
    database_summary: dict[str, Any] = {"exists": database.is_file()}
    with suppress(OSError):
        if database.is_file():
            database_summary["size_bytes"] = database.stat().st_size
        for suffix in ("-wal", "-shm"):
            sidecar = database.with_name(database.name + suffix)
            if sidecar.is_file():
                database_summary[f"sidecar{suffix}_bytes"] = sidecar.stat().st_size
    summary["database"] = database_summary

    aggregated = {
        "captures": config.camera.capture_directory,
        "logs": config.storage.log_directory,
        "backups": backup_directory,
        "queues": config.storage.data_directory / "queues",
        "runtime_state": config.storage.data_directory / "state",
    }
    truncated: list[str] = []
    for role, directory in aggregated.items():
        totals = _directory_totals(directory)
        summary[role] = totals
        if totals.get("truncated"):
            truncated.append(f"{role} ({totals.get('truncated_by')})")

    failure = None
    if truncated:
        failure = SourceError(
            "storage-summary",
            ErrorCode.DIAGNOSTIC_LIMIT_EXCEEDED,
            "Storage aggregation stopped at a traversal bound for: "
            f"{', '.join(sorted(truncated))}. The counts reported are partial.",
        )

    return summary, failure


# --- archive assembly -------------------------------------------------------


def _write_archive(members: Sequence[tuple[str, bytes]], target: Path) -> None:
    """Write members into a gzipped tar with fixed, safe metadata.

    Every :class:`~tarfile.TarInfo` is constructed here rather than derived from
    a file on disk. Names are the constants above, ownership is zeroed and the
    type is always a regular file, so the archive cannot contain a symlink, a
    device node, an absolute path, a ``..`` component or a real user's name.
    """
    with tarfile.open(target, "w:gz") as archive:
        for name, payload in members:
            info = tarfile.TarInfo(name=name)
            info.size = len(payload)
            info.mode = MEMBER_MODE
            info.type = tarfile.REGTYPE
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            info.mtime = 0
            archive.addfile(info, io.BytesIO(payload))


def _enforce_archive_bounds(members: Sequence[tuple[str, bytes]]) -> None:
    """Raise if the assembled members exceed the archive limits."""
    if len(members) > MAX_ARCHIVE_MEMBERS:
        raise OperationError(
            ErrorCode.DIAGNOSTIC_LIMIT_EXCEEDED,
            f"The bundle would contain {len(members)} members, above the "
            f"limit of {MAX_ARCHIVE_MEMBERS}.",
        )
    total = sum(len(payload) for _, payload in members)
    if total > MAX_ARCHIVE_BYTES:
        raise OperationError(
            ErrorCode.DIAGNOSTIC_LIMIT_EXCEEDED,
            f"The bundle would hold {total} bytes before compression, above "
            f"the limit of {MAX_ARCHIVE_BYTES}.",
        )


def bundle_filename(moment: datetime | None = None) -> str:
    """Return the bundle filename for a moment in time."""
    now = moment if moment is not None else datetime.now(UTC)
    stamp = now.astimezone(UTC).strftime(BUNDLE_TIMESTAMP_FORMAT)
    return f"{BUNDLE_NAME_PREFIX}{stamp}{BUNDLE_SUFFIX}"


def create_support_bundle(
    *,
    config: MGOConfig,
    destination: Path,
    raw_configuration: Mapping[str, Any] | None = None,
    backup_directory: Path | None = None,
    api_base_url: str = DEFAULT_API_BASE_URL,
    unit: str = SERVICE_UNIT_NAME,
    fetch: Fetcher | None = None,
    run: Runner | None = None,
    emitter: EventEmitter | None = None,
    now: datetime | None = None,
) -> BundleResult:
    """Generate one bounded, privacy-safe diagnostic bundle.

    Collects every source, records the failures, assembles the members,
    enforces the archive bounds and publishes atomically. Only an unwritable
    destination or an archive that cannot be written produces
    :attr:`BundleOutcome.FAILED`; any other failure degrades the outcome to
    ``partial`` while still producing a usable bundle.
    """
    events = emitter if emitter is not None else EventEmitter(BUNDLE_SERVICE)
    started = now if now is not None else datetime.now(UTC)
    base_url = _validated_base_url(api_base_url)
    fetcher = fetch if fetch is not None else _http_get
    runner = run if run is not None else _run_command
    backups = (
        backup_directory
        if backup_directory is not None
        else Path(SYSTEM_BACKUP_DIRECTORY.as_posix())
    )

    events.info("bundle.started", "Generating a diagnostic support bundle.")

    members: list[tuple[str, bytes]] = []
    errors: list[SourceError] = []

    for name, path in COLLECTED_ENDPOINTS:
        payload, failure = collect_endpoint(base_url, path, fetcher)
        members.append((name, _json_bytes(payload)))
        if failure is not None:
            errors.append(failure)

    service_status, failure = collect_service_status(unit, runner)
    members.append(("service-status.json", _json_bytes(service_status)))
    if failure is not None:
        errors.append(failure)

    journal, failure = collect_journal(unit, runner)
    members.append(("journal.log", journal.encode("utf-8", "replace")))
    if failure is not None:
        errors.append(failure)

    disk_usage, failure = collect_journal_disk_usage(runner)
    members.append(("journal-disk-usage.txt", disk_usage.encode("utf-8", "replace")))
    if failure is not None:
        errors.append(failure)

    try:
        members.append(
            (
                "configuration-summary.json",
                _json_bytes(collect_configuration_summary(config, raw_configuration)),
            )
        )
    except Exception as exc:  # pragma: no cover - defensive
        detail = f"The configuration summary could not be built: {type(exc).__name__}."
        members.append(
            ("configuration-summary.json", _json_bytes({"error": detail}))
        )
        errors.append(
            SourceError(
                "configuration-summary",
                ErrorCode.DIAGNOSTIC_REDACTION_FAILED,
                detail,
            )
        )

    storage_summary, storage_failure = collect_storage_summary(config, backups)
    members.append(("storage-summary.json", _json_bytes(storage_summary)))
    if storage_failure is not None:
        errors.append(storage_failure)

    members.append(("errors.json", _json_bytes([e.as_dict() for e in errors])))
    members.append(
        (
            "manifest.json",
            _json_bytes(
                {
                    "format_version": BUNDLE_FORMAT_VERSION,
                    "created_at": started.astimezone(UTC).isoformat(),
                    "application": "garden-observatory",
                    "application_version": get_application_version(),
                    "members": [
                        {
                            "name": name,
                            "size_bytes": len(payload),
                            "sha256": hashlib.sha256(payload).hexdigest(),
                        }
                        for name, payload in members
                    ],
                }
            ),
        )
    )

    outcome = BundleOutcome.PARTIAL if errors else BundleOutcome.COMPLETE
    members.append(
        (
            "generation-summary.json",
            _json_bytes(
                {
                    "outcome": outcome.value,
                    "generated_at": started.astimezone(UTC).isoformat(),
                    "files_included": [name for name, _ in members],
                    "files_skipped": [error.source for error in errors],
                    "error_count": len(errors),
                }
            ),
        )
    )

    _enforce_archive_bounds(members)

    try:
        destination.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise OperationError(
            ErrorCode.DIAGNOSTIC_OUTPUT_UNWRITABLE,
            f"The bundle directory could not be created: {exc.strerror or exc}.",
        ) from exc

    target = destination / bundle_filename(started)

    # Never overwrite an existing bundle. Two runs in the same second would
    # otherwise silently destroy the first one -- which, given a bundle is
    # generated precisely when something is going wrong, is the moment an
    # operator can least afford to lose evidence.
    if target.exists() or target.is_symlink():
        raise OperationError(
            ErrorCode.DIAGNOSTIC_OUTPUT_UNWRITABLE,
            f"A bundle named {target.name} already exists; it is never "
            "overwritten. Retry in a moment or choose another directory.",
        )

    descriptor, temporary_name = tempfile.mkstemp(
        dir=destination, prefix=".mgo-support-", suffix=".tmp"
    )
    os.close(descriptor)
    temporary = Path(temporary_name)

    try:
        _write_archive(members, temporary)

        # The compressed archive is the artefact that actually lands on the SD
        # card, so its real size is checked -- not just the pre-compression
        # total already bounded above.
        written = temporary.stat().st_size
        if written > MAX_ARCHIVE_BYTES:
            raise OperationError(
                ErrorCode.DIAGNOSTIC_LIMIT_EXCEEDED,
                f"The completed bundle is {written} bytes, above the limit of "
                f"{MAX_ARCHIVE_BYTES}. It was discarded rather than published.",
            )

        with suppress(OSError):
            os.chmod(temporary, BUNDLE_FILE_MODE)
        _fsync_path(temporary)
        os.replace(temporary, target)
        _fsync_path(destination)
    except OperationError:
        with suppress(OSError):
            temporary.unlink()
        raise
    except (OSError, tarfile.TarError) as exc:
        with suppress(OSError):
            temporary.unlink()
        raise OperationError(
            ErrorCode.DIAGNOSTIC_ARCHIVE_FAILED,
            f"The bundle archive could not be written: {exc}.",
        ) from exc

    size = target.stat().st_size
    events.emit(
        Severity.INFO if outcome is BundleOutcome.COMPLETE else Severity.WARNING,
        "bundle.completed",
        f"A {outcome.value} support bundle was written.",
        bundle_filename=target.name,
        size_bytes=size,
        files_included=len(members),
        files_skipped=len(errors),
        result=outcome.value,
    )

    return BundleResult(
        outcome=outcome,
        bundle_path=target,
        members=tuple(name for name, _ in members),
        errors=tuple(errors),
        size_bytes=size,
    )


__all__ = [
    "ALLOWED_LOOPBACK_HOSTS",
    "BUNDLE_FILE_MODE",
    "BUNDLE_FORMAT_VERSION",
    "BUNDLE_NAME_PREFIX",
    "BUNDLE_SERVICE",
    "BUNDLE_SUFFIX",
    "COLLECTED_ENDPOINTS",
    "DEFAULT_API_BASE_URL",
    "ENDPOINT_TIMEOUT_SECONDS",
    "JOURNAL_SINCE",
    "MAX_ARCHIVE_BYTES",
    "MAX_ARCHIVE_MEMBERS",
    "MAX_COMMAND_ERROR_DETAIL",
    "MAX_COMMAND_OUTPUT_BYTES",
    "MAX_ENDPOINT_RESPONSE_BYTES",
    "MAX_JOURNAL_BYTES",
    "MAX_JOURNAL_LINES",
    "MAX_STORAGE_DEPTH",
    "MAX_STORAGE_ENTRIES",
    "NON_CANONICAL_PATH",
    "SERVICE_PROPERTIES",
    "SUBPROCESS_TIMEOUT_SECONDS",
    "BundleOutcome",
    "BundleResult",
    "CommandResult",
    "SourceError",
    "bundle_filename",
    "collect_configuration_summary",
    "collect_endpoint",
    "collect_journal",
    "collect_journal_disk_usage",
    "collect_service_status",
    "collect_storage_summary",
    "create_support_bundle",
]
