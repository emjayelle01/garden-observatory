"""The notification manager: the single publication point for events.

Producers (the application lifespan, the camera monitor, the motion monitor,
and future features) publish a :class:`~mgo.notifications.models.NotificationEvent`
here and never talk to a transport. The manager fans each event out to every
registered provider, isolating each provider completely: one provider raising
or reporting failure never prevents delivery to the others and never
propagates to the producer.

A single asyncio event loop reads and writes the counters, so plain attributes
are sufficient; no locking is required (mirrors
:class:`mgo.motion.monitor.MotionState`).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from mgo.core.config import NotificationsConfig
from mgo.notifications.models import NotificationEvent
from mgo.notifications.providers import NotificationProvider, build_provider

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class DeliveryOutcome:
    """One provider's delivery outcome for one published event."""

    provider: str
    success: bool
    detail: str

    def as_dict(self) -> dict[str, Any]:
        """Return the outcome as JSON-compatible values."""
        return {
            "provider": self.provider,
            "success": self.success,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class PublishResult:
    """The overall outcome of publishing one event.

    ``accepted`` is ``False`` only when notifications are disabled and the
    event was dropped without any delivery attempt; ``outcomes`` then is empty.
    """

    accepted: bool
    outcomes: tuple[DeliveryOutcome, ...]

    @property
    def failure_count(self) -> int:
        """Number of providers that failed to deliver this event."""
        return sum(1 for outcome in self.outcomes if not outcome.success)


@dataclass(frozen=True)
class NotificationStatus:
    """A read-only snapshot of the manager for the status endpoint."""

    enabled: bool
    providers: tuple[str, ...]
    total_events_published: int
    total_delivery_failures: int
    last_event_at: datetime | None

    def as_dict(self) -> dict[str, Any]:
        """Return the status as JSON-compatible values."""
        return {
            "enabled": self.enabled,
            "providers": list(self.providers),
            "total_events_published": self.total_events_published,
            "total_delivery_failures": self.total_delivery_failures,
            "last_event_at": (
                self.last_event_at.isoformat()
                if self.last_event_at is not None
                else None
            ),
        }


class NotificationManager:
    """Registers providers and fans published events out to all of them.

    When constructed with ``enabled=False`` the manager is a truthful no-op:
    :meth:`publish` accepts nothing, counts nothing and delivers nothing, so
    producers can always publish unconditionally without checking
    configuration themselves.
    """

    def __init__(self, *, enabled: bool = True) -> None:
        self._enabled = enabled
        self._providers: list[NotificationProvider] = []
        self._total_events_published = 0
        self._total_delivery_failures = 0
        self._last_event_at: datetime | None = None

    @property
    def enabled(self) -> bool:
        """Whether this manager delivers events at all."""
        return self._enabled

    def register_provider(self, provider: NotificationProvider) -> None:
        """Register a provider to receive every subsequently published event.

        Duplicate names are rejected: each provider name may be registered at
        most once, so status reporting and fan-out stay unambiguous.
        """
        if any(existing.name == provider.name for existing in self._providers):
            raise ValueError(
                f"Notification provider {provider.name!r} is already registered"
            )
        self._providers.append(provider)
        LOGGER.info("Notification provider registered: %s", provider.name)

    def publish(self, event: NotificationEvent) -> PublishResult:
        """Fan one event out to every registered provider.

        Never raises. Each provider is attempted independently: an exception
        or a failure result from one provider is logged, counted as a delivery
        failure, and never prevents delivery to the remaining providers. When
        the manager is disabled the event is dropped without counting.
        """
        if not self._enabled:
            LOGGER.debug(
                "Notifications disabled; dropping event %s (%s)",
                event.event_type.value,
                event.event_id,
            )
            return PublishResult(accepted=False, outcomes=())

        outcomes: list[DeliveryOutcome] = []
        for provider in self._providers:
            try:
                result = provider.send(event)
            except Exception as exc:
                LOGGER.exception(
                    "Notification provider %s raised while delivering event "
                    "%s (%s)",
                    provider.name,
                    event.event_type.value,
                    event.event_id,
                )
                outcomes.append(
                    DeliveryOutcome(
                        provider=provider.name,
                        success=False,
                        detail=f"Provider raised: {exc}",
                    )
                )
                continue
            if not result.success:
                LOGGER.warning(
                    "Notification provider %s failed to deliver event %s "
                    "(%s): %s",
                    provider.name,
                    event.event_type.value,
                    event.event_id,
                    result.detail,
                )
            outcomes.append(
                DeliveryOutcome(
                    provider=provider.name,
                    success=result.success,
                    detail=result.detail,
                )
            )

        publish_result = PublishResult(accepted=True, outcomes=tuple(outcomes))
        self._total_events_published += 1
        self._total_delivery_failures += publish_result.failure_count
        self._last_event_at = event.timestamp
        return publish_result

    def status(self) -> NotificationStatus:
        """Return a read-only snapshot of the manager's state."""
        return NotificationStatus(
            enabled=self._enabled,
            providers=tuple(provider.name for provider in self._providers),
            total_events_published=self._total_events_published,
            total_delivery_failures=self._total_delivery_failures,
            last_event_at=self._last_event_at,
        )


def build_notification_manager(config: NotificationsConfig) -> NotificationManager:
    """Build the manager selected by configuration.

    A disabled configuration yields a no-op manager with no providers; an
    enabled one yields a manager with the single configured provider
    registered. Future multi-provider configuration extends here.
    """
    manager = NotificationManager(enabled=config.enabled)
    if config.enabled:
        manager.register_provider(build_provider(config.provider))
    return manager
