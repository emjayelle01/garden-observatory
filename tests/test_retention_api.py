"""Tests for ``GET /retention/status``.

The endpoint's whole value is that it is inert. It reports what retention has
done without doing any of it: no planner, no captures query, no ``stat``, no
deletion, no lifecycle row, no observation, no camera and no migration. These
tests assert that by attaching doubles that fail loudly if the endpoint touches
them, rather than by reading the implementation and believing it.

The route is also exercised through the real production ASGI application, not
only by calling the handler function, because a handler that works while the
route is unreachable is the exact failure this repository has already had once.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

import mgo.api.app as app_module
from mgo.api.app import app, retention_status
from mgo.retention import RetentionRuntimeState, RetentionState

_REQUIRED_FIELDS = {
    "enabled",
    "state",
    "total_runs",
    "total_captures_deleted",
    "total_bytes_reclaimed",
    "last_run_at",
    "last_run_candidate_count",
    "last_run_deleted_count",
    "last_run_bytes_reclaimed",
    "last_error",
}

NOW = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)


class _Exploding:
    """Any attribute access on this is a test failure."""

    def __init__(self, label: str) -> None:
        self._label = label

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(
            f"the retention status endpoint touched {self._label}.{name}"
        )


def _request(state: RetentionRuntimeState | None) -> SimpleNamespace:
    """Build a fake request whose app.state trips on any other subsystem."""
    app_state = SimpleNamespace(
        camera_coordinator=_Exploding("camera_coordinator"),
        capture_service=_Exploding("capture_service"),
        preview_service=_Exploding("preview_service"),
        capture_archive=_Exploding("capture_archive"),
        capture_workflow=_Exploding("capture_workflow"),
        event_capture_service=_Exploding("event_capture_service"),
        retention_service=_Exploding("retention_service"),
    )
    if state is not None:
        app_state.retention_state = state
    return SimpleNamespace(app=SimpleNamespace(state=app_state))


def _asgi_get(path: str) -> tuple[int, dict[str, Any]]:
    """Perform one in-process HTTP GET against the production ASGI app."""
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
def _pristine_retention_state() -> Any:
    """Detach any retention state from the shared production app object."""
    previous = getattr(app.state, "retention_state", None)
    if previous is not None:
        del app.state.retention_state
    try:
        yield
    finally:
        if hasattr(app.state, "retention_state"):
            del app.state.retention_state
        if previous is not None:
            app.state.retention_state = previous


# --- route registration -----------------------------------------------------


def test_the_production_app_registers_the_retention_status_route() -> None:
    """The route exists on the exact application object production serves."""
    matching = [
        route
        for route in app.routes
        if getattr(route, "path", None) == "/retention/status"
    ]

    assert len(matching) == 1
    assert "GET" in matching[0].methods  # type: ignore[attr-defined]


def test_the_openapi_document_exposes_only_a_get(tmp_path: Any) -> None:
    """There is no destructive retention endpoint of any kind.

    Task 14.1 deliberately ships no ``POST /retention/run``: unauthenticated
    destructive media deletion over HTTP is a decision a later task gets to
    make, and shipping the endpoint first would pre-empt it.
    """
    schema = app.openapi()

    assert set(schema["paths"]["/retention/status"].keys()) == {"get"}
    retention_paths = [
        path for path in schema["paths"] if path.startswith("/retention")
    ]
    assert retention_paths == ["/retention/status"]


def test_no_route_on_the_application_can_run_retention() -> None:
    """No path anywhere in the app exposes a retention mutation."""
    destructive = [
        route
        for route in app.routes
        if str(getattr(route, "path", "")).startswith("/retention")
        and getattr(route, "methods", set()) - {"GET", "HEAD", "OPTIONS"}
    ]

    assert destructive == []


def test_the_response_model_declares_exactly_the_status_fields() -> None:
    """The published contract is the bounded status model and nothing more."""
    schema = app.openapi()

    reference = schema["paths"]["/retention/status"]["get"]["responses"]["200"][
        "content"
    ]["application/json"]["schema"]["$ref"]
    model = schema["components"]["schemas"][reference.split("/")[-1]]

    assert set(model["properties"].keys()) == _REQUIRED_FIELDS


# --- what the endpoint reports ----------------------------------------------


def test_a_disabled_deployment_reports_disabled_with_zero_counters() -> None:
    """Retention off is HTTP 200 and a truthful ``disabled``, not an error."""
    response = retention_status(_request(RetentionRuntimeState(enabled=False)))

    payload = response.model_dump()
    assert payload["enabled"] is False
    assert payload["state"] == RetentionState.DISABLED.value
    assert payload["total_runs"] == 0
    assert payload["total_captures_deleted"] == 0
    assert payload["total_bytes_reclaimed"] == 0
    assert payload["last_run_at"] is None
    assert payload["last_error"] is None


def test_an_enabled_deployment_that_has_not_run_reports_idle() -> None:
    """Enabled and untouched is ``idle`` -- nothing schedules a first run."""
    response = retention_status(_request(RetentionRuntimeState(enabled=True)))

    assert response.state == RetentionState.IDLE.value
    assert response.enabled is True
    assert response.total_runs == 0


def test_a_run_in_progress_is_reported_as_running() -> None:
    """The snapshot taken mid-run says so."""
    state = RetentionRuntimeState(enabled=True)
    state.mark_running()

    assert retention_status(_request(state)).state == RetentionState.RUNNING.value


def test_a_failed_run_is_reported_as_error_with_http_200() -> None:
    """A failure is reported in the body, never as an HTTP error."""
    state = RetentionRuntimeState(enabled=True)
    state.record_run(
        completed_at=NOW,
        candidate_count=3,
        deleted_count=1,
        bytes_reclaimed=1024,
        error="A capture's media could not be removed.",
    )

    payload = retention_status(_request(state)).model_dump()

    assert payload["state"] == RetentionState.ERROR.value
    assert payload["last_error"] == "A capture's media could not be removed."
    assert payload["total_captures_deleted"] == 1


def test_counters_reflect_completed_runs() -> None:
    """Lifetime counters accumulate; the last-run fields describe only the last."""
    state = RetentionRuntimeState(enabled=True)
    state.record_run(
        completed_at=NOW,
        candidate_count=5,
        deleted_count=5,
        bytes_reclaimed=500,
        error=None,
    )
    state.record_run(
        completed_at=NOW,
        candidate_count=2,
        deleted_count=2,
        bytes_reclaimed=200,
        error=None,
    )

    payload = retention_status(_request(state)).model_dump()

    assert payload["total_runs"] == 2
    assert payload["total_captures_deleted"] == 7
    assert payload["total_bytes_reclaimed"] == 700
    assert payload["last_run_candidate_count"] == 2
    assert payload["last_run_deleted_count"] == 2
    assert payload["last_run_bytes_reclaimed"] == 200
    assert payload["last_run_at"] == NOW.isoformat()


def test_a_successful_run_clears_a_previous_error() -> None:
    """The state is the *current* truth, not a permanent record of a bad night."""
    state = RetentionRuntimeState(enabled=True)
    state.record_run(
        completed_at=NOW,
        candidate_count=1,
        deleted_count=0,
        bytes_reclaimed=0,
        error="A capture's media could not be removed.",
    )
    state.record_run(
        completed_at=NOW,
        candidate_count=1,
        deleted_count=1,
        bytes_reclaimed=10,
        error=None,
    )

    payload = retention_status(_request(state)).model_dump()

    assert payload["state"] == RetentionState.IDLE.value
    assert payload["last_error"] is None


# --- the endpoint is inert --------------------------------------------------


def test_the_endpoint_touches_no_other_subsystem() -> None:
    """Every neighbouring subsystem is a double that fails on any access."""
    response = retention_status(_request(RetentionRuntimeState(enabled=True)))

    assert response.state == RetentionState.IDLE.value


def test_the_endpoint_moves_no_counter() -> None:
    """A thousand polls leave the numbers exactly where they were."""
    state = RetentionRuntimeState(enabled=True)
    state.record_run(
        completed_at=NOW,
        candidate_count=4,
        deleted_count=4,
        bytes_reclaimed=44,
        error=None,
    )
    before = state.snapshot()

    for _ in range(50):
        retention_status(_request(state))

    assert state.snapshot() == before


def test_the_endpoint_opens_no_database_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No SQLite connection is opened, so no query and no migration can run."""
    import sqlite3

    def _explode(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the retention status endpoint opened SQLite")

    monkeypatch.setattr(sqlite3, "connect", _explode)

    response = retention_status(_request(RetentionRuntimeState(enabled=True)))

    assert response.enabled is True


def test_the_endpoint_stats_no_file(monkeypatch: pytest.MonkeyPatch) -> None:
    """No media path is examined, so nothing on disk is even looked at."""
    import mgo.retention.service as service_module

    for name in ("_path_exists", "_is_directory", "_is_regular_file", "_file_size"):

        def _explode(*args: Any, _name: str = name, **kwargs: Any) -> Any:
            raise AssertionError(
                f"the retention status endpoint called {_name}"
            )

        monkeypatch.setattr(service_module, name, _explode)

    response = retention_status(_request(RetentionRuntimeState(enabled=True)))

    assert response.enabled is True


def test_the_endpoint_never_invokes_the_planner_or_a_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Neither the policy planner nor the destructive run is reachable here."""
    import mgo.retention.policy as policy_module
    import mgo.retention.service as service_module

    def _explode(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the retention status endpoint ran retention")

    monkeypatch.setattr(policy_module, "plan_retention", _explode)
    monkeypatch.setattr(service_module.RetentionService, "run_once", _explode)
    monkeypatch.setattr(service_module.RetentionService, "dry_run", _explode)

    response = retention_status(_request(RetentionRuntimeState(enabled=True)))

    assert response.state == RetentionState.IDLE.value


def test_the_endpoint_deletes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """The unlink seam is replaced with a failure; the endpoint never reaches it."""
    import mgo.retention.service as service_module

    def _explode(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the retention status endpoint unlinked a file")

    monkeypatch.setattr(service_module, "_unlink", _explode)

    assert retention_status(_request(RetentionRuntimeState(enabled=True))).enabled


# --- through the real application -------------------------------------------


@pytest.mark.usefixtures("_pristine_retention_state")
def test_a_real_http_request_returns_200_and_every_field() -> None:
    """A real GET through the production app returns 200 and the full model.

    The lazily built fallback mirrors the repository's tracked configuration,
    which keeps retention disabled -- so this also proves the shipped default is
    off.
    """
    status, payload = _asgi_get("/retention/status")

    assert status == 200
    assert set(payload) == _REQUIRED_FIELDS
    assert payload["enabled"] is False
    assert payload["state"] == RetentionState.DISABLED.value


@pytest.mark.usefixtures("_pristine_retention_state")
def test_repeated_real_requests_are_stable() -> None:
    """Polling the live route changes nothing about what it reports."""
    first = _asgi_get("/retention/status")
    second = _asgi_get("/retention/status")

    assert first == second


@pytest.mark.usefixtures("_pristine_retention_state")
def test_the_lazily_built_fallback_matches_the_loaded_configuration() -> None:
    """The endpoint is truthful before the lifespan has attached anything."""
    _asgi_get("/retention/status")

    state = app.state.retention_state
    assert state.enabled is app_module.config.retention.enabled


@pytest.mark.usefixtures("_pristine_retention_state")
def test_no_response_field_can_carry_a_path_or_an_exception() -> None:
    """The published body is counters, a state word and a fixed message."""
    _, payload = _asgi_get("/retention/status")

    # Field *names* legitimately mention captures; it is the values that must
    # never name a location, so only those are inspected.
    rendered = json.dumps(
        [value for value in payload.values() if isinstance(value, str)]
    )
    assert "captures" not in rendered
    assert "mgo.db" not in rendered
    assert "Traceback" not in rendered
    assert "/" not in rendered
    assert "\\\\" not in rendered
