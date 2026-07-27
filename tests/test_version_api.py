"""Contract tests for ``GET /version``, ``GET /`` and ``GET /health``.

Task 8 added ``/version`` and *formalised* the pre-existing ``/`` and
``/health`` contracts; it did not create ``/health``. These tests therefore
serve two purposes: they pin the new endpoint's schema, and they lock the
existing endpoints' fields so a later change cannot quietly break a client.

Requests are driven through the **production** ASGI object (``mgo.api.app:app``
-- the exact app uvicorn serves) following the pattern established in
``tests/test_app_routes.py``, so route registration and response-model
serialisation are proven where they actually matter. Requests are in-process
and lifespan-free: no background monitor runs, no database is opened, no
hardware is touched and no subprocess is launched.
"""

from __future__ import annotations

import asyncio
import json
import platform
from collections.abc import Iterator
from typing import Any

import pytest

from mgo.api.app import APPLICATION_VERSION, app, config
from mgo.core.identity import (
    BUILD_COMMIT_ENV,
    UNKNOWN_VERSION,
    get_application_version,
    get_build_commit,
)

_VERSION_FIELDS = {
    "application",
    "version",
    "commit",
    "python_version",
    "architecture",
}

#: Every top-level field ``/health`` guaranteed before Task 8. None may be
#: removed, renamed or change meaning.
_HEALTH_FIELDS = {
    "status",
    "application",
    "hostname",
    "architecture",
    "python_version",
    "uptime_seconds",
    "cpu_percent",
    "memory",
    "disk",
    "temperature",
    "database",
    "camera",
    "preview",
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


@pytest.fixture(autouse=True)
def _clear_identity_caches() -> Iterator[None]:
    """Keep a test's fake metadata or environment out of the shared cache."""
    get_application_version.cache_clear()
    get_build_commit.cache_clear()
    yield
    get_application_version.cache_clear()
    get_build_commit.cache_clear()


# --- route registration on the production app -------------------------------


def test_production_app_registers_the_version_route() -> None:
    """The production app's route table contains GET /version.

    The regression guard for the ``test_app_routes`` validation finding: a
    handler can pass its own tests while the production service returns 404.
    """
    matching = [
        route for route in app.routes if getattr(route, "path", None) == "/version"
    ]

    assert len(matching) == 1
    assert "GET" in matching[0].methods  # type: ignore[attr-defined]


def test_openapi_schema_pins_the_version_contract() -> None:
    """The generated OpenAPI document exposes the route and its exact fields."""
    schema = app.openapi()

    path = schema["paths"]["/version"]
    assert set(path.keys()) == {"get"}

    reference = path["get"]["responses"]["200"]["content"]["application/json"][
        "schema"
    ]["$ref"]
    model = schema["components"]["schemas"][reference.split("/")[-1]]
    assert set(model["properties"].keys()) == _VERSION_FIELDS


def test_openapi_document_version_comes_from_package_metadata() -> None:
    """The OpenAPI ``info.version`` is the resolved release, not a literal."""
    schema = app.openapi()

    assert schema["info"]["version"] == APPLICATION_VERSION
    assert get_application_version() == APPLICATION_VERSION


# --- GET /version -----------------------------------------------------------


def test_version_returns_200_with_the_exact_schema() -> None:
    """A real GET returns 200 and precisely the documented field set."""
    status, body = _asgi_get("/version")

    assert status == 200
    assert set(body) == _VERSION_FIELDS


def test_version_reports_the_configured_application_identity() -> None:
    """``application`` is the configured name, shared with ``/`` and /health."""
    _, body = _asgi_get("/version")

    assert body["application"] == config.application.name


def test_version_reports_the_package_version() -> None:
    """``version`` is the resolved package version, not a hard-coded literal."""
    _, body = _asgi_get("/version")

    assert body["version"] == get_application_version()
    assert body["version"] != UNKNOWN_VERSION


def test_version_reports_truthful_platform_facts() -> None:
    """The platform fields describe the running interpreter and machine."""
    _, body = _asgi_get("/version")

    assert body["python_version"] == platform.python_version()
    assert body["architecture"] == platform.machine()


def test_commit_is_null_when_the_deployment_supplies_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An absent optional build identifier is ``null``, never an error."""
    monkeypatch.delenv(BUILD_COMMIT_ENV, raising=False)

    status, body = _asgi_get("/version")

    assert status == 200
    assert body["commit"] is None


def test_commit_is_reported_when_the_deployment_supplies_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A valid build commit reaches the response, normalised."""
    monkeypatch.setenv(BUILD_COMMIT_ENV, "  D381B6D  ")

    status, body = _asgi_get("/version")

    assert status == 200
    assert body["commit"] == "d381b6d"


def test_malformed_commit_is_not_echoed_to_the_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Arbitrary environment text never reaches the response."""
    monkeypatch.setenv(BUILD_COMMIT_ENV, "/etc/garden-observatory/mgo.toml")

    status, body = _asgi_get("/version")

    assert status == 200
    assert body["commit"] is None
    assert "garden-observatory/mgo.toml" not in json.dumps(body)


def test_version_serves_unknown_when_metadata_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unreadable package metadata degrades the field, not the endpoint.

    The endpoint keeps returning 200 with a truthful ``unknown`` rather than
    failing or inventing a version.
    """

    def _explode(name: str) -> str:
        raise OSError("dist-info is unreadable")

    monkeypatch.setattr("mgo.core.identity.distribution_version", _explode)

    status, body = _asgi_get("/version")

    assert status == 200
    assert body["version"] == UNKNOWN_VERSION
    assert set(body) == _VERSION_FIELDS


def test_version_is_deterministic_across_requests() -> None:
    """Repeated requests return byte-identical bodies."""
    first = _asgi_get("/version")
    second = _asgi_get("/version")
    third = _asgi_get("/version")

    assert first == second == third
    assert first[0] == 200


# --- GET /version independence ----------------------------------------------


def test_version_is_database_independent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Serving /version opens no database connection and runs no migration."""

    def _forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("/version must not touch the database")

    monkeypatch.setattr("mgo.core.database.connect_readonly", _forbidden)
    monkeypatch.setattr("mgo.core.database_health.connect_readonly", _forbidden)
    monkeypatch.setattr("mgo.core.database.database_connection", _forbidden)
    monkeypatch.setattr("mgo.api.app.apply_migrations", _forbidden)
    monkeypatch.setattr("mgo.core.database.apply_migrations", _forbidden)

    status, body = _asgi_get("/version")

    assert status == 200
    assert set(body) == _VERSION_FIELDS


def test_version_is_camera_independent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Serving /version runs no hardware detection and no capture."""

    def _forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("/version must not touch the camera")

    monkeypatch.setattr("mgo.core.camera_detection.build_detector", _forbidden)
    monkeypatch.setattr("mgo.camera.build_capture_backend", _forbidden)
    monkeypatch.setattr("mgo.camera.build_preview_backend", _forbidden)

    status, body = _asgi_get("/version")

    assert status == 200
    assert set(body) == _VERSION_FIELDS


def test_version_invokes_no_subprocess(monkeypatch: pytest.MonkeyPatch) -> None:
    """No request ever shells out -- to Git, ``vcgencmd`` or anything else.

    Git is not required at runtime and is never invoked, so ``/version`` is
    equally truthful on a machine with no Git and a checkout with no ``.git``.
    """

    def _forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("/version must not run a subprocess")

    monkeypatch.setattr("subprocess.run", _forbidden)
    monkeypatch.setattr("subprocess.Popen", _forbidden)
    monkeypatch.setattr("subprocess.check_output", _forbidden)

    status, body = _asgi_get("/version")

    assert status == 200
    assert set(body) == _VERSION_FIELDS


def test_version_does_not_depend_on_the_health_monitors() -> None:
    """With no monitor state attached at all, /version still answers fully.

    Nothing here is lifespan-managed, so the endpoint is usable from the first
    instant the process serves.
    """
    for attribute in ("database_state", "camera_state", "motion_state"):
        assert not hasattr(app.state, attribute)

    status, body = _asgi_get("/version")

    assert status == 200
    assert body["version"] == get_application_version()


# --- GET /version information disclosure ------------------------------------


def test_version_discloses_no_paths_secrets_or_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The response carries no filesystem, configuration or environment data."""
    monkeypatch.setenv("MGO_SECRET_TOKEN", "super-secret-value")

    _, body = _asgi_get("/version")
    serialised = json.dumps(body)

    assert "super-secret-value" not in serialised
    assert str(config.storage.database_path) not in serialised
    assert str(config.storage.data_directory) not in serialised
    assert str(config.camera.capture_directory) not in serialised
    for fragment in ("/etc/", "/var/", "C:\\", "\\\\", ".git", "github.com", "@"):
        assert fragment not in serialised


def test_version_does_not_expose_the_hostname() -> None:
    """Unlike /health, /version says nothing about *which machine* this is.

    /health deliberately reports a hostname; /version answers "what build",
    so it deliberately does not widen disclosure beyond what it needs.
    """
    _, body = _asgi_get("/version")

    assert "hostname" not in body


# --- GET / ------------------------------------------------------------------


def test_root_contract_is_unchanged() -> None:
    """``/`` keeps exactly the three keys and values it always returned."""
    status, body = _asgi_get("/")

    assert status == 200
    assert set(body) == {"name", "version", "status"}
    assert body["name"] == config.application.name
    assert body["status"] == "operational"


def test_root_version_matches_the_version_endpoint() -> None:
    """``/`` and ``/version`` can never disagree about the release.

    Both read the one central authority, which is the whole point of removing
    the duplicated literals.
    """
    _, root_body = _asgi_get("/")
    _, version_body = _asgi_get("/version")

    assert root_body["version"] == version_body["version"]
    assert root_body["name"] == version_body["application"]


def test_root_version_comes_from_package_metadata() -> None:
    """The value ``/`` reports is resolved, not hard-coded."""
    _, body = _asgi_get("/")

    assert body["version"] == get_application_version()


# --- GET /health formalisation ----------------------------------------------


def test_health_returns_200_with_every_required_field() -> None:
    """The pre-existing /health contract is preserved in full.

    This is the compatibility lock: adding /version must not have removed,
    renamed or restructured anything /health already guaranteed.
    """
    status, body = _asgi_get("/health")

    assert status == 200
    assert set(body) >= _HEALTH_FIELDS


def test_health_preserves_field_types_and_units() -> None:
    """Existing values keep their current types and units."""
    _, body = _asgi_get("/health")

    assert body["status"] in {"unknown", "healthy", "warning", "critical"}
    assert isinstance(body["application"], str)
    assert isinstance(body["hostname"], str)
    assert isinstance(body["architecture"], str)
    assert isinstance(body["python_version"], str)
    # Seconds since boot: a whole number, never negative.
    assert isinstance(body["uptime_seconds"], int)
    assert body["uptime_seconds"] >= 0
    # Percentages stay percentages; byte counts stay bytes.
    assert 0 <= body["cpu_percent"] <= 100
    assert set(body["memory"]) == {
        "total_bytes",
        "available_bytes",
        "used_percent",
        "status",
    }
    assert set(body["disk"]) == {
        "total_bytes",
        "free_bytes",
        "used_percent",
        "status",
    }
    assert body["memory"]["total_bytes"] > 0
    assert body["disk"]["total_bytes"] > 0
    assert set(body["temperature"]) == {"celsius", "status"}


def test_health_keeps_the_database_section_integrated() -> None:
    """The database component remains part of /health, unchanged in shape."""
    _, body = _asgi_get("/health")

    assert set(body["database"]) == {
        "status",
        "accessible",
        "schema_version",
        "expected_schema_version",
        "migration_status",
        "integrity",
    }


def test_health_keeps_camera_and_preview_independent() -> None:
    """Camera and preview remain separate, independently reported sections."""
    _, body = _asgi_get("/health")

    assert body["camera"]["status"] == "disabled"
    # Preview reports a lifecycle ``state``, not a health ``status`` -- it is
    # visibility-only and never changes the overall health status.
    assert set(body["preview"]) == {
        "enabled",
        "state",
        "owner",
        "uptime_seconds",
    }
    assert body["preview"]["state"] == "stopped"
    # A database fault is never reported as a camera or preview failure.
    assert body["camera"] is not body["database"]
    assert "database" not in body["camera"]
    assert "database" not in body["preview"]


def test_health_does_not_report_a_version() -> None:
    """Task 8 deliberately kept version identity out of /health.

    Version lives in /version (complete) and / (minimal). This test states that
    as an intentional decision rather than an oversight, so a later change is a
    deliberate contract change and not an accident.
    """
    _, body = _asgi_get("/health")

    assert "version" not in body


def test_health_discloses_no_paths_or_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """/health exposes no configuration path, database path or secret.

    Hostname and architecture are deliberate, pre-existing parts of the
    contract and are left exactly as they are.
    """
    monkeypatch.setenv("MGO_SECRET_TOKEN", "super-secret-value")

    _, body = _asgi_get("/health")
    serialised = json.dumps(body)

    assert "super-secret-value" not in serialised
    assert str(config.storage.database_path) not in serialised
    assert str(config.storage.data_directory) not in serialised
    # The database is named, never located.
    assert body["hostname"]


def test_health_requests_perform_no_database_or_hardware_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Serving /health opens no database, runs no migration, probes no camera.

    The cached-monitor architecture is the reason this holds; this test fails
    if a future change makes a health request do real work again.
    """

    def _forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("/health must not perform live work")

    monkeypatch.setattr("mgo.core.database.connect_readonly", _forbidden)
    monkeypatch.setattr("mgo.core.database_health.connect_readonly", _forbidden)
    monkeypatch.setattr("mgo.api.app.apply_migrations", _forbidden)
    monkeypatch.setattr("mgo.core.camera_detection.build_detector", _forbidden)

    status, body = _asgi_get("/health")

    assert status == 200
    assert set(body) >= _HEALTH_FIELDS


def test_health_requests_do_not_rediscover_package_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No endpoint re-resolves package metadata per request.

    Resolution happens once; a request that scanned ``sys.path`` for
    distribution metadata would be doing filesystem work it does not need.
    """
    calls: list[str] = []

    def _counting_version(name: str) -> str:
        calls.append(name)
        return "1.0.0"

    monkeypatch.setattr("mgo.core.identity.distribution_version", _counting_version)

    _asgi_get("/version")
    _asgi_get("/version")
    _asgi_get("/health")
    _asgi_get("/")

    assert len(calls) == 1


def test_health_status_reflects_the_worst_component() -> None:
    """Top-level status behaviour is unchanged: the worst component wins.

    With no monitor state attached the database default is unhealthy, which
    the pre-existing aggregation maps to a critical overall status.
    """
    _, body = _asgi_get("/health")

    assert body["database"]["status"] == "unhealthy"
    assert body["status"] == "critical"
