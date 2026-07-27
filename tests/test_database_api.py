"""Lifecycle and API tests for the Task 7 database foundation.

These drive the *production* application object (``mgo.api.app:app`` -- the
exact ASGI app uvicorn serves) so route registration and startup ordering are
proven where they actually matter, following the pattern established in
``tests/test_app_routes.py``.

The module-level configuration is redirected at a temporary database for every
test that starts the lifespan, so nothing here touches the tracked development
database, the live production path, or any Raspberry Pi hardware: the camera is
disabled by the default configuration, preview and motion are off, and no
subprocess is ever launched.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from mgo.api.app import app, lifespan
from mgo.core.config import PROJECT_ROOT, MGOConfig, StorageConfig, load_config
from mgo.core.database import CURRENT_SCHEMA_VERSION, DatabaseError
from mgo.core.database_health import (
    DatabaseHealth,
    DatabaseHealthState,
    DatabaseStatus,
    MigrationStatus,
)
from mgo.core.observations import list_observations

_NOW = datetime(2026, 7, 27, 10, 0, tzinfo=UTC)

#: Every state attribute the lifespan attaches, so a test can leave the shared
#: production app object exactly as it found it.
_STATE_ATTRIBUTES = (
    "database_state",
    "capture_archive",
    "notification_manager",
    "camera_state",
    "capture_service",
    "preview_service",
    "preview_broker",
    "motion_state",
)

_DATABASE_STATUS_FIELDS = {
    "status",
    "accessible",
    "database",
    "schema_version",
    "expected_schema_version",
    "migration_status",
    "journal_mode",
    "foreign_keys",
    "integrity",
    "detail",
    "checked_at",
}


def _asgi_get(path: str) -> tuple[int, Any]:
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
def pristine_app_state() -> Iterator[None]:
    """Snapshot and restore every lifespan-managed attribute on ``app.state``.

    The production app object is module-level and shared across the whole test
    session; without this, a lifespan run here would leak temporary services
    into unrelated test modules.
    """
    saved = {
        name: getattr(app.state, name)
        for name in _STATE_ATTRIBUTES
        if hasattr(app.state, name)
    }
    for name in saved:
        delattr(app.state, name)
    try:
        yield
    finally:
        for name in _STATE_ATTRIBUTES:
            if hasattr(app.state, name):
                delattr(app.state, name)
        for name, value in saved.items():
            setattr(app.state, name, value)


@pytest.fixture
def isolated_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> MGOConfig:
    """Point the production app's configuration at a temporary database."""
    base = load_config()
    isolated = replace(
        base,
        storage=StorageConfig(
            data_directory=tmp_path,
            log_directory=tmp_path / "logs",
            database_path=tmp_path / "db" / "mgo.db",
        ),
        # Keep the background health monitor quiet: it records one snapshot and
        # then waits, and the wait is cancelled at shutdown.
        health=replace(base.health, enabled=False),
    )
    monkeypatch.setattr("mgo.api.app.config", isolated)
    return isolated


def _health(status: DatabaseStatus) -> DatabaseHealth:
    """Build a database-health result with the given status."""
    return DatabaseHealth(
        status=status,
        accessible=status is not DatabaseStatus.UNHEALTHY,
        database_name="mgo.db",
        schema_version=CURRENT_SCHEMA_VERSION,
        expected_schema_version=CURRENT_SCHEMA_VERSION,
        migration_status=MigrationStatus.CURRENT,
        journal_mode="wal",
        foreign_keys_enabled=True,
        integrity="ok",
        detail=f"Database is {status.value}.",
        checked_at=_NOW,
    )


# --- route registration on the production app -------------------------------


def test_production_app_registers_the_database_status_route() -> None:
    """The production app's route table contains GET /database/status."""
    matching = [
        route
        for route in app.routes
        if getattr(route, "path", None) == "/database/status"
    ]

    assert len(matching) == 1
    assert "GET" in matching[0].methods  # type: ignore[attr-defined]


def test_existing_routes_remain_registered() -> None:
    """Adding the database endpoint detaches nothing that existed before."""
    paths = {getattr(route, "path", None) for route in app.routes}

    assert {
        "/",
        "/health",
        "/camera/status",
        "/camera/capture",
        "/camera/preview/status",
        "/camera/preview/start",
        "/camera/preview/stop",
        "/camera/preview/stream",
        "/preview",
        "/motion/status",
        "/notifications/status",
        "/captures",
        "/captures/{capture_id}",
        "/observations",
        "/database/status",
    } <= paths


def test_openapi_schema_exposes_the_database_status_model() -> None:
    """The generated OpenAPI document exposes the route and its fields."""
    schema = app.openapi()

    path = schema["paths"]["/database/status"]
    assert set(path.keys()) == {"get"}

    reference = path["get"]["responses"]["200"]["content"]["application/json"][
        "schema"
    ]["$ref"]
    model = schema["components"]["schemas"][reference.split("/")[-1]]
    assert set(model["properties"].keys()) == _DATABASE_STATUS_FIELDS


# --- lifecycle ordering -----------------------------------------------------


@pytest.mark.usefixtures("pristine_app_state")
def test_migration_completes_before_dependent_components_start(
    isolated_config: MGOConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Migration runs first, then the health check, then everything else."""
    from mgo.api import app as app_module

    order: list[str] = []
    real_migrations = app_module.apply_migrations
    real_check = app_module.perform_database_check

    def _tracked_migrations(*args: Any, **kwargs: Any) -> list[int]:
        order.append("migrate")
        return real_migrations(*args, **kwargs)

    def _tracked_check(*args: Any, **kwargs: Any) -> DatabaseHealth:
        order.append("database-check")
        return real_check(*args, **kwargs)

    monkeypatch.setattr(app_module, "apply_migrations", _tracked_migrations)
    monkeypatch.setattr(app_module, "perform_database_check", _tracked_check)

    async def _run() -> None:
        async with lifespan(app):
            order.append("serving")
            # Every dependent component exists, and the database state is real.
            assert app.state.capture_archive is not None
            assert app.state.camera_state is not None
            state: DatabaseHealthState = app.state.database_state
            health = state.get()
            assert health is not None
            assert health.status is DatabaseStatus.HEALTHY
            assert health.schema_version == CURRENT_SCHEMA_VERSION

    asyncio.run(_run())

    assert order == ["migrate", "database-check", "serving"]
    assert isolated_config.storage.database_path.exists()


@pytest.mark.usefixtures("pristine_app_state")
def test_migration_failure_prevents_application_startup(
    isolated_config: MGOConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed migration aborts startup rather than serving unsafely."""
    from mgo.api import app as app_module

    def _explode(*args: Any, **kwargs: Any) -> list[int]:
        raise DatabaseError("Failed to apply migration 002_capture_archive.sql")

    monkeypatch.setattr(app_module, "apply_migrations", _explode)
    served = False

    async def _run() -> None:
        nonlocal served
        async with lifespan(app):
            served = True

    with pytest.raises(DatabaseError):
        asyncio.run(_run())

    assert served is False
    # Nothing downstream of the migration was ever constructed.
    assert not hasattr(app.state, "camera_state")
    assert not hasattr(app.state, "database_state")


@pytest.mark.usefixtures("pristine_app_state")
def test_database_monitor_starts_once_and_stops_cleanly(
    isolated_config: MGOConfig,
) -> None:
    """Exactly one database monitor runs, and it is gone after shutdown."""
    captured: list[asyncio.Task[Any]] = []

    async def _run() -> None:
        async with lifespan(app):
            captured.extend(
                task
                for task in asyncio.all_tasks()
                if task.get_name() == "mgo-database-monitor"
            )
            assert len(captured) == 1
            assert not captured[0].done()

    asyncio.run(_run())

    assert len(captured) == 1
    assert captured[0].done()
    assert captured[0].exception() is None
    # No monitor task survives the event loop.
    assert captured[0].cancelled() is False


@pytest.mark.usefixtures("pristine_app_state")
def test_startup_records_the_database_health_observation(
    isolated_config: MGOConfig,
) -> None:
    """The initial healthy check lands in the timeline exactly once."""

    async def _run() -> None:
        async with lifespan(app):
            pass

    asyncio.run(_run())

    observations = list_observations(
        isolated_config.storage.database_path, kind="database_health"
    )
    assert len(observations) == 1
    assert observations[0].status == DatabaseStatus.HEALTHY.value


@pytest.mark.usefixtures("pristine_app_state")
def test_camera_disabled_operation_is_independent_of_database_health(
    isolated_config: MGOConfig,
) -> None:
    """A disabled camera and a healthy database are reported independently."""
    assert isolated_config.camera.enabled is False

    async def _run() -> None:
        async with lifespan(app):
            readiness = app.state.camera_state.get()
            database = app.state.database_state.get()
            assert readiness.status.value == "disabled"
            assert database.status is DatabaseStatus.HEALTHY

    asyncio.run(_run())


# --- /health and /database/status -------------------------------------------


@pytest.mark.usefixtures("pristine_app_state")
def test_health_includes_the_database_component() -> None:
    """GET /health reports a database section alongside the existing ones."""
    state = DatabaseHealthState()
    state.set(_health(DatabaseStatus.HEALTHY))
    app.state.database_state = state

    status, body = _asgi_get("/health")

    assert status == 200
    assert body["database"] == {
        "status": "healthy",
        "accessible": True,
        "schema_version": CURRENT_SCHEMA_VERSION,
        "expected_schema_version": CURRENT_SCHEMA_VERSION,
        "migration_status": "current",
        "integrity": "ok",
    }
    # Every pre-existing section is still present and unchanged in meaning.
    assert {"status", "camera", "preview", "memory", "disk", "temperature"} <= set(
        body
    )


@pytest.mark.usefixtures("pristine_app_state")
def test_unhealthy_database_degrades_the_top_level_status() -> None:
    """An unusable database makes overall health critical, not merely noted."""
    state = DatabaseHealthState()
    state.set(_health(DatabaseStatus.UNHEALTHY))
    app.state.database_state = state

    status, body = _asgi_get("/health")

    assert status == 200
    assert body["status"] == "critical"
    assert body["database"]["status"] == "unhealthy"
    # The camera is reported on its own terms; a database fault is never
    # mislabelled as a camera failure.
    assert body["camera"]["status"] == "disabled"


@pytest.mark.usefixtures("pristine_app_state")
def test_degraded_database_warns_without_claiming_an_outage() -> None:
    """A degraded database contributes a warning, not a critical status."""
    state = DatabaseHealthState()
    state.set(_health(DatabaseStatus.DEGRADED))
    app.state.database_state = state

    status, body = _asgi_get("/health")

    assert status == 200
    assert body["status"] in {"warning", "critical"}
    assert body["database"]["status"] == "degraded"


@pytest.mark.usefixtures("pristine_app_state")
def test_database_status_endpoint_returns_the_cached_state() -> None:
    """GET /database/status serves the monitored result with every field."""
    state = DatabaseHealthState()
    state.set(_health(DatabaseStatus.HEALTHY))
    app.state.database_state = state

    status, body = _asgi_get("/database/status")

    assert status == 200
    assert set(body) == _DATABASE_STATUS_FIELDS
    assert body["status"] == "healthy"
    assert body["database"] == "mgo.db"
    assert body["journal_mode"] == "wal"
    assert body["foreign_keys"] is True
    assert body["checked_at"] == _NOW.isoformat()


@pytest.mark.usefixtures("pristine_app_state")
def test_database_status_reports_unhealthy_with_http_200() -> None:
    """An unhealthy database is a status value, never an HTTP error."""
    state = DatabaseHealthState()
    state.set(_health(DatabaseStatus.UNHEALTHY))
    app.state.database_state = state

    status, body = _asgi_get("/database/status")

    assert status == 200
    assert body["status"] == "unhealthy"
    assert body["accessible"] is False


@pytest.mark.usefixtures("pristine_app_state")
def test_status_endpoint_before_any_check_is_safe() -> None:
    """With no state attached the endpoint reports the safe default."""
    status, body = _asgi_get("/database/status")

    assert status == 200
    assert body["status"] == "unhealthy"
    assert body["schema_version"] is None
    assert "not been evaluated" in body["detail"]


@pytest.mark.usefixtures("pristine_app_state")
def test_querying_status_performs_no_database_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The endpoint reads cached state; it never opens a connection."""
    state = DatabaseHealthState()
    state.set(_health(DatabaseStatus.HEALTHY))
    app.state.database_state = state

    def _forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("the status endpoint must not open the database")

    monkeypatch.setattr("mgo.core.database_health.connect_readonly", _forbidden)
    monkeypatch.setattr("mgo.core.database.connect_readonly", _forbidden)

    first = _asgi_get("/database/status")
    second = _asgi_get("/database/status")

    assert first == second
    assert first[0] == 200


# --- import hygiene ---------------------------------------------------------


def test_database_modules_import_without_side_effects(tmp_path: Path) -> None:
    """Importing every database module touches no filesystem and no hardware.

    Runs in a fresh interpreter so the import genuinely happens; an in-process
    reload would rebind the modules' enum and exception classes and corrupt
    identity checks elsewhere in the suite.
    """
    code = (
        "import mgo.core.database\n"
        "import mgo.core.database_health\n"
        "import mgo.core.database_monitor\n"
        "print(mgo.core.database.CURRENT_SCHEMA_VERSION)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "src")},
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(CURRENT_SCHEMA_VERSION)
    assert list(tmp_path.iterdir()) == []
