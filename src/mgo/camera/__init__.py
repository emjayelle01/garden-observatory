"""Camera capture layer for Matt's Garden Observatory.

This package owns the *capture* side of the camera domain: turning a request
for a still image into a stored file plus structured metadata. It is kept
separate from ``mgo.core.camera`` (readiness/detection) and from
``mgo.core.config`` (configuration loading); no camera capture logic lives in
core configuration.

Responsibilities are split across:

* :mod:`mgo.camera.models` -- typed value objects (:class:`CaptureResult`);
* :mod:`mgo.camera.exceptions` -- explicit failure types;
* :mod:`mgo.camera.backend` -- the :class:`CaptureBackend` interface and its
  implementations (``RPiCamBackend``, ``MockBackend``, ``NullBackend``);
* :mod:`mgo.camera.capture` -- the :class:`CaptureService` that orchestrates a
  capture and depends only on the backend interface;
* :mod:`mgo.camera.coordinator` -- the :class:`CameraCoordinator` that serialises
  every camera-*mutating* operation (preview start/stop, still capture,
  shutdown) and applies the managed-preview policies;
* :mod:`mgo.camera.simulator` -- the deterministic, hardware-free ``simulator``
  backend (capture, preview and MJPEG stream) selected through configuration.
"""

from __future__ import annotations

from mgo.camera.backend import (
    CaptureBackend,
    MockBackend,
    NullBackend,
    RPiCamBackend,
    build_capture_backend,
)
from mgo.camera.capture import CaptureService, build_capture_filename
from mgo.camera.coordinator import CameraCoordinator
from mgo.camera.exceptions import (
    BackendCaptureError,
    CameraCaptureError,
    CameraUnavailableError,
    CaptureTimeoutError,
    CaptureWriteError,
    InvalidCameraConfigurationError,
    PreviewError,
    PreviewStartError,
    PreviewUnavailableError,
)
from mgo.camera.models import CaptureResult, ImageDimensions
from mgo.camera.preview import PreviewService, PreviewState, PreviewStatus
from mgo.camera.preview_backend import (
    MockPreviewBackend,
    MockPreviewProcess,
    NullPreviewBackend,
    PreviewBackend,
    PreviewProcess,
    RPiCamPreviewBackend,
    build_preview_backend,
)
from mgo.camera.simulator import (
    SIMULATOR_BACKEND_NAME,
    SIMULATOR_CAPTURE_HEIGHT,
    SIMULATOR_CAPTURE_WIDTH,
    SIMULATOR_MARKER_TEXT,
    SIMULATOR_SEQUENCE_LENGTH,
    SimulatorCaptureBackend,
    SimulatorFrameSequence,
    SimulatorMjpegStream,
    SimulatorPreviewBackend,
    SimulatorPreviewProcess,
    encode_simulator_frame,
    render_simulator_frame,
)
from mgo.camera.streaming import (
    MJPEG_CONTENT_TYPE,
    FrameSource,
    MjpegBroker,
    MockFrameSource,
    PreviewProcessFrameSource,
    encode_multipart_frame,
    parse_mjpeg_frames,
)

__all__ = [
    "MJPEG_CONTENT_TYPE",
    "SIMULATOR_BACKEND_NAME",
    "SIMULATOR_CAPTURE_HEIGHT",
    "SIMULATOR_CAPTURE_WIDTH",
    "SIMULATOR_MARKER_TEXT",
    "SIMULATOR_SEQUENCE_LENGTH",
    "BackendCaptureError",
    "CameraCaptureError",
    "CameraCoordinator",
    "CameraUnavailableError",
    "CaptureBackend",
    "CaptureResult",
    "CaptureService",
    "CaptureTimeoutError",
    "CaptureWriteError",
    "FrameSource",
    "ImageDimensions",
    "InvalidCameraConfigurationError",
    "MjpegBroker",
    "MockBackend",
    "MockFrameSource",
    "MockPreviewBackend",
    "MockPreviewProcess",
    "NullBackend",
    "NullPreviewBackend",
    "PreviewBackend",
    "PreviewError",
    "PreviewProcess",
    "PreviewProcessFrameSource",
    "PreviewService",
    "PreviewStartError",
    "PreviewState",
    "PreviewStatus",
    "PreviewUnavailableError",
    "RPiCamBackend",
    "RPiCamPreviewBackend",
    "SimulatorCaptureBackend",
    "SimulatorFrameSequence",
    "SimulatorMjpegStream",
    "SimulatorPreviewBackend",
    "SimulatorPreviewProcess",
    "build_capture_backend",
    "build_capture_filename",
    "build_preview_backend",
    "encode_multipart_frame",
    "encode_simulator_frame",
    "parse_mjpeg_frames",
    "render_simulator_frame",
]
