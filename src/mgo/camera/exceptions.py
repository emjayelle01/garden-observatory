"""Explicit exceptions for the camera capture layer.

Every *expected* failure mode in the capture pipeline maps to one of these
exceptions. Lower layers (backends) translate operating-system and subprocess
failures into these domain errors so that raw ``subprocess`` or ``OSError``
exceptions never leak into the capture service or the API.

The hierarchy is deliberately shallow: a single :class:`CameraCaptureError`
base makes it trivial for callers to catch "any capture problem", while the
concrete subclasses let the API map each failure to a meaningful HTTP status.
"""

from __future__ import annotations


class CameraCaptureError(Exception):
    """Base class for all camera capture failures."""


class InvalidCameraConfigurationError(CameraCaptureError):
    """Configuration is structurally invalid for capture.

    Raised, for example, when capture is attempted but the camera is disabled,
    or when a required capture setting is missing or nonsensical.
    """


class CameraUnavailableError(CameraCaptureError):
    """The camera hardware or its tooling is not available.

    Covers a missing capture command, an explicit "no cameras available"
    response from the tooling, or a backend that never provides hardware.
    """


class CaptureTimeoutError(CameraCaptureError):
    """The capture did not complete within the allotted time."""


class BackendCaptureError(CameraCaptureError):
    """The capture backend failed for a reason specific to that backend.

    Typically a non-zero exit from an underlying capture command that is not
    already classified as "unavailable" or "timed out".
    """


class CaptureWriteError(CameraCaptureError):
    """The captured image could not be written to the capture directory.

    Covers failure to create the capture directory, a capture command that
    reported success without producing a file, and empty or unreadable output
    files.
    """
