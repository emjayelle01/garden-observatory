"""Typed motion-detection result model.

These value objects are the single, hardware-agnostic vocabulary for a motion
evaluation. They know nothing about FastAPI, subprocesses, the camera or
persistence -- they only describe *what was concluded* about scene change, so
they can be produced by the detector, held in application state, surfaced by the
API and recorded as an observation without any layer leaking into another.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from mgo.core.config import MotionConfig

_MAX_DETAIL_LENGTH = 500


class MotionStatus(StrEnum):
    """The truthful states a motion evaluation can report.

    The states are deliberately explicit so the API never has to infer motion
    from ambiguous evidence. Motion is measured *frame to frame* -- against the
    previous analysed frame, not a fixed quiet scene -- so the states describe
    current visual activity, never bird presence:

    * ``DISABLED`` -- motion detection is off by configuration;
    * ``WAITING_FOR_FRAMES`` -- enabled, but no preview frame is available yet
      (the rolling reference has been reset);
    * ``ESTABLISHING_BASELINE`` -- the first valid frame has become the rolling
      reference; no comparison was possible yet;
    * ``NO_MOTION`` -- the latest frame-to-frame change stayed within threshold;
    * ``MOTION_DETECTED`` -- the latest frame-to-frame change exceeded threshold;
    * ``ERROR`` -- a frame could not be decoded or the detector failed.
    """

    DISABLED = "disabled"
    WAITING_FOR_FRAMES = "waiting_for_frames"
    ESTABLISHING_BASELINE = "establishing_baseline"
    NO_MOTION = "no_motion"
    MOTION_DETECTED = "motion_detected"
    ERROR = "error"


def _bounded(detail: str) -> str:
    """Trim detail text to a safe, log- and API-friendly length."""
    collapsed = " ".join(detail.split())
    if len(collapsed) <= _MAX_DETAIL_LENGTH:
        return collapsed
    return collapsed[: _MAX_DETAIL_LENGTH - 1].rstrip() + "…"


@dataclass(frozen=True)
class MotionResult:
    """A typed, immutable, JSON-serialisable snapshot of one motion evaluation.

    ``score`` is the proportion of analysis pixels (0-1) that changed beyond the
    per-pixel noise threshold; it is ``0.0`` whenever no comparison was possible
    (disabled, waiting, establishing a baseline, or error). ``detected`` is only
    ever ``True`` alongside :attr:`MotionStatus.MOTION_DETECTED`.
    ``frames_available`` reports whether a usable frame backed this evaluation.
    """

    status: MotionStatus
    detected: bool
    score: float
    threshold: float
    frames_available: bool
    detail: str
    evaluated_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "detail", _bounded(self.detail))

    def as_dict(self) -> dict[str, Any]:
        """Return the result as JSON-compatible values (no frame bytes)."""
        return {
            "status": self.status.value,
            "detected": self.detected,
            "score": self.score,
            "threshold": self.threshold,
            "frames_available": self.frames_available,
            "detail": self.detail,
            "evaluated_at": self.evaluated_at.isoformat(),
        }


def _now(now: datetime | None) -> datetime:
    """Return a timezone-aware UTC timestamp for a result."""
    return now if now is not None else datetime.now(UTC)


def default_motion_result(
    config: MotionConfig,
    *,
    now: datetime | None = None,
) -> MotionResult:
    """Return a safe result for use before any evaluation has run.

    Mirrors ``mgo.core.camera.default_readiness``: when motion is disabled the
    result is ``DISABLED``; otherwise it is ``WAITING_FOR_FRAMES`` because no
    frame has been analysed yet. Never reports motion without evidence.
    """
    evaluated_at = _now(now)
    if not config.enabled:
        return MotionResult(
            status=MotionStatus.DISABLED,
            detected=False,
            score=0.0,
            threshold=config.changed_pixel_ratio_threshold,
            frames_available=False,
            detail="Motion detection is disabled by configuration.",
            evaluated_at=evaluated_at,
        )
    return MotionResult(
        status=MotionStatus.WAITING_FOR_FRAMES,
        detected=False,
        score=0.0,
        threshold=config.changed_pixel_ratio_threshold,
        frames_available=False,
        detail="Motion detection has not evaluated a frame yet.",
        evaluated_at=evaluated_at,
    )
