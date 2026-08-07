"""Tests for the shared capture-and-catalogue workflow.

The workflow exists so the manual endpoint and the motion-triggered worker
cannot drift apart, which makes its guarantees worth pinning precisely: the
coordinator runs once, the archive runs once and only after a successful
capture, the camera-operation lock is *not* held across the database write, an
archive failure never deletes a valid JPEG, and nothing in the module knows what
HTTP is.
"""

from __future__ import annotations

import ast
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from mgo.camera import CaptureService, MockBackend
from mgo.camera.coordinator import CameraCoordinator
from mgo.camera.exceptions import CameraUnavailableError, PreviewUnavailableError
from mgo.camera.models import CaptureResult
from mgo.camera.preview import PreviewService
from mgo.camera.preview_backend import MockPreviewBackend
from mgo.captures.archive import CaptureArchive, CaptureArchiveError
from mgo.captures.models import Capture
from mgo.captures.workflow import CaptureWorkflow
from mgo.core.config import CameraConfig, PreviewConfig

WORKFLOW_SOURCE = (
    Path(__file__).resolve().parents[1] / "src" / "mgo" / "captures" / "workflow.py"
)


def _camera_config(capture_directory: Path, *, enabled: bool = True) -> CameraConfig:
    """Build a camera config for workflow tests."""
    return CameraConfig(
        enabled=enabled,
        backend="mock",
        device_index=None,
        detection_interval_seconds=30,
        capture_directory=capture_directory,
    )


def _preview_config(*, enabled: bool = False) -> PreviewConfig:
    """Build a preview config for workflow tests."""
    return PreviewConfig(
        enabled=enabled,
        width=640,
        height=480,
        fps=15,
        startup_timeout_seconds=1.0,
        shutdown_timeout_seconds=1.0,
    )


def _result(tmp_path: Path) -> CaptureResult:
    """A verified capture result that never touched a camera."""
    return CaptureResult(
        success=True,
        filename="2026-08-07T10-00-00.000000Z.jpg",
        absolute_path=tmp_path / "2026-08-07T10-00-00.000000Z.jpg",
        timestamp=datetime(2026, 8, 7, 10, 0, tzinfo=UTC),
        width=4608,
        height=2592,
        filesize_bytes=1024,
        backend="mock",
    )


def _record(result: CaptureResult, metadata: dict[str, Any] | None) -> Capture:
    """A catalogue record matching ``result``."""
    return Capture(
        id=uuid.uuid4(),
        filename=result.filename,
        absolute_path=str(result.absolute_path),
        captured_at_utc=result.timestamp,
        width=result.width,
        height=result.height,
        filesize_bytes=result.filesize_bytes,
        camera_backend=result.backend,
        created_at_utc=datetime.now(UTC),
        extra_metadata=dict(metadata) if metadata else {},
    )


class _FakeCoordinator:
    """Records capture calls and whether a capture is currently in progress."""

    def __init__(
        self, result: CaptureResult, *, error: BaseException | None = None
    ) -> None:
        self._result = result
        self._error = error
        self.calls = 0
        self.inside_capture = False

    def capture_image(self) -> CaptureResult:
        self.calls += 1
        self.inside_capture = True
        try:
            if self._error is not None:
                raise self._error
            return self._result
        finally:
            self.inside_capture = False


class _FakeArchive:
    """Records archive calls, optionally failing, optionally probing."""

    def __init__(
        self,
        *,
        error: BaseException | None = None,
        probe: Any = None,
    ) -> None:
        self._error = error
        self._probe = probe
        self.calls = 0
        self.results: list[CaptureResult] = []
        self.metadata: list[dict[str, Any] | None] = []

    def record_capture(
        self,
        result: CaptureResult,
        *,
        extra_metadata: dict[str, Any] | None = None,
    ) -> Capture:
        self.calls += 1
        self.results.append(result)
        self.metadata.append(extra_metadata)
        if self._probe is not None:
            self._probe()
        if self._error is not None:
            raise self._error
        return _record(result, extra_metadata)


def _workflow(coordinator: Any, archive: Any) -> CaptureWorkflow:
    """Build the real workflow over test doubles."""
    return CaptureWorkflow(coordinator, archive)


def test_the_coordinator_is_invoked_exactly_once(tmp_path: Path) -> None:
    """One request for a still means one camera transaction, never two."""
    coordinator = _FakeCoordinator(_result(tmp_path))
    archive = _FakeArchive()

    _workflow(coordinator, archive).capture()

    assert coordinator.calls == 1


def test_the_archive_is_invoked_exactly_once(tmp_path: Path) -> None:
    """A successful capture produces exactly one catalogue record."""
    coordinator = _FakeCoordinator(_result(tmp_path))
    archive = _FakeArchive()

    _workflow(coordinator, archive).capture()

    assert archive.calls == 1


def test_the_persisted_record_is_returned(tmp_path: Path) -> None:
    """The workflow returns the archive's record, not the camera's result."""
    result = _result(tmp_path)
    coordinator = _FakeCoordinator(result)
    archive = _FakeArchive()

    record = _workflow(coordinator, archive).capture()

    assert isinstance(record, Capture)
    assert record.filename == result.filename
    assert record.camera_backend == result.backend
    # The exact result the coordinator produced is what was archived.
    assert archive.results == [result]


def test_supplied_metadata_reaches_the_archive(tmp_path: Path) -> None:
    """Structured attribution is passed through untouched."""
    coordinator = _FakeCoordinator(_result(tmp_path))
    archive = _FakeArchive()
    metadata = {"origin": "motion", "motion_score": 0.42}

    record = _workflow(coordinator, archive).capture(extra_metadata=metadata)

    assert archive.metadata == [{"origin": "motion", "motion_score": 0.42}]
    assert record.extra_metadata == {"origin": "motion", "motion_score": 0.42}


def test_metadata_is_defensively_copied(tmp_path: Path) -> None:
    """A caller that reuses its dictionary cannot rewrite what was persisted."""
    coordinator = _FakeCoordinator(_result(tmp_path))
    archive = _FakeArchive()
    metadata: dict[str, Any] = {"origin": "motion"}

    _workflow(coordinator, archive).capture(extra_metadata=metadata)
    metadata["origin"] = "tampered"
    metadata["injected"] = True

    assert archive.metadata == [{"origin": "motion"}]


def test_no_metadata_means_no_metadata(tmp_path: Path) -> None:
    """A manual capture supplies nothing and acquires no origin."""
    coordinator = _FakeCoordinator(_result(tmp_path))
    archive = _FakeArchive()

    record = _workflow(coordinator, archive).capture()

    assert archive.metadata == [None]
    assert record.extra_metadata == {}


def test_a_capture_failure_prevents_the_archive_call(tmp_path: Path) -> None:
    """Nothing is catalogued for an image that was never produced."""
    failure = CameraUnavailableError("camera is disabled")
    coordinator = _FakeCoordinator(_result(tmp_path), error=failure)
    archive = _FakeArchive()

    with pytest.raises(CameraUnavailableError) as excinfo:
        _workflow(coordinator, archive).capture()

    # The camera domain's own exception, unchanged and unwrapped.
    assert excinfo.value is failure
    assert archive.calls == 0


def test_an_archive_failure_propagates_unchanged(tmp_path: Path) -> None:
    """The archive's own domain error reaches the caller as-is."""
    failure = CaptureArchiveError("database is locked")
    coordinator = _FakeCoordinator(_result(tmp_path))
    archive = _FakeArchive(error=failure)

    with pytest.raises(CaptureArchiveError) as excinfo:
        _workflow(coordinator, archive).capture()

    assert excinfo.value is failure
    # The camera still ran exactly once; there is no retry hidden anywhere.
    assert coordinator.calls == 1


def test_an_archive_failure_never_deletes_the_jpeg(tmp_path: Path) -> None:
    """A valid capture survives a failed catalogue write, on disk.

    Driven through the real capture service and a real (unmigrated, therefore
    failing) archive, so the file under test is one the pipeline actually wrote.
    """
    capture_directory = tmp_path / "captures"
    service = CaptureService(
        _camera_config(capture_directory),
        MockBackend(width=640, height=480, name="mock"),
    )
    coordinator = CameraCoordinator(
        service, PreviewService(_preview_config(), MockPreviewBackend())
    )
    # No migration was applied, so the ``captures`` table does not exist.
    archive = CaptureArchive(tmp_path / "mgo.db")

    with pytest.raises(CaptureArchiveError):
        CaptureWorkflow(coordinator, archive).capture()

    jpegs = list(capture_directory.glob("*.jpg"))
    assert len(jpegs) == 1
    assert jpegs[0].stat().st_size > 0


def test_the_archive_runs_after_the_camera_transaction_closed(
    tmp_path: Path,
) -> None:
    """Database work must never happen inside the camera transaction."""
    coordinator = _FakeCoordinator(_result(tmp_path))
    observed: dict[str, bool] = {}

    def _probe() -> None:
        observed["inside_capture"] = coordinator.inside_capture

    archive = _FakeArchive(probe=_probe)

    _workflow(coordinator, archive).capture()

    assert observed["inside_capture"] is False


def test_the_camera_operation_lock_is_free_during_archive_work(
    tmp_path: Path,
) -> None:
    """The real coordinator's lock is released before the archive is touched.

    Structural reading is not proof: the coordinator's lock is a plain,
    non-reentrant mutex, so if it were still held here the coordinator call made
    from inside ``record_capture`` would deadlock rather than raise. The whole
    workflow therefore runs in a thread with a bounded join -- a deadlock shows
    up as a thread that never finished, not as a hung test session.
    """
    service = CaptureService(
        _camera_config(tmp_path / "captures"),
        MockBackend(width=640, height=480, name="mock"),
    )
    # Preview is disabled, so a start attempt fails fast -- but only *after* the
    # coordinator has taken its operation lock, which is the point.
    coordinator = CameraCoordinator(
        service, PreviewService(_preview_config(), MockPreviewBackend())
    )
    observed: dict[str, Any] = {}

    def _probe() -> None:
        try:
            coordinator.start_preview()
        except PreviewUnavailableError:
            observed["lock_was_free"] = True

    archive = _FakeArchive(probe=_probe)

    def _run() -> None:
        CaptureWorkflow(coordinator, archive).capture()
        observed["completed"] = True

    thread = threading.Thread(target=_run, name="workflow-lock-probe")
    thread.start()
    thread.join(timeout=10.0)

    assert thread.is_alive() is False, "the camera lock was held across the archive"
    assert observed.get("lock_was_free") is True
    assert observed.get("completed") is True


def test_the_workflow_knows_nothing_about_http() -> None:
    """The workflow is application/domain code, not a web layer.

    Asserted against the module's own AST rather than its behaviour: a workflow
    that imports FastAPI still works, right up until the second caller -- which
    is not a web request -- needs it to.
    """
    tree = ast.parse(WORKFLOW_SOURCE.read_bytes().decode("utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.add(node.module)

    forbidden = ("fastapi", "starlette", "mgo.api", "mgo.motion", "mgo.notifications")
    for module in imported:
        assert not module.startswith(forbidden), module

    # Identifiers only, read from the AST: prose in a docstring explaining what
    # the module deliberately does *not* know is not a violation of it.
    identifiers = {
        node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
    } | {
        node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
    }
    for token in ("HTTPException", "status_code", "Request", "Response"):
        assert token not in identifiers, token
