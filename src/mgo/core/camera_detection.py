"""Operating-system camera detection adapters for MGO.

This module contains the only code that knows how to probe real hardware. It
runs bounded, non-shell subprocess commands (``rpicam``/``libcamera`` tools)
and translates their results into hardware-agnostic
:class:`~mgo.core.camera.DetectionEvidence`.

Design rules enforced here:

* argument arrays only -- never ``shell=True``;
* every external command has a bounded timeout;
* command-not-found, non-zero exit, timeout, and malformed output are all
  handled without raising into the core logic;
* Windows and CI hosts without Raspberry Pi tooling stay supported: a missing
  command is reported as "no camera detected", not as an application error.
"""

from __future__ import annotations

import logging
import re
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from mgo.core.camera import CameraDetector, DetectionEvidence, DetectionOutcome
from mgo.core.config import CameraConfig

LOGGER = logging.getLogger(__name__)

_DEFAULT_TIMEOUT_SECONDS = 5.0
_CAMERA_ENTRY_PATTERN = re.compile(r"^\s*(?P<index>\d+)\s*:\s*\S")
_NO_CAMERA_MARKERS = ("no cameras available", "no cameras found")


class CommandOutcome(Enum):
    """The result category of attempting to run an external command."""

    COMPLETED = "completed"
    NOT_FOUND = "not_found"
    TIMED_OUT = "timed_out"


@dataclass(frozen=True)
class CommandResult:
    """A safely captured result of a bounded subprocess invocation."""

    outcome: CommandOutcome
    returncode: int | None
    stdout: str
    stderr: str


class CommandRunner(Protocol):
    """Runs an argument-array command with a bounded timeout."""

    def __call__(
        self,
        args: Sequence[str],
        *,
        timeout: float,
    ) -> CommandResult:
        """Execute ``args`` and return a captured :class:`CommandResult`."""
        ...


def run_subprocess(args: Sequence[str], *, timeout: float) -> CommandResult:
    """Run a command as an argument array, never via a shell.

    All expected failure modes are converted into a :class:`CommandResult`
    rather than being raised, so callers can reason about outcomes explicitly.
    """
    try:
        completed = subprocess.run(
            list(args),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        return CommandResult(CommandOutcome.NOT_FOUND, None, "", "")
    except subprocess.TimeoutExpired:
        return CommandResult(CommandOutcome.TIMED_OUT, None, "", "")

    return CommandResult(
        CommandOutcome.COMPLETED,
        completed.returncode,
        completed.stdout or "",
        completed.stderr or "",
    )


def _first_line(text: str) -> str:
    """Return the first non-empty line of text, or an empty string."""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def _parse_camera_entries(text: str) -> list[tuple[int, str]]:
    """Return ``(index, line)`` pairs for each enumerated camera device."""
    entries: list[tuple[int, str]] = []
    for line in text.splitlines():
        match = _CAMERA_ENTRY_PATTERN.match(line)
        if match is not None:
            entries.append((int(match.group("index")), line.strip()))
    return entries


def _interpret_output(
    command_label: str,
    stdout: str,
    device_index: int | None,
) -> DetectionEvidence:
    """Translate successful command output into detection evidence.

    When ``device_index`` is ``None`` any enumerated camera counts as
    available. When a specific index is configured, availability requires that
    exact index to be enumerated; otherwise readiness waits for hardware and
    the detail reports which indexes were actually present.
    """
    text = stdout.strip()
    if not text:
        return DetectionEvidence(
            DetectionOutcome.ERROR,
            f"{command_label} produced no output to evaluate.",
        )

    lowered = text.lower()
    if any(marker in lowered for marker in _NO_CAMERA_MARKERS):
        return DetectionEvidence(
            DetectionOutcome.NOT_DETECTED,
            "No cameras available; waiting for hardware.",
        )

    cameras = _parse_camera_entries(text)
    if not cameras:
        return DetectionEvidence(
            DetectionOutcome.ERROR,
            f"{command_label} output could not be parsed for camera devices.",
        )

    if device_index is None:
        summary = "; ".join(line for _, line in cameras)
        return DetectionEvidence(
            DetectionOutcome.DETECTED,
            f"Detected camera device(s): {summary}",
        )

    for index, line in cameras:
        if index == device_index:
            return DetectionEvidence(
                DetectionOutcome.DETECTED,
                f"Configured camera device_index {device_index} is present: {line}",
            )

    enumerated = ", ".join(str(index) for index, _ in cameras)
    return DetectionEvidence(
        DetectionOutcome.NOT_DETECTED,
        (
            f"Configured camera device_index {device_index} was not found; "
            f"enumerated indexes: {enumerated}."
        ),
    )


class CommandCameraDetector:
    """Detects cameras via a Raspberry Pi camera-listing command.

    Works with any tool whose ``--list-cameras`` output follows the
    ``rpicam``/``libcamera`` convention (numbered device entries, or an
    explicit "no cameras available" message).
    """

    def __init__(
        self,
        command: Sequence[str],
        *,
        runner: CommandRunner = run_subprocess,
        timeout: float = _DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._command = tuple(command)
        self._runner = runner
        self._timeout = timeout

    @property
    def _label(self) -> str:
        return self._command[0] if self._command else "camera command"

    def detect(self, config: CameraConfig) -> DetectionEvidence:
        """Probe for cameras, mapping every failure mode to safe evidence."""
        result = self._runner(self._command, timeout=self._timeout)

        if result.outcome is CommandOutcome.NOT_FOUND:
            return DetectionEvidence(
                DetectionOutcome.NOT_DETECTED,
                (
                    f"Camera tooling '{self._label}' is not installed; "
                    "no camera hardware detected."
                ),
            )

        if result.outcome is CommandOutcome.TIMED_OUT:
            return DetectionEvidence(
                DetectionOutcome.ERROR,
                f"Camera detection timed out after {self._timeout:g}s.",
            )

        if result.returncode != 0:
            stderr_hint = _first_line(result.stderr)
            suffix = f": {stderr_hint}" if stderr_hint else "."
            return DetectionEvidence(
                DetectionOutcome.ERROR,
                (
                    f"'{self._label}' exited with code "
                    f"{result.returncode}{suffix}"
                ),
            )

        return _interpret_output(self._label, result.stdout, config.device_index)


class NullCameraDetector:
    """A detector that never finds hardware.

    Useful for development or CI hosts where the camera path should be
    exercised as "enabled but no hardware" without invoking any command.
    """

    def detect(self, config: CameraConfig) -> DetectionEvidence:
        """Always report that no camera hardware is present."""
        return DetectionEvidence(
            DetectionOutcome.NOT_DETECTED,
            "Null camera backend never detects hardware.",
        )


def build_detector(backend: str) -> CameraDetector:
    """Construct the detector adapter for a configured backend name."""
    normalized = backend.strip().lower()

    if normalized == "rpicam":
        return CommandCameraDetector(("rpicam-hello", "--list-cameras"))
    if normalized == "libcamera":
        return CommandCameraDetector(("libcamera-hello", "--list-cameras"))
    if normalized in {"null", "none"}:
        return NullCameraDetector()

    raise ValueError(f"Unsupported camera backend: {backend!r}")
