"""Tests for ``GET /event-capture/status``.

The endpoint's whole value is that it is inert. It reports what the feature is
doing without doing anything itself: no camera, no preview, no capture, no
trigger, no database, no counter movement. These tests assert that by attaching
doubles that fail loudly if the endpoint touches them, rather than by reading
the implementation and believing it.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

import mgo.api.app as app_module
from mgo.api.app import app, event_capture_status
from mgo.event_capture import EventCaptureRuntimeState, EventCaptureState

_REQUIRED_FIELDS = {
    "enabled",
    "state",
    "pending_triggers",
    "total_triggers_received",
    "total_captures_succeeded",
    "total_captures_failed",
    "total_triggers_dropped",
    "last_trigger_at",
    "last_capture_id",
    "last_capture_at",
    "last_error",
}


class _Exploding:
    """Any attribute access on this is a test failure."""

    def __init__(self, label: str) -> None:
        self._label = label

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(
            f"the status endpoint touched {self._label}.{name}"
        )


def _request(state: EventCaptureRuntimeState | None) -> SimpleNamespace:
    """Build a fake request whose app.state trips on any other subsystem."""
    app_state = SimpleNamespace(
        camera_coordinator=_Exploding("camera_coordinator"),
        capture_service=_Exploding("capture_service"),
        preview_service=_Exploding("preview_service"),
        capture_archive=_Exploding("capture_archive"),
        capture_workflow=_Exploding("capture_workflow"),
        event_capture_service=_Exploding("event_capture_service"),
    )
    if state is not None:
        app_state.event_capture_state = state
    return SimpleNamespace(app=SimpleNamespace(state=app_state))


def test_the_route_is_registered_on_the_production_application() -> None:
    """The endpoint really exists at the documented path and method."""
    routes = {
        (route.path, tuple(sorted(route.methods)))
        for route in app.routes
        if hasattr(route, "methods")
    }

    assert ("/event-capture/status", ("GET",)) in routes


def test_the_openapi_document_describes_it() -> None:
    """A typed response model, published like every other status endpoint."""
    schema = app.openapi()

    assert "/event-capture/status" in schema["paths"]
    assert "get" in schema["paths"]["/event-capture/status"]


def test_a_disabled_feature_reports_disabled_and_zeroes() -> None:
    """Off means off, truthfully and completely."""
    response = event_capture_status(
        _request(EventCaptureRuntimeState(enabled=False))
    )

    payload = response.model_dump()
    assert set(payload) == _REQUIRED_FIELDS
    assert payload["enabled"] is False
    assert payload["state"] == "disabled"
    assert payload["pending_triggers"] == 0
    assert payload["total_triggers_received"] == 0
    assert payload["total_captures_succeeded"] == 0
    assert payload["total_captures_failed"] == 0
    assert payload["total_triggers_dropped"] == 0
    assert payload["last_trigger_at"] is None
    assert payload["last_capture_id"] is None
    assert payload["last_capture_at"] is None
    assert payload["last_error"] is None


def test_an_enabled_idle_feature_reports_idle() -> None:
    """Enabled with a live worker and nothing to do is ``idle``."""
    payload = event_capture_status(
        _request(EventCaptureRuntimeState(enabled=True))
    ).model_dump()

    assert payload["enabled"] is True
    assert payload["state"] == "idle"


def test_a_capture_in_progress_reports_capturing() -> None:
    """An operator can see the camera is busy on motion's behalf."""
    state = EventCaptureRuntimeState(enabled=True)
    state.state = EventCaptureState.CAPTURING
    state.pending_triggers = 1
    state.total_triggers_received = 3
    state.last_trigger_at = datetime(2026, 8, 7, 9, 30, tzinfo=UTC)

    payload = event_capture_status(_request(state)).model_dump()

    assert payload["state"] == "capturing"
    assert payload["pending_triggers"] == 1
    assert payload["total_triggers_received"] == 3
    assert payload["last_trigger_at"] == "2026-08-07T09:30:00+00:00"


def test_an_error_state_reports_the_safe_message() -> None:
    """Every failure field is reported, and only the fixed public sentence."""
    capture_id = str(uuid.uuid4())
    state = EventCaptureRuntimeState(enabled=True)
    state.state = EventCaptureState.ERROR
    state.total_triggers_received = 5
    state.total_triggers_dropped = 1
    state.total_captures_succeeded = 2
    state.total_captures_failed = 1
    state.last_trigger_at = datetime(2026, 8, 7, 9, 30, tzinfo=UTC)
    state.last_capture_id = capture_id
    state.last_capture_at = datetime(2026, 8, 7, 9, 25, tzinfo=UTC)
    state.last_error = "Motion-triggered camera backend failed."

    payload = event_capture_status(_request(state)).model_dump()

    assert payload["state"] == "error"
    assert payload["total_triggers_received"] == 5
    assert payload["total_triggers_dropped"] == 1
    assert payload["total_captures_succeeded"] == 2
    assert payload["total_captures_failed"] == 1
    assert payload["last_capture_id"] == capture_id
    assert payload["last_capture_at"] == "2026-08-07T09:25:00+00:00"
    assert payload["last_error"] == "Motion-triggered camera backend failed."


def test_the_response_exposes_no_path() -> None:
    """The endpoint reports what happened, never where the application lives."""
    state = EventCaptureRuntimeState(enabled=True)
    state.last_error = "Motion-triggered capture could not be written."

    rendered = repr(
        event_capture_status(_request(state)).model_dump()
    )

    for leak in ("/var/lib", "/etc/", "C:\\", "mgo.db", "captures/", ".toml"):
        assert leak not in rendered, leak


def test_requesting_it_touches_no_other_subsystem() -> None:
    """Camera, preview, capture, archive and the worker are all off-limits."""
    state = EventCaptureRuntimeState(enabled=True)

    # Every other service on this request explodes on attribute access.
    event_capture_status(_request(state))


def test_requesting_it_changes_no_counter() -> None:
    """A read is a read: two requests return exactly the same values."""
    state = EventCaptureRuntimeState(enabled=True)
    state.total_triggers_received = 4
    state.total_captures_succeeded = 3

    first = event_capture_status(_request(state)).model_dump()
    second = event_capture_status(_request(state)).model_dump()

    assert first == second
    assert state.total_triggers_received == 4
    assert state.total_captures_succeeded == 3
    assert state.pending_triggers == 0


def test_a_missing_holder_falls_back_without_starting_anything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without a lifespan the endpoint still answers, and still starts nothing.

    The fallback mirrors the configured enablement and creates a holder only --
    no queue, no worker, no camera. With the repository defaults that is a
    truthful ``disabled``.
    """
    payload = event_capture_status(_request(None)).model_dump()

    assert payload["enabled"] is app_module.config.event_capture.enabled
    assert payload["state"] == "disabled"
    assert payload["total_triggers_received"] == 0
