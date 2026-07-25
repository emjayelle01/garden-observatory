"""Frame acquisition for the motion monitor.

Motion detection must not own the camera or launch a second capture process: the
Raspberry Pi camera is a single shared resource already owned by the preview
service and read by exactly one component -- the streaming
:class:`~mgo.camera.streaming.MjpegBroker`. This module lets the motion monitor
become *another consumer* of that broker's fan-out, alongside any browser
viewers, using only the broker's public subscribe/read API.

The :class:`MotionFrameSource` protocol keeps the monitor testable without any
broker or hardware; :class:`BrokerFrameSource` is the production adapter and
:class:`MockMotionFrameSource` is a scripted double for tests.
"""

from __future__ import annotations

import queue
import threading
from collections import deque
from typing import Protocol

from mgo.camera.streaming import MjpegBroker, Subscriber


class MotionFrameSource(Protocol):
    """A source of the latest JPEG frame for motion analysis."""

    def read(self, timeout: float) -> bytes | None:
        """Return the latest JPEG frame, or ``None`` if none is available.

        ``None`` means "no frame right now" (the preview is not producing, or
        none arrived within ``timeout``); it is never an error. Implementations
        must not block longer than ``timeout``.
        """
        ...

    def close(self) -> None:
        """Release any held resources (e.g. a broker subscription)."""
        ...


class BrokerFrameSource:
    """Reads the latest preview frame by subscribing to the streaming broker.

    The broker is the single reader of the preview process's MJPEG output and
    fans frames out to all subscribers, so subscribing here shares the *existing*
    stream rather than starting a competing camera process. A browser connecting
    or disconnecting does not disturb this subscription, and closing this source
    leaves any browser viewers streaming.

    When the preview stops producing frames the broker delivers an end-of-stream
    sentinel; this source releases its subscription then, and re-subscribes on
    the next :meth:`read`, so the monitor recovers automatically once preview
    resumes -- without holding a stale subscription that could stop the broker's
    pump from restarting for browser viewers.
    """

    def __init__(self, broker: MjpegBroker) -> None:
        self._broker = broker
        self._lock = threading.Lock()
        self._subscriber: Subscriber | None = None

    def read(self, timeout: float) -> bytes | None:
        with self._lock:
            if self._subscriber is None:
                self._subscriber = self._broker.subscribe()
            subscriber = self._subscriber
        try:
            frame = subscriber.get(timeout=timeout)
        except queue.Empty:
            # No frame arrived within the window: the source is still live, so
            # keep the subscription and let the caller wait again.
            return None
        if frame is None:
            # End-of-stream: preview stopped producing. Drop the subscription so
            # a fresh pump is started (for us and any browser) on the next read.
            self._release()
            return None
        return frame

    def close(self) -> None:
        """Unsubscribe from the broker; safe to call more than once."""
        self._release()

    def _release(self) -> None:
        with self._lock:
            subscriber = self._subscriber
            self._subscriber = None
        if subscriber is not None:
            self._broker.unsubscribe(subscriber)


class MockMotionFrameSource:
    """A scripted :class:`MotionFrameSource` for hardware-free tests.

    Yields the supplied ``frames`` in order on successive reads; a ``None`` entry
    models a moment with no frame available. Once exhausted, further reads return
    the ``exhausted`` value (``None`` by default) so a monitor loop simply keeps
    waiting. Records the number of reads and whether it was closed.
    """

    def __init__(
        self,
        frames: list[bytes | None],
        *,
        exhausted: bytes | None = None,
    ) -> None:
        self._frames: deque[bytes | None] = deque(frames)
        self._exhausted = exhausted
        self.read_count = 0
        self.closed = False

    def read(self, timeout: float) -> bytes | None:
        self.read_count += 1
        if self._frames:
            return self._frames.popleft()
        return self._exhausted

    def close(self) -> None:
        self.closed = True
