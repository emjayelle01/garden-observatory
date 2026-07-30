"""Tests for the camera capture layer.

None of these tests require camera hardware or the ``rpicam`` tooling. The
capture pipeline is exercised end to end through :class:`MockBackend`, and the
``RPiCamBackend`` failure mapping is verified with an injected fake command
runner.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from mgo.camera import (
    CameraUnavailableError,
    CaptureResult,
    CaptureService,
    CaptureTimeoutError,
    CaptureWriteError,
    ImageDimensions,
    MockBackend,
    NullBackend,
    RPiCamBackend,
    build_capture_backend,
    build_capture_filename,
)
from mgo.camera.backend import DEFAULT_CAPTURE_HEIGHT, DEFAULT_CAPTURE_WIDTH
from mgo.camera.exceptions import BackendCaptureError
from mgo.core.camera_detection import CommandOutcome, CommandResult
from mgo.core.config import CameraConfig

_FIXED_TIME = datetime(2026, 7, 24, 8, 15, 23, 397636, tzinfo=UTC)


def _camera_config(
    capture_directory: Path,
    *,
    enabled: bool = True,
    backend: str = "mock",
) -> CameraConfig:
    """Build a camera configuration pointing at a temp capture directory."""
    return CameraConfig(
        enabled=enabled,
        backend=backend,
        device_index=None,
        detection_interval_seconds=30,
        capture_directory=capture_directory,
    )


def _fixed_clock(moment: datetime = _FIXED_TIME):
    """Return a clock callable that always yields ``moment``."""
    return lambda: moment


def _fixed_runner(result: CommandResult):
    """Return a command runner that always yields ``result``."""

    def runner(args: Sequence[str], *, timeout: float) -> CommandResult:
        return result

    return runner


# --- filename generation --------------------------------------------------


def test_filename_is_deterministic_and_filesystem_safe() -> None:
    """Filenames use a UTC stamp with no spaces or path-hostile characters."""
    filename = build_capture_filename(_FIXED_TIME)

    assert filename == "2026-07-24T08-15-23.397636Z.jpg"
    assert " " not in filename
    assert ":" not in filename  # colons are invalid on Windows


def test_filename_includes_microseconds() -> None:
    """Filenames carry six-digit UTC microseconds and stay sortable."""
    pattern = re.compile(
        r"^\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}\.\d{6}Z\.jpg$"
    )

    assert pattern.match(build_capture_filename(_FIXED_TIME))


def test_same_second_timestamps_produce_distinct_filenames() -> None:
    """Two captures within the same second must not collide."""
    first = datetime(2026, 7, 24, 8, 15, 23, 100000, tzinfo=UTC)
    second = datetime(2026, 7, 24, 8, 15, 23, 900000, tzinfo=UTC)

    name_first = build_capture_filename(first)
    name_second = build_capture_filename(second)

    assert name_first != name_second
    # Chronological order is preserved by lexical order.
    assert name_first < name_second


def test_filename_normalises_to_utc() -> None:
    """A non-UTC timestamp is converted to UTC before formatting."""
    plus_two = timezone(timedelta(hours=2))
    local = datetime(2026, 7, 24, 10, 15, 23, 397636, tzinfo=plus_two)

    # 10:15:23.397636 at +02:00 is 08:15:23.397636Z.
    assert build_capture_filename(local) == "2026-07-24T08-15-23.397636Z.jpg"


# --- capture service ------------------------------------------------------


def test_successful_capture_returns_metadata(tmp_path: Path) -> None:
    """A successful capture returns fully-populated structured metadata."""
    backend = MockBackend(width=1920, height=1080, name="mock")
    service = CaptureService(
        _camera_config(tmp_path / "captures"),
        backend,
        clock=_fixed_clock(),
    )

    result = service.capture_image()

    assert isinstance(result, CaptureResult)
    assert result.success is True
    assert result.filename == "2026-07-24T08-15-23.397636Z.jpg"
    assert result.absolute_path.name == result.filename
    assert result.absolute_path.exists()
    assert result.timestamp == _FIXED_TIME
    assert result.width == 1920
    assert result.height == 1080
    assert result.filesize_bytes > 0
    assert result.backend == "mock"
    assert backend.captures == 1


def test_capture_creates_missing_directory(tmp_path: Path) -> None:
    """The capture directory is created if it does not already exist."""
    capture_dir = tmp_path / "deep" / "nested" / "captures"
    assert not capture_dir.exists()

    service = CaptureService(
        _camera_config(capture_dir),
        MockBackend(),
        clock=_fixed_clock(),
    )
    result = service.capture_image()

    assert capture_dir.is_dir()
    assert result.absolute_path.parent == capture_dir.resolve()


def test_capture_writes_beneath_capture_directory(tmp_path: Path) -> None:
    """The stored image must live beneath the configured capture directory."""
    capture_dir = tmp_path / "captures"
    service = CaptureService(
        _camera_config(capture_dir),
        MockBackend(),
        clock=_fixed_clock(),
    )

    result = service.capture_image()

    assert result.absolute_path.parent == capture_dir.resolve()


def test_capture_result_serialises(tmp_path: Path) -> None:
    """The result serialises to plain JSON-compatible values."""
    service = CaptureService(
        _camera_config(tmp_path / "captures"),
        MockBackend(width=4608, height=2592),
        clock=_fixed_clock(),
    )

    payload = service.capture_image().as_dict()

    assert payload["success"] is True
    assert payload["filename"] == "2026-07-24T08-15-23.397636Z.jpg"
    assert payload["timestamp"] == "2026-07-24T08:15:23.397636+00:00"
    assert payload["width"] == 4608
    assert payload["height"] == 2592
    assert isinstance(payload["absolute_path"], str)
    assert payload["filesize_bytes"] > 0


def test_disabled_camera_cannot_capture(tmp_path: Path) -> None:
    """Capturing while disabled raises without invoking the backend."""
    backend = MockBackend()
    service = CaptureService(
        _camera_config(tmp_path / "captures", enabled=False),
        backend,
        clock=_fixed_clock(),
    )

    with pytest.raises(CameraUnavailableError):
        service.capture_image()

    assert backend.captures == 0


def test_backend_failure_propagates_as_domain_error(tmp_path: Path) -> None:
    """A backend failure surfaces as an explicit capture exception."""
    backend = MockBackend(error=BackendCaptureError("sensor exploded"))
    service = CaptureService(
        _camera_config(tmp_path / "captures"),
        backend,
        clock=_fixed_clock(),
    )

    with pytest.raises(BackendCaptureError, match="sensor exploded"):
        service.capture_image()


def test_directory_creation_failure_is_write_error(tmp_path: Path) -> None:
    """If the capture directory can't be created, a write error is raised.

    A file is placed where the capture directory should be, so ``mkdir`` fails.
    """
    blocker = tmp_path / "captures"
    blocker.write_text("I am a file, not a directory")

    service = CaptureService(
        _camera_config(blocker),
        MockBackend(),
        clock=_fixed_clock(),
    )

    with pytest.raises(CaptureWriteError):
        service.capture_image()


def test_backend_success_without_file_is_write_error(tmp_path: Path) -> None:
    """A backend that reports success without a file is a write error.

    The mock is configured to skip writing but still return dimensions; the
    service must not trust that and should fail when the file is missing at
    filesize time. (RPiCamBackend guards this itself; here we cover the
    service's own filesize guard via a backend that skips both.)
    """
    backend = MockBackend(write_file=False)
    service = CaptureService(
        _camera_config(tmp_path / "captures"),
        backend,
        clock=_fixed_clock(),
    )

    with pytest.raises(CaptureWriteError):
        service.capture_image()


# --- failed-capture cleanup -----------------------------------------------


def _jpg_files(directory: Path) -> list[Path]:
    """Return the capture files currently present in ``directory``."""
    return list(directory.glob("*.jpg")) if directory.exists() else []


def test_partial_file_removed_after_backend_failure(tmp_path: Path) -> None:
    """A partial file left by a failing backend is removed."""
    capture_dir = tmp_path / "captures"
    backend = MockBackend(payload=b"partial-bytes", error=BackendCaptureError("boom"))
    service = CaptureService(_camera_config(capture_dir), backend, clock=_fixed_clock())

    with pytest.raises(BackendCaptureError, match="boom"):
        service.capture_image()

    assert _jpg_files(capture_dir) == []


def test_empty_file_removed_and_raises_write_error(tmp_path: Path) -> None:
    """A zero-byte output is removed and reported as a write error.

    The service must catch this itself, independent of the backend.
    """
    capture_dir = tmp_path / "captures"
    backend = MockBackend(payload=b"")  # writes an empty file, no error
    service = CaptureService(_camera_config(capture_dir), backend, clock=_fixed_clock())

    with pytest.raises(CaptureWriteError):
        service.capture_image()

    assert _jpg_files(capture_dir) == []


def test_timeout_removes_partial_file(tmp_path: Path) -> None:
    """A capture timeout removes any partial file that was created."""
    capture_dir = tmp_path / "captures"
    backend = MockBackend(
        payload=b"partial-bytes",
        error=CaptureTimeoutError("timed out"),
    )
    service = CaptureService(_camera_config(capture_dir), backend, clock=_fixed_clock())

    with pytest.raises(CaptureTimeoutError):
        service.capture_image()

    assert _jpg_files(capture_dir) == []


def test_successful_capture_file_is_retained(tmp_path: Path) -> None:
    """A successful capture leaves its file in place."""
    capture_dir = tmp_path / "captures"
    service = CaptureService(
        _camera_config(capture_dir), MockBackend(), clock=_fixed_clock()
    )

    result = service.capture_image()

    assert result.absolute_path.exists()
    assert _jpg_files(capture_dir) == [result.absolute_path]


def test_cleanup_failure_does_not_hide_original_exception(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """If cleanup itself fails, the original capture error still propagates."""
    capture_dir = tmp_path / "captures"
    backend = MockBackend(
        payload=b"partial-bytes",
        error=BackendCaptureError("original failure"),
    )
    service = CaptureService(_camera_config(capture_dir), backend, clock=_fixed_clock())

    def _failing_unlink(self: Path, *args: object, **kwargs: object) -> None:
        raise OSError("cannot remove file")

    monkeypatch.setattr(Path, "unlink", _failing_unlink)

    with (
        caplog.at_level(logging.WARNING),
        pytest.raises(BackendCaptureError, match="original failure"),
    ):
        service.capture_image()

    # The cleanup failure is logged, never raised in place of the real error.
    assert any(
        "Failed to remove partial capture file" in record.message
        for record in caplog.records
    )


# --- backends -------------------------------------------------------------


def test_null_backend_reports_unavailable(tmp_path: Path) -> None:
    """The null backend never produces an image."""
    with pytest.raises(CameraUnavailableError):
        NullBackend().capture(tmp_path / "x.jpg")


def test_mock_backend_writes_real_bytes(tmp_path: Path) -> None:
    """The mock backend writes non-empty bytes and reports dimensions."""
    destination = tmp_path / "x.jpg"

    dimensions = MockBackend(width=640, height=480).capture(destination)

    assert dimensions == ImageDimensions(640, 480)
    assert destination.stat().st_size > 0


def test_rpicam_backend_missing_command_is_unavailable(tmp_path: Path) -> None:
    """A missing capture command maps to camera-unavailable."""
    backend = RPiCamBackend(
        "rpicam-still",
        runner=_fixed_runner(CommandResult(CommandOutcome.NOT_FOUND, None, "", "")),
    )

    with pytest.raises(CameraUnavailableError, match="not installed"):
        backend.capture(tmp_path / "x.jpg")


def test_rpicam_backend_timeout_maps_to_timeout(tmp_path: Path) -> None:
    """A capture timeout maps to a capture-timeout error."""
    backend = RPiCamBackend(
        "rpicam-still",
        timeout=4.0,
        runner=_fixed_runner(CommandResult(CommandOutcome.TIMED_OUT, None, "", "")),
    )

    with pytest.raises(CaptureTimeoutError, match="timed out"):
        backend.capture(tmp_path / "x.jpg")


def test_rpicam_backend_no_camera_is_unavailable(tmp_path: Path) -> None:
    """A non-zero exit reporting no cameras maps to unavailable."""
    backend = RPiCamBackend(
        "rpicam-still",
        runner=_fixed_runner(
            CommandResult(CommandOutcome.COMPLETED, 1, "", "No cameras available!")
        ),
    )

    with pytest.raises(CameraUnavailableError, match="No camera detected"):
        backend.capture(tmp_path / "x.jpg")


def test_rpicam_backend_nonzero_exit_is_backend_error(tmp_path: Path) -> None:
    """Any other non-zero exit maps to a backend error with a hint."""
    backend = RPiCamBackend(
        "rpicam-still",
        runner=_fixed_runner(
            CommandResult(CommandOutcome.COMPLETED, 70, "", "fatal: bad tuning file")
        ),
    )

    with pytest.raises(BackendCaptureError, match="bad tuning file"):
        backend.capture(tmp_path / "x.jpg")


def test_rpicam_backend_success_without_file_is_write_error(
    tmp_path: Path,
) -> None:
    """A zero exit that produced no file is a write error, not a success."""
    backend = RPiCamBackend(
        "rpicam-still",
        runner=_fixed_runner(CommandResult(CommandOutcome.COMPLETED, 0, "", "")),
    )

    with pytest.raises(CaptureWriteError):
        backend.capture(tmp_path / "missing.jpg")


def test_rpicam_backend_success_returns_dimensions(tmp_path: Path) -> None:
    """A successful capture that wrote a file returns configured dimensions."""
    destination = tmp_path / "x.jpg"

    def runner(args: Sequence[str], *, timeout: float) -> CommandResult:
        # Simulate the tool writing an output file.
        Path(args[-1]).write_bytes(b"\xff\xd8\xff\xd9")
        return CommandResult(CommandOutcome.COMPLETED, 0, "", "")

    backend = RPiCamBackend("rpicam-still", width=800, height=600, runner=runner)

    dimensions = backend.capture(destination)

    assert dimensions == ImageDimensions(800, 600)
    assert destination.exists()


def test_rpicam_backend_builds_expected_args(tmp_path: Path) -> None:
    """The capture command is a shell-free argument array with output path."""
    captured: dict[str, Sequence[str]] = {}

    def runner(args: Sequence[str], *, timeout: float) -> CommandResult:
        captured["args"] = tuple(args)
        Path(args[-1]).write_bytes(b"\xff\xd8\xff\xd9")
        return CommandResult(CommandOutcome.COMPLETED, 0, "", "")

    destination = tmp_path / "x.jpg"
    RPiCamBackend("rpicam-still", runner=runner).capture(destination)

    args = captured["args"]
    assert args[0] == "rpicam-still"
    assert "--nopreview" in args
    assert "--output" in args
    assert args[-1] == str(destination)
    assert str(DEFAULT_CAPTURE_WIDTH) in args
    assert str(DEFAULT_CAPTURE_HEIGHT) in args


@pytest.mark.parametrize("command", ["rpicam-still", "libcamera-still"])
def test_the_capture_command_array_is_exactly_preserved(
    command: str, tmp_path: Path
) -> None:
    """Task 12 adds no argument to the production still-capture command.

    Pinned as an *exact* array rather than a membership check: the physical
    camera acceptance procedure assesses the current default autofocus and
    exposure behaviour first, so no tuning flag may appear before then.
    """
    captured: dict[str, Sequence[str]] = {}

    def runner(args: Sequence[str], *, timeout: float) -> CommandResult:
        captured["args"] = tuple(args)
        Path(args[-1]).write_bytes(b"\xff\xd8\xff\xd9")
        return CommandResult(CommandOutcome.COMPLETED, 0, "", "")

    destination = tmp_path / "x.jpg"
    RPiCamBackend(command, runner=runner).capture(destination)

    assert captured["args"] == (
        command,
        "--nopreview",
        "--timeout",
        "2000",
        "--width",
        str(DEFAULT_CAPTURE_WIDTH),
        "--height",
        str(DEFAULT_CAPTURE_HEIGHT),
        "--output",
        str(destination),
    )
    for forbidden in (
        "--autofocus-mode",
        "--autofocus-range",
        "--autofocus-speed",
        "--autofocus-window",
        "--autofocus-on-capture",
        "--lens-position",
        "--exposure",
        "--awb",
        "--roi",
    ):
        assert forbidden not in captured["args"]


# --- backend factory ------------------------------------------------------


def test_build_capture_backend_selects_backend(tmp_path: Path) -> None:
    """The factory maps backend names to the right capture adapters."""
    rpicam = build_capture_backend(_camera_config(tmp_path, backend="rpicam"))
    libcamera = build_capture_backend(
        _camera_config(tmp_path, backend="libcamera")
    )
    null = build_capture_backend(_camera_config(tmp_path, backend="null"))

    assert isinstance(rpicam, RPiCamBackend)
    assert rpicam.name == "rpicam-still"
    assert isinstance(libcamera, RPiCamBackend)
    assert libcamera.name == "libcamera-still"
    assert isinstance(null, NullBackend)


def test_build_capture_backend_rejects_unknown(tmp_path: Path) -> None:
    """An unknown backend name raises a clear error."""
    with pytest.raises(ValueError, match="Unsupported camera backend"):
        build_capture_backend(_camera_config(tmp_path, backend="webcam"))
