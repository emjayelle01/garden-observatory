"""FastAPI application for Matt's Garden Observatory."""

from __future__ import annotations

import asyncio
import queue
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel

from mgo.api.preview_page import render_preview_page
from mgo.camera import (
    MJPEG_CONTENT_TYPE,
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
from mgo.core.health import collect_health
from mgo.core.health_monitor import run_health_monitor
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
        payload={"version": "0.1.0"},
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


class NotificationStatusResponse(BaseModel):
    """Typed, read-only projection of the notification manager's state."""

    enabled: bool
    providers: list[str]
    total_events_published: int
    total_delivery_failures: int
    last_event_at: str | None


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
    apply_migrations(config.storage.database_path)
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


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Initialise persistent MGO services during application startup."""
    applied_versions = apply_migrations(config.storage.database_path)

    # The numbered migration runner creates the ``captures`` table (migration
    # 002) alongside the observation schema in the shared database. The archive
    # only needs the shared path; it opens bounded connections per operation and
    # never manages its own engine or schema.
    app.state.capture_archive = CaptureArchive(config.storage.database_path)

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
        payload={"version": "0.1.0"},
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

    camera_state = CameraState()
    app.state.camera_state = camera_state
    app.state.capture_service = CaptureService(
        config.camera,
        build_capture_backend(config.camera),
    )
    # Preview shares the camera with capture; it starts STOPPED and is only ever
    # started explicitly via the API. It is attached here so the endpoints and
    # /health reflect a single, canonical preview service.
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
    # background monitor is only started when motion is enabled.
    motion_state = MotionState()
    motion_state.set(default_motion_result(config.motion))
    app.state.motion_state = motion_state
    motion_stop_event = asyncio.Event()
    motion_task: asyncio.Task[None] | None = None
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

    try:
        yield
    finally:
        stop_event.set()
        camera_stop_event.set()
        motion_stop_event.set()
        monitor_tasks = [health_task, camera_task]
        if motion_task is not None:
            monitor_tasks.append(motion_task)
        await asyncio.gather(*monitor_tasks)
        # Ensure no preview process is left running (no orphans) on shutdown.
        await asyncio.to_thread(app.state.preview_service.shutdown)
        notification_manager.publish(
            _system_event(EventType.SYSTEM_STOP, "MGO API stopped")
        )
        record_observation(
            config.storage.database_path,
            kind="application_stop",
            source="mgo-api",
            status="success",
            summary="MGO API stopped",
            payload={"version": "0.1.0"},
        )


app = FastAPI(
    title=config.application.name,
    version="0.1.0",
    description="API for Matt's Garden Observatory.",
    lifespan=lifespan,
)


@app.get("/")
def root() -> dict[str, str]:
    """Return the application identity."""
    return {
        "name": config.application.name,
        "version": "0.1.0",
        "status": "operational",
    }


@app.get("/health")
def health(request: Request) -> dict[str, Any]:
    """Return live system health, camera readiness and preview status.

    A stopped preview is not an error: preview is reported for visibility only
    and never changes the overall health status.
    """
    result = collect_health(config)
    result["camera"] = _current_camera_readiness(request.app).as_dict()
    result["preview"] = _preview_service(request.app).status().health_dict()
    return result


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
    """
    service = _capture_service(request.app)
    # Camera ownership is exclusive and capture is authoritative: release any
    # active preview first so the capture never contends for the camera. Preview
    # is left stopped; the caller may restart it explicitly afterwards.
    await asyncio.to_thread(_preview_service(request.app).release_for_capture)
    try:
        result = await asyncio.to_thread(service.capture_image)
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

    # The capture is complete and verified on disk. Persist its metadata as the
    # authoritative catalogue record. A persistence failure must NOT delete the
    # JPEG: the capture itself remains valid, so we surface an error and leave
    # the file in place for a later reconciliation.
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
    """
    service = _preview_service(request.app)
    try:
        status = await asyncio.to_thread(service.start)
    except PreviewUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except PreviewStartError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return status.as_dict()


@app.post("/camera/preview/stop")
async def camera_preview_stop(request: Request) -> dict[str, Any]:
    """Stop the live preview and return the final status. Idempotent."""
    service = _preview_service(request.app)
    status = await asyncio.to_thread(service.stop)
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
