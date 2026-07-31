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
dependencies. Those requests are deterministic, in-process and lifespan-free:
the endpoint lazily builds its manager from the repository's default (disabled)
configuration, so no hardware, database writes or background monitors are
involved.

The second half of this module covers the application *lifespan* itself, added
in Task 12: the managed preview lifecycle (auto-start, capture restoration,
ordering against motion monitoring and coordinated shutdown) exists only in
startup and shutdown, so it can only be proven by running the real lifespan.
Those tests use an isolated temporary configuration and the deterministic
Task 11 simulator backend -- still no Raspberry Pi, camera or camera tooling.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import threading
import time
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

import mgo.api.app as app_module

# The exact production application object: the systemd/uvicorn entry point
# serves ``mgo.api.app:app``. Importing anything narrower (a handler, a
# sub-app) would not prove production routing.
from mgo.api.app import app, lifespan
from mgo.camera import CameraCoordinator
from mgo.camera.exceptions import PreviewStartError
from mgo.camera.preview import PreviewService, PreviewState
from mgo.camera.preview_backend import MockPreviewBackend
from mgo.camera.simulator import SimulatorCaptureBackend
from mgo.core.config import MGOConfig, parse_config_text
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


# --- application lifespan: managed preview lifecycle (Task 12) ---------------
#
# These drive the *real* lifespan of the production application object against
# an isolated temporary configuration, because auto-start only exists there:
# nothing a request does may ever start preview. The camera backend is the
# deterministic Task 11 simulator (or a hardware-free double), so no Raspberry
# Pi, camera or camera tooling is involved.

_LIFESPAN_CONFIG = """
[application]
name = "Matt's Garden Observatory"
environment = "test"
host = "127.0.0.1"
port = 8080

[storage]
data_directory = "{root}"
log_directory = "{root}"
database_path = "{root}/mgo.db"

[camera]
enabled = true
backend = "{backend}"
detection_interval_seconds = 3600
capture_directory = "{root}/captures"

[preview]
enabled = true
auto_start = {auto_start}
restore_after_capture = {restore_after_capture}
width = 640
height = 480
fps = 15
startup_timeout_seconds = 2.0
shutdown_timeout_seconds = 2.0

[motion]
enabled = {motion}
analysis_interval_seconds = 0.2

[database]
health_check_interval_seconds = 3600
busy_timeout_seconds = 5.0

[health]
enabled = false
collection_interval_seconds = 3600
temperature_warning_celsius = 70.0
temperature_critical_celsius = 80.0
disk_warning_percent = 80.0
disk_critical_percent = 90.0
memory_warning_percent = 85.0
memory_critical_percent = 95.0
"""

#: The simulator producer thread's name -- the evidence for "exactly one
#: preview process", hardware-free.
_PRODUCER_THREAD = "mgo-simulator-preview"


def _producer_count() -> int:
    """Return how many simulator preview producers are currently alive."""
    return sum(
        1 for thread in threading.enumerate() if thread.name == _PRODUCER_THREAD
    )


def _lifespan_config(
    tmp_path: Path,
    *,
    backend: str = "simulator",
    auto_start: bool = False,
    restore_after_capture: bool = False,
    motion: bool = False,
) -> MGOConfig:
    """Build an isolated configuration through the real parser and validator."""
    return parse_config_text(
        _LIFESPAN_CONFIG.format(
            root=tmp_path.as_posix(),
            backend=backend,
            auto_start=str(auto_start).lower(),
            restore_after_capture=str(restore_after_capture).lower(),
            motion=str(motion).lower(),
        )
    )


def _run_lifespan(
    monkeypatch: pytest.MonkeyPatch,
    configuration: MGOConfig,
    body: Callable[[], Awaitable[None]],
) -> None:
    """Run the real lifespan around ``body``, restoring app state afterwards."""
    monkeypatch.setattr(app_module, "config", configuration)
    previous = dict(app.state._state)

    async def _main() -> None:
        async with lifespan(app):
            await body()

    try:
        asyncio.run(_main())
    finally:
        app.state._state.clear()
        app.state._state.update(previous)


async def _asgi_call(
    method: str, path: str
) -> tuple[int, dict[str, Any]]:
    """Drive one in-process request against the production app, awaited.

    The synchronous ``_asgi_get`` above cannot be used inside a running loop.
    """
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
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

    await app(scope, _receive, _send)
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


def test_default_configuration_performs_no_auto_start(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """With auto-start off, startup launches no preview at all."""
    observed: dict[str, Any] = {}

    async def _body() -> None:
        observed["state"] = app.state.preview_service.status().state
        observed["producers"] = _producer_count()
        observed["coordinator"] = app.state.camera_coordinator

    _run_lifespan(monkeypatch, _lifespan_config(tmp_path), _body)

    assert observed["state"] is PreviewState.STOPPED
    assert observed["producers"] == 0
    # The coordinator is always attached, even when no policy is enabled.
    assert isinstance(observed["coordinator"], CameraCoordinator)


def test_auto_start_reaches_a_running_preview(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """With auto-start on, preview is running before the first request."""
    observed: dict[str, Any] = {}

    async def _body() -> None:
        status = app.state.preview_service.status()
        observed["state"] = status.state
        observed["backend"] = status.backend
        observed["producers"] = _producer_count()
        observed["http"] = await _asgi_call("GET", "/camera/preview/status")

    _run_lifespan(
        monkeypatch, _lifespan_config(tmp_path, auto_start=True), _body
    )

    assert observed["state"] is PreviewState.RUNNING
    assert observed["backend"] == "simulator"
    # Exactly one preview producer -- auto-start never doubles the camera.
    assert observed["producers"] == 1
    code, payload = observed["http"]
    assert code == 200
    assert payload["state"] == "running"
    assert payload["owner"] == "preview"
    # Every producer is gone once the lifespan has exited.
    assert _producer_count() == 0


def test_auto_start_uses_the_real_first_frame_validation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A process that dies during startup fails the auto-start, truthfully.

    Auto-start goes through ``PreviewService.start`` rather than around it, so
    a launched-but-dead process is never reported as a running preview.
    """
    monkeypatch.setattr(
        app_module,
        "build_preview_backend",
        lambda _backend: MockPreviewBackend(launched_dead=True),
    )
    observed: dict[str, Any] = {}

    async def _body() -> None:
        status = app.state.preview_service.status()
        observed["state"] = status.state
        observed["error"] = status.last_error

    _run_lifespan(
        monkeypatch, _lifespan_config(tmp_path, auto_start=True), _body
    )

    assert observed["state"] is PreviewState.FAILED
    assert "exited during startup" in observed["error"]


def test_expected_auto_start_failure_keeps_the_api_serving(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A camera that cannot start must not take the whole application down.

    An operator needs the API most when the camera is broken: health, camera
    status and preview status all have to keep answering.
    """
    observed: dict[str, Any] = {}

    async def _body() -> None:
        observed["health"] = await _asgi_call("GET", "/health")
        observed["preview"] = await _asgi_call("GET", "/camera/preview/status")
        observed["camera"] = await _asgi_call("GET", "/camera/status")

    # The null backend never starts a preview process -- the hardware-free
    # stand-in for an absent or busy camera.
    _run_lifespan(
        monkeypatch,
        _lifespan_config(tmp_path, backend="null", auto_start=True),
        _body,
    )

    assert observed["health"][0] == 200
    assert observed["preview"][0] == 200
    assert observed["camera"][0] == 200
    preview = observed["preview"][1]
    assert preview["state"] == "failed"
    assert preview["last_error"] is not None
    assert preview["owner"] is None
    # Truthful: never reported as running, never reset to stopped.
    assert preview["state"] != "running"


def test_auto_start_makes_exactly_one_attempt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A failed auto-start is never retried in a loop."""
    backend = MockPreviewBackend(error=PreviewStartError("camera busy"))
    monkeypatch.setattr(
        app_module, "build_preview_backend", lambda _backend: backend
    )
    observed: dict[str, int] = {}

    async def _body() -> None:
        # Give any (unwanted) retry loop several scheduling opportunities.
        for _ in range(20):
            await asyncio.sleep(0.01)
        observed["attempts"] = backend.start_calls

    _run_lifespan(
        monkeypatch, _lifespan_config(tmp_path, auto_start=True), _body
    )

    assert observed["attempts"] == 1


def test_motion_monitoring_starts_after_the_auto_start_attempt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Motion must not open in a misleading waiting state.

    When auto-start was explicitly requested, frames are on their way, so the
    monitor is started only once that attempt has resolved.
    """
    observed: list[PreviewState] = []

    async def _stub_motion_monitor(
        _config: object,
        _state: object,
        _source: object,
        _detector: object,
        stop_event: asyncio.Event,
        **_kwargs: object,
    ) -> None:
        observed.append(app.state.preview_service.status().state)
        await stop_event.wait()

    monkeypatch.setattr(
        app_module, "run_motion_monitor", _stub_motion_monitor
    )

    async def _body() -> None:
        # Let the created monitor task reach its first statement.
        for _ in range(10):
            await asyncio.sleep(0)

    _run_lifespan(
        monkeypatch,
        _lifespan_config(tmp_path, auto_start=True, motion=True),
        _body,
    )

    assert observed == [PreviewState.RUNNING]


def test_shutdown_routes_through_the_coordinator(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Application shutdown stops preview via the coordinator, not around it."""
    calls: list[str] = []

    class _RecordingCoordinator(CameraCoordinator):
        def shutdown(self) -> None:
            calls.append("shutdown")
            super().shutdown()

    monkeypatch.setattr(app_module, "CameraCoordinator", _RecordingCoordinator)

    async def _body() -> None:
        assert app.state.preview_service.status().state is PreviewState.RUNNING

    _run_lifespan(
        monkeypatch, _lifespan_config(tmp_path, auto_start=True), _body
    )

    assert calls == ["shutdown"]
    assert _producer_count() == 0


# --- simulator-backed integration of the managed lifecycle ------------------


def test_capture_restores_the_preview_it_interrupted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A real capture through the API returns preview to running, once."""
    observed: dict[str, Any] = {}

    async def _body() -> None:
        observed["before"] = app.state.preview_service.status().state
        observed["capture"] = await _asgi_call("POST", "/camera/capture")
        observed["after"] = app.state.preview_service.status().as_dict()
        observed["producers"] = _producer_count()

    _run_lifespan(
        monkeypatch,
        _lifespan_config(
            tmp_path, auto_start=True, restore_after_capture=True
        ),
        _body,
    )

    assert observed["before"] is PreviewState.RUNNING
    code, payload = observed["capture"]
    assert code == 200
    assert payload["success"] is True
    assert payload["backend"] == "simulator"
    assert Path(payload["absolute_path"]).exists()
    assert observed["after"]["state"] == "running"
    # Restored, not duplicated: still exactly one producer.
    assert observed["producers"] == 1
    assert _producer_count() == 0


def test_capture_with_preview_stopped_leaves_it_stopped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Restoration never invents a preview the operator did not start."""
    observed: dict[str, Any] = {}

    async def _body() -> None:
        observed["before"] = app.state.preview_service.status().state
        observed["capture"] = await _asgi_call("POST", "/camera/capture")
        observed["after"] = app.state.preview_service.status().state
        observed["producers"] = _producer_count()

    _run_lifespan(
        monkeypatch,
        _lifespan_config(
            tmp_path, auto_start=False, restore_after_capture=True
        ),
        _body,
    )

    assert observed["before"] is PreviewState.STOPPED
    assert observed["capture"][0] == 200
    assert observed["after"] is PreviewState.STOPPED
    assert observed["producers"] == 0


def test_motion_consumes_frames_after_an_auto_started_preview(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Motion progresses past waiting_for_frames with no operator action."""
    observed: dict[str, Any] = {}

    async def _body() -> None:
        deadline = 15.0
        waited = 0.0
        payload: dict[str, Any] = {}
        while waited < deadline:
            _code, payload = await _asgi_call("GET", "/motion/status")
            if payload["frames_available"]:
                break
            await asyncio.sleep(0.1)
            waited += 0.1
        observed["motion"] = payload

    _run_lifespan(
        monkeypatch,
        _lifespan_config(tmp_path, auto_start=True, motion=True),
        _body,
    )

    assert observed["motion"]["frames_available"] is True
    assert observed["motion"]["status"] != "waiting_for_frames"


def test_the_managed_lifecycle_uses_no_physical_camera_command(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Every backend in play is the simulator; no camera tool is involved.

    Subprocess creation is made fatal for the duration, so an auto-start, a
    capture and a restoration that reached for ``rpicam-vid`` or
    ``rpicam-still`` would fail loudly rather than silently depending on tooling
    that a development machine does not have.
    """
    def _forbidden(*args: object, **kwargs: object) -> object:
        raise AssertionError(f"the managed lifecycle ran a subprocess: {args!r}")

    monkeypatch.setattr(subprocess, "Popen", _forbidden)
    monkeypatch.setattr(subprocess, "run", _forbidden)
    observed: dict[str, Any] = {}

    async def _body() -> None:
        _code, preview = await _asgi_call("GET", "/camera/preview/status")
        _code, capture = await _asgi_call("POST", "/camera/capture")
        observed["preview_backend"] = preview["backend"]
        observed["capture_backend"] = capture["backend"]

    _run_lifespan(
        monkeypatch,
        _lifespan_config(
            tmp_path, auto_start=True, restore_after_capture=True
        ),
        _body,
    )

    assert observed["preview_backend"] == "simulator"
    assert observed["capture_backend"] == "simulator"


# --- unexpected auto-start faults (Task 12 review correction) ---------------
#
# An *expected* preview failure is caught and the application serves on. An
# UNEXPECTED exception during auto-start is a programming defect and must still
# propagate -- but it used to escape before the lifespan's cleanup scope had
# even opened, leaving monitor tasks running and a camera process alive. These
# tests pin the corrected scope.

_READINESS_THREAD = "mgo-preview-readiness"


def _readiness_readers() -> int:
    """Count live preview startup-readiness reader threads."""
    return sum(
        1 for thread in threading.enumerate() if thread.name == _READINESS_THREAD
    )


def _await_no_camera_threads() -> None:
    """Wait, bounded, for every producer and readiness thread to exit."""
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and (
        _producer_count() or _readiness_readers()
    ):
        time.sleep(0.02)


class _MonitorSpy:
    """Records whether a monitor coroutine ran and whether it exited cleanly."""

    def __init__(self, stop_event_index: int) -> None:
        self._stop_event_index = stop_event_index
        self.entered = False
        self.exited = False

    async def __call__(self, *args: Any, **kwargs: Any) -> None:
        self.entered = True
        stop_event: asyncio.Event = args[self._stop_event_index]
        await stop_event.wait()
        self.exited = True


def _install_monitor_spies(
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, _MonitorSpy]:
    """Replace every background monitor with a spy that honours its stop event.

    Real monitors would also exit, but a spy makes "this task was signalled and
    awaited" directly observable rather than inferred.
    """
    spies = {
        "health": _MonitorSpy(1),
        "database": _MonitorSpy(2),
        "camera": _MonitorSpy(2),
        "motion": _MonitorSpy(4),
    }
    monkeypatch.setattr(app_module, "run_health_monitor", spies["health"])
    monkeypatch.setattr(app_module, "run_database_monitor", spies["database"])
    monkeypatch.setattr(app_module, "run_camera_monitor", spies["camera"])
    monkeypatch.setattr(app_module, "run_motion_monitor", spies["motion"])
    return spies


def _run_lifespan_expecting(
    monkeypatch: pytest.MonkeyPatch,
    configuration: MGOConfig,
    error_type: type[BaseException],
) -> dict[str, Any]:
    """Run the real lifespan expecting startup to fail, and inspect the loop.

    Task and thread state is read *inside* the running loop, before
    ``asyncio.run`` tears it down and hides whatever leaked.
    """
    monkeypatch.setattr(app_module, "config", configuration)
    previous = dict(app.state._state)
    observed: dict[str, Any] = {"reached_yield": False}

    async def _main() -> None:
        with pytest.raises(error_type) as excinfo:
            async with lifespan(app):
                observed["reached_yield"] = True
        observed["error"] = excinfo.value
        observed["live_tasks"] = sorted(
            task.get_name()
            for task in asyncio.all_tasks()
            if (task.get_name() or "").startswith("mgo-") and not task.done()
        )
        observed["producers"] = _producer_count()

    try:
        asyncio.run(_main())
    finally:
        app.state._state.clear()
        app.state._state.update(previous)
    return observed


def test_an_unexpected_auto_start_fault_is_cleaned_up_and_propagates(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A programming defect during auto-start leaves nothing behind.

    It must still escape -- an operator has to see it -- but the monitor tasks
    the lifespan created and the camera process it launched are stopped exactly
    as they would be on a normal shutdown.
    """
    spies = _install_monitor_spies(monkeypatch)
    injected = RuntimeError("programming error inside startup validation")
    observed_launch: dict[str, int] = {}

    def _explode(self: PreviewService, process: object) -> str | None:
        # The preview process is already launched at this point.
        observed_launch["producers_at_fault"] = _producer_count()
        raise injected

    monkeypatch.setattr(PreviewService, "_validate_startup", _explode)

    shutdowns: list[str] = []

    class _RecordingCoordinator(CameraCoordinator):
        def shutdown(self) -> None:
            shutdowns.append("shutdown")
            super().shutdown()

    monkeypatch.setattr(app_module, "CameraCoordinator", _RecordingCoordinator)

    observed = _run_lifespan_expecting(
        monkeypatch,
        _lifespan_config(tmp_path, auto_start=True, motion=True),
        RuntimeError,
    )

    # The simulator preview process really was running when the fault happened.
    assert observed_launch["producers_at_fault"] == 1
    # The original exception escaped startup unchanged.
    assert observed["error"] is injected
    assert observed["reached_yield"] is False
    # Cleanup ran through the coordinator.
    assert shutdowns == ["shutdown"]
    # Every monitor the lifespan created was signalled and awaited.
    assert spies["health"].entered and spies["health"].exited
    assert spies["database"].entered and spies["database"].exited
    assert spies["camera"].entered and spies["camera"].exited
    # The fault preceded motion-monitor creation, so it was never started.
    assert spies["motion"].entered is False
    # No monitor task and no camera thread survived.
    assert observed["live_tasks"] == []
    assert observed["producers"] == 0
    _await_no_camera_threads()
    assert _producer_count() == 0
    assert _readiness_readers() == 0


def test_an_unexpected_motion_startup_fault_is_also_cleaned_up(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The widened cleanup scope also covers motion-monitor creation."""
    spies = _install_monitor_spies(monkeypatch)
    injected = RuntimeError("programming error creating the motion monitor")

    def _explode(*args: Any, **kwargs: Any) -> None:
        raise injected

    monkeypatch.setattr(app_module, "BrokerFrameSource", _explode)

    observed = _run_lifespan_expecting(
        monkeypatch,
        _lifespan_config(tmp_path, auto_start=True, motion=True),
        RuntimeError,
    )

    assert observed["error"] is injected
    assert spies["health"].exited
    assert spies["database"].exited
    assert spies["camera"].exited
    assert spies["motion"].entered is False
    assert observed["live_tasks"] == []
    assert observed["producers"] == 0
    _await_no_camera_threads()
    assert _readiness_readers() == 0


def test_an_expected_auto_start_failure_still_reaches_serving(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The correction did not turn an expected hardware failure into a crash."""
    observed: dict[str, Any] = {}

    async def _body() -> None:
        observed["reached_yield"] = True
        observed["health"] = await _asgi_call("GET", "/health")
        observed["preview"] = await _asgi_call("GET", "/camera/preview/status")

    _run_lifespan(
        monkeypatch,
        _lifespan_config(tmp_path, backend="null", auto_start=True),
        _body,
    )

    assert observed["reached_yield"] is True
    assert observed["health"][0] == 200
    assert observed["preview"][1]["state"] == "failed"
    assert observed["preview"][1]["last_error"] is not None
    _await_no_camera_threads()
    assert _producer_count() == 0
    assert _readiness_readers() == 0


# --- motion recovery after a coordinated capture ----------------------------


async def _wait_for_motion_frames(available: bool, timeout: float = 20.0) -> bool:
    """Poll ``/motion/status`` until ``frames_available`` matches, or time out."""
    waited = 0.0
    while waited < timeout:
        _code, payload = await _asgi_call("GET", "/motion/status")
        if payload["frames_available"] is available:
            return True
        await asyncio.sleep(0.05)
        waited += 0.05
    return False


def test_motion_recovers_after_a_capture_restores_preview(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Motion loses frames while a capture owns the camera, then gets them back.

    The simulator capture is deliberately slowed so the window in which no
    preview producer exists is longer than a motion analysis interval; a real
    ``rpicam-still`` capture takes far longer than this. Without that the test
    would be racing the sampler rather than proving recovery.
    """
    original_capture = SimulatorCaptureBackend.capture

    def _slow_capture(self: SimulatorCaptureBackend, destination: Path) -> Any:
        time.sleep(0.8)
        return original_capture(self, destination)

    monkeypatch.setattr(SimulatorCaptureBackend, "capture", _slow_capture)
    observed: dict[str, Any] = {}

    async def _body() -> None:
        observed["frames_before"] = await _wait_for_motion_frames(True)
        _code, before = await _asgi_call("GET", "/camera/preview/status")
        observed["before"] = before

        capture = asyncio.create_task(_asgi_call("POST", "/camera/capture"))
        # While the capture owns the camera there is no preview producer, so
        # motion truthfully reports that it has no frames.
        observed["unavailable_observed"] = await _wait_for_motion_frames(False)
        observed["capture"] = await capture

        observed["frames_after"] = await _wait_for_motion_frames(True)
        _code, after = await _asgi_call("GET", "/camera/preview/status")
        observed["after"] = after
        observed["producers"] = _producer_count()
        _code, motion = await _asgi_call("GET", "/motion/status")
        observed["motion"] = motion

    _run_lifespan(
        monkeypatch,
        _lifespan_config(
            tmp_path, auto_start=True, restore_after_capture=True, motion=True
        ),
        _body,
    )

    assert observed["frames_before"] is True
    assert observed["unavailable_observed"] is True
    assert observed["capture"][0] == 200
    assert observed["capture"][1]["success"] is True
    assert observed["frames_after"] is True
    # A genuinely new preview generation, not the interrupted one resumed.
    assert observed["after"]["state"] == "running"
    assert observed["after"]["started_at"] != observed["before"]["started_at"]
    # Restored, not duplicated.
    assert observed["producers"] == 1
    assert observed["motion"]["frames_available"] is True
    assert observed["motion"]["status"] != "waiting_for_frames"
    _await_no_camera_threads()
    assert _producer_count() == 0
