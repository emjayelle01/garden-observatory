"""Tests for motion frame acquisition and shared camera-lifecycle coexistence.

No Raspberry Pi hardware is required: a mock frame source drives the real
streaming broker, and the motion frame source shares that broker exactly as it
would in production. These cover multi-consumer coexistence (browser preview and
motion monitoring together), independent disconnect, that no second camera
process is created, and broker self-healing after the source ends.
"""

from __future__ import annotations

import threading
import time

from mgo.camera.streaming import MjpegBroker, MockFrameSource
from mgo.motion.frame_source import BrokerFrameSource

_JPEG_A = b"\xff\xd8" + b"AAAA" + b"\xff\xd9"
_JPEG_B = b"\xff\xd8" + b"BBBB" + b"\xff\xd9"


def _wait_for(predicate: object, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():  # type: ignore[operator]
            return True
        time.sleep(0.01)
    return False


def _live_broker(*frames: bytes) -> tuple[MjpegBroker, threading.Event]:
    """A broker over a source that streams frames continuously (a live camera)."""
    hold = threading.Event()
    source = MockFrameSource(list(frames), hold_open=hold, loop=True)
    return MjpegBroker(lambda: source), hold


# --- basic frame acquisition ----------------------------------------------


def test_broker_frame_source_reads_latest_frame() -> None:
    """The motion frame source pulls frames from the shared broker."""
    broker, _hold = _live_broker(_JPEG_A, _JPEG_B)
    source = BrokerFrameSource(broker)
    try:
        frame = source.read(timeout=2.0)
        assert frame in (_JPEG_A, _JPEG_B)
    finally:
        source.close()

    assert _wait_for(lambda: broker.viewer_count == 0)


def test_broker_frame_source_returns_none_without_a_frame() -> None:
    """When the source has ended, a read returns ``None`` (no frame, not error)."""
    # A non-looping, empty source ends immediately -> end-of-stream sentinel.
    broker = MjpegBroker(lambda: MockFrameSource([]))
    source = BrokerFrameSource(broker)
    try:
        assert source.read(timeout=2.0) is None
    finally:
        source.close()


# --- shared camera lifecycle (coexistence) --------------------------------


def test_motion_and_browser_preview_coexist() -> None:
    """A browser viewer and the motion monitor both receive frames at once."""
    broker, _hold = _live_broker(_JPEG_A)
    motion = BrokerFrameSource(broker)
    browser = broker.subscribe()
    try:
        assert motion.read(timeout=2.0) == _JPEG_A
        assert browser.get(timeout=2.0) == _JPEG_A
        assert broker.viewer_count == 2
    finally:
        motion.close()
        broker.unsubscribe(browser)


def test_browser_disconnect_keeps_motion_frames_flowing() -> None:
    """A browser disconnecting does not stop frames needed by motion."""
    broker, _hold = _live_broker(_JPEG_A)
    motion = BrokerFrameSource(broker)
    try:
        assert motion.read(timeout=2.0) == _JPEG_A

        browser = broker.subscribe()
        assert browser.get(timeout=2.0) == _JPEG_A
        broker.unsubscribe(browser)

        # Motion still receives frames after the browser has gone.
        assert motion.read(timeout=2.0) == _JPEG_A
    finally:
        motion.close()


def test_stopping_motion_keeps_browser_frames_flowing() -> None:
    """Stopping the motion monitor does not stop frames for a browser client."""
    broker, _hold = _live_broker(_JPEG_A)
    motion = BrokerFrameSource(broker)
    browser = broker.subscribe()
    try:
        assert motion.read(timeout=2.0) == _JPEG_A
        assert browser.get(timeout=2.0) == _JPEG_A

        motion.close()

        # The browser keeps streaming after motion monitoring stops.
        assert browser.get(timeout=2.0) == _JPEG_A
        assert broker.viewer_count == 1
    finally:
        broker.unsubscribe(browser)


def test_single_frame_source_is_shared_no_second_camera_process() -> None:
    """Both consumers share one broker source; no competing source is created."""
    hold = threading.Event()
    created = 0
    lock = threading.Lock()

    def factory() -> MockFrameSource:
        nonlocal created
        with lock:
            created += 1
        return MockFrameSource([_JPEG_A], hold_open=hold, loop=True)

    broker = MjpegBroker(factory)
    motion = BrokerFrameSource(broker)
    browser = broker.subscribe()
    try:
        assert motion.read(timeout=2.0) == _JPEG_A
        assert browser.get(timeout=2.0) == _JPEG_A
        # A single pump/source backs both consumers.
        assert created == 1
    finally:
        motion.close()
        broker.unsubscribe(browser)


def test_no_lingering_consumers_after_all_leave() -> None:
    """After motion and the browser both leave, the broker has no consumers."""
    broker, _hold = _live_broker(_JPEG_A)
    motion = BrokerFrameSource(broker)
    browser = broker.subscribe()

    motion.read(timeout=2.0)
    browser.get(timeout=2.0)
    motion.close()
    broker.unsubscribe(browser)

    assert _wait_for(lambda: broker.viewer_count == 0)


# --- broker self-heal (regression) ----------------------------------------


def test_broker_revives_pump_for_a_new_consumer_after_source_ends() -> None:
    """A stale long-lived subscription must not stop a new consumer's frames.

    Models the motion monitor holding a subscription while the preview stream
    ends (source exhausted), then a browser connecting. The broker must start a
    fresh pump for the browser rather than leaving it frame-starved.
    """
    ending = MockFrameSource([])  # ends immediately -> pump dies
    live = MockFrameSource([_JPEG_B], hold_open=threading.Event(), loop=True)
    sources = [ending, live]
    index = 0
    lock = threading.Lock()

    def factory() -> MockFrameSource:
        nonlocal index
        with lock:
            source = sources[index]
            index += 1
        return source

    broker = MjpegBroker(factory)

    # A long-lived consumer subscribes; its source ends and the pump dies while
    # the consumer stays subscribed. Draining the end-of-stream sentinel proves
    # the pump has fully torn down (and cleared itself) before we continue.
    stale = broker.subscribe()
    assert stale.get(timeout=2.0) is None

    # A new consumer (browser) connects and must still receive frames.
    browser = broker.subscribe()
    try:
        assert browser.get(timeout=2.0) == _JPEG_B
    finally:
        broker.unsubscribe(stale)
        broker.unsubscribe(browser)
