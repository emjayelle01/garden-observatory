"""Tests for the deterministic frame-difference motion detector.

No Raspberry Pi hardware is required: frames are generated in-memory with
Pillow and pixel comparison is exercised directly with synthetic analysis
frames, so results are fully deterministic.
"""

from __future__ import annotations

import io

import pytest
from PIL import Image, ImageDraw

from mgo.core.config import MotionConfig
from mgo.motion.detector import (
    AnalysisFrame,
    FrameDecodeError,
    FrameDifferenceDetector,
)


def _config(
    *,
    analysis_width: int = 160,
    analysis_height: int = 90,
    pixel_difference_threshold: int = 20,
    changed_pixel_ratio_threshold: float = 0.02,
) -> MotionConfig:
    return MotionConfig(
        enabled=True,
        analysis_interval_seconds=1.0,
        analysis_width=analysis_width,
        analysis_height=analysis_height,
        pixel_difference_threshold=pixel_difference_threshold,
        changed_pixel_ratio_threshold=changed_pixel_ratio_threshold,
        cooldown_seconds=5.0,
    )


def _jpeg(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=90)
    return buffer.getvalue()


def _solid(value: int, size: tuple[int, int] = (320, 240)) -> bytes:
    return _jpeg(Image.new("L", size, value))


def _with_rectangle(
    background: int,
    foreground: int,
    box: tuple[int, int, int, int],
    size: tuple[int, int] = (320, 240),
) -> bytes:
    image = Image.new("L", size, background)
    ImageDraw.Draw(image).rectangle(box, fill=foreground)
    return _jpeg(image)


def _frame(width: int, height: int, luma: bytes) -> AnalysisFrame:
    return AnalysisFrame(width=width, height=height, luma=luma)


# --- decode ---------------------------------------------------------------


def test_decode_normalises_to_analysis_resolution() -> None:
    """Decoding reduces any source frame to the configured analysis size."""
    detector = FrameDifferenceDetector(_config())

    frame = detector.decode(_solid(128, size=(640, 480)))

    assert frame.width == 160
    assert frame.height == 90
    assert len(frame.luma) == 160 * 90


def test_decode_is_deterministic() -> None:
    """The same bytes always decode to the same analysis frame."""
    detector = FrameDifferenceDetector(_config())
    jpeg = _solid(100)

    assert detector.decode(jpeg).luma == detector.decode(jpeg).luma


@pytest.mark.parametrize(
    "payload",
    [b"", b"not-a-jpeg-at-all", b"\xff\xd8truncated-jpeg-no-end"],
)
def test_decode_rejects_malformed_data(payload: bytes) -> None:
    """Malformed or empty frame data raises a truthful decode error."""
    detector = FrameDifferenceDetector(_config())

    with pytest.raises(FrameDecodeError):
        detector.decode(payload)


# --- scoring / motion decision --------------------------------------------


def test_identical_frames_produce_no_motion() -> None:
    """Two identical frames yield a zero score and no motion."""
    detector = FrameDifferenceDetector(_config())
    frame = detector.decode(_solid(120))

    score = detector.score(frame, frame)

    assert score == 0.0
    assert detector.is_motion(score) is False


def test_small_noise_stays_below_threshold() -> None:
    """A small whole-frame luminance shift is treated as noise, not motion."""
    detector = FrameDifferenceDetector(_config(pixel_difference_threshold=20))
    baseline = detector.decode(_solid(120))
    # A +6 shift is below the per-pixel noise threshold everywhere.
    current = detector.decode(_solid(126))

    score = detector.score(baseline, current)

    assert score == 0.0
    assert detector.is_motion(score) is False


def test_small_changed_region_below_ratio_threshold_is_no_motion() -> None:
    """A tiny changed region stays below the changed-pixel ratio threshold."""
    detector = FrameDifferenceDetector(
        _config(changed_pixel_ratio_threshold=0.02)
    )
    baseline = detector.decode(_solid(0))
    # A 10x10 bright square in a 320x240 frame is well under 2% of the area.
    current = detector.decode(_with_rectangle(0, 255, (10, 10, 20, 20)))

    score = detector.score(baseline, current)

    assert score < 0.02
    assert detector.is_motion(score) is False


def test_large_changed_region_above_threshold_is_motion() -> None:
    """A large changed region exceeds the threshold and reports motion."""
    detector = FrameDifferenceDetector(
        _config(changed_pixel_ratio_threshold=0.02)
    )
    baseline = detector.decode(_solid(0))
    current = detector.decode(_with_rectangle(0, 255, (40, 40, 200, 200)))

    score = detector.score(baseline, current)

    assert score > 0.02
    assert detector.is_motion(score) is True


def test_score_is_deterministic_for_the_same_inputs() -> None:
    """Scoring the same frames repeatedly yields an identical score."""
    detector = FrameDifferenceDetector(_config())
    baseline = detector.decode(_solid(0))
    current = detector.decode(_with_rectangle(0, 255, (40, 40, 200, 200)))

    first = detector.score(baseline, current)
    second = detector.score(baseline, current)

    assert first == second


def test_score_counts_only_pixels_above_the_noise_floor() -> None:
    """Per-pixel differences at or below the threshold do not count as change."""
    detector = FrameDifferenceDetector(
        _config(
            analysis_width=10,
            analysis_height=10,
            pixel_difference_threshold=20,
            changed_pixel_ratio_threshold=0.05,
        )
    )
    baseline = _frame(10, 10, bytes([0] * 100))
    # 3 pixels change by 255 (well above the floor); the rest change by exactly
    # the threshold (20), which must be treated as noise, not change.
    luma = bytearray([20] * 100)
    luma[0] = luma[1] = luma[2] = 255
    current = _frame(10, 10, bytes(luma))

    score = detector.score(baseline, current)

    assert score == pytest.approx(0.03)
    assert detector.is_motion(score) is False


def test_score_rejects_mismatched_analysis_dimensions() -> None:
    """Comparing analysis frames of differing sizes is a programming error."""
    detector = FrameDifferenceDetector(_config())

    with pytest.raises(ValueError, match="differing dimensions"):
        detector.score(_frame(10, 10, bytes(100)), _frame(8, 8, bytes(64)))


def test_frames_of_different_source_sizes_compare_after_normalisation() -> None:
    """Different source resolutions normalise to a comparable analysis frame."""
    detector = FrameDifferenceDetector(_config())
    small = detector.decode(_solid(64, size=(320, 240)))
    large = detector.decode(_solid(64, size=(1280, 960)))

    # Same analysis dimensions, so scoring runs without a dimension error.
    assert (small.width, small.height) == (large.width, large.height)
    assert detector.score(small, large) < 0.02
