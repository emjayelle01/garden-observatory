"""Typed notification event model.

These value objects are the single vocabulary for a notification: producers
build a :class:`NotificationEvent` and hand it to the manager; providers only
ever consume it. The event knows nothing about FastAPI, transports or
persistence -- it only describes *what happened*, so any future provider
(Telegram, email, ...) can render it however that transport requires.

``payload`` is deliberately structured data: it must never contain a
pre-formatted human message, because formatting is a per-transport concern that
belongs to the provider that delivers the event.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

_MAX_TEXT_LENGTH = 500


class EventType(StrEnum):
    """The kinds of event the observatory can announce.

    The set is deliberately generic infrastructure vocabulary -- lifecycle,
    hardware availability, motion activity and errors. No species-specific
    event exists yet; bird events belong to a future task and will be added
    here when that task defines them.
    """

    SYSTEM_START = "system_start"
    SYSTEM_STOP = "system_stop"
    CAMERA_AVAILABLE = "camera_available"
    CAMERA_UNAVAILABLE = "camera_unavailable"
    MOTION_STATE_CHANGED = "motion_state_changed"
    NEW_OBSERVATION = "new_observation"
    ERROR = "error"


class EventSeverity(StrEnum):
    """How urgent an event is, independent of its type.

    Severity is advisory metadata for providers (a future Telegram provider
    might only forward ``WARNING`` and above); the manager itself treats all
    severities identically.
    """

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


def _bounded(text: str) -> str:
    """Collapse whitespace and trim text to a safe, log-friendly length."""
    collapsed = " ".join(text.split())
    if len(collapsed) <= _MAX_TEXT_LENGTH:
        return collapsed
    return collapsed[: _MAX_TEXT_LENGTH - 1].rstrip() + "…"


@dataclass(frozen=True)
class NotificationEvent:
    """A typed, immutable, JSON-serialisable description of one event.

    ``title`` is a short human-readable headline and ``summary`` one or two
    sentences of context; both are plain text with no transport formatting.
    ``payload`` carries the structured facts (dictionaries of JSON-compatible
    values) that a provider may render however its transport needs.
    ``correlation_id`` optionally ties the event to a related record, such as
    an observation or capture identifier.
    """

    event_id: str
    timestamp: datetime
    event_type: EventType
    severity: EventSeverity
    source: str
    title: str
    summary: str
    payload: dict[str, Any] = field(default_factory=dict)
    correlation_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "title", _bounded(self.title))
        object.__setattr__(self, "summary", _bounded(self.summary))

    def as_dict(self) -> dict[str, Any]:
        """Return the event as JSON-compatible values."""
        return {
            "event_id": self.event_id,
            "timestamp": self.timestamp.isoformat(),
            "event_type": self.event_type.value,
            "severity": self.severity.value,
            "source": self.source,
            "title": self.title,
            "summary": self.summary,
            "payload": self.payload,
            "correlation_id": self.correlation_id,
        }


def create_event(
    event_type: EventType,
    *,
    source: str,
    title: str,
    summary: str,
    severity: EventSeverity = EventSeverity.INFO,
    payload: dict[str, Any] | None = None,
    correlation_id: str | None = None,
    event_id: str | None = None,
    timestamp: datetime | None = None,
) -> NotificationEvent:
    """Build an event, generating identity and timestamp when not supplied.

    ``event_id`` defaults to a fresh UUID4 string and ``timestamp`` to the
    current timezone-aware UTC instant. Both are injectable so tests stay
    deterministic.
    """
    return NotificationEvent(
        event_id=event_id if event_id is not None else str(uuid.uuid4()),
        timestamp=timestamp if timestamp is not None else datetime.now(UTC),
        event_type=event_type,
        severity=severity,
        source=source,
        title=title,
        summary=summary,
        payload=payload if payload is not None else {},
        correlation_id=correlation_id,
    )
