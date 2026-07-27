"""Production-app routing tests for ``GET /notifications/status``.

These exist because of a validation finding: the endpoint's handler tests all
passed while a production service returned 404, since every API test called
the route *function* directly and nothing ever exercised HTTP routing on the
real application object. These tests close that gap by importing the exact
ASGI app production serves (``uvicorn mgo.api.app:app`` -- there is no router
or application factory; routes are registered directly on this module-level
object) and driving real in-process HTTP requests through it.

The ASGI interface is driven directly because the test dependencies do not
include ``httpx`` (required by FastAPI's ``TestClient``) and this task adds no
dependencies. Requests are deterministic, in-process and lifespan-free: the
endpoint lazily builds its manager from the repository's default (disabled)
configuration, so no hardware, database writes or background monitors are
involved.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from typing import Any

import pytest

# The exact production application object: the systemd/uvicorn entry point
# serves ``mgo.api.app:app``. Importing anything narrower (a handler, a
# sub-app) would not prove production routing.
from mgo.api.app import app
from mgo.notifications import NotificationManager, NullProvider

_EXPECTED_FIELDS = {
    "enabled",
    "providers",
    "total_events_published",
    "total_delivery_failures",
    "last_event_at",
}


def _asgi_get(path: str) -> tuple[int, dict[str, Any]]:
    """Perform one in-process HTTP GET against the production ASGI app.

    Returns ``(status_code, decoded_json_body)``. Exercises the full routing
    stack -- scope matching, dispatch, response model serialisation -- exactly
    as a production HTTP request does, without sockets or a server.
    """
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"testserver")],
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
    }
    messages: list[dict[str, Any]] = []

    async def _receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def _send(message: dict[str, Any]) -> None:
        messages.append(message)

    asyncio.run(app(scope, _receive, _send))

    status = next(
        message["status"]
        for message in messages
        if message["type"] == "http.response.start"
    )
    body = b"".join(
        message.get("body", b"")
        for message in messages
        if message["type"] == "http.response.body"
    )
    return status, json.loads(body) if body else {}


@pytest.fixture
def _pristine_notification_state() -> Iterator[None]:
    """Detach any notification manager from the app state, restoring it after.

    The endpoint lazily attaches a manager built from configuration; tests
    must not leak that (or an injected fake) into other tests via the shared
    production app object.
    """
    previous = getattr(app.state, "notification_manager", None)
    if previous is not None:
        del app.state.notification_manager
    try:
        yield
    finally:
        if hasattr(app.state, "notification_manager"):
            del app.state.notification_manager
        if previous is not None:
            app.state.notification_manager = previous


# --- route registration on the production app -------------------------------


def test_production_app_registers_notification_status_route() -> None:
    """The production app's route table must contain GET /notifications/status.

    This is the regression test for the validation finding: it fails if the
    route is ever detached from the application object production serves.
    """
    matching = [
        route
        for route in app.routes
        if getattr(route, "path", None) == "/notifications/status"
    ]

    assert len(matching) == 1
    assert "GET" in matching[0].methods  # type: ignore[attr-defined]


def test_openapi_schema_includes_notification_status() -> None:
    """The generated OpenAPI document must expose the route and its fields."""
    schema = app.openapi()

    path = schema["paths"]["/notifications/status"]
    assert set(path.keys()) == {"get"}

    reference = path["get"]["responses"]["200"]["content"]["application/json"][
        "schema"
    ]["$ref"]
    model = schema["components"]["schemas"][reference.split("/")[-1]]
    assert set(model["properties"].keys()) == _EXPECTED_FIELDS


# --- real HTTP dispatch through the production app --------------------------


@pytest.mark.usefixtures("_pristine_notification_state")
def test_get_notifications_status_returns_200_with_all_fields() -> None:
    """A real GET through the production app returns 200 and every field."""
    status, body = _asgi_get("/notifications/status")

    assert status == 200
    assert set(body.keys()) == _EXPECTED_FIELDS


@pytest.mark.usefixtures("_pristine_notification_state")
def test_endpoint_available_when_notifications_disabled() -> None:
    """With the default (disabled) configuration the endpoint still serves.

    The repository default config disables notifications; the endpoint must
    return 200 with truthful disabled values, never 404 or an error.
    """
    status, body = _asgi_get("/notifications/status")

    assert status == 200
    assert body["enabled"] is False
    assert body["providers"] == []
    assert body["total_events_published"] == 0
    assert body["total_delivery_failures"] == 0
    assert body["last_event_at"] is None


@pytest.mark.usefixtures("_pristine_notification_state")
def test_querying_status_has_no_side_effects() -> None:
    """Requesting the status never publishes, delivers or counts anything."""
    manager = NotificationManager()
    manager.register_provider(NullProvider())
    app.state.notification_manager = manager

    first_status, first_body = _asgi_get("/notifications/status")
    second_status, second_body = _asgi_get("/notifications/status")

    assert first_status == 200
    assert second_status == 200
    assert first_body == second_body
    live = manager.status()
    assert live.total_events_published == 0
    assert live.total_delivery_failures == 0
    assert live.last_event_at is None
