"""Notification providers: the pluggable delivery side of the framework.

A provider owns exactly one concern -- delivering an event over one transport.
Business logic never touches a provider directly; only the
:class:`~mgo.notifications.manager.NotificationManager` calls :meth:`send`.

Only two providers exist in this task, both transport-free by design:

* :class:`LoggingProvider` writes events to the application log, giving the
  framework an end-to-end observable delivery path for validation;
* :class:`NullProvider` accepts and discards events, for tests and disabled
  configurations.

Future transports (Telegram, email, ...) become new subclasses registered with
the manager -- no producer code changes.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass

from mgo.notifications.models import EventSeverity, NotificationEvent

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class DeliveryResult:
    """The outcome of one provider delivering (or failing to deliver) an event.

    Providers report failure by returning ``success=False`` with a truthful
    ``detail``; raising is tolerated by the manager but returning cleanly is
    the contract.
    """

    success: bool
    detail: str = ""


class NotificationProvider(ABC):
    """Abstract delivery transport for notification events.

    Implementations must be cheap to construct, must not require network
    access at import time, and should return a :class:`DeliveryResult` rather
    than raising; the manager isolates exceptions regardless, so a faulty
    provider can never crash publication to other providers.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """A short, stable identifier for this provider (used in status)."""

    @abstractmethod
    def send(self, event: NotificationEvent) -> DeliveryResult:
        """Deliver one event, returning success/failure cleanly."""


#: Log level used for each event severity by the logging provider.
_SEVERITY_LOG_LEVELS: dict[EventSeverity, int] = {
    EventSeverity.INFO: logging.INFO,
    EventSeverity.WARNING: logging.WARNING,
    EventSeverity.ERROR: logging.ERROR,
    EventSeverity.CRITICAL: logging.CRITICAL,
}


class LoggingProvider(NotificationProvider):
    """Delivers events to the application log.

    Exists to validate the framework end to end: every published event becomes
    a structured log line at a level matching its severity. It performs no I/O
    beyond logging and can never block.
    """

    @property
    def name(self) -> str:
        return "log"

    def send(self, event: NotificationEvent) -> DeliveryResult:
        level = _SEVERITY_LOG_LEVELS.get(event.severity, logging.INFO)
        LOGGER.log(
            level,
            "Notification %s [%s] from %s: %s — %s",
            event.event_type.value,
            event.severity.value,
            event.source,
            event.title,
            event.summary,
        )
        return DeliveryResult(success=True, detail="Event written to the log")


class NullProvider(NotificationProvider):
    """Accepts and discards every event.

    Exists for tests and for configurations where notifications are wired but
    intentionally deliver nowhere.
    """

    @property
    def name(self) -> str:
        return "null"

    def send(self, event: NotificationEvent) -> DeliveryResult:
        return DeliveryResult(success=True, detail="Event discarded")


#: Factory mapping of configured provider names to implementations. Future
#: providers are added here (and to ``SUPPORTED_NOTIFICATION_PROVIDERS`` in
#: ``mgo.core.config``) without touching producer code.
_PROVIDER_FACTORIES: dict[str, type[NotificationProvider]] = {
    "log": LoggingProvider,
    "null": NullProvider,
}


def build_provider(name: str) -> NotificationProvider:
    """Build the provider selected by a configured name.

    Names are matched case-insensitively after trimming, mirroring the camera
    backend factory. An unknown name raises :class:`ValueError` naming the
    supported providers.
    """
    factory = _PROVIDER_FACTORIES.get(name.strip().lower())
    if factory is None:
        supported = ", ".join(sorted(_PROVIDER_FACTORIES))
        raise ValueError(
            f"Unsupported notification provider {name!r}; "
            f"supported providers: {supported}"
        )
    return factory()
