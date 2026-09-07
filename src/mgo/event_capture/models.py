"""Typed value objects for motion-triggered still capture.

Everything here is a small, immutable, JSON-serialisable value plus the one
mutable runtime-state holder the application owns for the life of the process.
None of it knows about FastAPI, SQLite, the camera, subprocesses or the motion
detector's internals -- it only describes *what was triggered*, *what a capture
attempt concluded* and *what the feature is currently doing*.

Two rules shape the shapes below:

* **A trigger carries facts, not buffers.** It holds the motion score, the
  threshold and the instant of the evaluation, copied at submission time. It
  never holds JPEG bytes, preview frames, detector analysis buffers, a
  filesystem path, a configuration object or an exception.
* **Nothing raw is ever exposed.** A failed attempt is reduced to one of a
  fixed set of categories, each with a fixed public message. Exception text,
  ``repr``, tracebacks, paths and command lines go to the log and stay there.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from mgo.event_capture.admission import (
    AdmissionDecision,
    AdmissionState,
    SuppressionReason,
)
from mgo.motion.models import MotionResult, MotionStatus


class EventCaptureState(StrEnum):
    """The truthful states motion-triggered capture can report.

    * ``DISABLED`` -- the feature is off by configuration and no worker exists;
    * ``IDLE`` -- enabled, the worker is alive and no capture is executing;
    * ``CAPTURING`` -- one motion-triggered still capture is executing now;
    * ``ERROR`` -- the most recent attempted automatic capture failed. The
      worker is still alive and a later trigger is still accepted; a later
      successful capture returns the state to ``IDLE``.
    """

    DISABLED = "disabled"
    IDLE = "idle"
    CAPTURING = "capturing"
    ERROR = "error"


class EventCaptureErrorCategory(StrEnum):
    """The failure categories an automatic capture attempt can end in."""

    CAMERA_UNAVAILABLE = "camera_unavailable"
    CAPTURE_TIMEOUT = "capture_timeout"
    BACKEND_FAILURE = "backend_failure"
    WRITE_FAILURE = "write_failure"
    ARCHIVE_FAILURE = "archive_failure"
    OVERSIZE_CAPTURE = "oversize_capture"
    UNEXPECTED = "unexpected"


#: The one public sentence each category is allowed to say. These are the only
#: strings that ever reach ``GET /event-capture/status`` or an event-capture
#: observation. They are fixed text, so no exception message, filesystem path,
#: subprocess command line, environment value or configuration content can ride
#: out on them, and they are short enough to need no truncation.
SAFE_ERROR_MESSAGES: dict[EventCaptureErrorCategory, str] = {
    EventCaptureErrorCategory.CAMERA_UNAVAILABLE: (
        "Camera unavailable for motion-triggered capture."
    ),
    EventCaptureErrorCategory.CAPTURE_TIMEOUT: (
        "Motion-triggered capture timed out."
    ),
    EventCaptureErrorCategory.BACKEND_FAILURE: (
        "Motion-triggered camera backend failed."
    ),
    EventCaptureErrorCategory.WRITE_FAILURE: (
        "Motion-triggered capture could not be written."
    ),
    EventCaptureErrorCategory.ARCHIVE_FAILURE: (
        "Capture completed but metadata could not be archived."
    ),
    EventCaptureErrorCategory.OVERSIZE_CAPTURE: (
        "Motion-triggered capture exceeded the reserved size and was not "
        "published."
    ),
    EventCaptureErrorCategory.UNEXPECTED: (
        "Motion-triggered capture failed unexpectedly."
    ),
}

#: Upper bound on any human-readable error string this package publishes. The
#: fixed messages above are far shorter; the bound exists so the contract holds
#: even if a future category is added carelessly.
_MAX_ERROR_LENGTH = 200


def safe_error_message(category: EventCaptureErrorCategory) -> str:
    """Return the bounded public message for ``category``."""
    return SAFE_ERROR_MESSAGES[category][:_MAX_ERROR_LENGTH]


@dataclass(frozen=True)
class MotionTrigger:
    """One accepted material motion transition, reduced to attribution facts.

    Built by :meth:`from_motion_result` at submission time, which copies the
    four values it needs out of the :class:`~mgo.motion.models.MotionResult`
    rather than retaining the result object. Nothing downstream can therefore
    reach the detector's state, and the trigger stays valid however the motion
    subsystem evolves.
    """

    status: MotionStatus
    score: float
    threshold: float
    evaluated_at: datetime

    @classmethod
    def from_motion_result(cls, result: MotionResult) -> MotionTrigger:
        """Copy the attribution facts out of a motion result."""
        return cls(
            status=result.status,
            score=result.score,
            threshold=result.threshold,
            evaluated_at=result.evaluated_at,
        )

    def capture_metadata(self) -> dict[str, Any]:
        """Return the ``extra_metadata`` recorded against an automatic capture.

        ``origin`` is what distinguishes an automatic capture from a manual one
        in the catalogue. The capture's own path already lives in the catalogue
        record, so it is deliberately not duplicated here.
        """
        return {
            "origin": "motion",
            "motion_status": self.status.value,
            "motion_score": self.score,
            "motion_threshold": self.threshold,
            "motion_evaluated_at": self.evaluated_at.isoformat(),
        }

    def observation_payload(self) -> dict[str, Any]:
        """Return the motion facts recorded on an event-capture observation."""
        return {
            "motion_score": self.score,
            "motion_threshold": self.threshold,
            "motion_evaluated_at": self.evaluated_at.isoformat(),
        }


@dataclass(frozen=True)
class EventCaptureStatus:
    """An immutable snapshot of the event-capture runtime state.

    Counters are *process-lifetime* only and are never persisted: they describe
    what this running process has done since it started, which is exactly what
    an operator watching a live service needs. Restarting the service resets
    them, and the durable record of what was captured remains the capture
    catalogue and the observation timeline.
    """

    enabled: bool
    state: EventCaptureState
    pending_triggers: int
    total_triggers_received: int
    total_captures_succeeded: int
    total_captures_failed: int
    total_triggers_dropped: int
    last_trigger_at: datetime | None
    last_capture_id: str | None
    last_capture_at: datetime | None
    last_error: str | None
    # Task 14.5 admission and suppression state. Every field defaults so the
    # pre-existing constructions of this snapshot keep working; the values are
    # the facts of the *last admission evaluation*, never a fresh probe -- the
    # status endpoint must stay as cheap and as side-effect free as it was.
    admission_state: AdmissionState = AdmissionState.DISABLED
    total_triggers_suppressed: int = 0
    last_suppression_reason: SuppressionReason | None = None
    last_suppressed_at: datetime | None = None
    hourly_count: int = 0
    hourly_limit: int | None = None
    hourly_remaining: int | None = None
    daily_count: int = 0
    daily_limit: int | None = None
    daily_remaining: int | None = None
    storage_reserve_ok: bool | None = None
    storage_free_bytes: int | None = None
    minimum_free_bytes: int | None = None
    maximum_capture_bytes: int | None = None
    last_admitted_at: datetime | None = None
    worker_busy: bool = False
    total_global_scene_changes: int = 0

    def as_dict(self) -> dict[str, Any]:
        """Return the snapshot as JSON-compatible values.

        No filesystem path, no configuration value and no raw exception text
        appears here -- only counters, a bounded category message and
        timestamps rendered as ISO-8601 UTC, matching every other MGO status
        endpoint. ``storage_free_bytes`` is a number, not a location.
        """
        return {
            "enabled": self.enabled,
            "state": self.state.value,
            "pending_triggers": self.pending_triggers,
            "total_triggers_received": self.total_triggers_received,
            "total_captures_succeeded": self.total_captures_succeeded,
            "total_captures_failed": self.total_captures_failed,
            "total_triggers_dropped": self.total_triggers_dropped,
            "last_trigger_at": _isoformat(self.last_trigger_at),
            "last_capture_id": self.last_capture_id,
            "last_capture_at": _isoformat(self.last_capture_at),
            "last_error": self.last_error,
            "admission_state": self.admission_state.value,
            "total_triggers_suppressed": self.total_triggers_suppressed,
            "last_suppression_reason": (
                self.last_suppression_reason.value
                if self.last_suppression_reason is not None
                else None
            ),
            "last_suppressed_at": _isoformat(self.last_suppressed_at),
            "hourly_count": self.hourly_count,
            "hourly_limit": self.hourly_limit,
            "hourly_remaining": self.hourly_remaining,
            "daily_count": self.daily_count,
            "daily_limit": self.daily_limit,
            "daily_remaining": self.daily_remaining,
            "storage_reserve_ok": self.storage_reserve_ok,
            "storage_free_bytes": self.storage_free_bytes,
            "minimum_free_bytes": self.minimum_free_bytes,
            "maximum_capture_bytes": self.maximum_capture_bytes,
            "last_admitted_at": _isoformat(self.last_admitted_at),
            "worker_busy": self.worker_busy,
            "total_global_scene_changes": self.total_global_scene_changes,
        }


def _isoformat(value: datetime | None) -> str | None:
    """Render an optional timestamp the way every MGO endpoint renders one."""
    return value.isoformat() if value is not None else None


class EventCaptureRuntimeState:
    """Holds the live event-capture state for the life of the process.

    Mirrors :class:`mgo.motion.monitor.MotionState`: a single asyncio event loop
    reads and writes it, so plain attributes are sufficient and no locking is
    required. The status endpoint only ever calls :meth:`snapshot`, which
    mutates nothing and touches no subsystem.

    A holder constructed with ``enabled=False`` starts -- and stays -- in
    ``DISABLED``: that state means "no worker exists", so it is truthful both
    for a disabled deployment and for the moment before the worker is created.
    """

    def __init__(self, *, enabled: bool) -> None:
        self.enabled = enabled
        self.state = (
            EventCaptureState.IDLE if enabled else EventCaptureState.DISABLED
        )
        self.pending_triggers = 0
        self.total_triggers_received = 0
        self.total_captures_succeeded = 0
        self.total_captures_failed = 0
        self.total_triggers_dropped = 0
        self.last_trigger_at: datetime | None = None
        self.last_capture_id: str | None = None
        self.last_capture_at: datetime | None = None
        self.last_error: str | None = None
        # Task 14.5. The last admission decision is kept whole so the snapshot
        # reports the counts the gate actually compared, not a re-derivation.
        self.last_decision: AdmissionDecision | None = None
        self.total_triggers_suppressed = 0
        self.last_suppression_reason: SuppressionReason | None = None
        self.last_suppressed_at: datetime | None = None
        self.last_admitted_at: datetime | None = None
        self.total_global_scene_changes = 0

    def apply_decision(self, decision: AdmissionDecision) -> None:
        """Remember the facts of an admission evaluation."""
        self.last_decision = decision

    def record_suppression(
        self, reason: SuppressionReason, at: datetime
    ) -> None:
        """Count one suppressed trigger. Touches no database."""
        self.total_triggers_suppressed += 1
        self.last_suppression_reason = reason
        self.last_suppressed_at = at

    def snapshot(self) -> EventCaptureStatus:
        """Return an immutable copy of the current state. Side-effect free."""
        decision = self.last_decision
        if not self.enabled:
            admission_state = AdmissionState.DISABLED
        elif decision is None:
            admission_state = AdmissionState.UNKNOWN
        elif decision.admitted:
            admission_state = AdmissionState.OPEN
        else:
            admission_state = AdmissionState.SUPPRESSED
        return EventCaptureStatus(
            enabled=self.enabled,
            state=self.state,
            pending_triggers=self.pending_triggers,
            total_triggers_received=self.total_triggers_received,
            total_captures_succeeded=self.total_captures_succeeded,
            total_captures_failed=self.total_captures_failed,
            total_triggers_dropped=self.total_triggers_dropped,
            last_trigger_at=self.last_trigger_at,
            last_capture_id=self.last_capture_id,
            last_capture_at=self.last_capture_at,
            last_error=self.last_error,
            admission_state=admission_state,
            total_triggers_suppressed=self.total_triggers_suppressed,
            last_suppression_reason=self.last_suppression_reason,
            last_suppressed_at=self.last_suppressed_at,
            hourly_count=decision.hourly_count if decision else 0,
            hourly_limit=decision.hourly_limit if decision else None,
            hourly_remaining=decision.hourly_remaining if decision else None,
            daily_count=decision.daily_count if decision else 0,
            daily_limit=decision.daily_limit if decision else None,
            daily_remaining=decision.daily_remaining if decision else None,
            storage_reserve_ok=decision.storage_reserve_ok if decision else None,
            storage_free_bytes=decision.storage_free_bytes if decision else None,
            minimum_free_bytes=decision.minimum_free_bytes if decision else None,
            maximum_capture_bytes=(
                decision.maximum_capture_bytes if decision else None
            ),
            last_admitted_at=self.last_admitted_at,
            worker_busy=self.state is EventCaptureState.CAPTURING,
            total_global_scene_changes=self.total_global_scene_changes,
        )


__all__ = [
    "SAFE_ERROR_MESSAGES",
    "EventCaptureErrorCategory",
    "EventCaptureRuntimeState",
    "EventCaptureState",
    "EventCaptureStatus",
    "MotionTrigger",
    "safe_error_message",
]
