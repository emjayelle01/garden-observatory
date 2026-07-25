"""Lightweight, deterministic frame-difference motion detector.

The algorithm is deliberately simple and explainable -- there is no background
modelling, optical flow, segmentation or machine learning of any kind:

1. decode a JPEG frame;
2. reduce it to a small analysis resolution;
3. convert it to greyscale (luminance);
4. compare corresponding pixels against a reference frame;
5. ignore per-pixel changes below a noise threshold;
6. compute the proportion of changed pixels;
7. report motion when that proportion exceeds the configured threshold.

The detector is pure and deterministic: the same input frames and configuration
always yield the same score. It embeds no FastAPI, subprocess or persistence
concerns -- it only decodes bytes and compares pixels. Image resources are
opened inside a ``with`` block so they are released promptly.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from typing import Protocol

from PIL import Image

from mgo.core.config import MotionConfig


class FrameDecodeError(Exception):
    """A frame could not be decoded into an analysis image.

    Raised for malformed, truncated or non-image bytes. The monitor maps this to
    a truthful ``error`` status rather than silently reporting no motion.
    """


@dataclass(frozen=True)
class AnalysisFrame:
    """A decoded frame reduced to greyscale at the analysis resolution.

    ``luma`` holds exactly ``width * height`` bytes, one 0-255 luminance value
    per pixel in row-major order. Frames are normalised to the configured
    analysis resolution on decode, so the source camera resolution never affects
    the comparison and two frames from the same detector always share dimensions.
    """

    width: int
    height: int
    luma: bytes


class MotionDetector(Protocol):
    """A frame-difference detector the monitor depends on.

    Implementations decode a JPEG frame to a normalised :class:`AnalysisFrame`
    and score the difference between two such frames. They must be deterministic
    for the same inputs and must not perform any recognition.
    """

    def decode(self, frame: bytes) -> AnalysisFrame:
        """Decode ``frame`` to a normalised greyscale analysis frame."""
        ...

    def score(self, baseline: AnalysisFrame, current: AnalysisFrame) -> float:
        """Return the changed-pixel ratio (0-1) between two analysis frames."""
        ...

    def is_motion(self, score: float) -> bool:
        """Return whether ``score`` exceeds the configured motion threshold."""
        ...


class FrameDifferenceDetector:
    """A :class:`MotionDetector` built on per-pixel luminance differencing.

    All tunables come from :class:`~mgo.core.config.MotionConfig`, so behaviour
    is fully driven by validated configuration. Downscaling uses a fixed
    resampling filter so results are deterministic across runs.
    """

    #: A fixed, deterministic resampling filter for the analysis downscale.
    _RESAMPLE = Image.Resampling.BILINEAR

    def __init__(self, config: MotionConfig) -> None:
        self._width = config.analysis_width
        self._height = config.analysis_height
        self._pixel_threshold = config.pixel_difference_threshold
        self._ratio_threshold = config.changed_pixel_ratio_threshold

    def decode(self, frame: bytes) -> AnalysisFrame:
        """Decode ``frame`` to greyscale at the analysis resolution.

        Raises :class:`FrameDecodeError` for any input that is not a decodable
        image (malformed, truncated or empty), so the caller can report a
        truthful error instead of a misleading no-motion result.
        """
        if not frame:
            raise FrameDecodeError("Empty frame cannot be decoded")
        try:
            with Image.open(io.BytesIO(frame)) as image:
                # ``convert`` then ``resize`` both return new images; the source
                # is released by the ``with`` block. Load is forced by resize.
                reduced = image.convert("L").resize(
                    (self._width, self._height), self._RESAMPLE
                )
                luma = reduced.tobytes()
        except FrameDecodeError:
            raise
        except Exception as exc:  # Pillow raises a variety of decode errors
            raise FrameDecodeError(f"Could not decode frame: {exc}") from exc
        return AnalysisFrame(width=self._width, height=self._height, luma=luma)

    def score(self, baseline: AnalysisFrame, current: AnalysisFrame) -> float:
        """Return the proportion of pixels that changed beyond the noise floor.

        Both frames must share the analysis dimensions (they always do when
        produced by :meth:`decode`); a mismatch is a programming error and raises
        :class:`ValueError`. Per-pixel changes at or below
        ``pixel_difference_threshold`` are treated as noise and ignored.
        """
        if (baseline.width, baseline.height) != (current.width, current.height):
            raise ValueError(
                "Cannot compare analysis frames of differing dimensions: "
                f"{baseline.width}x{baseline.height} vs "
                f"{current.width}x{current.height}"
            )
        total = len(current.luma)
        if total == 0:
            return 0.0
        threshold = self._pixel_threshold
        changed = 0
        for base_pixel, current_pixel in zip(
            baseline.luma, current.luma, strict=True
        ):
            if abs(base_pixel - current_pixel) > threshold:
                changed += 1
        return changed / total

    def is_motion(self, score: float) -> bool:
        """Return whether ``score`` exceeds the changed-pixel ratio threshold."""
        return score > self._ratio_threshold
