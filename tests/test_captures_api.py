"""Tests for the capture-archive API endpoints.

These call the route functions directly with a lightweight fake request (the
same pattern as ``test_camera_api``), attaching both a capture service and a
capture archive to ``app.state``. They verify that a successful capture returns
a ``capture_id`` while remaining backwards compatible, that the archive
endpoints reflect stored metadata newest-first, that unknown ids yield 404, and
that a persistence failure preserves the JPEG.

Task 13.1 moved the endpoint's two steps behind the shared
:class:`~mgo.captures.workflow.CaptureWorkflow`, which the motion-triggered
worker also uses. The endpoint's public contract did not change, and the tests
below are the proof: the same path, the same fields, the same ``capture_id``,
the same failure mapping, the same JPEG-preservation behaviour -- and a manual
capture that never acquires a motion origin.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException

from mgo.api.app import camera_capture, capture, captures
from mgo.camera import CameraCoordinator, CaptureService, MockBackend
from mgo.camera.exceptions import (
    BackendCaptureError,
    CameraCaptureError,
    CameraUnavailableError,
    CaptureTimeoutError,
    CaptureWriteError,
)
from mgo.camera.preview import PreviewService
from mgo.camera.preview_backend import MockPreviewBackend
from mgo.captures.archive import CaptureArchive
from mgo.captures.workflow import CaptureWorkflow
from mgo.core.config import CameraConfig, PreviewConfig
from mgo.core.database import apply_migrations


def _capture_config(capture_directory: Path, *, enabled: bool = True) -> CameraConfig:
    """Build a camera config for capture-endpoint tests."""
    return CameraConfig(
        enabled=enabled,
        backend="mock",
        device_index=None,
        detection_interval_seconds=30,
        capture_directory=capture_directory,
    )


def _archive(tmp_path: Path) -> CaptureArchive:
    """Build a migration-provisioned archive over a temporary database."""
    database_path = tmp_path / "mgo.db"
    apply_migrations(database_path)
    return CaptureArchive(database_path)


def _request(
    *,
    service: CaptureService | None = None,
    archive: CaptureArchive | object | None = None,
) -> SimpleNamespace:
    """Build a fake request exposing the requested ``app.state`` services."""
    app_state = SimpleNamespace()
    if service is not None:
        app_state.capture_service = service
    if archive is not None:
        app_state.capture_archive = archive
    return SimpleNamespace(app=SimpleNamespace(state=app_state))


def _service(tmp_path: Path) -> CaptureService:
    """A capture service backed by a mock backend writing to ``tmp_path``."""
    return CaptureService(
        _capture_config(tmp_path / "captures"),
        MockBackend(width=4608, height=2592, name="mock"),
    )


def test_capture_endpoint_returns_capture_id(tmp_path: Path) -> None:
    """POST /camera/capture returns a capture_id for a persisted capture."""
    archive = _archive(tmp_path)
    request = _request(service=_service(tmp_path), archive=archive)

    result = asyncio.run(camera_capture(request))

    assert "capture_id" in result
    uuid.UUID(result["capture_id"])  # parses as a valid UUID
    # Exactly one record was created for the capture.
    assert len(archive.list_captures()) == 1


def test_capture_endpoint_is_backwards_compatible(tmp_path: Path) -> None:
    """The response keeps every Task 2B field and only *adds* capture_id."""
    archive = _archive(tmp_path)
    request = _request(service=_service(tmp_path), archive=archive)

    result = asyncio.run(camera_capture(request))

    expected_2b_fields = {
        "success",
        "filename",
        "absolute_path",
        "timestamp",
        "width",
        "height",
        "filesize_bytes",
        "backend",
    }
    assert expected_2b_fields.issubset(result.keys())
    assert result.keys() == expected_2b_fields | {"capture_id"}


def test_capture_endpoint_persists_matching_metadata(tmp_path: Path) -> None:
    """The stored record matches the metadata returned to the caller."""
    archive = _archive(tmp_path)
    request = _request(service=_service(tmp_path), archive=archive)

    result = asyncio.run(camera_capture(request))

    stored = archive.get_capture(uuid.UUID(result["capture_id"]))
    assert stored is not None
    assert stored.filename == result["filename"]
    assert stored.absolute_path == result["absolute_path"]
    assert stored.width == result["width"]
    assert stored.height == result["height"]
    assert stored.filesize_bytes == result["filesize_bytes"]
    assert stored.camera_backend == result["backend"]


def test_captures_lists_newest_first(tmp_path: Path) -> None:
    """GET /captures returns compact metadata ordered newest first."""
    archive = _archive(tmp_path)
    service = _service(tmp_path)
    request = _request(service=service, archive=archive)

    first = asyncio.run(camera_capture(request))
    second = asyncio.run(camera_capture(request))

    listing = captures(_request(archive=archive))
    assert [item["capture_id"] for item in listing] == [
        second["capture_id"],
        first["capture_id"],
    ]
    # The listing is metadata only and never exposes an image path/body.
    assert set(listing[0].keys()) == {
        "capture_id",
        "timestamp",
        "filename",
        "width",
        "height",
        "filesize_bytes",
        "backend",
    }


def test_capture_detail_returns_stored_metadata(tmp_path: Path) -> None:
    """GET /captures/{id} returns the full stored metadata for one capture."""
    archive = _archive(tmp_path)
    request = _request(service=_service(tmp_path), archive=archive)
    created = asyncio.run(camera_capture(request))

    detail = capture(created["capture_id"], _request(archive=archive))

    assert detail["capture_id"] == created["capture_id"]
    assert detail["filename"] == created["filename"]
    assert detail["absolute_path"] == created["absolute_path"]
    assert detail["width"] == created["width"]
    assert "created_at" in detail
    assert detail["extra_metadata"] == {}


def test_capture_detail_unknown_id_returns_404(tmp_path: Path) -> None:
    """A well-formed but unknown id yields HTTP 404."""
    archive = _archive(tmp_path)

    with pytest.raises(HTTPException) as excinfo:
        capture(str(uuid.uuid4()), _request(archive=archive))

    assert excinfo.value.status_code == 404


def test_capture_detail_malformed_id_returns_404(tmp_path: Path) -> None:
    """A malformed id cannot refer to a capture and yields HTTP 404."""
    archive = _archive(tmp_path)

    with pytest.raises(HTTPException) as excinfo:
        capture("not-a-uuid", _request(archive=archive))

    assert excinfo.value.status_code == 404


def test_persistence_failure_preserves_jpeg_and_errors(tmp_path: Path) -> None:
    """If metadata persistence fails, the JPEG is kept and an error is raised."""
    # An archive whose table was never created: record_capture will fail.
    broken_archive = CaptureArchive(tmp_path / "mgo.db")
    capture_dir = tmp_path / "captures"
    request = _request(
        service=CaptureService(
            _capture_config(capture_dir),
            MockBackend(width=640, height=480, name="mock"),
        ),
        archive=broken_archive,
    )

    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(camera_capture(request))

    assert excinfo.value.status_code == 500
    # The captured file must remain on disk despite the persistence failure.
    jpegs = list(capture_dir.glob("*.jpg"))
    assert len(jpegs) == 1
    assert jpegs[0].stat().st_size > 0


def test_capture_endpoint_no_duplicate_records(tmp_path: Path) -> None:
    """Two captures create exactly two distinct records — no duplicates."""
    archive = _archive(tmp_path)
    request = _request(service=_service(tmp_path), archive=archive)

    first = asyncio.run(camera_capture(request))
    second = asyncio.run(camera_capture(request))

    captures_stored = archive.list_captures()
    assert len(captures_stored) == 2
    assert first["capture_id"] != second["capture_id"]


def test_unpersisted_result_never_reaches_archive_on_disabled(
    tmp_path: Path,
) -> None:
    """A failed capture (disabled camera) creates no catalogue record."""
    archive = _archive(tmp_path)
    request = _request(
        service=CaptureService(
            _capture_config(tmp_path / "captures", enabled=False),
            MockBackend(),
        ),
        archive=archive,
    )

    with pytest.raises(HTTPException):
        asyncio.run(camera_capture(request))

    assert archive.list_captures() == []


# --- Task 13.1: the shared workflow did not change this contract -------------


def _workflow_request(workflow: object) -> SimpleNamespace:
    """Build a fake request exposing a pre-built capture workflow."""
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(capture_workflow=workflow))
    )


class _FailingCoordinator:
    """A coordinator double whose capture always raises ``error``."""

    def __init__(self, error: BaseException) -> None:
        self._error = error

    def capture_image(self) -> Any:
        raise self._error


class _RecordingArchive:
    """Wraps a real archive, remembering what metadata it was handed."""

    def __init__(self, inner: CaptureArchive) -> None:
        self._inner = inner
        self.metadata: list[dict[str, Any] | None] = []

    def record_capture(
        self, result: Any, *, extra_metadata: dict[str, Any] | None = None
    ) -> Any:
        self.metadata.append(extra_metadata)
        return self._inner.record_capture(result, extra_metadata=extra_metadata)


def test_the_manual_endpoint_uses_the_shared_workflow(tmp_path: Path) -> None:
    """The route resolves the one workflow both capture paths run through."""
    archive = _archive(tmp_path)
    coordinator = CameraCoordinator(
        _service(tmp_path),
        PreviewService(
            PreviewConfig(
                enabled=False,
                width=640,
                height=480,
                fps=15,
                startup_timeout_seconds=1.0,
                shutdown_timeout_seconds=1.0,
            ),
            MockPreviewBackend(),
        ),
    )
    workflow = CaptureWorkflow(coordinator, archive)

    result = asyncio.run(camera_capture(_workflow_request(workflow)))

    assert result["success"] is True
    uuid.UUID(result["capture_id"])
    assert len(archive.list_captures()) == 1


@pytest.mark.parametrize(
    ("error", "status_code"),
    [
        (CameraUnavailableError("no cameras available"), 503),
        (CaptureTimeoutError("timed out"), 504),
        (BackendCaptureError("exit code 1"), 502),
        (CaptureWriteError("could not write"), 500),
        (CameraCaptureError("something else in the camera domain"), 500),
    ],
)
def test_the_failure_mapping_is_unchanged(
    tmp_path: Path, error: BaseException, status_code: int
) -> None:
    """Every camera-domain failure still maps to the status code it always did."""
    workflow = CaptureWorkflow(
        _FailingCoordinator(error),  # type: ignore[arg-type]
        _archive(tmp_path),
    )

    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(camera_capture(_workflow_request(workflow)))

    assert excinfo.value.status_code == status_code
    # The camera domain's own message still reaches the client, as before.
    assert excinfo.value.detail == str(error)


def test_a_manual_capture_never_becomes_a_motion_capture(tmp_path: Path) -> None:
    """An operator's capture carries no motion attribution, ever."""
    recording = _RecordingArchive(_archive(tmp_path))
    workflow = CaptureWorkflow(
        CameraCoordinator(
            _service(tmp_path),
            PreviewService(
                PreviewConfig(
                    enabled=False,
                    width=640,
                    height=480,
                    fps=15,
                    startup_timeout_seconds=1.0,
                    shutdown_timeout_seconds=1.0,
                ),
                MockPreviewBackend(),
            ),
        ),
        recording,  # type: ignore[arg-type]
    )

    result = asyncio.run(camera_capture(_workflow_request(workflow)))

    # The route supplies nothing at all -- not an empty dictionary, nothing.
    assert recording.metadata == [None]
    stored = _archive_of(recording).get_capture(uuid.UUID(result["capture_id"]))
    assert stored is not None
    assert stored.extra_metadata == {}
    assert "origin" not in stored.extra_metadata


def _archive_of(recording: _RecordingArchive) -> CaptureArchive:
    """Reach the real archive behind the recording wrapper."""
    return recording._inner


def test_the_response_fields_are_exactly_the_task_2b_set_plus_capture_id(
    tmp_path: Path,
) -> None:
    """A second, explicit guard on the response shape after the refactor."""
    request = _request(service=_service(tmp_path), archive=_archive(tmp_path))

    result = asyncio.run(camera_capture(request))

    assert set(result) == {
        "success",
        "filename",
        "absolute_path",
        "timestamp",
        "width",
        "height",
        "filesize_bytes",
        "backend",
        "capture_id",
    }
    assert result["success"] is True
    assert result["timestamp"].endswith("+00:00")
    assert Path(result["absolute_path"]).is_absolute()
    assert Path(result["absolute_path"]).exists()
    assert result["filename"] == Path(result["absolute_path"]).name
