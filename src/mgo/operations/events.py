"""Structured operational events for MGO operations tooling.

The project requires operational logs carrying a timestamp, service, severity,
event ID and error code. This module provides exactly that and nothing more: one
JSON object per line, written to a stream.

It is deliberately **not** a logging framework. A backup job's output is read in
two places -- ``journalctl -u mgo-backup.service`` and an operator's terminal --
and both want the same thing: one complete, parseable record per event. A
third-party structured-logging dependency would add install surface, a
configuration system and a filtering model to solve a problem that is
``json.dumps`` plus a fixed field set.

Three properties are load-bearing:

* **Every line is valid JSON.** A message containing a newline, a quote, a tab
  or non-ASCII text must not be able to split or corrupt a record, because a
  half-parsed line is worse than no line at all. ``json.dumps`` escapes all of
  them, and the writer emits exactly one ``\\n`` itself.
* **Emission never raises.** An event is a *report* about work; it must not be
  able to fail the work it reports on. A field that cannot be serialised is
  replaced by a marker rather than propagating a ``TypeError`` out of a backup.
* **Redaction is by default, not by exception.** Values are dropped whenever
  their key *looks* sensitive, so a field added by a future author is redacted
  until someone deliberately decides otherwise.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import IO, Any

from mgo.operations.errors import ErrorCode

#: Placeholder substituted for any value withheld by :func:`redact_mapping`.
REDACTED = "<redacted>"

#: Placeholder substituted for a value that cannot be serialised to JSON.
UNSERIALISABLE = "<unserialisable>"

#: Upper bound for an event message. Operational messages are summaries; an
#: unbounded one could carry a large SQLite error, a captured command output or
#: a path listing into the journal on every failed run.
MAX_MESSAGE_LENGTH = 500

#: Substrings that make a field name sensitive. Matching is case-insensitive and
#: on *substring*, so ``api_key``, ``API_KEY``, ``bot_token`` and
#: ``smtp_password`` are all caught by the entries below.
#:
#: ``key`` is included even though it is broad. A false positive costs one
#: redacted diagnostic field; a false negative costs a leaked credential, and
#: this vocabulary is applied to values that end up in a support bundle sent to
#: another person.
SENSITIVE_KEY_MARKERS: tuple[str, ...] = (
    "secret",
    "token",
    "password",
    "passwd",
    "credential",
    "private",
    "api_key",
    "apikey",
    "key",
    "auth",
    "signature",
    "cookie",
    "session",
)


class Severity(StrEnum):
    """The closed severity vocabulary for operational events.

    Closed on purpose: an operator filtering ``severity == "ERROR"`` must not
    silently miss records because one call site invented ``"err"`` or
    ``"fatal"``.
    """

    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


def is_sensitive_key(name: str) -> bool:
    """Return whether a field name should have its value withheld."""
    lowered = name.lower()
    return any(marker in lowered for marker in SENSITIVE_KEY_MARKERS)


def redact_mapping(values: Mapping[str, Any]) -> dict[str, Any]:
    """Return ``values`` with every sensitive-looking value replaced.

    Keys are preserved -- knowing that a ``token`` setting *exists* is useful
    diagnostic information and is not itself a secret -- while the value becomes
    :data:`REDACTED`. Nested mappings are redacted recursively, because a
    sensitive value one level down is exactly as sensitive.

    Redaction is applied to the key regardless of the value's type: a
    ``password`` whose value is ``None``, ``0`` or ``False`` is still redacted,
    so the presence or absence of a credential cannot be inferred from whether
    a field was withheld.
    """
    redacted: dict[str, Any] = {}
    for key, value in values.items():
        if is_sensitive_key(key):
            redacted[key] = REDACTED
        elif isinstance(value, Mapping):
            redacted[key] = redact_mapping(value)
        else:
            redacted[key] = value
    return redacted


def _bounded_message(message: str) -> str:
    """Collapse whitespace and trim a message to a journal-friendly length.

    Collapsing runs of whitespace also removes embedded newlines from the
    *content*; JSON escaping already guarantees the record stays on one line, so
    this is about readability rather than correctness.
    """
    collapsed = " ".join(message.split())
    if len(collapsed) <= MAX_MESSAGE_LENGTH:
        return collapsed
    return collapsed[: MAX_MESSAGE_LENGTH - 1].rstrip() + "…"


def _jsonable(value: Any) -> Any:
    """Return a JSON-serialisable stand-in for ``value``.

    Handles the types the operations tooling actually produces (paths become
    strings, enums become their values, datetimes become ISO 8601) and falls
    back to :data:`UNSERIALISABLE` rather than letting an unexpected object
    raise out of an event emission.
    """
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(item) for item in value]
    try:
        return str(value)
    except Exception:  # pragma: no cover - a __str__ that raises is pathological
        return UNSERIALISABLE


@dataclass(frozen=True)
class OperationEvent:
    """One structured operational event.

    The six required fields are always present, in a stable order, even when a
    value is ``None``. An absent key and a null value mean different things to a
    log consumer, and "this run reported no error code" is information worth
    recording explicitly.
    """

    service: str
    severity: Severity
    event_id: str
    message: str
    error_code: ErrorCode | None = None
    timestamp: datetime | None = None
    fields: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        """Return the serialisable record, with extra fields redacted."""
        moment = self.timestamp if self.timestamp is not None else datetime.now(UTC)
        record: dict[str, Any] = {
            "timestamp": moment.astimezone(UTC).isoformat(),
            "service": self.service,
            "severity": self.severity.value,
            "event_id": self.event_id,
            "message": _bounded_message(self.message),
            "error_code": (
                self.error_code.value if self.error_code is not None else None
            ),
        }
        for key, value in redact_mapping(self.fields).items():
            # The six required fields are never displaced by an extra field: a
            # caller passing ``severity=...`` in ``fields`` must not be able to
            # rewrite the record's own severity.
            if key in record:
                continue
            record[key] = _jsonable(value)
        return record

    def to_json(self) -> str:
        """Return this event as a single JSON line (no trailing newline).

        ``ensure_ascii=False`` keeps non-ASCII text readable rather than
        escaping it into ``\\uXXXX`` sequences; the output is written as UTF-8.
        Keys are *not* sorted, so the six required fields keep their declared
        order and a human scanning the journal sees them first.
        """
        try:
            return json.dumps(self.as_dict(), ensure_ascii=False)
        except (TypeError, ValueError):
            # Last-resort record. Emission must never raise, and a minimal
            # truthful line beats a crashed backup.
            return json.dumps(
                {
                    "timestamp": datetime.now(UTC).isoformat(),
                    "service": self.service,
                    "severity": Severity.ERROR.value,
                    "event_id": "event.serialisation_failed",
                    "message": "An operational event could not be serialised.",
                    "error_code": ErrorCode.UNEXPECTED_ERROR.value,
                },
                ensure_ascii=False,
            )


class EventEmitter:
    """Writes structured events as JSON lines to a stream.

    Defaults to ``stdout`` because that is what ``systemd`` captures into the
    journal for a ``Type=oneshot`` unit: the backup service needs no file
    handler, no log path and no rotation of its own.

    The emitter is intentionally tiny and holds no state beyond its stream and
    service name. It is constructed per command run, not shared globally, so
    tests can capture output by passing a :class:`io.StringIO` and no module
    import has a side effect on the process's logging configuration.
    """

    def __init__(self, service: str, stream: IO[str] | None = None) -> None:
        self._service = service
        self._stream = stream if stream is not None else sys.stdout

    @property
    def service(self) -> str:
        """The component name stamped on every event from this emitter."""
        return self._service

    def emit(
        self,
        severity: Severity,
        event_id: str,
        message: str,
        *,
        error_code: ErrorCode | None = None,
        **fields: Any,
    ) -> OperationEvent:
        """Write one event and return it.

        Returning the event lets a caller assert on what was reported without
        re-parsing the stream, and lets a command build its summary from the
        same objects the operator saw.

        A stream that cannot be written to (a closed pipe, a full disk) is
        swallowed: losing a log line must not fail an otherwise successful
        backup, and the caller's own exit status remains the authority on
        whether the work succeeded.
        """
        event = OperationEvent(
            service=self._service,
            severity=severity,
            event_id=event_id,
            message=message,
            error_code=error_code,
            fields=fields,
        )
        try:
            self._stream.write(event.to_json() + "\n")
            self._stream.flush()
        except (OSError, ValueError):
            pass
        return event

    def debug(self, event_id: str, message: str, **fields: Any) -> OperationEvent:
        """Emit a ``DEBUG`` event."""
        return self.emit(Severity.DEBUG, event_id, message, **fields)

    def info(self, event_id: str, message: str, **fields: Any) -> OperationEvent:
        """Emit an ``INFO`` event."""
        return self.emit(Severity.INFO, event_id, message, **fields)

    def warning(
        self,
        event_id: str,
        message: str,
        *,
        error_code: ErrorCode | None = None,
        **fields: Any,
    ) -> OperationEvent:
        """Emit a ``WARNING`` event."""
        return self.emit(
            Severity.WARNING, event_id, message, error_code=error_code, **fields
        )

    def error(
        self,
        event_id: str,
        message: str,
        *,
        error_code: ErrorCode,
        **fields: Any,
    ) -> OperationEvent:
        """Emit an ``ERROR`` event. A stable code is required, not optional."""
        return self.emit(
            Severity.ERROR, event_id, message, error_code=error_code, **fields
        )


__all__ = [
    "MAX_MESSAGE_LENGTH",
    "REDACTED",
    "SENSITIVE_KEY_MARKERS",
    "UNSERIALISABLE",
    "EventEmitter",
    "OperationEvent",
    "Severity",
    "is_sensitive_key",
    "redact_mapping",
]
