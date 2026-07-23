"""Tests for core camera readiness logic and detection adapters."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest

from mgo.core.camera import (
    CameraReadiness,
    CameraStatus,
    DetectionEvidence,
    DetectionOutcome,
    default_readiness,
    detect_camera_readiness,
)
from mgo.core.camera_detection import (
    CommandCameraDetector,
    CommandOutcome,
    CommandResult,
    NullCameraDetector,
    build_detector,
)
from mgo.core.config import CameraConfig


def _camera_config(
    *,
    enabled: bool = True,
    backend: str = "rpicam",
    device_index: int | None = None,
) -> CameraConfig:
    """Build a camera configuration for tests."""
    return CameraConfig(
        enabled=enabled,
        backend=backend,
        device_index=device_index,
        detection_interval_seconds=30,
        capture_directory=Path("data/captures"),
    )


class _RecordingDetector:
    """A detector that returns fixed evidence and counts invocations."""

    def __init__(self, evidence: DetectionEvidence) -> None:
        self.evidence = evidence
        self.calls = 0

    def detect(self, config: CameraConfig) -> DetectionEvidence:
        self.calls += 1
        return self.evidence


class _RaisingDetector:
    """A detector that always raises to simulate an adapter failure."""

    def __init__(self) -> None:
        self.calls = 0

    def detect(self, config: CameraConfig) -> DetectionEvidence:
        self.calls += 1
        raise RuntimeError("adapter exploded")


def _fixed_runner(result: CommandResult):
    """Return a command runner that always yields ``result``."""

    def runner(args: Sequence[str], *, timeout: float) -> CommandResult:
        return result

    return runner


# --- core readiness logic -------------------------------------------------


def test_disabled_camera_does_not_call_detector() -> None:
    """When disabled, detection must not be attempted."""
    detector = _RaisingDetector()

    readiness = detect_camera_readiness(_camera_config(enabled=False), detector)

    assert detector.calls == 0
    assert readiness.status is CameraStatus.DISABLED
    assert readiness.available is False


def test_enabled_without_hardware_is_waiting() -> None:
    """A 'not detected' outcome maps to waiting_for_hardware."""
    detector = _RecordingDetector(
        DetectionEvidence(DetectionOutcome.NOT_DETECTED, "no camera")
    )

    readiness = detect_camera_readiness(_camera_config(), detector)

    assert detector.calls == 1
    assert readiness.status is CameraStatus.WAITING_FOR_HARDWARE
    assert readiness.available is False


def test_detected_hardware_is_available() -> None:
    """A 'detected' outcome maps to available with available=True."""
    detector = _RecordingDetector(
        DetectionEvidence(DetectionOutcome.DETECTED, "found imx708")
    )

    readiness = detect_camera_readiness(_camera_config(), detector)

    assert readiness.status is CameraStatus.AVAILABLE
    assert readiness.available is True
    assert "imx708" in readiness.detail


def test_detector_exception_becomes_error() -> None:
    """An adapter exception must be captured as an error readiness."""
    detector = _RaisingDetector()

    readiness = detect_camera_readiness(_camera_config(), detector)

    assert detector.calls == 1
    assert readiness.status is CameraStatus.ERROR
    assert readiness.available is False
    assert "adapter exploded" in readiness.detail


def test_error_evidence_becomes_error() -> None:
    """Explicit error evidence maps to error status."""
    detector = _RecordingDetector(
        DetectionEvidence(DetectionOutcome.ERROR, "permission denied")
    )

    readiness = detect_camera_readiness(_camera_config(), detector)

    assert readiness.status is CameraStatus.ERROR
    assert readiness.available is False


def test_default_readiness_reflects_configuration() -> None:
    """The safe default should honour the enabled flag."""
    disabled = default_readiness(_camera_config(enabled=False))
    enabled = default_readiness(_camera_config(enabled=True))

    assert disabled.status is CameraStatus.DISABLED
    assert enabled.status is CameraStatus.WAITING_FOR_HARDWARE
    assert enabled.available is False


def test_readiness_as_dict_is_serialisable() -> None:
    """The readiness result serialises to plain JSON-compatible values."""
    readiness = CameraReadiness(
        enabled=True,
        backend="rpicam",
        status=CameraStatus.AVAILABLE,
        available=True,
        detail="ok",
        checked_at=datetime(2026, 7, 23, tzinfo=UTC),
    )

    payload = readiness.as_dict()

    assert payload == {
        "enabled": True,
        "backend": "rpicam",
        "status": "available",
        "available": True,
        "detail": "ok",
        "checked_at": "2026-07-23T00:00:00+00:00",
    }


# --- command-based adapter ------------------------------------------------


def test_missing_command_reports_waiting_not_error() -> None:
    """A missing Raspberry Pi command is an expected non-crash condition.

    Semantics: on Windows/CI without rpicam tooling, absence of the command
    means no camera can be confirmed, i.e. waiting_for_hardware -- not an
    application error.
    """
    detector = CommandCameraDetector(
        ("rpicam-hello", "--list-cameras"),
        runner=_fixed_runner(
            CommandResult(CommandOutcome.NOT_FOUND, None, "", "")
        ),
    )

    readiness = detect_camera_readiness(_camera_config(), detector)

    assert readiness.status is CameraStatus.WAITING_FOR_HARDWARE
    assert readiness.available is False


def test_command_timeout_is_error() -> None:
    """A detection timeout must be handled safely as an error."""
    detector = CommandCameraDetector(
        ("rpicam-hello", "--list-cameras"),
        runner=_fixed_runner(
            CommandResult(CommandOutcome.TIMED_OUT, None, "", "")
        ),
        timeout=3.0,
    )

    readiness = detect_camera_readiness(_camera_config(), detector)

    assert readiness.status is CameraStatus.ERROR
    assert "timed out" in readiness.detail.lower()


def test_malformed_output_is_error() -> None:
    """Unparseable successful output is handled safely as an error."""
    detector = CommandCameraDetector(
        ("rpicam-hello", "--list-cameras"),
        runner=_fixed_runner(
            CommandResult(CommandOutcome.COMPLETED, 0, "unexpected noise", "")
        ),
    )

    readiness = detect_camera_readiness(_camera_config(), detector)

    assert readiness.status is CameraStatus.ERROR


def test_no_cameras_message_is_waiting() -> None:
    """The tool's explicit 'no cameras' message maps to waiting."""
    detector = CommandCameraDetector(
        ("rpicam-hello", "--list-cameras"),
        runner=_fixed_runner(
            CommandResult(CommandOutcome.COMPLETED, 0, "No cameras available!", "")
        ),
    )

    readiness = detect_camera_readiness(_camera_config(), detector)

    assert readiness.status is CameraStatus.WAITING_FOR_HARDWARE


def test_enumerated_camera_is_available() -> None:
    """Evidence of an enumerated device maps to available."""
    output = (
        "Available cameras\n"
        "-----------------\n"
        "0 : imx708 [4608x2592] (/base/soc/i2c0mux/i2c@1/imx708@1a)\n"
    )
    detector = CommandCameraDetector(
        ("rpicam-hello", "--list-cameras"),
        runner=_fixed_runner(
            CommandResult(CommandOutcome.COMPLETED, 0, output, "")
        ),
    )

    readiness = detect_camera_readiness(_camera_config(), detector)

    assert readiness.status is CameraStatus.AVAILABLE
    assert readiness.available is True
    assert "imx708" in readiness.detail


_MULTI_CAMERA_OUTPUT = (
    "Available cameras\n"
    "-----------------\n"
    "0 : imx708 [4608x2592] (/base/soc/i2c0mux/i2c@1/imx708@1a)\n"
    "1 : imx500 [4056x3040] (/base/soc/i2c0mux/i2c@0/imx500@1a)\n"
)


def _multi_camera_detector(device_index: int | None) -> CommandCameraDetector:
    """A command detector serving two enumerated cameras (indexes 0 and 1)."""
    return CommandCameraDetector(
        ("rpicam-hello", "--list-cameras"),
        runner=_fixed_runner(
            CommandResult(CommandOutcome.COMPLETED, 0, _MULTI_CAMERA_OUTPUT, "")
        ),
    )


def test_unset_device_index_reports_available() -> None:
    """With no configured index, any enumerated camera is available."""
    readiness = detect_camera_readiness(
        _camera_config(device_index=None),
        _multi_camera_detector(None),
    )

    assert readiness.status is CameraStatus.AVAILABLE
    assert readiness.available is True


def test_configured_device_index_present_is_available() -> None:
    """A configured index that is enumerated reports available."""
    readiness = detect_camera_readiness(
        _camera_config(device_index=1),
        _multi_camera_detector(1),
    )

    assert readiness.status is CameraStatus.AVAILABLE
    assert readiness.available is True
    assert "device_index 1" in readiness.detail
    assert "imx500" in readiness.detail


def test_configured_device_index_absent_is_waiting() -> None:
    """A configured index that is not enumerated waits for hardware."""
    readiness = detect_camera_readiness(
        _camera_config(device_index=3),
        _multi_camera_detector(3),
    )

    assert readiness.status is CameraStatus.WAITING_FOR_HARDWARE
    assert readiness.available is False
    assert "device_index 3" in readiness.detail
    # Detail should truthfully report which indexes were present.
    assert "0" in readiness.detail
    assert "1" in readiness.detail


def test_multiple_devices_select_configured_index() -> None:
    """With multiple devices, the configured index selects the right one."""
    readiness_zero = detect_camera_readiness(
        _camera_config(device_index=0),
        _multi_camera_detector(0),
    )
    readiness_one = detect_camera_readiness(
        _camera_config(device_index=1),
        _multi_camera_detector(1),
    )

    assert readiness_zero.available is True
    assert "imx708" in readiness_zero.detail
    assert readiness_one.available is True
    assert "imx500" in readiness_one.detail


def test_nonzero_exit_is_error() -> None:
    """A non-zero exit code is treated as a detection error."""
    detector = CommandCameraDetector(
        ("rpicam-hello", "--list-cameras"),
        runner=_fixed_runner(
            CommandResult(CommandOutcome.COMPLETED, 1, "", "fatal: bad args")
        ),
    )

    readiness = detect_camera_readiness(_camera_config(), detector)

    assert readiness.status is CameraStatus.ERROR
    assert "1" in readiness.detail


def test_null_backend_never_detects() -> None:
    """The null backend reports waiting without touching any command."""
    readiness = detect_camera_readiness(_camera_config(), NullCameraDetector())

    assert readiness.status is CameraStatus.WAITING_FOR_HARDWARE


def test_build_detector_selects_backend() -> None:
    """The detector factory maps backend names to adapters."""
    assert isinstance(build_detector("rpicam"), CommandCameraDetector)
    assert isinstance(build_detector("libcamera"), CommandCameraDetector)
    assert isinstance(build_detector("null"), NullCameraDetector)


def test_build_detector_rejects_unknown_backend() -> None:
    """An unknown backend name raises a clear error."""
    with pytest.raises(ValueError, match="Unsupported camera backend"):
        build_detector("webcam")
