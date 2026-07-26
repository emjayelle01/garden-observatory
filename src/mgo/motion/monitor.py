"""Application-managed background motion monitor.

Mirrors the persistent camera/health monitor pattern: a long-lived asyncio task
that periodically pulls the latest preview frame, evaluates scene change, keeps
the newest :class:`~mgo.motion.models.MotionResult` in runtime state, and
persists an observation only on a *material* transition.

Design guarantees:

* **Disabled is a no-op.** When motion is disabled the monitor sets a truthful
  ``disabled`` result and returns without ever consuming a frame.
* **No busy loop.** Each cycle waits one analysis interval (interruptible by the
  stop event); frame acquisition is bounded by a timeout.
* **No competing camera process.** Frames come from the shared streaming broker
  via a :class:`~mgo.motion.frame_source.MotionFrameSource`; the monitor never
  starts preview or the camera.
* **Failures are isolated.** A bad frame or detector fault becomes an ``error``
  result; it never terminates the task or the application, and a later good
  frame recovers the monitor.
* **Off the event loop.** Frame reads and pixel comparison run in worker threads
  so a slow analysis never blocks the loop or other frame consumers.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime

from mgo.core.config import MGOConfig, MotionConfig
from mgo.core.observations import Observation, record_observation
from mgo.motion.detector import AnalysisFrame, FrameDecodeError, MotionDetector
from mgo.motion.frame_source import MotionFrameSource
from mgo.motion.models import MotionResult, MotionStatus, default_motion_result

LOGGER = logging.getLogger(__name__)

Clock = Callable[[], datetime]
ObservationRecorder = Callable[..., Observation]

_STATUS_SUMMARIES: dict[MotionStatus, str] = {
    MotionStatus.DISABLED: "Motion detection disabled by configuration",
    MotionStatus.WAITING_FOR_FRAMES: "Motion monitor waiting for preview frames",
    MotionStatus.ESTABLISHING_BASELINE: "Motion baseline established",
    MotionStatus.NO_MOTION: "No motion detected",
    MotionStatus.MOTION_DETECTED: "Motion detected in the camera scene",
    MotionStatus.ERROR: "Motion detection error",
}


def _utc_now() -> datetime:
    """Return the current timezone-aware UTC instant."""
    return datetime.now(UTC)


class MotionState:
    """Holds the most recent motion result.

    A single asyncio event loop reads and writes this holder, so a plain
    attribute is sufficient; no locking is required (mirrors
    :class:`mgo.core.camera.CameraState`).
    """

    def __init__(self) -> None:
        self._latest: MotionResult | None = None

    def get(self) -> MotionResult | None:
        """Return the latest motion result, or ``None`` if none recorded yet."""
        return self._latest

    def set(self, result: MotionResult) -> None:
        """Replace the latest motion result."""
        self._latest = result


class _MotionEvaluator:
    """Rolling-reference management and result production for one monitor.

    Deterministic given ``(frame, now)``: it decodes the frame, compares it with
    the *previous analysed frame* and produces a :class:`MotionResult`. Motion is
    therefore recent visual *activity* -- a meaningful change since the last
    frame -- not a departure from a fixed historically quiet image. The rolling
    reference is:

    * **established** on the first frame after start (or after frames became
      unavailable); that first cycle reports ``establishing_baseline``;
    * **advanced** after *every* successful comparison -- the current frame
      becomes the reference for the next cycle whether the result was
      ``no_motion`` or ``motion_detected``. A lasting scene change therefore
      settles to ``no_motion`` once it stops changing, and continued activity
      legitimately keeps reading as ``motion_detected``;
    * **preserved** across a bad/failed frame, so a single decode error does not
      corrupt the reference with invalid data;
    * **reset** when frames become unavailable, so recovery re-establishes it.

    There is deliberately no time-based refresh and no maximum-motion timeout:
    the reference advances because another frame was analysed, never because a
    timer elapsed.
    """

    def __init__(
        self,
        config: MotionConfig,
        detector: MotionDetector,
    ) -> None:
        self._config = config
        self._detector = detector
        self._reference: AnalysisFrame | None = None

    def on_no_frame(self, now: datetime) -> MotionResult:
        """Report ``waiting_for_frames`` and reset the rolling reference."""
        self._reference = None
        return MotionResult(
            status=MotionStatus.WAITING_FOR_FRAMES,
            detected=False,
            score=0.0,
            threshold=self._config.changed_pixel_ratio_threshold,
            frames_available=False,
            detail="No preview frame is available for motion analysis.",
            evaluated_at=now,
        )

    def evaluate(self, frame: bytes, now: datetime) -> MotionResult:
        """Evaluate one frame against the previous one, never raising.

        On success the current frame always becomes the reference for the next
        evaluation. On a decode/detector error the previous reference is kept
        intact so a later valid frame recovers a normal comparison.
        """
        threshold = self._config.changed_pixel_ratio_threshold
        try:
            current = self._detector.decode(frame)
        except FrameDecodeError as exc:
            # Keep the existing reference: a single bad frame does not invalidate
            # it. Report a truthful error rather than "no motion".
            return MotionResult(
                status=MotionStatus.ERROR,
                detected=False,
                score=0.0,
                threshold=threshold,
                frames_available=True,
                detail=f"Frame could not be decoded: {exc}",
                evaluated_at=now,
            )
        except Exception as exc:  # unexpected detector fault
            # Smallest safe rule: an unexpected fault produced no valid frame, so
            # preserve the last good reference and recover on the next frame.
            LOGGER.exception("Motion detector raised an unexpected error")
            return MotionResult(
                status=MotionStatus.ERROR,
                detected=False,
                score=0.0,
                threshold=threshold,
                frames_available=True,
                detail=f"Motion detector failed: {exc}",
                evaluated_at=now,
            )

        if self._reference is None:
            self._reference = current
            return MotionResult(
                status=MotionStatus.ESTABLISHING_BASELINE,
                detected=False,
                score=0.0,
                threshold=threshold,
                frames_available=True,
                detail="Established the reference frame for motion analysis.",
                evaluated_at=now,
            )

        score = self._detector.score(self._reference, current)
        detected = self._detector.is_motion(score)
        # Rolling reference: the current frame is always adopted as the reference
        # for the next comparison, whether or not motion was detected. This is
        # what lets a lasting change settle to no_motion once it stops changing.
        self._reference = current
        if detected:
            return MotionResult(
                status=MotionStatus.MOTION_DETECTED,
                detected=True,
                score=score,
                threshold=threshold,
                frames_available=True,
                detail=(
                    f"Changed-pixel ratio {score:.4f} since the previous frame "
                    f"exceeded threshold {threshold:.4f}."
                ),
                evaluated_at=now,
            )
        return MotionResult(
            status=MotionStatus.NO_MOTION,
            detected=False,
            score=score,
            threshold=threshold,
            frames_available=True,
            detail=(
                f"Changed-pixel ratio {score:.4f} since the previous frame "
                f"stayed within threshold {threshold:.4f}."
            ),
            evaluated_at=now,
        )


class _MotionObserver:
    """Decides when a result warrants a persisted observation.

    Persists on a change of status, with one exception: re-entry into
    ``motion_detected`` within ``cooldown_seconds`` of the last recorded motion
    is suppressed, so a flickering or continuous event is recorded once rather
    than repeatedly. The eventual return to ``no_motion`` after a *recorded*
    motion is always persisted -- cooldown never hides it.
    """

    def __init__(
        self,
        config: MGOConfig,
        *,
        recorder: ObservationRecorder,
    ) -> None:
        self._config = config
        self._recorder = recorder
        self._last_persisted: MotionStatus | None = None
        self._last_motion_at: datetime | None = None

    def _should_persist(self, result: MotionResult, now: datetime) -> bool:
        if result.status == self._last_persisted:
            return False
        # Suppress re-entry into motion within the cooldown of the last recorded
        # motion, so a continuous or flickering event is recorded once.
        return not (
            result.status is MotionStatus.MOTION_DETECTED
            and self._last_motion_at is not None
            and (now - self._last_motion_at).total_seconds()
            < self._config.motion.cooldown_seconds
        )

    def record(self, result: MotionResult, now: datetime) -> bool:
        """Persist ``result`` if it is a material transition. Returns whether it did."""
        if not self._should_persist(result, now):
            return False
        self._recorder(
            self._config.storage.database_path,
            kind="motion_status",
            source="mgo-motion",
            status=result.status.value,
            summary=_STATUS_SUMMARIES.get(result.status, "Motion status changed"),
            payload=result.as_dict(),
        )
        self._last_persisted = result.status
        if result.status is MotionStatus.MOTION_DETECTED:
            self._last_motion_at = now
        return True


def _log_transition(previous: MotionStatus | None, current: MotionStatus) -> None:
    """Log a concise message when the live status changes (never per frame)."""
    if previous == current:
        return
    if current is MotionStatus.MOTION_DETECTED:
        LOGGER.info("Motion detected in the camera scene")
    elif current is MotionStatus.NO_MOTION and previous is (
        MotionStatus.MOTION_DETECTED
    ):
        LOGGER.info("Motion ended; scene returned to no motion")
    elif current is MotionStatus.ESTABLISHING_BASELINE:
        LOGGER.info("Motion baseline established")
    elif current is MotionStatus.WAITING_FOR_FRAMES:
        LOGGER.info("Motion monitor waiting for preview frames")
    elif current is MotionStatus.NO_MOTION:
        LOGGER.info("Motion monitor active; no motion")
    elif current is MotionStatus.ERROR:
        LOGGER.warning("Motion detection error; monitor continues")


async def _analyse_once(
    frame_source: MotionFrameSource,
    evaluator: _MotionEvaluator,
    observer: _MotionObserver,
    state: MotionState,
    *,
    read_timeout: float,
    clock: Clock,
) -> MotionResult:
    """Acquire one frame, evaluate it, update state and persist transitions."""
    try:
        frame = await asyncio.to_thread(frame_source.read, read_timeout)
    except Exception:
        LOGGER.exception("Motion frame acquisition failed")
        frame = None

    previous = state.get()
    now = clock()
    if frame is None:
        result = evaluator.on_no_frame(now)
    else:
        result = await asyncio.to_thread(evaluator.evaluate, frame, now)

    state.set(result)
    _log_transition(previous.status if previous is not None else None, result.status)
    observer.record(result, now)
    return result


async def run_motion_monitor(
    config: MGOConfig,
    state: MotionState,
    frame_source: MotionFrameSource,
    detector: MotionDetector,
    stop_event: asyncio.Event,
    *,
    recorder: ObservationRecorder = record_observation,
    clock: Clock = _utc_now,
) -> None:
    """Run the motion monitor until ``stop_event`` is set.

    A disabled monitor records a truthful ``disabled`` state and returns without
    consuming any frame. Otherwise it analyses one frame per interval, isolating
    every per-cycle failure so the monitor -- and the application -- survive, and
    always releases the frame source on exit so no subscription or task leaks.
    """
    motion = config.motion
    if not motion.enabled:
        state.set(default_motion_result(motion, now=clock()))
        LOGGER.info("Motion detection disabled; monitor not started")
        return

    interval = motion.analysis_interval_seconds
    LOGGER.info(
        "Motion monitoring started (interval=%.1fs, analysis=%dx%d, "
        "ratio threshold=%.4f)",
        interval,
        motion.analysis_width,
        motion.analysis_height,
        motion.changed_pixel_ratio_threshold,
    )
    evaluator = _MotionEvaluator(motion, detector)
    observer = _MotionObserver(config, recorder=recorder)

    try:
        while not stop_event.is_set():
            # Pace one interval, interruptible by shutdown, so there is no busy
            # loop and shutdown is prompt.
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
                break
            except TimeoutError:
                pass
            await _analyse_once(
                frame_source,
                evaluator,
                observer,
                state,
                read_timeout=interval,
                clock=clock,
            )
    except asyncio.CancelledError:
        LOGGER.info("Motion monitoring cancelled")
        raise
    finally:
        await asyncio.to_thread(frame_source.close)
        LOGGER.info("Motion monitoring stopped")
