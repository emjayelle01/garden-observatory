"""Tests for the browser preview streaming API and page.

These call the route functions directly with a lightweight fake request (the
same pattern as ``test_camera_api``), attaching a running preview service and a
broker backed by a mock frame source. They cover the MJPEG content type, the
not-running gate, frame delivery, viewer connect/disconnect, multiple viewers,
capture-while-streaming, the browser status update and the HTML page.

No Raspberry Pi hardware is required; all frame production is mocked.
"""

from __future__ import annotations

import asyncio
import threading
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.responses import HTMLResponse

from mgo.api.app import (
    camera_capture,
    camera_preview_status,
    camera_preview_stream,
    preview_page,
)
from mgo.camera import CaptureService, MockBackend
from mgo.camera.preview import PreviewService
from mgo.camera.preview_backend import MockPreviewBackend
from mgo.camera.streaming import MjpegBroker, MockFrameSource
from mgo.captures.archive import CaptureArchive
from mgo.core.config import CameraConfig, PreviewConfig
from mgo.core.database import apply_migrations

_JPEG_A = b"\xff\xd8" + b"AAAA" + b"\xff\xd9"
_JPEG_B = b"\xff\xd8" + b"BBBB" + b"\xff\xd9"


def _preview_config(*, enabled: bool = True) -> PreviewConfig:
    return PreviewConfig(
        enabled=enabled,
        width=1280,
        height=720,
        fps=15,
        startup_timeout_seconds=5.0,
        shutdown_timeout_seconds=5.0,
    )


def _running_preview() -> PreviewService:
    """Return a preview service already in the RUNNING state."""
    service = PreviewService(_preview_config(), MockPreviewBackend())
    service.start()
    return service


def _broker(*frames: bytes) -> tuple[MjpegBroker, threading.Event, MockFrameSource]:
    """Return a broker over a mock source that streams frames continuously.

    ``loop=True`` models a live camera producing frames indefinitely, so a
    viewer that connects at any time receives frames. The source ends only when
    closed (on the last viewer disconnecting).
    """
    hold = threading.Event()
    source = MockFrameSource(list(frames), hold_open=hold, loop=True)
    return MjpegBroker(lambda: source), hold, source


def _request(
    *,
    preview: PreviewService | None = None,
    broker: MjpegBroker | None = None,
    capture_service: CaptureService | None = None,
    capture_archive: CaptureArchive | None = None,
) -> SimpleNamespace:
    """Build a fake request exposing the requested ``app.state`` services."""
    state = SimpleNamespace()
    if preview is not None:
        state.preview_service = preview
    if broker is not None:
        state.preview_broker = broker
    if capture_service is not None:
        state.capture_service = capture_service
    if capture_archive is not None:
        state.capture_archive = capture_archive
    return SimpleNamespace(app=SimpleNamespace(state=state))


# --- gate -----------------------------------------------------------------


def test_stream_requires_preview_running() -> None:
    """Streaming while preview is stopped returns HTTP 409 and never starts it."""
    preview = PreviewService(_preview_config(), MockPreviewBackend())
    broker, _hold, _source = _broker(_JPEG_A)
    request = _request(preview=preview, broker=broker)

    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(camera_preview_stream(request))

    assert excinfo.value.status_code == 409
    assert preview.status().state.value == "stopped"


# --- content type + frame delivery ---------------------------------------


def test_stream_returns_multipart_mjpeg_and_frames() -> None:
    """A running preview streams multipart MJPEG parts to a viewer."""
    preview = _running_preview()
    broker, _hold, source = _broker(_JPEG_A, _JPEG_B)
    request = _request(preview=preview, broker=broker)

    async def _run() -> tuple[str, list[bytes], int]:
        response = await camera_preview_stream(request)
        agen = response.body_iterator
        chunks = [await agen.__anext__()]
        viewers = broker.viewer_count
        await agen.aclose()
        return response.media_type or "", chunks, viewers

    media_type, chunks, viewers = asyncio.run(_run())

    assert "multipart/x-mixed-replace" in media_type
    assert "boundary=" in media_type
    assert viewers == 1
    assert b"Content-Type: image/jpeg" in chunks[0]
    assert (_JPEG_A in chunks[0]) or (_JPEG_B in chunks[0])
    # The viewer was cleanly removed on disconnect (aclose).
    assert broker.viewer_count == 0
    assert source.closed is True


def test_stream_disconnect_removes_viewer() -> None:
    """Closing the client stream unsubscribes the viewer and stops the pump."""
    preview = _running_preview()
    broker, _hold, source = _broker(_JPEG_A)
    request = _request(preview=preview, broker=broker)

    async def _run() -> None:
        response = await camera_preview_stream(request)
        agen = response.body_iterator
        await agen.__anext__()
        await agen.aclose()

    asyncio.run(_run())

    assert broker.viewer_count == 0
    assert source.closed is True


def test_stream_supports_multiple_viewers() -> None:
    """Two simultaneous viewers each receive frames from the one source."""
    preview = _running_preview()
    broker, _hold, _source = _broker(_JPEG_A, _JPEG_B)
    request = _request(preview=preview, broker=broker)

    async def _run() -> tuple[bytes, bytes, int]:
        first = await camera_preview_stream(request)
        second = await camera_preview_stream(request)
        agen_a = first.body_iterator
        agen_b = second.body_iterator
        chunk_a = await agen_a.__anext__()
        chunk_b = await agen_b.__anext__()
        viewers = broker.viewer_count
        await agen_a.aclose()
        await agen_b.aclose()
        return chunk_a, chunk_b, viewers

    chunk_a, chunk_b, viewers = asyncio.run(_run())

    assert viewers == 2
    assert b"image/jpeg" in chunk_a
    assert b"image/jpeg" in chunk_b
    assert broker.viewer_count == 0


# --- capture interaction --------------------------------------------------


def test_capture_while_viewers_connected(tmp_path: Path) -> None:
    """A capture interrupts preview even while a viewer is streaming."""
    preview = _running_preview()
    broker, _hold, _source = _broker(_JPEG_A)
    capture_service = CaptureService(
        CameraConfig(
            enabled=True,
            backend="mock",
            device_index=None,
            detection_interval_seconds=30,
            capture_directory=tmp_path / "captures",
        ),
        MockBackend(width=4608, height=2592, name="mock"),
    )
    database_path = tmp_path / "mgo.db"
    apply_migrations(database_path)
    request = _request(
        preview=preview,
        broker=broker,
        capture_service=capture_service,
        capture_archive=CaptureArchive(database_path),
    )

    # A viewer is connected to the (independent) stream broker.
    viewer = broker.subscribe()
    try:
        result = asyncio.run(camera_capture(request))

        # Capture succeeded and preview was released (stopped), while the
        # streaming layer remained independent of the preview lifecycle.
        uuid.UUID(result["capture_id"])
        assert preview.status().state.value == "stopped"
        assert broker.viewer_count == 1
    finally:
        broker.unsubscribe(viewer)


def test_browser_status_reflects_capture_interruption(tmp_path: Path) -> None:
    """After a capture, the status a browser polls shows preview stopped."""
    preview = _running_preview()
    broker, _hold, _source = _broker(_JPEG_A)
    capture_service = CaptureService(
        CameraConfig(
            enabled=True,
            backend="mock",
            device_index=None,
            detection_interval_seconds=30,
            capture_directory=tmp_path / "captures",
        ),
        MockBackend(name="mock"),
    )
    database_path = tmp_path / "mgo.db"
    apply_migrations(database_path)
    request = _request(
        preview=preview,
        broker=broker,
        capture_service=capture_service,
        capture_archive=CaptureArchive(database_path),
    )

    assert camera_preview_status(request)["state"] == "running"
    asyncio.run(camera_capture(request))
    assert camera_preview_status(request)["state"] == "stopped"


# --- browser page ---------------------------------------------------------


def test_preview_page_renders_html() -> None:
    """The preview page is standalone HTML wired to the preview endpoints."""
    response = preview_page()

    assert isinstance(response, HTMLResponse)
    assert response.media_type == "text/html"
    body = response.body.decode("utf-8")
    assert "Live Preview" in body
    assert "Start Preview" in body
    assert "Stop Preview" in body
    assert "Refresh status" in body
    assert "/camera/preview/stream" in body
    assert "/camera/preview/status" in body
    # No JavaScript framework is used.
    assert "react" not in body.lower()
    assert "vue" not in body.lower()
