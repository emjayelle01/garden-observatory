"""Motion-triggered still capture for Matt's Garden Observatory.

This package connects two subsystems that already existed and were deliberately
kept apart: the motion monitor, which reports frame-to-frame scene change, and
the camera coordinator, which owns every camera mutation. When the feature is
enabled, a material transition into ``motion_detected`` may cause exactly one
full-resolution still capture, catalogued with the motion facts that triggered
it and correlated to one immutable observation.

What this is **not**: motion is scene activity, never bird presence. There is no
detection, no classification, no species, no confidence, no region of interest,
no pre/post-event buffer, no burst, no video and no AI framework anywhere in
this package. It runs one existing capture path in response to one existing
signal.

* :mod:`mgo.event_capture.models` -- the trigger, the state vocabulary, the
  runtime-state holder and the fixed safe error messages;
* :mod:`mgo.event_capture.service` -- the bounded queue, the single background
  worker and the observation recording.

The feature is disabled by default. When it is off, no worker exists, no queue
exists and nothing here runs.
"""

from __future__ import annotations

from mgo.event_capture.models import (
    SAFE_ERROR_MESSAGES,
    EventCaptureErrorCategory,
    EventCaptureRuntimeState,
    EventCaptureState,
    EventCaptureStatus,
    MotionTrigger,
    safe_error_message,
)
from mgo.event_capture.service import (
    FAILURE_STATUS,
    FAILURE_SUMMARY,
    OBSERVATION_KIND,
    OBSERVATION_SOURCE,
    QUEUE_CAPACITY,
    SUCCESS_STATUS,
    SUCCESS_SUMMARY,
    WORKER_TASK_NAME,
    EventCaptureService,
    classify_failure,
)

__all__ = [
    "FAILURE_STATUS",
    "FAILURE_SUMMARY",
    "OBSERVATION_KIND",
    "OBSERVATION_SOURCE",
    "QUEUE_CAPACITY",
    "SAFE_ERROR_MESSAGES",
    "SUCCESS_STATUS",
    "SUCCESS_SUMMARY",
    "WORKER_TASK_NAME",
    "EventCaptureErrorCategory",
    "EventCaptureRuntimeState",
    "EventCaptureService",
    "EventCaptureState",
    "EventCaptureStatus",
    "MotionTrigger",
    "classify_failure",
    "safe_error_message",
]
