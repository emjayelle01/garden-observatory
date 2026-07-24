"""Capture backend abstraction for the camera layer.

A *backend* is the only component that knows how a physical (or simulated)
still image is produced. The capture service depends solely on the
:class:`CaptureBackend` protocol and never on a concrete implementation, so new
hardware paths can be added without touching the service or the API.

Backends translate every expected operating-system failure into an explicit
:mod:`mgo.camera.exceptions` error. Raw ``subprocess`` failures must never
escape a backend.

Implementations provided here:

* :class:`RPiCamBackend` -- captures with a Raspberry Pi ``*-still`` command
  (``rpicam-still`` on Bookworm, ``libcamera-still`` on older stacks);
* :class:`MockBackend` -- writes deterministic bytes with no hardware, for
  development and tests;
* :class:`NullBackend` -- always reports the camera as unavailable.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from mgo.camera.exceptions import (
    BackendCaptureError,
    CameraUnavailableError,
    CaptureTimeoutError,
    CaptureWriteError,
)
from mgo.camera.models import ImageDimensions
from mgo.core.camera_detection import (
    CommandOutcome,
    CommandResult,
    CommandRunner,
    run_subprocess,
)
from mgo.core.config import CameraConfig

#: Default still resolution: the full-frame output of the Camera Module 3
#: (Sony IMX708). Chosen so metadata is deterministic and honest about what the
#: backend explicitly requests from the capture tool.
DEFAULT_CAPTURE_WIDTH = 4608
DEFAULT_CAPTURE_HEIGHT = 2592

#: How long to let the sensor's auto-exposure/white-balance settle before the
#: still is taken (milliseconds). Passed to the capture tool as its timeout.
DEFAULT_WARMUP_MS = 2000

#: Wall-clock ceiling for the whole capture subprocess (seconds). Comfortably
#: larger than the warm-up so a healthy capture never trips it.
DEFAULT_CAPTURE_TIMEOUT_SECONDS = 30.0

#: Markers a capture tool prints when no camera is connected.
_NO_CAMERA_MARKERS = ("no cameras available", "no cameras found")


class CaptureBackend(Protocol):
    """A hardware/OS-specific still-capture adapter.

    A backend writes exactly one image to ``destination`` and returns the
    dimensions it produced. It must raise a :mod:`mgo.camera.exceptions` error
    on any failure and must never raise raw subprocess/OS exceptions.
    """

    @property
    def name(self) -> str:
        """A short, stable identifier for this backend (for metadata)."""
        ...

    def capture(self, destination: Path) -> ImageDimensions:
        """Capture one still to ``destination`` and report its dimensions."""
        ...


def _first_line(text: str) -> str:
    """Return the first non-empty line of text, or an empty string."""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


class RPiCamBackend:
    """Captures a still via a Raspberry Pi ``*-still`` command.

    The command is run as an argument array with a bounded timeout (never via a
    shell). Every failure mode of the subprocess is mapped to an explicit
    capture exception, and a "successful" run that produced no usable file is
    treated as a write error rather than being trusted blindly.
    """

    def __init__(
        self,
        command: str,
        *,
        width: int = DEFAULT_CAPTURE_WIDTH,
        height: int = DEFAULT_CAPTURE_HEIGHT,
        warmup_ms: int = DEFAULT_WARMUP_MS,
        timeout: float = DEFAULT_CAPTURE_TIMEOUT_SECONDS,
        runner: CommandRunner = run_subprocess,
    ) -> None:
        self._command = command
        self._width = width
        self._height = height
        self._warmup_ms = warmup_ms
        self._timeout = timeout
        self._runner = runner

    @property
    def name(self) -> str:
        return self._command

    def _build_args(self, destination: Path) -> Sequence[str]:
        """Assemble the capture command's argument array."""
        return (
            self._command,
            "--nopreview",
            "--timeout",
            str(self._warmup_ms),
            "--width",
            str(self._width),
            "--height",
            str(self._height),
            "--output",
            str(destination),
        )

    def capture(self, destination: Path) -> ImageDimensions:
        """Capture a still, mapping every failure mode to a domain error."""
        result = self._runner(self._build_args(destination), timeout=self._timeout)
        self._raise_for_result(result)
        self._verify_output(destination)
        return ImageDimensions(width=self._width, height=self._height)

    def _raise_for_result(self, result: CommandResult) -> None:
        """Translate a command result into a capture exception, if needed."""
        if result.outcome is CommandOutcome.NOT_FOUND:
            raise CameraUnavailableError(
                f"Capture tool '{self._command}' is not installed."
            )

        if result.outcome is CommandOutcome.TIMED_OUT:
            raise CaptureTimeoutError(
                f"Capture with '{self._command}' timed out after "
                f"{self._timeout:g}s."
            )

        if result.returncode != 0:
            combined = f"{result.stdout}\n{result.stderr}".lower()
            if any(marker in combined for marker in _NO_CAMERA_MARKERS):
                raise CameraUnavailableError(
                    "No camera detected by the capture tool."
                )
            hint = _first_line(result.stderr) or _first_line(result.stdout)
            suffix = f": {hint}" if hint else "."
            raise BackendCaptureError(
                f"'{self._command}' exited with code {result.returncode}{suffix}"
            )

    def _verify_output(self, destination: Path) -> None:
        """Ensure the command actually produced a non-empty image file."""
        try:
            size = destination.stat().st_size
        except OSError as exc:
            raise CaptureWriteError(
                f"Capture tool reported success but no file was found at "
                f"{destination}: {exc}"
            ) from exc

        if size <= 0:
            raise CaptureWriteError(
                f"Capture produced an empty file at {destination}."
            )


#: Deterministic placeholder image bytes for the mock backend. This is not a
#: decodable photo; it simply carries the JPEG SOI/EOI markers around a small,
#: fixed body so the mock writes real, non-empty bytes and filesize metadata is
#: genuine — without pulling in an imaging dependency.
_MOCK_JPEG_BYTES = b"\xff\xd8" + b"MGO-MOCK-CAPTURE" * 8 + b"\xff\xd9"


class MockBackend:
    """A hardware-free backend that writes deterministic image bytes.

    It is the primary tool for testing the capture pipeline: capture, filename
    generation, directory creation and metadata are all exercised without any
    camera. An optional ``error`` lets tests simulate a backend failure, and an
    optional ``write_file=False`` simulates a tool that reports success without
    producing a file.
    """

    def __init__(
        self,
        *,
        width: int = DEFAULT_CAPTURE_WIDTH,
        height: int = DEFAULT_CAPTURE_HEIGHT,
        name: str = "mock",
        payload: bytes = _MOCK_JPEG_BYTES,
        error: Exception | None = None,
        write_file: bool = True,
    ) -> None:
        self._width = width
        self._height = height
        self._name = name
        self._payload = payload
        self._error = error
        self._write_file = write_file
        self.captures = 0

    @property
    def name(self) -> str:
        return self._name

    def capture(self, destination: Path) -> ImageDimensions:
        """Write deterministic bytes then optionally raise the configured error.

        Writing before raising lets tests simulate a backend that leaves a
        partial file behind on failure, exercising the service's cleanup path.
        """
        self.captures += 1
        if self._write_file:
            destination.write_bytes(self._payload)
        if self._error is not None:
            raise self._error
        return ImageDimensions(width=self._width, height=self._height)


class NullBackend:
    """A backend that always reports the camera as unavailable.

    Useful where capture must be wired up but no hardware should ever be used
    (for example a deployment with the camera intentionally absent).
    """

    @property
    def name(self) -> str:
        return "null"

    def capture(self, destination: Path) -> ImageDimensions:
        """Always raise :class:`CameraUnavailableError`."""
        raise CameraUnavailableError(
            "Null capture backend never produces images."
        )


def build_capture_backend(config: CameraConfig) -> CaptureBackend:
    """Construct the capture backend for a configured backend name.

    Mirrors ``mgo.core.camera_detection.build_detector`` so the detection and
    capture layers agree on backend vocabulary. ``rpicam``/``libcamera`` select
    the matching ``*-still`` command; ``null``/``none`` select
    :class:`NullBackend`.
    """
    normalized = config.backend.strip().lower()

    if normalized == "rpicam":
        return RPiCamBackend("rpicam-still")
    if normalized == "libcamera":
        return RPiCamBackend("libcamera-still")
    if normalized in {"null", "none"}:
        return NullBackend()

    raise ValueError(f"Unsupported camera backend: {config.backend!r}")
