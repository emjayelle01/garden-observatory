"""Tests for the logging and null notification providers and their factory."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import pytest

from mgo.core.config import SUPPORTED_NOTIFICATION_PROVIDERS
from mgo.notifications.models import (
    EventSeverity,
    EventType,
    NotificationEvent,
    create_event,
)
from mgo.notifications.providers import (
    LoggingProvider,
    NullProvider,
    build_provider,
)

_NOW = datetime(2026, 7, 26, 12, 0, 0, tzinfo=UTC)


def _event(severity: EventSeverity = EventSeverity.INFO) -> NotificationEvent:
    return create_event(
        EventType.SYSTEM_START,
        source="mgo-api",
        title="Application started",
        summary="MGO API started",
        severity=severity,
        event_id="event-1",
        timestamp=_NOW,
    )


def test_logging_provider_name() -> None:
    assert LoggingProvider().name == "log"


def test_logging_provider_logs_and_succeeds(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The logging provider writes one log line and reports success."""
    provider = LoggingProvider()

    with caplog.at_level(logging.INFO, logger="mgo.notifications.providers"):
        result = provider.send(_event())

    assert result.success is True
    assert len(caplog.records) == 1
    record = caplog.records[0]
    assert record.levelno == logging.INFO
    assert "system_start" in record.getMessage()
    assert "Application started" in record.getMessage()


@pytest.mark.parametrize(
    ("severity", "expected_level"),
    [
        (EventSeverity.INFO, logging.INFO),
        (EventSeverity.WARNING, logging.WARNING),
        (EventSeverity.ERROR, logging.ERROR),
        (EventSeverity.CRITICAL, logging.CRITICAL),
    ],
)
def test_logging_provider_maps_severity_to_level(
    caplog: pytest.LogCaptureFixture,
    severity: EventSeverity,
    expected_level: int,
) -> None:
    with caplog.at_level(logging.INFO, logger="mgo.notifications.providers"):
        LoggingProvider().send(_event(severity))

    assert caplog.records[0].levelno == expected_level


def test_null_provider_name() -> None:
    assert NullProvider().name == "null"


def test_null_provider_discards_and_succeeds(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The null provider accepts the event, emits nothing, and succeeds."""
    with caplog.at_level(logging.DEBUG, logger="mgo.notifications"):
        result = NullProvider().send(_event())

    assert result.success is True
    assert caplog.records == []


def test_build_provider_log() -> None:
    assert isinstance(build_provider("log"), LoggingProvider)


def test_build_provider_null() -> None:
    assert isinstance(build_provider("null"), NullProvider)


def test_build_provider_normalises_name() -> None:
    """Names match case-insensitively after trimming, like camera backends."""
    assert isinstance(build_provider("  LOG "), LoggingProvider)


def test_build_provider_rejects_unknown_name() -> None:
    with pytest.raises(ValueError, match="Unsupported notification provider"):
        build_provider("telegram")


def test_factory_covers_exactly_the_supported_providers() -> None:
    """Every supported configured name builds, keeping config and factory in sync."""
    for name in SUPPORTED_NOTIFICATION_PROVIDERS:
        assert build_provider(name).name == name
