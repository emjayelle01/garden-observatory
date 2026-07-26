"""Notification framework for Matt's Garden Observatory.

Event-driven infrastructure only: producers publish typed
:class:`~mgo.notifications.models.NotificationEvent` objects to the
:class:`~mgo.notifications.manager.NotificationManager`, which fans them out
to pluggable :class:`~mgo.notifications.providers.NotificationProvider`
implementations. Business logic never calls a delivery transport directly.
"""

from mgo.notifications.manager import (
    DeliveryOutcome,
    NotificationManager,
    NotificationStatus,
    PublishResult,
    build_notification_manager,
)
from mgo.notifications.models import (
    EventSeverity,
    EventType,
    NotificationEvent,
    create_event,
)
from mgo.notifications.providers import (
    DeliveryResult,
    LoggingProvider,
    NotificationProvider,
    NullProvider,
    build_provider,
)

__all__ = [
    "DeliveryOutcome",
    "DeliveryResult",
    "EventSeverity",
    "EventType",
    "LoggingProvider",
    "NotificationEvent",
    "NotificationManager",
    "NotificationProvider",
    "NotificationStatus",
    "NullProvider",
    "PublishResult",
    "build_notification_manager",
    "build_provider",
    "create_event",
]
