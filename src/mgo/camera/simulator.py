"""Deterministic camera simulator backend for Matt's Garden Observatory.

This module is the *only* place that knows how simulated imagery is produced. It
is a supported runtime backend, selected through normal configuration
(``[camera] backend = "simulator"``), not a test double: it satisfies the same
:class:`~mgo.camera.backend.CaptureBackend`,
:class:`~mgo.camera.preview_backend.PreviewBackend` and
:class:`~mgo.camera.preview_backend.PreviewProcess` protocols the physical
Raspberry Pi backends satisfy, so the real capture service, preview service,
MJPEG streaming broker and motion pipeline drive it unchanged.

It exists so the whole pipeline can be exercised with **no** Raspberry Pi camera,
``rpicam-*``/``libcamera-*`` tooling, video device, subprocess, external media
file or network access.

Truthfulness is a hard requirement. The simulator always identifies itself as
``simulator`` and never claims a physical camera is connected, enumerated or
ready -- see :mod:`mgo.core.camera_detection` for the readiness sentence.

Contents:

* frame generation -- a small, bounded, wall-clock-independent scene rendered
  with Pillow and encoded as a real JPEG (:func:`render_simulator_frame`,
  :func:`encode_simulator_frame`, :class:`SimulatorFrameSequence`);
* :class:`SimulatorCaptureBackend` -- deterministic still capture;
* :class:`SimulatorPreviewBackend` / :class:`SimulatorPreviewProcess` -- an
  in-process preview "process" emitting raw MJPEG on a blocking binary stream.
"""

from __future__ import annotations

import functools
import io
import logging
import threading
from collections import deque
from pathlib import Path
from typing import IO, TYPE_CHECKING, cast

from PIL import Image, ImageDraw, ImageFont

from mgo.camera.exceptions import CaptureWriteError, PreviewStartError
from mgo.camera.models import ImageDimensions
from mgo.core.config import PreviewConfig

if TYPE_CHECKING:  # pragma: no cover - typing-only import
    from _typeshed import WriteableBuffer

LOGGER = logging.getLogger(__name__)

#: The stable backend identifier reported in every status and metadata field.
SIMULATOR_BACKEND_NAME = "simulator"

#: The static marker drawn into every generated frame, so any simulated image is
#: identifiable as simulated by looking at it.
SIMULATOR_MARKER_TEXT = "MGO CAMERA SIMULATOR"

#: Deterministic still-capture resolution. The physical Camera Module 3's full
#: 4608x2592 frame is deliberately *not* imitated: generating it would cost real
#: memory and CPU for no gain, and reported metadata must describe the image
#: actually produced.
SIMULATOR_CAPTURE_WIDTH = 1280
SIMULATOR_CAPTURE_HEIGHT = 720

#: The sequence index whose scene a still capture renders. Fixed so repeated
#: captures are byte-identical, and chosen as an "object present" scene so a
#: capture is visibly a populated simulated scene rather than an empty one.
SIMULATOR_CAPTURE_SEQUENCE_INDEX = 2

#: Backend-specific bounds. The simulator generates every pixel itself, so it
#: needs its own explicit ceiling rather than trusting a configuration file: an
#: unreasonable request is refused, never silently clamped.
MIN_SIMULATOR_FRAME_WIDTH = 160
MIN_SIMULATOR_FRAME_HEIGHT = 120
MAX_SIMULATOR_PREVIEW_WIDTH = 1920
MAX_SIMULATOR_PREVIEW_HEIGHT = 1080
MAX_SIMULATOR_PREVIEW_FPS = 30

#: Frames retained for a slow consumer. Deliberately tiny: when the consumer
#: falls behind, the *stale* frame is discarded rather than queued, so memory
#: stays bounded exactly as it does for a live camera.
SIMULATOR_FRAME_BUFFER_FRAMES = 2

#: Number of logical frames before the scene sequence repeats.
SIMULATOR_SEQUENCE_LENGTH = 8

#: The three scenes the sequence is built from.
SCENE_QUIET = 0
SCENE_OBJECT_NEAR = 1
SCENE_OBJECT_FAR = 2

#: The logical frame sequence. The repeated pairs are load-bearing: they let the
#: existing frame-difference detector settle to ``no_motion``, while the 1->2,
#: 3->4 and 5->6 transitions produce a deterministic ``motion_detected``. No
#: frame counter or timestamp is ever drawn, so the simulator never claims
#: continuous motion merely because an index advanced.
SIMULATOR_SEQUENCE_SCENES = (
    SCENE_QUIET,
    SCENE_QUIET,
    SCENE_OBJECT_NEAR,
    SCENE_OBJECT_NEAR,
    SCENE_OBJECT_FAR,
    SCENE_OBJECT_FAR,
    SCENE_QUIET,
    SCENE_QUIET,
)

#: JPEG quality for generated frames: high enough that the analysis downscale
#: sees clean edges, low enough to keep frames small.
_JPEG_QUALITY = 85

# -- scene geometry and palette ------------------------------------------------
#
# Geometry is expressed as fractions of the frame so the same scene renders at
# any supported resolution. Colours are fixed constants: nothing here depends on
# wall-clock time, hostname, process identity or random numbers.

_BACKGROUND_COLOUR = (148, 150, 146)
_POST_COLOUR = (92, 74, 54)
_TRAY_COLOUR = (116, 94, 68)
_TRAY_RIM_COLOUR = (74, 58, 40)
_OBJECT_COLOUR = (24, 26, 24)
_MARKER_PLATE_COLOUR = (238, 240, 236)
_MARKER_TEXT_COLOUR = (26, 28, 26)

#: Fixed feeder-like furniture (left, top, right, bottom as frame fractions).
_POST_BOX = (0.465, 0.30, 0.535, 1.0)
_TRAY_BOX = (0.300, 0.255, 0.700, 0.320)
_TRAY_RIM_BOX = (0.300, 0.310, 0.700, 0.330)

#: The single moving test object: size, then its origin per non-quiet scene. The
#: two positions never overlap each other or the fixed furniture, so a move
#: changes a large, predictable share of the frame.
_OBJECT_SIZE = (0.300, 0.380)
_OBJECT_ORIGINS = {
    SCENE_OBJECT_NEAR: (0.100, 0.500),
    SCENE_OBJECT_FAR: (0.560, 0.500),
}

#: The marker is drawn from a 1x bitmap mask scaled by an integer factor with
#: nearest-neighbour resampling, so it stays crisp and bit-identical everywhere.
_MARKER_SCALE_DIVISOR = 320
_MARKER_PAD_DIVISOR = 36

#: How long to wait for the producer thread to finish after it has been asked to
#: stop. It only ever sleeps for one frame interval, so this is generous.
_PRODUCER_JOIN_SECONDS = 2.0

#: Exit codes reported by the simulated process handle, mirroring the meaning of
#: a real process exiting normally versus being force-killed.
_EXIT_TERMINATED = 0
_EXIT_KILLED = -9


# -- frame generation ---------------------------------------------------------


def validate_simulator_frame_size(width: int, height: int) -> None:
    """Raise :class:`ValueError` unless a frame size is within the bounds.

    The simulator renders every pixel itself, so its own bounds -- not a
    configuration file -- decide what is safe to generate.
    """
    if not (MIN_SIMULATOR_FRAME_WIDTH <= width <= MAX_SIMULATOR_PREVIEW_WIDTH):
        raise ValueError(
            f"Simulator frame width must be between {MIN_SIMULATOR_FRAME_WIDTH} "
            f"and {MAX_SIMULATOR_PREVIEW_WIDTH}"
        )
    if not (MIN_SIMULATOR_FRAME_HEIGHT <= height <= MAX_SIMULATOR_PREVIEW_HEIGHT):
        raise ValueError(
            f"Simulator frame height must be between "
            f"{MIN_SIMULATOR_FRAME_HEIGHT} and {MAX_SIMULATOR_PREVIEW_HEIGHT}"
        )


def simulator_scene(sequence_index: int) -> int:
    """Return the scene identifier for ``sequence_index``.

    The sequence repeats every :data:`SIMULATOR_SEQUENCE_LENGTH` frames and
    depends on nothing but the index, so any index maps to the same scene for
    ever.
    """
    return SIMULATOR_SEQUENCE_SCENES[sequence_index % SIMULATOR_SEQUENCE_LENGTH]


def _pixel_box(
    width: int, height: int, fractions: tuple[float, float, float, float]
) -> tuple[int, int, int, int]:
    """Convert a fractional box to inclusive pixel coordinates."""
    left, top, right, bottom = fractions
    return (
        round(left * width),
        round(top * height),
        max(round(left * width), round(right * width) - 1),
        max(round(top * height), round(bottom * height) - 1),
    )


@functools.lru_cache(maxsize=1)
def _marker_mask() -> Image.Image:
    """Render :data:`SIMULATOR_MARKER_TEXT` once as a crisp 1x bitmap mask.

    Pillow's *bundled bitmap* font is used deliberately
    (:func:`PIL.ImageFont.load_default_imagefont`): it needs no external font
    file and no FreeType rasterisation, so the mask contains only fully-on and
    fully-off pixels and is identical on every platform.
    """
    font = ImageFont.load_default_imagefont()
    probe = ImageDraw.Draw(Image.new("L", (1, 1)))
    # A bitmap font reports whole-pixel bounds; rounding is exact, and keeps the
    # mask dimensions integral for Pillow.
    left, top, right, bottom = (
        round(value)
        for value in probe.textbbox((0, 0), SIMULATOR_MARKER_TEXT, font=font)
    )
    mask = Image.new("L", (right - left, bottom - top), 0)
    ImageDraw.Draw(mask).text(
        (-left, -top), SIMULATOR_MARKER_TEXT, fill=255, font=font
    )
    return mask


def _draw_marker(image: Image.Image, draw: ImageDraw.ImageDraw) -> None:
    """Draw the static simulator marker onto ``image``.

    The marker sits in the top-left corner, clear of the moving object's two
    positions, so it is present and unchanged in every frame of the sequence.
    """
    mask = _marker_mask()
    scale = max(1, image.width // _MARKER_SCALE_DIVISOR)
    scaled = mask.resize(
        (mask.width * scale, mask.height * scale), Image.Resampling.NEAREST
    )
    pad = max(2, image.height // _MARKER_PAD_DIVISOR)
    inset = max(1, scale)
    draw.rectangle(
        (
            pad,
            pad,
            pad + scaled.width + 2 * inset,
            pad + scaled.height + 2 * inset,
        ),
        fill=_MARKER_PLATE_COLOUR,
    )
    image.paste(_MARKER_TEXT_COLOUR, (pad + inset, pad + inset), scaled)


def render_simulator_frame(
    sequence_index: int, width: int, height: int
) -> Image.Image:
    """Render one deterministic RGB scene for ``sequence_index``.

    The result depends only on the arguments: no wall-clock time, hostname,
    process identifier or random number participates, and nothing time-varying
    (no counter, no timestamp) is drawn into the image.
    """
    validate_simulator_frame_size(width, height)

    image = Image.new("RGB", (width, height), _BACKGROUND_COLOUR)
    draw = ImageDraw.Draw(image)

    # Fixed feeder-like furniture: unchanged in every frame, so it contributes
    # nothing to the frame-to-frame difference the motion detector measures.
    draw.rectangle(_pixel_box(width, height, _POST_BOX), fill=_POST_COLOUR)
    draw.rectangle(_pixel_box(width, height, _TRAY_BOX), fill=_TRAY_COLOUR)
    draw.rectangle(_pixel_box(width, height, _TRAY_RIM_BOX), fill=_TRAY_RIM_COLOUR)

    scene = simulator_scene(sequence_index)
    origin = _OBJECT_ORIGINS.get(scene)
    if origin is not None:
        object_left, object_top = origin
        object_width, object_height = _OBJECT_SIZE
        draw.rectangle(
            _pixel_box(
                width,
                height,
                (
                    object_left,
                    object_top,
                    object_left + object_width,
                    object_top + object_height,
                ),
            ),
            fill=_OBJECT_COLOUR,
        )

    _draw_marker(image, draw)
    return image


def encode_simulator_frame(
    sequence_index: int, width: int, height: int
) -> bytes:
    """Return the deterministic JPEG bytes for one sequence frame.

    Pillow writes only a JFIF header: no EXIF block, no timestamp, no hostname
    and no filesystem path is embedded, so a simulated image carries no private
    metadata.
    """
    image = render_simulator_frame(sequence_index, width, height)
    buffer = io.BytesIO()
    try:
        image.save(buffer, format="JPEG", quality=_JPEG_QUALITY)
    finally:
        image.close()
    return buffer.getvalue()


class SimulatorFrameSequence:
    """A fixed-size deterministic JPEG frame sequence.

    Frames are encoded lazily and cached *per scene*, so the repeated pairs in
    :data:`SIMULATOR_SEQUENCE_SCENES` are byte-identical by construction and the
    producer re-encodes nothing once every scene has been seen. The cache is
    bounded by the three scenes.
    """

    def __init__(self, width: int, height: int) -> None:
        validate_simulator_frame_size(width, height)
        self._width = width
        self._height = height
        self._cache: dict[int, bytes] = {}

    @property
    def width(self) -> int:
        """The generated frame width in pixels."""
        return self._width

    @property
    def height(self) -> int:
        """The generated frame height in pixels."""
        return self._height

    def frame(self, sequence_index: int) -> bytes:
        """Return the JPEG bytes for ``sequence_index``."""
        scene = simulator_scene(sequence_index)
        cached = self._cache.get(scene)
        if cached is None:
            cached = encode_simulator_frame(
                sequence_index, self._width, self._height
            )
            self._cache[scene] = cached
        return cached


# -- still capture ------------------------------------------------------------


class SimulatorCaptureBackend:
    """A :class:`~mgo.camera.backend.CaptureBackend` that generates its image.

    It launches no subprocess, opens no device and probes no hardware; it writes
    one deterministic, decodable JPEG and reports the dimensions it actually
    produced. Expected filesystem failures are translated into the existing
    camera-domain exceptions, so no raw ``OSError`` escapes.
    """

    def __init__(
        self,
        *,
        width: int = SIMULATOR_CAPTURE_WIDTH,
        height: int = SIMULATOR_CAPTURE_HEIGHT,
        sequence_index: int = SIMULATOR_CAPTURE_SEQUENCE_INDEX,
    ) -> None:
        validate_simulator_frame_size(width, height)
        self._width = width
        self._height = height
        self._sequence_index = sequence_index

    @property
    def name(self) -> str:
        return SIMULATOR_BACKEND_NAME

    def capture(self, destination: Path) -> ImageDimensions:
        """Write one simulated still to ``destination`` and report its size."""
        payload = encode_simulator_frame(
            self._sequence_index, self._width, self._height
        )
        try:
            destination.write_bytes(payload)
        except OSError as exc:
            raise CaptureWriteError(
                f"Could not write the simulated capture to {destination}: {exc}"
            ) from exc
        LOGGER.info(
            "Generated simulated still %dx%d (%d bytes); no physical camera "
            "was used",
            self._width,
            self._height,
            len(payload),
        )
        return ImageDimensions(width=self._width, height=self._height)


# -- preview ------------------------------------------------------------------


class _FrameMailbox:
    """A bounded latest-frames mailbox with an explicit end-of-stream signal.

    At most ``capacity`` frames are held; a further offer discards the *stale*
    frame rather than growing, so a slow consumer costs a dropped frame and
    never memory. Readers block on a condition variable (never a busy loop) and
    are released promptly by :meth:`end`.
    """

    def __init__(self, capacity: int = SIMULATOR_FRAME_BUFFER_FRAMES) -> None:
        if capacity < 1:
            raise ValueError("Simulator frame mailbox capacity must be positive")
        self._capacity = capacity
        self._condition = threading.Condition()
        self._frames: deque[bytes] = deque()
        self._ended = False

    @property
    def capacity(self) -> int:
        """The maximum number of frames the mailbox will ever hold."""
        return self._capacity

    @property
    def depth(self) -> int:
        """The number of frames currently waiting to be read."""
        with self._condition:
            return len(self._frames)

    def offer(self, frame: bytes) -> None:
        """Publish the newest frame, discarding stale frames when full."""
        with self._condition:
            if self._ended:
                return
            while len(self._frames) >= self._capacity:
                self._frames.popleft()
            self._frames.append(frame)
            self._condition.notify_all()

    def end(self) -> None:
        """Signal end-of-stream, releasing any waiting reader. Idempotent."""
        with self._condition:
            self._ended = True
            self._frames.clear()
            self._condition.notify_all()

    def take(self) -> bytes | None:
        """Block for the next frame, or return ``None`` at end-of-stream."""
        with self._condition:
            while not self._frames and not self._ended:
                self._condition.wait()
            if self._frames:
                return self._frames.popleft()
            return None


class SimulatorMjpegStream(io.RawIOBase):
    """A blocking binary stream of concatenated complete JPEG frames.

    This is the simulator's equivalent of ``rpicam-vid``'s stdout pipe and makes
    exactly the same raw Motion-JPEG promise, so the existing
    :func:`~mgo.camera.streaming.parse_mjpeg_frames` demultiplexer consumes it
    with no change. A read blocks until the next frame is available and returns
    zero bytes -- ordinary end-of-file -- once the stream has ended.
    """

    def __init__(self, mailbox: _FrameMailbox) -> None:
        super().__init__()
        self._mailbox = mailbox
        self._pending = b""
        self._ended = False

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: WriteableBuffer) -> int:
        """Fill ``buffer`` from the current frame, blocking for the next one."""
        view = memoryview(buffer).cast("B")
        if not self._pending:
            if self._ended:
                return 0
            frame = self._mailbox.take()
            if frame is None:
                self._ended = True
                return 0
            self._pending = frame
        taken = min(view.nbytes, len(self._pending))
        view[:taken] = self._pending[:taken]
        self._pending = self._pending[taken:]
        return taken


class SimulatorPreviewProcess:
    """An in-process :class:`~mgo.camera.preview_backend.PreviewProcess`.

    Constructing the handle *starts* the producer, mirroring
    :class:`subprocess.Popen`, so a producer only ever exists because the normal
    preview start path asked for one. Exactly one bounded daemon producer thread
    runs per handle; it sleeps on a stop event between frames (no busy loop) and
    exits promptly on :meth:`terminate`, :meth:`kill` or :meth:`close`.

    The handle is truthful about being simulated: :attr:`pid` is ``None`` because
    there is no operating-system process, and :meth:`read_error` is empty because
    there is no child stderr.
    """

    def __init__(
        self,
        sequence: SimulatorFrameSequence,
        *,
        fps: int,
        mailbox_capacity: int = SIMULATOR_FRAME_BUFFER_FRAMES,
    ) -> None:
        if fps < 1:
            raise ValueError("Simulator preview frame rate must be positive")
        self._sequence = sequence
        self._interval = 1.0 / fps
        self._mailbox = _FrameMailbox(mailbox_capacity)
        self._stream = SimulatorMjpegStream(self._mailbox)
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._exit_code: int | None = None
        self._producer = threading.Thread(
            target=self._produce,
            name="mgo-simulator-preview",
            daemon=True,
        )
        self._producer.start()

    # -- PreviewProcess protocol -------------------------------------------

    @property
    def pid(self) -> int | None:
        """Always ``None``: the simulator runs no operating-system process."""
        return None

    def poll(self) -> int | None:
        with self._lock:
            return self._exit_code

    def terminate(self) -> None:
        """Ask the producer to stop, settling a normal exit code. Idempotent."""
        self._settle(_EXIT_TERMINATED)

    def kill(self) -> None:
        """Stop the producer, settling a forced-kill exit code. Idempotent."""
        self._settle(_EXIT_KILLED)

    def wait(self, timeout: float | None = None) -> int | None:
        """Wait up to ``timeout`` for exit; return the code or ``None``."""
        self._stop.wait(timeout)
        code = self.poll()
        if code is None:
            return None
        self._producer.join(_PRODUCER_JOIN_SECONDS)
        return code

    def read_error(self) -> str:
        """Always empty: a simulated producer has no child stderr to report."""
        return ""

    def frame_stream(self) -> IO[bytes] | None:
        """Return the simulator's raw MJPEG stream.

        :class:`SimulatorMjpegStream` is a real binary stream (an
        :class:`io.RawIOBase`); the cast only bridges it to the ``IO[bytes]``
        annotation the protocol shares with a ``subprocess`` pipe.
        """
        return cast("IO[bytes]", self._stream)

    def close(self) -> None:
        """Stop the producer and release the stream. Idempotent."""
        self._settle(_EXIT_TERMINATED)
        self._producer.join(_PRODUCER_JOIN_SECONDS)
        self._stream.close()

    # -- internals ---------------------------------------------------------

    @property
    def producer_alive(self) -> bool:
        """Whether the bounded producer thread is still running."""
        return self._producer.is_alive()

    @property
    def buffered_frames(self) -> int:
        """Frames currently waiting for the consumer (bounded by design)."""
        return self._mailbox.depth

    def _settle(self, code: int) -> None:
        """Record an exit code (first one wins) and release everything."""
        with self._lock:
            if self._exit_code is None:
                self._exit_code = code
        self._stop.set()
        self._mailbox.end()

    def _produce(self) -> None:
        """Publish the deterministic sequence at the configured cadence."""
        index = 0
        while not self._stop.is_set():
            self._mailbox.offer(self._sequence.frame(index))
            index += 1
            # Waiting on the stop event both paces the producer and makes
            # shutdown immediate; a bare sleep would delay it by a frame.
            self._stop.wait(self._interval)


class SimulatorPreviewBackend:
    """A :class:`~mgo.camera.preview_backend.PreviewBackend` with no hardware.

    ``start`` validates the requested geometry and frame rate against the
    simulator's own bounds and *refuses* an unsafe request rather than silently
    clamping it, then returns an in-process handle that begins producing frames
    immediately so the existing startup validation sees its first complete JPEG
    well inside the configured startup window.
    """

    @property
    def name(self) -> str:
        return SIMULATOR_BACKEND_NAME

    def start(self, config: PreviewConfig) -> SimulatorPreviewProcess:
        """Start the simulated preview producer for ``config``."""
        _validate_preview_request(config)
        LOGGER.info(
            "Starting deterministic camera simulator preview (%dx%d @ %dfps); "
            "no physical camera is in use",
            config.width,
            config.height,
            config.fps,
        )
        sequence = SimulatorFrameSequence(config.width, config.height)
        return SimulatorPreviewProcess(sequence, fps=config.fps)


def _validate_preview_request(config: PreviewConfig) -> None:
    """Reject a preview request outside the simulator's safety bounds.

    Failures map to :class:`~mgo.camera.exceptions.PreviewStartError` -- the
    existing preview-domain failure model -- with a concise message that names
    only the offending setting and its bounds, never a path or any environment
    detail.
    """
    if not (
        MIN_SIMULATOR_FRAME_WIDTH
        <= config.width
        <= MAX_SIMULATOR_PREVIEW_WIDTH
    ):
        raise PreviewStartError(
            f"Simulator preview width must be between "
            f"{MIN_SIMULATOR_FRAME_WIDTH} and {MAX_SIMULATOR_PREVIEW_WIDTH}."
        )
    if not (
        MIN_SIMULATOR_FRAME_HEIGHT
        <= config.height
        <= MAX_SIMULATOR_PREVIEW_HEIGHT
    ):
        raise PreviewStartError(
            f"Simulator preview height must be between "
            f"{MIN_SIMULATOR_FRAME_HEIGHT} and {MAX_SIMULATOR_PREVIEW_HEIGHT}."
        )
    if not (1 <= config.fps <= MAX_SIMULATOR_PREVIEW_FPS):
        raise PreviewStartError(
            f"Simulator preview frame rate must be between 1 and "
            f"{MAX_SIMULATOR_PREVIEW_FPS}."
        )
