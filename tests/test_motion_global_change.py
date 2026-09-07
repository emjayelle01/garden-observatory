"""Tests for whole-frame change filtering (Task 14.5, brief §8.3).

Deterministic synthetic JPEG frames drive the *real* detector and evaluator.
No camera, no hardware, no wall clock. Each named case in the brief has a test
here: no change, small noise, localised object motion, whole-frame brightening,
whole-frame darkening, camera occlusion, a frame dimension or format change,
baseline acquisition, recovery after preview interruption, and motion following
a global rebaseline. Every threshold boundary is checked as an exact edge.
"""

from __future__ import annotations

import io
import time
from datetime import UTC, datetime, timedelta

import pytest
from PIL import Image, ImageDraw

from mgo.core.config import MotionConfig
from mgo.motion.detector import FrameDifferenceDetector
from mgo.motion.models import MotionStatus
from mgo.motion.monitor import _MotionEvaluator

_T0 = datetime(2026, 9, 7, 6, 0, tzinfo=UTC)
_SIZE = (320, 240)


def _config(
    *,
    motion_threshold: float = 0.08,
    global_threshold: float = 0.5,
    pixel_threshold: int = 20,
) -> MotionConfig:
    return MotionConfig(
        enabled=True,
        analysis_interval_seconds=1.0,
        analysis_width=160,
        analysis_height=90,
        pixel_difference_threshold=pixel_threshold,
        changed_pixel_ratio_threshold=motion_threshold,
        cooldown_seconds=5.0,
        global_change_ratio_threshold=global_threshold,
    )


def _jpeg(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=90)
    return buffer.getvalue()


def _textured(offset: int = 0, size: tuple[int, int] = _SIZE) -> Image.Image:
    """A garden-like scene: a smooth gradient, so JPEG noise is realistic.

    ``offset`` brightens every pixel uniformly, the signature of an exposure
    step. Clamped to 0-255.
    """
    width, height = size
    image = Image.new("L", size)
    pixels = image.load()
    assert pixels is not None
    for y in range(height):
        for x in range(width):
            base = 60 + (x * 120) // width + (y * 40) // height
            pixels[x, y] = max(0, min(255, base + offset))
    return image


def _with_subject(
    offset: int = 0, box: tuple[int, int, int, int] = (40, 60, 150, 170)
) -> bytes:
    """The scene with a bright subject covering about 16% of the frame.

    Comfortably above the 8% motion threshold and far below the 50% global
    ceiling: a bird-sized visitor, not a hand over the lens.
    """
    image = _textured(offset)
    ImageDraw.Draw(image).rectangle(box, fill=245)
    return _jpeg(image)


def _scene(offset: int = 0) -> bytes:
    return _jpeg(_textured(offset))


def _solid(value: int) -> bytes:
    return _jpeg(Image.new("L", _SIZE, value))


def _statuses(
    evaluator: _MotionEvaluator, frames: list[bytes | None]
) -> list[MotionStatus]:
    statuses = []
    for index, frame in enumerate(frames):
        now = _T0 + timedelta(seconds=index)
        if frame is None:
            result = evaluator.on_no_frame(now)
        else:
            result = evaluator.evaluate(frame, now)
        statuses.append(result.status)
    return statuses


# --- detector: compensation ------------------------------------------------------


def test_no_change_scores_zero() -> None:
    detector = FrameDifferenceDetector(_config())
    reference = detector.decode(_scene())
    comparison = detector.compare(reference, detector.decode(_scene()))

    assert comparison.ratio == 0.0
    assert comparison.raw_ratio == 0.0
    assert comparison.luminance_shift == 0.0


def test_small_noise_stays_below_the_motion_threshold() -> None:
    detector = FrameDifferenceDetector(_config())
    reference = detector.decode(_scene())
    # Quality-80 re-encode: only JPEG noise differs.
    buffer = io.BytesIO()
    _textured().save(buffer, format="JPEG", quality=80)
    comparison = detector.compare(reference, detector.decode(buffer.getvalue()))

    assert not detector.is_motion(comparison.ratio)
    assert not detector.is_global_change(comparison.raw_ratio)


def test_a_uniform_brightening_below_the_ceiling_is_absorbed() -> None:
    """A modest exposure step moves many pixels a little; compensation cancels it."""
    detector = FrameDifferenceDetector(_config(pixel_threshold=12))
    reference = detector.decode(_scene(0))
    comparison = detector.compare(reference, detector.decode(_scene(15)))

    assert comparison.luminance_shift == pytest.approx(15, abs=1.5)
    assert comparison.raw_ratio > 0.08  # uncompensated this would read as motion
    assert comparison.ratio < 0.02  # compensated it is nothing
    assert not detector.is_motion(comparison.ratio)


def test_a_localised_subject_survives_compensation() -> None:
    detector = FrameDifferenceDetector(_config())
    reference = detector.decode(_scene())
    comparison = detector.compare(reference, detector.decode(_with_subject()))

    assert detector.is_motion(comparison.ratio)
    assert not detector.is_global_change(max(comparison.ratio, comparison.raw_ratio))
    assert comparison.ratio == pytest.approx(comparison.raw_ratio, abs=0.02)


def test_a_localised_subject_under_a_brightening_still_reads_as_motion() -> None:
    """Compensation removes the exposure step and leaves the bird."""
    detector = FrameDifferenceDetector(_config(pixel_threshold=12))
    reference = detector.decode(_scene(0))
    comparison = detector.compare(reference, detector.decode(_with_subject(15)))

    assert detector.is_motion(comparison.ratio)
    assert not detector.is_global_change(comparison.ratio)


def test_whole_frame_brightening_is_a_global_change() -> None:
    detector = FrameDifferenceDetector(_config())
    reference = detector.decode(_scene(0))
    comparison = detector.compare(reference, detector.decode(_scene(90)))

    assert comparison.raw_ratio > 0.5
    assert comparison.luminance_shift > 0
    assert detector.is_global_change(max(comparison.ratio, comparison.raw_ratio))


def test_whole_frame_darkening_is_a_global_change() -> None:
    detector = FrameDifferenceDetector(_config())
    reference = detector.decode(_scene(0))
    comparison = detector.compare(reference, detector.decode(_scene(-90)))

    assert comparison.raw_ratio > 0.5
    assert comparison.luminance_shift < 0
    assert detector.is_global_change(max(comparison.ratio, comparison.raw_ratio))


def test_camera_occlusion_is_a_global_change() -> None:
    detector = FrameDifferenceDetector(_config())
    reference = detector.decode(_scene())
    comparison = detector.compare(reference, detector.decode(_solid(3)))

    assert detector.is_global_change(max(comparison.ratio, comparison.raw_ratio))


def test_a_content_reset_is_a_global_change_even_with_no_mean_shift() -> None:
    """Every pixel changes but the mean does not: compensation cannot hide it."""
    detector = FrameDifferenceDetector(_config())
    reference = detector.decode(_scene())
    mirrored = _textured().transpose(Image.Transpose.FLIP_LEFT_RIGHT)
    comparison = detector.compare(reference, detector.decode(_jpeg(mirrored)))

    assert abs(comparison.luminance_shift) < 3
    assert comparison.ratio > 0.5
    assert detector.is_global_change(comparison.ratio)


def test_a_source_dimension_or_format_change_compares_after_normalisation() -> None:
    detector = FrameDifferenceDetector(_config())
    reference = detector.decode(_scene())
    other_size = _jpeg(_textured(size=(640, 480)))
    rgb = _textured().convert("RGB")
    buffer = io.BytesIO()
    rgb.save(buffer, format="PNG")

    for frame in (other_size, buffer.getvalue()):
        comparison = detector.compare(reference, detector.decode(frame))
        assert not detector.is_motion(comparison.ratio), comparison


# --- detector: exact edges ---------------------------------------------------------


def test_the_global_ceiling_is_strictly_greater() -> None:
    detector = FrameDifferenceDetector(_config(global_threshold=0.5))

    assert detector.is_global_change(0.5) is False
    assert detector.is_global_change(0.5000001) is True
    assert detector.is_motion(0.08) is False
    assert detector.is_motion(0.0800001) is True


def test_the_bands_are_contiguous() -> None:
    detector = FrameDifferenceDetector(
        _config(motion_threshold=0.1, global_threshold=0.4)
    )
    for score in (0.0, 0.1, 0.3, 0.4, 0.41, 1.0):
        motion = detector.is_motion(score)
        global_change = detector.is_global_change(score)
        if global_change:
            assert motion  # anything above the ceiling is above the floor
        assert (score > 0.4) == global_change
        assert (score > 0.1) == motion


# --- evaluator: state machine --------------------------------------------------------


def test_baseline_then_global_change_then_settles() -> None:
    evaluator = _MotionEvaluator(_config(), FrameDifferenceDetector(_config()))

    statuses = _statuses(evaluator, [_scene(0), _scene(90), _scene(90), _scene(90)])

    assert statuses == [
        MotionStatus.ESTABLISHING_BASELINE,
        MotionStatus.GLOBAL_CHANGE,
        MotionStatus.NO_MOTION,
        MotionStatus.NO_MOTION,
    ]


def test_a_global_change_is_never_reported_as_detected() -> None:
    evaluator = _MotionEvaluator(_config(), FrameDifferenceDetector(_config()))
    evaluator.evaluate(_scene(0), _T0)

    result = evaluator.evaluate(_scene(90), _T0 + timedelta(seconds=1))

    assert result.status is MotionStatus.GLOBAL_CHANGE
    assert result.detected is False
    assert result.frames_available is True
    assert result.global_change_threshold == 0.5
    assert result.raw_score > 0.5
    assert result.luminance_shift > 0
    assert "re-baselined" in result.detail
    assert len(result.detail) <= 500


def test_motion_after_a_global_rebaseline_is_still_detected() -> None:
    evaluator = _MotionEvaluator(_config(), FrameDifferenceDetector(_config()))

    statuses = _statuses(
        evaluator,
        [_scene(0), _scene(90), _scene(90), _with_subject(90), _with_subject(90)],
    )

    assert statuses == [
        MotionStatus.ESTABLISHING_BASELINE,
        MotionStatus.GLOBAL_CHANGE,
        MotionStatus.NO_MOTION,
        MotionStatus.MOTION_DETECTED,
        MotionStatus.NO_MOTION,
    ]


def test_recovery_after_preview_interruption_re_establishes_the_baseline() -> None:
    evaluator = _MotionEvaluator(_config(), FrameDifferenceDetector(_config()))

    statuses = _statuses(
        evaluator, [_scene(0), _scene(0), None, _scene(90), _scene(90)]
    )

    # After frames vanish the reference is reset, so the first new frame -- however
    # different -- is a baseline, not a global change or a motion event.
    assert statuses == [
        MotionStatus.ESTABLISHING_BASELINE,
        MotionStatus.NO_MOTION,
        MotionStatus.WAITING_FOR_FRAMES,
        MotionStatus.ESTABLISHING_BASELINE,
        MotionStatus.NO_MOTION,
    ]


def test_a_modest_exposure_drift_never_becomes_motion() -> None:
    """The Task 14.4 pattern: a step, then a static scene."""
    config = _config(pixel_threshold=12)
    evaluator = _MotionEvaluator(config, FrameDifferenceDetector(config))

    statuses = _statuses(evaluator, [_scene(0), _scene(15), _scene(30), _scene(30)])

    assert MotionStatus.MOTION_DETECTED not in statuses
    assert statuses[-1] is MotionStatus.NO_MOTION


def test_the_no_motion_result_carries_the_diagnostics() -> None:
    evaluator = _MotionEvaluator(_config(), FrameDifferenceDetector(_config()))
    evaluator.evaluate(_scene(0), _T0)

    result = evaluator.evaluate(_scene(0), _T0 + timedelta(seconds=1))

    assert result.status is MotionStatus.NO_MOTION
    assert result.raw_score == 0.0
    assert result.luminance_shift == 0.0
    assert result.global_change_threshold == 0.5
    assert result.as_dict()["global_change_threshold"] == 0.5


# --- cost --------------------------------------------------------------------


def test_compensated_comparison_is_cheap_at_the_analysis_resolution() -> None:
    """One pass at 160x90 stays comfortably within the one-second cycle.

    A generous bound, deliberately: this is a Windows workstation under an
    unknown load, and the point is to catch a pathological regression (a
    quadratic pass, a per-pixel allocation), not to benchmark the Pi.
    """
    detector = FrameDifferenceDetector(_config())
    reference = detector.decode(_scene(0))
    current = detector.decode(_with_subject(15))

    started = time.perf_counter()
    for _ in range(20):
        detector.compare(reference, current)
    per_comparison = (time.perf_counter() - started) / 20

    assert per_comparison < 0.25
