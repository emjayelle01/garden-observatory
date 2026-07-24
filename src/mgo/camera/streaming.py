"""Browser MJPEG streaming for the live camera preview.

Streaming is deliberately separate from :class:`~mgo.camera.preview.PreviewService`:

* ``PreviewService`` owns the camera, the preview process and its supervision;
* this module owns only the *delivery* of already-produced JPEG frames to
  browser clients as a ``multipart/x-mixed-replace`` stream, plus connection
  and disconnect handling.

The browser never owns the camera -- it only consumes frames while preview is
running. Capture keeps priority over preview; this layer introduces no new
camera-ownership rules.

The :class:`MjpegBroker` fans a single frame source out to many viewers. Each
viewer holds a one-slot mailbox (latest frame wins), so a slow client drops
frames instead of growing memory, and there is no busy-polling: the pump blocks
on the frame source and viewers block on their mailbox.
"""

from __future__ import annotations

import contextlib
import logging
import queue
import threading
import time
from collections.abc import Callable, Iterator
from typing import IO, Protocol

LOGGER = logging.getLogger(__name__)

#: Multipart boundary and the corresponding response content type.
MJPEG_BOUNDARY = "mgopreviewframe"
MJPEG_CONTENT_TYPE = f"multipart/x-mixed-replace; boundary={MJPEG_BOUNDARY}"

#: JPEG start-of-image / end-of-image markers, used to split a raw MJPEG stream.
_SOI = b"\xff\xd8"
_EOI = b"\xff\xd9"

#: How long a connected viewer waits for a frame before the stream is ended.
#: Bounded so a viewer never blocks forever if frames stop without a sentinel.
STREAM_IDLE_TIMEOUT_SECONDS = 10.0

#: Sentinel pushed to viewers when the frame source ends (e.g. preview stopped).
_END_OF_STREAM: bytes | None = None


def encode_multipart_frame(jpeg: bytes) -> bytes:
    """Encode one JPEG frame as a multipart/x-mixed-replace part."""
    return (
        b"--"
        + MJPEG_BOUNDARY.encode("ascii")
        + b"\r\nContent-Type: image/jpeg\r\nContent-Length: "
        + str(len(jpeg)).encode("ascii")
        + b"\r\n\r\n"
        + jpeg
        + b"\r\n"
    )


def parse_mjpeg_frames(
    stream: IO[bytes], *, chunk_size: int = 65536
) -> Iterator[bytes]:
    """Yield complete JPEG frames from a raw MJPEG byte ``stream``.

    Splits on JPEG SOI (``FFD8``)/EOI (``FFD9``) markers. Bytes before the first
    SOI are discarded; a partial trailing frame is retained until completed.
    Ends when the stream is exhausted (e.g. the preview process closed stdout).
    """
    buffer = bytearray()
    while True:
        chunk = stream.read(chunk_size)
        if not chunk:
            return
        buffer.extend(chunk)
        while True:
            start = buffer.find(_SOI)
            if start == -1:
                buffer.clear()
                break
            end = buffer.find(_EOI, start + 2)
            if end == -1:
                del buffer[:start]
                break
            end += len(_EOI)
            yield bytes(buffer[start:end])
            del buffer[:end]


class FrameSource(Protocol):
    """A source of JPEG frames for the streaming layer.

    ``frames`` yields JPEG byte strings until the source ends; ``close`` asks it
    to stop so the pump can exit promptly on the last disconnect.
    """

    def frames(self) -> Iterator[bytes]:
        """Yield JPEG frames until the source is exhausted or closed."""
        ...

    def close(self) -> None:
        """Signal the source to stop producing frames."""
        ...


class MockFrameSource:
    """A hardware-free :class:`FrameSource` for tests.

    Yields ``frames`` in order. With ``loop=True`` it cycles them continuously
    (like a live camera) until :meth:`close`, so late viewers still receive
    frames. Otherwise, if ``hold_open`` is given it blocks on that event after
    the frames are exhausted; with neither it simply ends after one pass.
    """

    def __init__(
        self,
        frames: list[bytes],
        *,
        hold_open: threading.Event | None = None,
        loop: bool = False,
        frame_interval: float = 0.005,
    ) -> None:
        self._frames = list(frames)
        self._hold_open = hold_open
        self._loop = loop
        self._frame_interval = frame_interval
        self.closed = False

    def frames(self) -> Iterator[bytes]:
        while not self.closed:
            for frame in self._frames:
                if self.closed:
                    return
                yield frame
                if self._loop:
                    time.sleep(self._frame_interval)
            if not self._loop:
                break
        if self._hold_open is not None:
            self._hold_open.wait()

    def close(self) -> None:
        self.closed = True
        if self._hold_open is not None:
            self._hold_open.set()


class Subscriber:
    """A single viewer's one-slot frame mailbox (latest frame wins)."""

    def __init__(self) -> None:
        self._mailbox: queue.Queue[bytes | None] = queue.Queue(maxsize=1)

    def offer(self, frame: bytes | None) -> None:
        """Deliver the latest frame, discarding any unread previous frame."""
        with contextlib.suppress(queue.Empty):
            self._mailbox.get_nowait()
        with contextlib.suppress(queue.Full):  # single producer; already drained
            self._mailbox.put_nowait(frame)

    def get(self, timeout: float) -> bytes | None:
        """Return the next frame, ``None`` at end of stream, or raise on timeout."""
        return self._mailbox.get(timeout=timeout)


class FrameStreamProvider(Protocol):
    """Something that can expose the live preview's MJPEG byte stream."""

    def frame_stream(self) -> IO[bytes] | None:
        """Return the current preview frame stream, or ``None`` if unavailable."""
        ...


class PreviewProcessFrameSource:
    """Reads frames from the running preview process's MJPEG output.

    It consumes the byte stream of the *existing* process supervised by
    :class:`~mgo.camera.preview.PreviewService` (the single camera owner) and
    never starts or stops that process. When no frame stream is exposed -- the
    default launch discards preview output -- it yields nothing and ends, so a
    connected browser sees the stream close gracefully.
    """

    def __init__(self, provider: FrameStreamProvider) -> None:
        self._provider = provider
        self._closed = False

    def frames(self) -> Iterator[bytes]:
        stream = self._provider.frame_stream()
        if stream is None:
            return
        for frame in parse_mjpeg_frames(stream):
            if self._closed:
                return
            yield frame

    def close(self) -> None:
        self._closed = True


class MjpegBroker:
    """Fans a single :class:`FrameSource` out to multiple browser viewers.

    A background pump thread is created when the first viewer subscribes and
    stopped when the last unsubscribes; between those it blocks on the source,
    so there is no busy loop. The broker is independent of the preview
    lifecycle: it never starts or stops preview.
    """

    def __init__(self, source_factory: Callable[[], FrameSource]) -> None:
        self._source_factory = source_factory
        self._lock = threading.Lock()
        self._subscribers: set[Subscriber] = set()
        self._source: FrameSource | None = None
        self._pump: threading.Thread | None = None

    @property
    def viewer_count(self) -> int:
        """Return the number of currently-connected viewers."""
        with self._lock:
            return len(self._subscribers)

    def subscribe(self) -> Subscriber:
        """Register a viewer, starting the pump if it is the first."""
        subscriber = Subscriber()
        with self._lock:
            first = not self._subscribers
            self._subscribers.add(subscriber)
            count = len(self._subscribers)
            if first:
                self._start_pump_locked()
        LOGGER.info("Preview viewer connected (viewers=%d)", count)
        return subscriber

    def unsubscribe(self, subscriber: Subscriber) -> None:
        """Deregister a viewer, stopping the pump if it was the last."""
        with self._lock:
            self._subscribers.discard(subscriber)
            count = len(self._subscribers)
            if not self._subscribers:
                self._stop_pump_locked()
        LOGGER.info("Preview viewer disconnected (viewers=%d)", count)

    def _start_pump_locked(self) -> None:
        source = self._source_factory()
        self._source = source
        self._pump = threading.Thread(
            target=self._run_pump,
            args=(source,),
            name="mgo-preview-stream",
            daemon=True,
        )
        self._pump.start()
        LOGGER.info("Preview stream started")

    def _stop_pump_locked(self) -> None:
        # Closing the source ends its frame iterator, letting the daemon pump
        # exit on its own; we never join under the lock to avoid deadlock with
        # the pump's own broadcast, which also takes the lock.
        if self._source is not None:
            self._source.close()
        self._source = None
        self._pump = None

    def _run_pump(self, source: FrameSource) -> None:
        try:
            for frame in source.frames():
                if source is not self._source:
                    break
                self._broadcast(frame)
        except Exception:
            LOGGER.exception("Preview stream error")
        finally:
            source.close()
            self._broadcast(_END_OF_STREAM)
            LOGGER.info("Preview stream stopped")

    def _broadcast(self, frame: bytes | None) -> None:
        with self._lock:
            subscribers = list(self._subscribers)
        for subscriber in subscribers:
            subscriber.offer(frame)
