"""Localised subjects of increasing size against the global-change ceiling.

Task 14.5A, brief §13. The ceiling is a maximum-ratio rule: a subject that
changes more than ``global_change_ratio_threshold`` of the analysis frame is
reported as a whole-frame change and never triggers a capture. That is a
deliberate trade of false negatives (a bird filling most of the frame) for
false positives (an exposure step or a covered lens read as motion), and the
exact edge is documented here as a test rather than as prose: at the default
0.5, subjects up to 49% of the frame are motion, and subjects of 51% and more
are global change. The median compensation is what keeps the smaller
subjects readable at all -- it cancels a uniform shift and leaves the subject
-- but it cannot tell a subject covering most of the frame from a lighting
step, and the ceiling does not try to.

Fixtures are synthetic JPEGs at 320x240 analysed at the shipped 160x90, with
a bright rectangle covering the requested fraction of the frame from the top.
"""

from __future__ import annotations

import io

import pytest
from PIL import Image, ImageDraw

from mgo.core.config import MotionConfig
from mgo.motion.detector import FrameDifferenceDetector

_SIZE = (320, 240)


def _config(global_threshold: float = 0.5) -> MotionConfig:
    return MotionConfig(
        enabled=True,
        analysis_interval_seconds=1.0,
        analysis_width=160,
        analysis_height=90,
        pixel_difference_threshold=20,
        changed_pixel_ratio_threshold=0.08,
        cooldown_seconds=5.0,
        global_change_ratio_threshold=global_threshold,
    )


def _scene(offset: int = 0) -> Image.Image:
    width, height = _SIZE
    image = Image.new("L", _SIZE)
    pixels = image.load()
    assert pixels is not None
    for y in range(height):
        for x in range(width):
            base = 60 + (x * 120) // width + (y * 40) // height
            pixels[x, y] = max(0, min(255, base + offset))
    return image


def _jpeg(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=90)
    return buffer.getvalue()


def _with_subject(fraction: float, *, offset: int = 0, bright: int = 245) -> bytes:
    """The scene with a bright subject covering ``fraction`` of the frame."""
    image = _scene(offset)
    width, height = _SIZE
    rows = round(height * fraction)
    if rows:
        ImageDraw.Draw(image).rectangle((0, 0, width - 1, rows - 1), fill=bright)
    return _jpeg(image)


def _verdict(
    detector: FrameDifferenceDetector, reference: bytes, current: bytes
) -> str:
    comparison = detector.compare(detector.decode(reference), detector.decode(current))
    if detector.is_global_change(max(comparison.ratio, comparison.raw_ratio)):
        return "global_change"
    if detector.is_motion(comparison.ratio):
        return "motion"
    return "no_motion"


@pytest.mark.parametrize(
    ("fraction", "expected"),
    [
        (0.10, "motion"),
        (0.25, "motion"),
        (0.40, "motion"),
        (0.49, "motion"),
        (0.51, "global_change"),
        (0.70, "global_change"),
    ],
)
def test_a_localised_subject_against_the_default_ceiling(
    fraction: float, expected: str
) -> None:
    """The documented edge: under half the frame is motion, over half is not."""
    detector = FrameDifferenceDetector(_config())

    assert _verdict(detector, _jpeg(_scene()), _with_subject(fraction)) == expected


@pytest.mark.parametrize("fraction", [0.10, 0.25, 0.40, 0.49])
def test_a_localised_subject_survives_a_simultaneous_exposure_step(
    fraction: float,
) -> None:
    """Compensation removes the step and leaves the subject, below the edge."""
    detector = FrameDifferenceDetector(_config())

    assert (
        _verdict(detector, _jpeg(_scene(0)), _with_subject(fraction, offset=12))
        == "motion"
    )


@pytest.mark.parametrize("fraction", [0.51, 0.70])
def test_a_large_subject_is_the_documented_false_negative(fraction: float) -> None:
    """Above the ceiling the median IS the subject's shift, so the background
    reads as changed and the raw ratio is over half: global change, and no
    capture. Raising the ceiling is the operator's trade to make."""
    detector = FrameDifferenceDetector(_config())
    reference = detector.decode(_jpeg(_scene()))
    comparison = detector.compare(reference, detector.decode(_with_subject(fraction)))

    assert comparison.raw_ratio > 0.5
    assert abs(comparison.luminance_shift) > 20  # the median moved with the subject
    assert (
        _verdict(detector, _jpeg(_scene()), _with_subject(fraction)) == "global_change"
    )


def test_raising_the_ceiling_recovers_a_large_subject() -> None:
    """The trade is configurable: at a ceiling of 0.8 a 70% subject is motion."""
    detector = FrameDifferenceDetector(_config(global_threshold=0.8))

    assert _verdict(detector, _jpeg(_scene()), _with_subject(0.70)) == "motion"


def test_a_whole_frame_change_is_global_at_any_ceiling_below_one() -> None:
    detector = FrameDifferenceDetector(_config(global_threshold=0.99))

    assert _verdict(detector, _jpeg(_scene(0)), _jpeg(_scene(90))) == "global_change"
