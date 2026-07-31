"""FastAPI application for Matt's Garden Observatory."""

from __future__ import annotations

import asyncio
import logging
import queue
import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from types import TracebackType
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel

from mgo.api.dashboard_page import render_dashboard_page
from mgo.api.preview_page import render_preview_page
from mgo.camera import (
    MJPEG_CONTENT_TYPE,
    CameraCoordinator,
    CameraUnavailableError,
    CaptureService,
    CaptureTimeoutError,
    CaptureWriteError,
    MjpegBroker,
    PreviewProcessFrameSource,
    PreviewService,
    build_capture_backend,
    build_preview_backend,
    encode_multipart_frame,
)
from mgo.camera.exceptions import (
    BackendCaptureError,
    CameraCaptureError,
    PreviewError,
    PreviewStartError,
    PreviewUnavailableError,
)
from mgo.camera.preview import PreviewState
from mgo.camera.streaming import STREAM_IDLE_TIMEOUT_SECONDS
from mgo.captures import (
    CaptureArchive,
    CaptureArchiveError,
    capture_detail,
    capture_summary,
)
from mgo.core.camera import CameraReadiness, CameraState, default_readiness
from mgo.core.camera_detection import build_detector
from mgo.core.camera_monitor import perform_camera_check, run_camera_monitor
from mgo.core.config import load_config
from mgo.core.database import apply_migrations
from mgo.core.database_health import (
    DatabaseHealth,
    DatabaseHealthState,
    unavailable_health,
)
from mgo.core.database_monitor import perform_database_check, run_database_monitor
from mgo.core.health import collect_health, worst_status
from mgo.core.health_monitor import run_health_monitor
from mgo.core.identity import build_identity, get_application_version
from mgo.core.observations import list_observations, record_observation
from mgo.motion import (
    BrokerFrameSource,
    FrameDifferenceDetector,
    MotionResult,
    MotionState,
    MotionStatus,
    default_motion_result,
    run_motion_monitor,
)
from mgo.notifications import (
    EventSeverity,
    EventType,
    NotificationEvent,
    NotificationManager,
    build_notification_manager,
    create_event,
)

config = load_config()

LOGGER = logging.getLogger(__name__)

#: The one release version this module reports, resolved once from package
#: metadata. Every place that used to carry a hard-coded literal -- the OpenAPI
#: document, ``GET /``, the lifecycle notification events and the persisted
#: start/stop observations -- now reads this, so they cannot disagree.
APPLICATION_VERSION = get_application_version()


def _current_camera_readiness(app: FastAPI) -> CameraReadiness:
    """Return the latest monitored readiness, or a safe startup default."""
    state: CameraState | None = getattr(app.state, "camera_state", None)
    readiness = state.get() if state is not None else None
    return readiness if readiness is not None else default_readiness(config.camera)


def _current_motion_result(app: FastAPI) -> MotionResult:
    """Return the latest motion result, or a safe startup default.

    Reads application-managed state only; it never runs a frame comparison, so
    requesting the status triggers no hardware activity.
    """
    state: MotionState | None = getattr(app.state, "motion_state", None)
    result = state.get() if state is not None else None
    return result if result is not None else default_motion_result(config.motion)


def _current_database_health(app: FastAPI) -> DatabaseHealth:
    """Return the latest checked database health, or a safe startup default.

    Reads application-managed state only. Requesting the status never opens a
    database connection, so the endpoint cannot add load to -- or block on -- a
    struggling database.
    """
    state: DatabaseHealthState | None = getattr(app.state, "database_state", None)
    health = state.get() if state is not None else None
    return health if health is not None else unavailable_health(config)


def _notification_manager(app: FastAPI) -> NotificationManager:
    """Return the notification manager, building one if none was attached.

    The lifespan attaches a manager to ``app.state``; a lazily built fallback
    keeps the status endpoint usable in contexts (such as direct unit tests)
    that never ran startup, mirroring :func:`_capture_service`.
    """
    manager: NotificationManager | None = getattr(
        app.state, "notification_manager", None
    )
    if manager is not None:
        return manager
    manager = build_notification_manager(config.notifications)
    app.state.notification_manager = manager
    return manager


def _system_event(event_type: EventType, summary: str) -> NotificationEvent:
    """Build an application lifecycle (start/stop) notification event."""
    return create_event(
        event_type,
        source="mgo-api",
        title="MGO application lifecycle",
        summary=summary,
        payload={"version": APPLICATION_VERSION},
    )


def _camera_event(readiness: CameraReadiness) -> NotificationEvent:
    """Map a material camera readiness change to a notification event.

    Availability decides the event type; anything not available (disabled,
    waiting for hardware, error) is a ``CAMERA_UNAVAILABLE`` at warning
    severity so a future real transport can meaningfully alert on it.
    """
    if readiness.available:
        event_type = EventType.CAMERA_AVAILABLE
        severity = EventSeverity.INFO
        title = "Camera available"
    else:
        event_type = EventType.CAMERA_UNAVAILABLE
        severity = EventSeverity.WARNING
        title = "Camera unavailable"
    return create_event(
        event_type,
        source="mgo-camera",
        title=title,
        summary=readiness.detail,
        severity=severity,
        payload=readiness.as_dict(),
    )


def _motion_event(result: MotionResult) -> NotificationEvent:
    """Map a material motion transition to a notification event.

    Every material transition is a single ``MOTION_STATE_CHANGED`` event; the
    new state travels in the structured payload, not in the event type, so the
    type vocabulary stays small while providers see the full result.
    """
    return create_event(
        EventType.MOTION_STATE_CHANGED,
        source="mgo-motion",
        title="Motion state changed",
        summary=result.detail,
        payload=result.as_dict(),
    )


class VersionResponse(BaseModel):
    """Typed, read-only projection of the application's build identity.

    Every field is configured metadata or a pure platform lookup. There is
    deliberately no filesystem path, configuration location, hostname, remote
    URL, command output or raw environment value here: the endpoint reports
    *what build is running*, and nothing about where it lives.

    ``commit`` is ``None`` whenever the deployment supplied no valid
    ``MGO_BUILD_COMMIT``; ``version`` is ``unknown`` whenever package metadata
    cannot be read. Both are truthful absences rather than errors.
    """

    application: str
    version: str
    commit: str | None
    python_version: str
    architecture: str


class NotificationStatusResponse(BaseModel):
    """Typed, read-only projection of the notification manager's state."""

    enabled: bool
    providers: list[str]
    total_events_published: int
    total_delivery_failures: int
    last_event_at: str | None


class DatabaseStatusResponse(BaseModel):
    """Typed, read-only projection of the latest database-health check.

    ``database`` is the database file's *name*, not its absolute path: the
    status endpoints expose no filesystem layout, and the configured path is
    already available to an operator from the configuration file.
    """

    status: str
    accessible: bool
    database: str
    schema_version: int | None
    expected_schema_version: int
    migration_status: str
    journal_mode: str | None
    foreign_keys: bool
    integrity: str
    detail: str
    checked_at: str


class MotionStatusResponse(BaseModel):
    """Typed, read-only projection of the latest motion-monitor state."""

    enabled: bool
    status: str
    detected: bool
    score: float
    threshold: float
    frames_available: bool
    detail: str
    evaluated_at: str


def _capture_service(app: FastAPI) -> CaptureService:
    """Return the capture service, building a default if none was attached.

    The lifespan attaches a service to ``app.state``; tests can attach one
    backed by a mock so the endpoint is exercised without camera hardware.
    """
    service: CaptureService | None = getattr(app.state, "capture_service", None)
    if service is not None:
        return service
    return CaptureService(config.camera, build_capture_backend(config.camera))


def _capture_archive(app: FastAPI) -> CaptureArchive:
    """Return the capture archive, building a default if none was attached.

    The lifespan attaches an archive to ``app.state`` after running migrations;
    a lazily built fallback keeps the endpoints usable in contexts (such as
    direct unit tests) that never ran startup. The fallback applies the existing
    numbered migrations first -- the same idempotent runner used at startup -- so
    the ``captures`` table exists before use, mirroring :func:`_capture_service`.
    """
    archive: CaptureArchive | None = getattr(app.state, "capture_archive", None)
    if archive is not None:
        return archive
    apply_migrations(
        config.storage.database_path,
        busy_timeout_seconds=config.database.busy_timeout_seconds,
    )
    archive = CaptureArchive(config.storage.database_path)
    app.state.capture_archive = archive
    return archive


def _preview_service(app: FastAPI) -> PreviewService:
    """Return the preview service, building a default if none was attached.

    The lifespan attaches a service to ``app.state``; a lazily built fallback
    keeps the endpoints usable in contexts (such as direct unit tests) that
    never ran startup, mirroring :func:`_capture_service`.
    """
    service: PreviewService | None = getattr(app.state, "preview_service", None)
    if service is not None:
        return service
    service = PreviewService(
        config.preview,
        build_preview_backend(config.camera.backend),
    )
    app.state.preview_service = service
    return service


def _preview_broker(app: FastAPI) -> MjpegBroker:
    """Return the MJPEG streaming broker, building a default if none attached.

    The broker's frame source reads the *existing* preview process supervised by
    the preview service (single camera owner); the broker never controls the
    process, keeping streaming independent of the preview lifecycle.
    """
    broker: MjpegBroker | None = getattr(app.state, "preview_broker", None)
    if broker is not None:
        return broker
    service = _preview_service(app)
    broker = MjpegBroker(lambda: PreviewProcessFrameSource(service))
    app.state.preview_broker = broker
    return broker


def _camera_coordinator(app: FastAPI) -> CameraCoordinator:
    """Return the camera coordinator, building one if none was attached.

    The lifespan attaches a coordinator to ``app.state``; a lazily built
    fallback keeps the mutating endpoints usable in contexts (such as direct
    unit tests) that never ran startup, mirroring :func:`_capture_service`.

    The fallback deliberately composes :func:`_capture_service` and
    :func:`_preview_service`, so a test that attached its own mock-backed
    services gets a coordinator over *those* services and never a production
    backend built behind its back.
    """
    coordinator: CameraCoordinator | None = getattr(
        app.state, "camera_coordinator", None
    )
    if coordinator is not None:
        return coordinator
    coordinator = CameraCoordinator(
        _capture_service(app),
        _preview_service(app),
        restore_after_capture=config.preview.restore_after_capture,
    )
    app.state.camera_coordinator = coordinator
    return coordinator


def _attempt_preview_auto_start(coordinator: CameraCoordinator) -> None:
    """Make the single configured preview start attempt during startup.

    An *expected* preview failure -- camera absent, camera busy, tool missing,
    encoder failure, no first frame, permission denied, process exit during
    startup -- must not stop the application from serving: an operator needs the
    API precisely when the camera is broken. The failure is logged, preview is
    left truthfully in ``FAILED`` with its own ``last_error``, and nothing
    retries in a loop. Unexpected faults are deliberately *not* caught here;
    they are programming errors, not hardware absence.
    """
    try:
        status = coordinator.start_preview()
    except PreviewError as exc:
        LOGGER.error(
            "Preview auto-start failed; the API continues to serve without a "
            "live preview: %s",
            exc,
        )
        return
    LOGGER.info(
        "Preview auto-started during startup (state=%s)", status.state.value
    )


async def _shutdown_lifespan(
    *,
    stop_events: Sequence[asyncio.Event],
    monitor_tasks: Sequence[asyncio.Task[None]],
    coordinator: CameraCoordinator,
    notification_manager: NotificationManager,
) -> BaseException | None:
    """Run every shutdown stage and return the first failure, if any.

    Each stage is attempted even when an earlier one failed. That is the whole
    point: the stages are independent obligations -- stop the monitors, release
    the camera, announce the stop, record it -- and a failure in one is never a
    reason to skip the rest. In particular, a monitor that raises on the way out
    must not be able to strand a camera process.

    Every supplied monitor task is driven to a terminal state before camera
    shutdown begins, so the application can never return from its lifespan with
    a monitor it created still running.

    No failure is swallowed: every one is logged with its traceback, and the
    *first* in ``monitor_tasks`` order is returned so the caller can decide
    whether it is the failure worth raising or whether an earlier, more
    informative one already exists.
    """
    for event in stop_events:
        event.set()

    first_error: BaseException | None = None

    def _record(stage: str, exc: BaseException) -> None:
        nonlocal first_error
        LOGGER.error("Lifespan cleanup stage %r failed", stage, exc_info=exc)
        if first_error is None:
            first_error = exc

    # ``return_exceptions=True`` is load-bearing, not a style choice. The
    # default propagates the first monitor exception the instant it happens and
    # stops awaiting the rest, so cleanup would advance to camera shutdown --
    # and the lifespan would return -- while another monitor was still running.
    # Collecting results instead means every task is terminal (returned, raised
    # or already cancelled) before the next stage begins.
    #
    # A monitor that fails is never a reason to cancel the others: the stop
    # events above are their cooperative shutdown mechanism, and cutting a
    # healthy monitor short would lose whatever it was still finishing.
    results = await asyncio.gather(*monitor_tasks, return_exceptions=True)
    for task, result in zip(monitor_tasks, results, strict=True):
        if isinstance(result, BaseException):
            # Ordered by ``monitor_tasks``, not by which failed first in
            # wall-clock terms, so the retained failure is deterministic.
            # ``CancelledError`` counts: a cancelled monitor is terminal, but it
            # is not a clean shutdown either.
            _record(f"monitor-tasks[{task.get_name()}]", result)

    try:
        # Ensure no preview process is left running (no orphans) on shutdown.
        # Routed through the coordinator so shutdown waits for an in-flight
        # capture transaction -- including any preview restoration -- to finish
        # rather than racing a preview back into existence behind it.
        await asyncio.to_thread(coordinator.shutdown)
    except BaseException as exc:
        _record("camera-shutdown", exc)

    try:
        notification_manager.publish(
            _system_event(EventType.SYSTEM_STOP, "MGO API stopped")
        )
    except BaseException as exc:
        _record("stop-notification", exc)

    try:
        record_observation(
            config.storage.database_path,
            kind="application_stop",
            source="mgo-api",
            status="success",
            summary="MGO API stopped",
            payload={"version": APPLICATION_VERSION},
        )
    except BaseException as exc:
        _record("stop-observation", exc)

    return first_error


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Initialise persistent MGO services during application startup.

    Ordering is deliberate and load-bearing: configuration is already resolved
    at import time, the database is migrated *first*, and its health is
    established before any other service exists. Migration failure propagates
    out of the lifespan so the application refuses to start rather than serving
    against a schema it cannot trust -- there is no partially migrated running
    state. Camera, preview, motion and notification startup all follow, and
    none of them can affect database health.
    """
    applied_versions = apply_migrations(
        config.storage.database_path,
        busy_timeout_seconds=config.database.busy_timeout_seconds,
    )

    # The numbered migration runner creates the ``captures`` table (migration
    # 002) alongside the observation schema in the shared database. The archive
    # only needs the shared path; it opens bounded connections per operation and
    # never manages its own engine or schema.
    app.state.capture_archive = CaptureArchive(config.storage.database_path)

    # Establish the initial database-health state before serving, so /health
    # and /database/status are truthful from the first request rather than
    # reporting an "unevaluated" placeholder until the first monitor tick.
    database_state = DatabaseHealthState()
    app.state.database_state = database_state
    await asyncio.to_thread(perform_database_check, config, database_state)

    if applied_versions:
        record_observation(
            config.storage.database_path,
            kind="database_migration",
            source="mgo-api",
            status="success",
            summary="Database migrations applied",
            payload={"versions": applied_versions},
        )

    record_observation(
        config.storage.database_path,
        kind="application_start",
        source="mgo-api",
        status="success",
        summary="MGO API started",
        payload={"version": APPLICATION_VERSION},
    )

    # The notification manager is the single publication point for events.
    # Producers below publish typed events to it and never touch a delivery
    # transport; with notifications disabled it is a truthful no-op.
    notification_manager = build_notification_manager(config.notifications)
    app.state.notification_manager = notification_manager
    notification_manager.publish(
        _system_event(EventType.SYSTEM_START, "MGO API started")
    )

    stop_event = asyncio.Event()
    health_task = asyncio.create_task(
        run_health_monitor(config, stop_event),
        name="mgo-health-monitor",
    )

    # The initial check already ran above; the monitor waits one interval
    # before its first periodic re-check to avoid a duplicate startup check.
    database_stop_event = asyncio.Event()
    database_task = asyncio.create_task(
        run_database_monitor(
            config,
            database_state,
            database_stop_event,
            run_initial=False,
        ),
        name="mgo-database-monitor",
    )

    camera_state = CameraState()
    app.state.camera_state = camera_state
    capture_service = CaptureService(
        config.camera,
        build_capture_backend(config.camera),
    )
    app.state.capture_service = capture_service
    # Preview shares the camera with capture. It starts STOPPED and is started
    # either explicitly via the API or -- when preview.auto_start is enabled --
    # once below, after camera readiness has been established. It is attached
    # here so the endpoints and /health reflect a single, canonical service.
    preview_service = PreviewService(
        config.preview,
        build_preview_backend(config.camera.backend),
    )
    app.state.preview_service = preview_service
    # The streaming broker fans the preview process's frames out to browsers.
    # Its frame source reads the existing preview process (single owner); it
    # never starts or stops preview.
    app.state.preview_broker = MjpegBroker(
        lambda: PreviewProcessFrameSource(preview_service)
    )
    # Every camera-*mutating* operation (preview start/stop, still capture,
    # shutdown) goes through this one coordinator, so they can never interleave.
    # Status and frame-stream reads deliberately bypass it.
    camera_coordinator = CameraCoordinator(
        capture_service,
        preview_service,
        restore_after_capture=config.preview.restore_after_capture,
    )
    app.state.camera_coordinator = camera_coordinator
    camera_detector = build_detector(config.camera.backend)

    # Material camera readiness changes become notification events. The monitor
    # only knows a callback; the mapping to an event -- and the manager -- stay
    # here in the application wiring.
    def _publish_camera_change(readiness: CameraReadiness) -> None:
        notification_manager.publish(_camera_event(readiness))

    # Evaluate readiness once before serving so /camera/status and /health
    # report a truthful state immediately after startup.
    await perform_camera_check(
        config,
        camera_state,
        detector=camera_detector,
        on_material_change=_publish_camera_change,
    )
    camera_stop_event = asyncio.Event()
    # The initial check already ran above; the monitor waits one interval
    # before its first periodic recheck to avoid a duplicate startup probe.
    camera_task = asyncio.create_task(
        run_camera_monitor(
            config,
            camera_state,
            camera_stop_event,
            detector=camera_detector,
            run_initial=False,
            on_material_change=_publish_camera_change,
        ),
        name="mgo-camera-monitor",
    )

    # Motion detection shares the preview stream via the broker (single camera
    # owner); it never starts preview or a second camera process. The state is
    # always attached so /motion/status is truthful even when disabled; the
    # background monitor is only started when motion is enabled, below.
    motion_state = MotionState()
    motion_state.set(default_motion_result(config.motion))
    app.state.motion_state = motion_state
    motion_stop_event = asyncio.Event()
    # Declared before the cleanup scope opens so the ``finally`` below can always
    # reason about it, whether or not the monitor was ever created.
    motion_task: asyncio.Task[None] | None = None
    # The *primary* failure -- whatever went wrong in startup or serving -- is
    # tracked explicitly rather than left to ``finally`` semantics, because an
    # exception raised inside a ``finally`` silently replaces the one already in
    # flight. Here that would mean a monitor failing on the way out could hide
    # the programming defect that caused the shutdown in the first place.
    primary_error: BaseException | None = None
    primary_traceback: TracebackType | None = None
    cleanup_error: BaseException | None = None

    # The cleanup scope deliberately opens BEFORE preview auto-start and before
    # the motion monitor is created, not just around ``yield``. An unexpected
    # exception during either would otherwise escape with the monitor tasks
    # already running and a preview process already launched, leaving orphans
    # that nothing would ever stop. Everything below therefore gets the same
    # shutdown as a normal run, and the original exception is re-raised.
    try:
        # Managed preview: one start attempt, made here so it resolves BEFORE
        # motion monitoring begins -- otherwise motion would open in
        # "waiting_for_frames" even though frames were on their way. Off by
        # default; the process management runs in a worker thread so it never
        # blocks the event loop.
        if config.preview.auto_start:
            await asyncio.to_thread(
                _attempt_preview_auto_start, camera_coordinator
            )

        if config.motion.enabled:
            # Material motion transitions become notification events, mirroring
            # the camera wiring above.
            def _publish_motion_transition(result: MotionResult) -> None:
                notification_manager.publish(_motion_event(result))

            motion_task = asyncio.create_task(
                run_motion_monitor(
                    config,
                    motion_state,
                    BrokerFrameSource(app.state.preview_broker),
                    FrameDifferenceDetector(config.motion),
                    motion_stop_event,
                    transition_listener=_publish_motion_transition,
                ),
                name="mgo-motion-monitor",
            )

        yield
    except BaseException as exc:
        # Held rather than propagated immediately so cleanup runs first and,
        # crucially, so a *cleanup* failure cannot take this exception's place.
        # It is re-raised below, as the same object.
        primary_error = exc
        primary_traceback = exc.__traceback__
    finally:
        monitor_tasks = [health_task, database_task, camera_task]
        if motion_task is not None:
            monitor_tasks.append(motion_task)
        cleanup_error = await _shutdown_lifespan(
            stop_events=(
                stop_event,
                database_stop_event,
                camera_stop_event,
                motion_stop_event,
            ),
            monitor_tasks=monitor_tasks,
            coordinator=camera_coordinator,
            notification_manager=notification_manager,
        )

    if primary_error is not None:
        if cleanup_error is not None:
            # Both failed. The startup/serving failure is what an operator has
            # to diagnose; the cleanup failure would otherwise silently take its
            # place, so it is logged here and the original is raised.
            LOGGER.error(
                "Lifespan cleanup also failed while an earlier failure was "
                "propagating; the earlier failure is the one raised",
                exc_info=cleanup_error,
            )
        raise primary_error.with_traceback(primary_traceback)

    if cleanup_error is not None:
        # Nothing else failed, so the first cleanup failure is the result --
        # raised only after every remaining stage was attempted.
        raise cleanup_error


app = FastAPI(
    title=config.application.name,
    version=APPLICATION_VERSION,
    description="API for Matt's Garden Observatory.",
    lifespan=lifespan,
)


@app.get("/")
def root() -> dict[str, str]:
    """Return the application identity.

    Deliberately minimal and unchanged: the same three keys it has always
    returned. Only the *source* of ``version`` changed -- from a hard-coded
    literal to the central package-metadata authority -- so any existing client
    or probe keeps working. Full build identity lives at ``GET /version``.
    """
    return {
        "name": config.application.name,
        "version": APPLICATION_VERSION,
        "status": "operational",
    }


@app.get("/version")
def version() -> VersionResponse:
    """Return the application's release and build identity.

    Read-only, side-effect free and deterministic for a running process: it
    reads values resolved once at import (package metadata, the optional build
    commit) plus two pure ``platform`` lookups. It performs no database I/O, no
    hardware detection, no subprocess and no network call, so it answers
    truthfully with no camera attached, no usable database and no Git
    installed. It always returns HTTP 200 while the application is serving.

    ``version`` is the installed distribution's version, or ``unknown`` when
    package metadata cannot be read. ``commit`` is the validated
    ``MGO_BUILD_COMMIT`` value, or ``null`` when the deployment did not supply
    one -- an absent optional build identifier is not an error.
    """
    identity = build_identity(config.application.name)
    return VersionResponse(
        application=identity.application,
        version=identity.version,
        commit=identity.commit,
        python_version=identity.python_version,
        architecture=identity.architecture,
    )


@app.get("/health")
def health(request: Request) -> dict[str, Any]:
    """Return live system health, database, camera readiness and preview status.

    A stopped preview is not an error: preview is reported for visibility only
    and never changes the overall health status. The database *does* affect the
    top-level status, because nothing the application records can be trusted
    while its database is unusable -- a degraded database contributes
    ``warning`` and an unhealthy one ``critical``. Camera readiness is reported
    independently, so a database problem is never mislabelled as a camera
    failure (and vice versa).

    The database section reads the state recorded by the background monitor; no
    database I/O happens per request.
    """
    result = collect_health(config)
    database_health = _current_database_health(request.app)
    result["database"] = database_health.health_dict()
    result["status"] = worst_status(
        str(result["status"]),
        database_health.severity,
    )
    result["camera"] = _current_camera_readiness(request.app).as_dict()
    result["preview"] = _preview_service(request.app).status().health_dict()
    return result


@app.get("/database/status")
def database_status(request: Request) -> DatabaseStatusResponse:
    """Return the latest database-health check result.

    Read-only and truthful: it reflects the most recent result recorded by the
    background database monitor and never opens a database connection, runs a
    migration or repairs anything itself. Always returns HTTP 200 when the
    application is serving -- including when the database itself is unhealthy,
    which is reported in ``status`` rather than as an HTTP error.
    """
    health_result = _current_database_health(request.app)
    return DatabaseStatusResponse(
        status=health_result.status.value,
        accessible=health_result.accessible,
        database=health_result.database_name,
        schema_version=health_result.schema_version,
        expected_schema_version=health_result.expected_schema_version,
        migration_status=health_result.migration_status.value,
        journal_mode=health_result.journal_mode,
        foreign_keys=health_result.foreign_keys_enabled,
        integrity=health_result.integrity,
        detail=health_result.detail,
        checked_at=health_result.checked_at.isoformat(),
    )


@app.get("/camera/status")
def camera_status(request: Request) -> dict[str, Any]:
    """Return the latest monitored camera readiness result.

    This never triggers hardware detection; it reflects the most recent state
    recorded by the background camera monitor.
    """
    return _current_camera_readiness(request.app).as_dict()


@app.get("/motion/status")
def motion_status(request: Request) -> MotionStatusResponse:
    """Return the latest motion-monitor state.

    Read-only and truthful: it reflects the most recent result recorded by the
    background motion monitor and never runs a frame comparison itself. Always
    returns HTTP 200 when the application is healthy -- including the ``disabled``
    state when motion detection is off and the ``waiting_for_frames`` state when
    no preview frame is available.
    """
    result = _current_motion_result(request.app)
    return MotionStatusResponse(
        # Derived from state so the flag is always consistent with the reported
        # status: only the disabled state means motion detection is off.
        enabled=result.status is not MotionStatus.DISABLED,
        status=result.status.value,
        detected=result.detected,
        score=result.score,
        threshold=result.threshold,
        frames_available=result.frames_available,
        detail=result.detail,
        evaluated_at=result.evaluated_at.isoformat(),
    )


@app.get("/notifications/status")
def notifications_status(request: Request) -> NotificationStatusResponse:
    """Return the notification framework's current status.

    Read-only and truthful: it reflects the manager's live counters and never
    publishes or delivers anything itself. Always returns HTTP 200 when the
    application is healthy -- including the disabled state, where no provider
    is configured and every counter stays at zero.
    """
    status = _notification_manager(request.app).status()
    return NotificationStatusResponse(
        enabled=status.enabled,
        providers=list(status.providers),
        total_events_published=status.total_events_published,
        total_delivery_failures=status.total_delivery_failures,
        last_event_at=(
            status.last_event_at.isoformat()
            if status.last_event_at is not None
            else None
        ),
    )


@app.post("/camera/capture")
async def camera_capture(request: Request) -> dict[str, Any]:
    """Capture a single still image and return its metadata.

    Returns HTTP 200 with the capture metadata on success. Expected failures
    are mapped to meaningful status codes: an unavailable camera to 503, a
    capture timeout to 504, and backend/write failures to 502/500. Detection
    runs in a worker thread so the capture subprocess never blocks the loop.

    Camera ownership is exclusive and capture is authoritative. The coordinator
    releases any active preview so the capture never contends for the camera and
    -- only when ``preview.restore_after_capture`` is enabled and preview was
    genuinely running beforehand -- restarts it afterwards. A restoration
    failure never changes this response: the capture's own outcome is the
    answer, and preview truth stays on the preview status endpoint.
    """
    coordinator = _camera_coordinator(request.app)
    try:
        result = await asyncio.to_thread(coordinator.capture_image)
    except CameraUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except CaptureTimeoutError as exc:
        raise HTTPException(status_code=504, detail=str(exc)) from exc
    except BackendCaptureError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except CaptureWriteError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except CameraCaptureError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    # The capture is complete and verified on disk, and the camera-operation
    # lock has already been released -- database work never holds the camera.
    # Persist its metadata as the authoritative catalogue record. A persistence
    # failure must NOT delete the JPEG: the capture itself remains valid, so we
    # surface an error and leave the file in place for a later reconciliation.
    archive = _capture_archive(request.app)
    try:
        record = archive.record_capture(result)
    except CaptureArchiveError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    response = result.as_dict()
    response["capture_id"] = str(record.id)
    return response


@app.get("/camera/preview/status")
def camera_preview_status(request: Request) -> dict[str, Any]:
    """Return the current live-preview status.

    Read-only: it reconciles an unexpected process exit but never starts or
    stops the preview.
    """
    return _preview_service(request.app).status().as_dict()


@app.post("/camera/preview/start")
async def camera_preview_start(request: Request) -> dict[str, Any]:
    """Start the live preview and return its status.

    Idempotent: if preview is already running this returns HTTP 200 with the
    current status and never launches a duplicate process. A disabled preview
    maps to 503; a process that cannot start maps to 502. Runs in a worker
    thread so process management never blocks the event loop.

    Routed through the coordinator, so a start requested while a capture holds
    the camera waits for that capture rather than racing it.
    """
    coordinator = _camera_coordinator(request.app)
    try:
        status = await asyncio.to_thread(coordinator.start_preview)
    except PreviewUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except PreviewStartError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return status.as_dict()


@app.post("/camera/preview/stop")
async def camera_preview_stop(request: Request) -> dict[str, Any]:
    """Stop the live preview and return the final status. Idempotent.

    Routed through the coordinator, so a stop requested during a capture is
    applied after that transaction (including any preview restoration) rather
    than being lost or interleaved with it.
    """
    coordinator = _camera_coordinator(request.app)
    status = await asyncio.to_thread(coordinator.stop_preview)
    return status.as_dict()


async def _mjpeg_stream(broker: MjpegBroker) -> AsyncIterator[bytes]:
    """Yield multipart MJPEG parts to one viewer until it disconnects.

    Subscribes to the broker for the duration of the connection and always
    unsubscribes on exit -- normal end, client disconnect (``GeneratorExit``) or
    error -- so a departed viewer is removed cleanly and the pump stops when the
    last viewer leaves. The blocking mailbox read runs off the event loop.
    """
    subscriber = broker.subscribe()
    try:
        while True:
            try:
                frame = await asyncio.to_thread(
                    subscriber.get, STREAM_IDLE_TIMEOUT_SECONDS
                )
            except queue.Empty:
                # No frame within the idle window: end the stream rather than
                # hold the connection open indefinitely.
                return
            if frame is None:
                # End-of-stream sentinel: the preview stopped producing frames.
                return
            yield encode_multipart_frame(frame)
    finally:
        broker.unsubscribe(subscriber)


@app.get("/camera/preview/stream")
async def camera_preview_stream(request: Request) -> StreamingResponse:
    """Stream the live preview to the browser as multipart/x-mixed-replace MJPEG.

    Requires preview to already be running (HTTP 409 otherwise) and never starts
    it. Supports multiple simultaneous viewers; each disconnect is handled
    cleanly. The browser only consumes frames and never owns the camera.
    """
    preview = _preview_service(request.app)
    if preview.status().state is not PreviewState.RUNNING:
        raise HTTPException(
            status_code=409,
            detail="Preview is not running; start it before streaming.",
        )
    broker = _preview_broker(request.app)
    return StreamingResponse(
        _mjpeg_stream(broker),
        media_type=MJPEG_CONTENT_TYPE,
    )


@app.get("/preview", response_class=HTMLResponse)
def preview_page() -> HTMLResponse:
    """Serve the simple browser live-preview page."""
    return HTMLResponse(content=render_preview_page())


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard_page() -> HTMLResponse:
    """Serve the local operational dashboard page.

    Purely additive and deliberately inert: it returns a constant HTML shell
    and nothing else. It collects no health, inspects no hardware, opens no
    database connection, starts no monitor, never touches the camera or
    preview, publishes no notification and mutates no application state --
    opening the dashboard is as cheap and as safe as opening a static file.

    Every live value is fetched by the *browser* from the existing contracts
    (``/health``, ``/version``, ``/motion/status``, ``/notifications/status``),
    so this route composes no subsystem status of its own and cannot disagree
    with the API. ``GET /`` remains the minimal JSON identity endpoint.
    """
    return HTMLResponse(content=render_dashboard_page())


@app.get("/captures")
def captures(request: Request) -> list[dict[str, Any]]:
    """Return the capture catalogue, newest first.

    Metadata only: no binary image data is returned.
    """
    archive = _capture_archive(request.app)
    return [capture_summary(capture) for capture in archive.list_captures()]


@app.get("/captures/{capture_id}")
def capture(capture_id: str, request: Request) -> dict[str, Any]:
    """Return the stored metadata for a single capture.

    Returns HTTP 404 for both malformed and unknown identifiers, since neither
    can refer to a catalogued capture.
    """
    archive = _capture_archive(request.app)
    try:
        identifier = uuid.UUID(capture_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Capture not found") from exc

    record = archive.get_capture(identifier)
    if record is None:
        raise HTTPException(status_code=404, detail="Capture not found")
    return capture_detail(record)


@app.get("/observations")
def observations(
    limit: int = Query(default=100, ge=1, le=1000),
    kind: str | None = None,
) -> list[dict[str, Any]]:
    """Return the latest observatory timeline entries."""
    records = list_observations(
        config.storage.database_path,
        limit=limit,
        kind=kind,
    )

    return [
        {
            "id": record.id,
            "observed_at": record.observed_at,
            "kind": record.kind,
            "source": record.source,
            "status": record.status,
            "summary": record.summary,
            "payload": record.payload,
            "correlation_id": record.correlation_id,
            "created_at": record.created_at,
        }
        for record in records
    ]
