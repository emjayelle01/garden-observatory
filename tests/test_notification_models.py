"""Tests for the typed notification event model."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from mgo.notifications.models import (
    EventSeverity,
    EventType,
    NotificationEvent,
    create_event,
)

_NOW = datetime(2026, 7, 26, 12, 0, 0, tzinfo=UTC)


def _event(**overrides: object) -> NotificationEvent:
    """Build a fully explicit, deterministic event."""
    fields: dict[str, object] = {
        "event_type": EventType.SYSTEM_START,
        "source": "mgo-api",
        "title": "Application started",
        "summary": "MGO API started",
        "severity": EventSeverity.INFO,
        "payload": {"version": "0.1.0"},
        "correlation_id": None,
        "event_id": "event-1",
        "timestamp": _NOW,
    }
    fields.update(overrides)
    event_type = fields.pop("event_type")
    return create_event(event_type, **fields)  # type: ignore[arg-type]


def test_create_event_uses_supplied_fields() -> None:
    """Explicit identity, timestamp and content are used verbatim."""
    event = _event(correlation_id="obs-42")

    assert event.event_id == "event-1"
    assert event.timestamp == _NOW
    assert event.event_type is EventType.SYSTEM_START
    assert event.severity is EventSeverity.INFO
    assert event.source == "mgo-api"
    assert event.title == "Application started"
    assert event.summary == "MGO API started"
    assert event.payload == {"version": "0.1.0"}
    assert event.correlation_id == "obs-42"


def test_create_event_generates_identity_and_timestamp() -> None:
    """Absent identity/timestamp are generated: UUID4 and aware UTC now."""
    before = datetime.now(UTC)
    event = create_event(
        EventType.ERROR,
        source="mgo-api",
        title="Something failed",
        summary="detail",
    )
    after = datetime.now(UTC)

    # A generated event_id must parse as a UUID.
    uuid.UUID(event.event_id)
    assert event.timestamp.tzinfo is not None
    assert before <= event.timestamp <= after


def test_create_event_generates_unique_ids() -> None:
    first = create_event(
        EventType.ERROR, source="s", title="t", summary="d"
    )
    second = create_event(
        EventType.ERROR, source="s", title="t", summary="d"
    )

    assert first.event_id != second.event_id


def test_create_event_defaults() -> None:
    """Severity defaults to info; payload to an empty dict; no correlation."""
    event = create_event(
        EventType.SYSTEM_STOP, source="mgo-api", title="t", summary="d"
    )

    assert event.severity is EventSeverity.INFO
    assert event.payload == {}
    assert event.correlation_id is None


def test_create_event_payloads_are_not_shared() -> None:
    """Each defaulted payload is a distinct dict (no shared mutable state)."""
    first = create_event(
        EventType.ERROR, source="s", title="t", summary="d"
    )
    second = create_event(
        EventType.ERROR, source="s", title="t", summary="d"
    )

    assert first.payload is not second.payload


def test_title_and_summary_are_bounded_and_collapsed() -> None:
    """Whitespace collapses and over-long text trims to a bounded length."""
    event = _event(title="  spaced \n title  ", summary="x" * 2000)

    assert event.title == "spaced title"
    assert len(event.summary) <= 500
    assert event.summary.endswith("…")


def test_as_dict_is_json_compatible() -> None:
    event = _event(correlation_id="obs-42")

    assert event.as_dict() == {
        "event_id": "event-1",
        "timestamp": _NOW.isoformat(),
        "event_type": "system_start",
        "severity": "info",
        "source": "mgo-api",
        "title": "Application started",
        "summary": "MGO API started",
        "payload": {"version": "0.1.0"},
        "correlation_id": "obs-42",
    }


def test_event_type_vocabulary() -> None:
    """The initial event-type vocabulary is exactly the Task 5 set."""
    assert {member.value for member in EventType} == {
        "system_start",
        "system_stop",
        "camera_available",
        "camera_unavailable",
        "motion_state_changed",
        "new_observation",
        "error",
    }
