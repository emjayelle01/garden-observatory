"""Tests for the notification manager: registration, fan-out and isolation."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from mgo.core.config import NotificationsConfig
from mgo.notifications.manager import (
    NotificationManager,
    build_notification_manager,
)
from mgo.notifications.models import EventType, NotificationEvent, create_event
from mgo.notifications.providers import (
    DeliveryResult,
    LoggingProvider,
    NotificationProvider,
    NullProvider,
)

_NOW = datetime(2026, 7, 26, 12, 0, 0, tzinfo=UTC)


class _RecordingProvider(NotificationProvider):
    """A provider capturing every delivered event, with scriptable outcome."""

    def __init__(
        self,
        name: str,
        *,
        success: bool = True,
        error: Exception | None = None,
    ) -> None:
        self._name = name
        self._success = success
        self._error = error
        self.events: list[NotificationEvent] = []

    @property
    def name(self) -> str:
        return self._name

    def send(self, event: NotificationEvent) -> DeliveryResult:
        self.events.append(event)
        if self._error is not None:
            raise self._error
        return DeliveryResult(success=self._success, detail="scripted")


def _event(event_id: str = "event-1") -> NotificationEvent:
    return create_event(
        EventType.SYSTEM_START,
        source="mgo-api",
        title="Application started",
        summary="MGO API started",
        event_id=event_id,
        timestamp=_NOW,
    )


# --- registration ----------------------------------------------------------


def test_register_provider_appears_in_status() -> None:
    manager = NotificationManager()
    manager.register_provider(_RecordingProvider("a"))
    manager.register_provider(_RecordingProvider("b"))

    assert manager.status().providers == ("a", "b")


def test_register_duplicate_name_is_rejected() -> None:
    manager = NotificationManager()
    manager.register_provider(_RecordingProvider("a"))

    with pytest.raises(ValueError, match="already registered"):
        manager.register_provider(_RecordingProvider("a"))


# --- fan-out ---------------------------------------------------------------


def test_publish_delivers_to_all_providers() -> None:
    manager = NotificationManager()
    first = _RecordingProvider("a")
    second = _RecordingProvider("b")
    manager.register_provider(first)
    manager.register_provider(second)

    event = _event()
    result = manager.publish(event)

    assert result.accepted is True
    assert result.failure_count == 0
    assert first.events == [event]
    assert second.events == [event]


def test_publish_with_no_providers_still_counts() -> None:
    """Publishing to nobody is accepted: the event happened, delivery is empty."""
    manager = NotificationManager()

    result = manager.publish(_event())

    assert result.accepted is True
    assert result.outcomes == ()
    assert manager.status().total_events_published == 1


# --- provider failure isolation --------------------------------------------


def test_raising_provider_never_blocks_others() -> None:
    """A provider raising mid-fan-out must not prevent later deliveries."""
    manager = NotificationManager()
    faulty = _RecordingProvider("faulty", error=RuntimeError("boom"))
    healthy = _RecordingProvider("healthy")
    manager.register_provider(faulty)
    manager.register_provider(healthy)

    result = manager.publish(_event())

    assert result.accepted is True
    assert result.failure_count == 1
    assert len(healthy.events) == 1
    outcomes = {outcome.provider: outcome for outcome in result.outcomes}
    assert outcomes["faulty"].success is False
    assert "boom" in outcomes["faulty"].detail
    assert outcomes["healthy"].success is True


def test_publish_never_raises_from_provider_exception() -> None:
    manager = NotificationManager()
    manager.register_provider(
        _RecordingProvider("faulty", error=RuntimeError("boom"))
    )

    # Must not raise.
    result = manager.publish(_event())

    assert result.failure_count == 1


def test_failed_delivery_result_is_counted() -> None:
    """A provider returning success=False counts as a delivery failure."""
    manager = NotificationManager()
    manager.register_provider(_RecordingProvider("sad", success=False))

    manager.publish(_event("event-1"))
    manager.publish(_event("event-2"))

    status = manager.status()
    assert status.total_events_published == 2
    assert status.total_delivery_failures == 2


def test_failure_counters_accumulate_across_providers() -> None:
    manager = NotificationManager()
    manager.register_provider(_RecordingProvider("sad", success=False))
    manager.register_provider(
        _RecordingProvider("faulty", error=RuntimeError("boom"))
    )
    manager.register_provider(_RecordingProvider("healthy"))

    manager.publish(_event())

    status = manager.status()
    assert status.total_events_published == 1
    assert status.total_delivery_failures == 2


# --- disabled manager ------------------------------------------------------


def test_disabled_manager_drops_events_without_delivery() -> None:
    manager = NotificationManager(enabled=False)
    provider = _RecordingProvider("a")
    manager.register_provider(provider)

    result = manager.publish(_event())

    assert result.accepted is False
    assert result.outcomes == ()
    assert provider.events == []
    status = manager.status()
    assert status.enabled is False
    assert status.total_events_published == 0
    assert status.total_delivery_failures == 0
    assert status.last_event_at is None


# --- status ----------------------------------------------------------------


def test_status_tracks_last_event_timestamp() -> None:
    manager = NotificationManager()
    manager.register_provider(_RecordingProvider("a"))

    assert manager.status().last_event_at is None
    manager.publish(_event())

    status = manager.status()
    assert status.last_event_at == _NOW
    assert status.total_events_published == 1


def test_status_as_dict_is_json_compatible() -> None:
    manager = NotificationManager()
    manager.register_provider(_RecordingProvider("a"))
    manager.publish(_event())

    assert manager.status().as_dict() == {
        "enabled": True,
        "providers": ["a"],
        "total_events_published": 1,
        "total_delivery_failures": 0,
        "last_event_at": _NOW.isoformat(),
    }


# --- configuration-driven build --------------------------------------------


def test_build_manager_disabled_registers_no_provider() -> None:
    manager = build_notification_manager(
        NotificationsConfig(enabled=False, provider="log")
    )

    assert manager.enabled is False
    assert manager.status().providers == ()


def test_build_manager_enabled_registers_configured_provider() -> None:
    manager = build_notification_manager(
        NotificationsConfig(enabled=True, provider="log")
    )

    assert manager.enabled is True
    assert manager.status().providers == ("log",)


def test_build_manager_supports_null_provider() -> None:
    manager = build_notification_manager(
        NotificationsConfig(enabled=True, provider="null")
    )

    assert manager.status().providers == ("null",)


def test_built_manager_delivers_end_to_end() -> None:
    """A config-built manager publishes through the real providers."""
    manager = build_notification_manager(
        NotificationsConfig(enabled=True, provider="null")
    )

    result = manager.publish(_event())

    assert result.accepted is True
    assert result.failure_count == 0
    assert manager.status().total_events_published == 1


def test_real_providers_can_share_a_manager() -> None:
    """The two real providers coexist under one manager without failures."""
    manager = NotificationManager()
    manager.register_provider(LoggingProvider())
    manager.register_provider(NullProvider())

    result = manager.publish(_event())

    assert result.failure_count == 0
    assert manager.status().providers == ("log", "null")
