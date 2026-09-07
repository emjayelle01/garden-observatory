"""Lightweight, deterministic frame-difference motion detector.

The algorithm is deliberately simple and explainable -- there is no background
modelling, optical flow, segmentation or machine learning of any kind:

1. decode a JPEG frame;
2. reduce it to a small analysis resolution;
3. convert it to greyscale (luminance);
4. measure the *median* per-pixel luminance shift between the reference and
   the current frame, and subtract it from every per-pixel difference (Task
   14.5), so a uniform exposure or lighting step cancels instead of reading
   as change;
5. compare corresponding pixels against the previous analysed (reference) frame;
6. ignore per-pixel changes below a noise threshold;
7. compute the proportion of changed pixels;
8. report motion when that proportion exceeds the configured threshold, and a
   *global change* -- never motion -- when it exceeds the global ceiling.

The brightness compensation is the smallest normalisation that addresses the
Task 14.4 evidence: two whole-frame transitions at ratios of about 0.99 and
0.71 that settled to exactly 0.0 on the next frame, which is the signature of
an exposure step rather than a subject. A step shifts every pixel by roughly
the same amount; subtracting that shift removes it while a localised subject,
which moves only some pixels, survives. The shift is the *median* per-pixel
difference, not the mean: a bright bird over a sixth of the frame moves the
mean by more than the noise floor and would make every unchanged background
pixel read as changed, whereas the median of the differences stays at zero
until more than half the frame has moved -- which is global-change territory
anyway. The global ceiling then catches what compensation cannot -- a covered
lens, a camera knock or a frame reset, where the content itself changed
everywhere.

The detector only compares two frames it is handed; which frame is the reference
(a fixed baseline versus a rolling previous frame) is decided by the caller.

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
class FrameComparison:
    """The measured difference between a reference and a current frame.

    ``raw_ratio`` is the changed-pixel proportion with no compensation, kept so
    an operator can see how much of a global event the compensation absorbed.
    ``ratio`` is the compensated proportion every decision is made on.
    ``luminance_shift`` is the median per-pixel luminance change (current
    minus reference, -255..255) that was subtracted before comparing.
    """

    ratio: float
    raw_ratio: float
    luminance_shift: float


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

    def score(self, reference: AnalysisFrame, current: AnalysisFrame) -> float:
        """Return the changed-pixel ratio (0-1) between two analysis frames."""
        ...

    def compare(
        self, reference: AnalysisFrame, current: AnalysisFrame
    ) -> FrameComparison:
        """Return the full comparison: compensated ratio, raw ratio and shift."""
        ...

    def is_motion(self, score: float) -> bool:
        """Return whether ``score`` exceeds the configured motion threshold."""
        ...

    def is_global_change(self, score: float) -> bool:
        """Return whether ``score`` exceeds the global-change ceiling."""
        ...


def _median_difference(differences: list[int]) -> int:
    """Return the median of per-pixel differences in -255..255, exactly.

    A counting sort over the 511 possible values: one pass to fill the
    histogram, one short walk to the middle. The lower median is returned for
    an even count, which keeps the result an integer offset.
    """
    histogram = [0] * 511
    for difference in differences:
        histogram[difference + 255] += 1
    middle = (len(differences) + 1) // 2
    seen = 0
    for index, count in enumerate(histogram):
        seen += count
        if seen >= middle:
            return index - 255
    return 0  # unreachable for a non-empty list; a safe answer regardless


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
        self._global_threshold = config.global_change_ratio_threshold

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

    def score(self, reference: AnalysisFrame, current: AnalysisFrame) -> float:
        """Return the compensated changed-pixel ratio (0-1) between two frames.

        Kept as the one-number convenience every earlier caller used; it is
        exactly :attr:`FrameComparison.ratio` from :meth:`compare`.
        """
        return self.compare(reference, current).ratio

    def compare(
        self, reference: AnalysisFrame, current: AnalysisFrame
    ) -> FrameComparison:
        """Measure the difference between two analysis frames.

        Both frames must share the analysis dimensions (they always do when
        produced by :meth:`decode`); a mismatch is a programming error and raises
        :class:`ValueError`. Per-pixel changes at or below
        ``pixel_difference_threshold`` are treated as noise and ignored.

        Two ratios are produced from the per-pixel differences. The raw ratio
        counts every pixel whose luminance moved by more than the noise floor.
        The compensated ratio first subtracts the *median* per-pixel shift
        between the frames, so a uniform brightening or darkening -- which
        moves every pixel by about the same amount -- contributes nothing,
        while a subject that moved only some pixels still does. The median is
        found exactly with a 511-bin histogram (integer arithmetic; no
        sampling), so the result is deterministic for the same inputs.
        """
        if (reference.width, reference.height) != (current.width, current.height):
            raise ValueError(
                "Cannot compare analysis frames of differing dimensions: "
                f"{reference.width}x{reference.height} vs "
                f"{current.width}x{current.height}"
            )
        total = len(current.luma)
        if total == 0:
            return FrameComparison(ratio=0.0, raw_ratio=0.0, luminance_shift=0.0)

        differences = [
            current_pixel - reference_pixel
            for reference_pixel, current_pixel in zip(
                reference.luma, current.luma, strict=True
            )
        ]
        offset = _median_difference(differences)

        threshold = self._pixel_threshold
        raw_changed = 0
        changed = 0
        for difference in differences:
            if abs(difference) > threshold:
                raw_changed += 1
            if abs(difference - offset) > threshold:
                changed += 1
        return FrameComparison(
            ratio=changed / total,
            raw_ratio=raw_changed / total,
            luminance_shift=float(offset),
        )

    def is_motion(self, score: float) -> bool:
        """Return whether ``score`` exceeds the changed-pixel ratio threshold."""
        return score > self._ratio_threshold

    def is_global_change(self, score: float) -> bool:
        """Return whether ``score`` exceeds the global-change ceiling.

        Strictly greater, mirroring :meth:`is_motion`, so the two thresholds
        partition the ratio line into three contiguous bands: no motion,
        motion, global change. Configuration guarantees the ceiling sits above
        the motion threshold. The monitor passes the larger of the raw and the
        compensated ratio, so a uniform exposure step -- which compensates to
        nearly nothing -- is still reported as the whole-frame event it is.
        """
        return score > self._global_threshold
