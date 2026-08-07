"""Motion-detection foundation for Matt's Garden Observatory.

This package owns the *motion* side of the camera domain: deciding whether a
meaningful visual change has occurred between camera frames. It is deliberately
separate from readiness detection (``mgo.core.camera``), still capture
(``mgo.camera.capture``) and live preview/streaming (``mgo.camera.preview`` /
``mgo.camera.streaming``).

Scope of this foundation:

* it detects *scene change*, not birds or any other object; there is no
  recognition, classification, tracking or inference of any kind;
* it consumes JPEG frames from the *existing* preview stream (via the shared
  streaming broker) and never launches a second camera process;
* it never takes a still image, and imports nothing that can: no camera
  coordinator, no capture workflow, no capture archive. A material transition is
  announced through the monitor's transition-listener callback, and *what*
  listens -- notifications, and since Task 13.1 the optional motion-triggered
  capture feature -- is decided by the application composition layer, not here.
  ``motion_detected`` still means the scene changed, never that a bird is
  present, whatever a listener chooses to do about it.

Responsibilities are split across:

* :mod:`mgo.motion.models` -- the typed :class:`MotionStatus` vocabulary and the
  immutable :class:`MotionResult` value object;
* :mod:`mgo.motion.detector` -- the :class:`MotionDetector` protocol and the
  lightweight, deterministic :class:`FrameDifferenceDetector`;
* :mod:`mgo.motion.frame_source` -- the :class:`MotionFrameSource` protocol and a
  :class:`BrokerFrameSource` that shares the preview stream broker;
* :mod:`mgo.motion.monitor` -- the application-managed background
  :func:`run_motion_monitor` and the :class:`MotionState` runtime holder.
"""

from __future__ import annotations

from mgo.motion.detector import (
    AnalysisFrame,
    FrameDecodeError,
    FrameDifferenceDetector,
    MotionDetector,
)
from mgo.motion.frame_source import (
    BrokerFrameSource,
    MockMotionFrameSource,
    MotionFrameSource,
)
from mgo.motion.models import MotionResult, MotionStatus, default_motion_result
from mgo.motion.monitor import MotionState, run_motion_monitor

__all__ = [
    "AnalysisFrame",
    "BrokerFrameSource",
    "FrameDecodeError",
    "FrameDifferenceDetector",
    "MockMotionFrameSource",
    "MotionDetector",
    "MotionFrameSource",
    "MotionResult",
    "MotionState",
    "MotionStatus",
    "default_motion_result",
    "run_motion_monitor",
]
