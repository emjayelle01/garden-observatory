"""FastAPI application for Matt's Garden Observatory."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request

from mgo.camera import (
    CameraUnavailableError,
    CaptureService,
    CaptureTimeoutError,
    CaptureWriteError,
    PreviewService,
    build_capture_backend,
    build_preview_backend,
)
from mgo.camera.exceptions import (
    BackendCaptureError,
    CameraCaptureError,
    PreviewStartError,
    PreviewUnavailableError,
)
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

config = load_config()


def _current_camera_readiness(app: FastAPI) -> CameraReadiness:
    """Return the latest monitored readiness, or a safe startup default."""
    state: CameraState | None = getattr(app.state, "camera_state", None)
    readiness = state.get() if state is not None else None
    return readiness if readiness is not None else default_readiness(config.camera)


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
    app.state.preview_service = PreviewService(
        config.preview,
        build_preview_backend(config.camera.backend),
    )
    camera_detector = build_detector(config.camera.backend)
    # Evaluate readiness once before serving so /camera/status and /health
    # report a truthful state immediately after startup.
    await perform_camera_check(config, camera_state, detector=camera_detector)
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
        ),
        name="mgo-camera-monitor",
    )

    try:
        yield
    finally:
        stop_event.set()
        camera_stop_event.set()
        await asyncio.gather(health_task, camera_task)
        # Ensure no preview process is left running (no orphans) on shutdown.
        await asyncio.to_thread(app.state.preview_service.shutdown)
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
