"""Tests for ``GET /notifications/status`` and the app's event mapping helpers.

These call the route function directly with a lightweight fake request (the
same pattern as ``test_motion_api``), attaching a notification manager to
``app.state``. No provider transport or hardware is required: the endpoint
only reads manager state and never publishes anything.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from mgo.api.app import (
    APPLICATION_VERSION,
    NotificationStatusResponse,
    _camera_event,
    _motion_event,
    _system_event,
    notifications_status,
)
from mgo.core.camera import CameraReadiness, CameraStatus
from mgo.motion.models import MotionResult, MotionStatus
from mgo.notifications import (
    EventSeverity,
    EventType,
    NotificationManager,
    NullProvider,
    create_event,
)

_NOW = datetime(2026, 7, 26, 12, 0, 0, tzinfo=UTC)


def _request(manager: NotificationManager | None) -> SimpleNamespace:
    """Build a fake request whose app.state holds the given manager."""
    state = SimpleNamespace()
    if manager is not None:
        state.notification_manager = manager
    return SimpleNamespace(app=SimpleNamespace(state=state))


def _publish_one(manager: NotificationManager) -> None:
    manager.publish(
        create_event(
            EventType.SYSTEM_START,
            source="mgo-api",
            title="t",
            summary="d",
            event_id="event-1",
            timestamp=_NOW,
        )
    )


# --- status endpoint --------------------------------------------------------


def test_status_without_state_reports_config_default() -> None:
    """With no manager attached the endpoint builds one from configuration.

    The repository default configuration disables notifications, so the
    fallback manager reports disabled with no providers and zero counters.
    """
    response = notifications_status(_request(None))

    assert isinstance(response, NotificationStatusResponse)
    assert response.enabled is False
    assert response.providers == []
    assert response.total_events_published == 0
    assert response.total_delivery_failures == 0
    assert response.last_event_at is None


def test_status_reports_enabled_manager_with_provider() -> None:
    manager = NotificationManager()
    manager.register_provider(NullProvider())

    response = notifications_status(_request(manager))

    assert response.enabled is True
    assert response.providers == ["null"]


def test_status_reports_publish_counters() -> None:
    manager = NotificationManager()
    manager.register_provider(NullProvider())
    _publish_one(manager)

    response = notifications_status(_request(manager))

    assert response.total_events_published == 1
    assert response.total_delivery_failures == 0
    assert response.last_event_at == _NOW.isoformat()


def test_status_reports_delivery_failures() -> None:
    class _FailingProvider(NullProvider):
        def send(self, event: object) -> object:
            raise RuntimeError("boom")

    manager = NotificationManager()
    manager.register_provider(_FailingProvider())
    _publish_one(manager)

    response = notifications_status(_request(manager))

    assert response.total_events_published == 1
    assert response.total_delivery_failures == 1


def test_status_never_publishes() -> None:
    """Requesting the status must not change the manager's counters."""
    manager = NotificationManager()
    manager.register_provider(NullProvider())

    notifications_status(_request(manager))
    notifications_status(_request(manager))

    assert manager.status().total_events_published == 0


# --- event mapping helpers --------------------------------------------------


def _readiness(status: CameraStatus, available: bool) -> CameraReadiness:
    return CameraReadiness(
        enabled=True,
        backend="null",
        status=status,
        available=available,
        detail="detail text",
        checked_at=_NOW,
    )


def test_system_event_mapping() -> None:
    event = _system_event(EventType.SYSTEM_START, "MGO API started")

    assert event.event_type is EventType.SYSTEM_START
    assert event.severity is EventSeverity.INFO
    assert event.source == "mgo-api"
    assert event.summary == "MGO API started"
    # Compared against the central authority rather than a literal: the point
    # is that lifecycle events carry the *resolved* release version, not that
    # the release happens to be a particular number today.
    assert event.payload == {"version": APPLICATION_VERSION}


def test_camera_event_maps_available_readiness() -> None:
    event = _camera_event(_readiness(CameraStatus.AVAILABLE, True))

    assert event.event_type is EventType.CAMERA_AVAILABLE
    assert event.severity is EventSeverity.INFO
    assert event.source == "mgo-camera"
    assert event.payload["available"] is True


def test_camera_event_maps_unavailable_readiness() -> None:
    event = _camera_event(_readiness(CameraStatus.WAITING_FOR_HARDWARE, False))

    assert event.event_type is EventType.CAMERA_UNAVAILABLE
    assert event.severity is EventSeverity.WARNING
    assert event.summary == "detail text"
    assert event.payload["available"] is False


def test_motion_event_mapping() -> None:
    result = MotionResult(
        status=MotionStatus.MOTION_DETECTED,
        detected=True,
        score=0.5,
        threshold=0.08,
        frames_available=True,
        detail="motion detail",
        evaluated_at=_NOW,
    )

    event = _motion_event(result)

    assert event.event_type is EventType.MOTION_STATE_CHANGED
    assert event.source == "mgo-motion"
    assert event.summary == "motion detail"
    # The structured result travels in the payload, not a formatted message.
    assert event.payload["status"] == "motion_detected"
    assert event.payload["score"] == 0.5
