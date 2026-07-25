"""Deterministic tests for MJPEG broker pump/source *generation* ownership.

These prove that a stale pump finishing after a replacement pump has started can
neither feed nor signal end-of-stream to the replacement generation's consumers.
Progression is driven entirely by ``threading.Event`` barriers and a queue-fed
source -- there are no timing-dependent sleeps in the synchronisation, so the
race is reproduced deterministically.

The stale-pump end-of-stream test (``test_stale_pump_does_not_deliver_eos_...``)
fails against the pre-fix implementation and passes after the generation-aware
correction.
"""

from __future__ import annotations

import queue
import threading
import time
from collections.abc import Iterator

import pytest

from mgo.camera.streaming import (
    FrameSource,
    MjpegBroker,
    MockFrameSource,
    Subscriber,
)

_JPEG_A = b"\xff\xd8" + b"AAAA" + b"\xff\xd9"
_JPEG_B = b"\xff\xd8" + b"BBBB" + b"\xff\xd9"
_JPEG_C = b"\xff\xd8" + b"CCCC" + b"\xff\xd9"


def _wait_for(predicate: object, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():  # type: ignore[operator]
            return True
        time.sleep(0.01)
    return False


class _EndingSource:
    """Yields its frames then ends; ``close`` blocks until released.

    This deterministically parks the owning pump *inside* its teardown -- after
    it has relinquished broker ownership but before it signals end-of-stream --
    which is exactly the window the generation guarantee must protect.
    """

    def __init__(self, frames: list[bytes], *, close_gate: threading.Event) -> None:
        self._frames = list(frames)
        self._close_gate = close_gate
        self.closed = False
        self.close_entered = threading.Event()

    def frames(self) -> Iterator[bytes]:
        yield from self._frames

    def close(self) -> None:
        self.closed = True
        self.close_entered.set()
        self._close_gate.wait(timeout=5.0)


class _QueueSource:
    """A frame source fed and terminated on demand (no timing dependence)."""

    _STOP = object()

    def __init__(self) -> None:
        self._queue: queue.Queue[object] = queue.Queue()
        self.closed = False

    def push(self, frame: bytes) -> None:
        self._queue.put(frame)

    def end(self) -> None:
        self._queue.put(self._STOP)

    def frames(self) -> Iterator[bytes]:
        while True:
            item = self._queue.get()
            if item is self._STOP or self.closed:
                return
            assert isinstance(item, bytes)
            yield item

    def close(self) -> None:
        self.closed = True
        self._queue.put(self._STOP)


class _RaisingSource:
    """Yields its frames and then fails, to exercise the pump's error path."""

    def __init__(self, frames: list[bytes]) -> None:
        self._frames = list(frames)
        self.closed = False

    def frames(self) -> Iterator[bytes]:
        yield from self._frames
        raise RuntimeError("source failure")

    def close(self) -> None:
        self.closed = True


def _fresh_live_broker(*frames: bytes) -> MjpegBroker:
    """A broker whose factory returns a *new* looping source per generation."""

    def factory() -> MockFrameSource:
        return MockFrameSource(
            list(frames), hold_open=threading.Event(), loop=True
        )

    return MjpegBroker(factory)


def _two_generations() -> tuple[
    MjpegBroker,
    Subscriber,
    Subscriber,
    _EndingSource,
    _QueueSource,
    threading.Event,
]:
    """Set up generation 1 parked in teardown, with generation 2 live.

    Returns ``(broker, a, b, gen1, gen2, close_gate)``:

    * ``a`` is a generation-1 consumer that received its frame;
    * generation 1's source is exhausted and its pump is parked in ``close``
      (ownership already relinquished);
    * ``b`` is a generation-2 consumer that received a known frame;
    * releasing ``close_gate`` lets generation 1 finish and signal end-of-stream.
    """
    close_gate = threading.Event()
    gen1 = _EndingSource([_JPEG_A], close_gate=close_gate)
    gen2 = _QueueSource()
    created: list[FrameSource] = []

    def factory() -> FrameSource:
        source: FrameSource = gen1 if not created else gen2
        created.append(source)
        return source

    broker = MjpegBroker(factory)

    a = broker.subscribe()
    assert a.get(timeout=2.0) == _JPEG_A
    # gen1 exhausted -> pump relinquished ownership and is now parked in close().
    assert gen1.close_entered.wait(timeout=2.0)

    # A pre-fix broker cannot start generation 2 while generation 1 is still
    # tearing down (it clears its references only *after* closing the source, and
    # gates new pumps on thread liveness), so this frame never arrives there.
    b = broker.subscribe()
    gen2.push(_JPEG_B)
    assert b.get(timeout=2.0) == _JPEG_B

    # Secondary confirmation of the generation binding (post-fix behaviour).
    assert a.generation == 1
    assert b.generation == 2

    return broker, a, b, gen1, gen2, close_gate


# --- the blocking-defect regression --------------------------------------


def test_stale_pump_does_not_deliver_eos_to_replacement_generation() -> None:
    """A stale pump's end-of-stream must not reach a replacement consumer.

    Reproduces the reviewed race deterministically. Fails against the pre-fix
    implementation (which broadcasts the sentinel to *all* current subscribers)
    and passes after generation-scoped delivery.
    """
    broker, a, b, _gen1, gen2, close_gate = _two_generations()

    # Release the stale generation-1 pump; it now signals end-of-stream.
    close_gate.set()

    # Barrier: the genuine generation-1 sentinel reaches its own consumer (A).
    # Once this returns, the stale broadcast has completed.
    assert a.get(timeout=2.0) is None

    # The stale sentinel must NOT have reached B (generation 2): its mailbox is
    # empty (correct) rather than holding a spurious end-of-stream (the defect).
    with pytest.raises(queue.Empty):
        b.get(timeout=0.5)

    # Generation 2 is unharmed and keeps delivering frames.
    gen2.push(_JPEG_C)
    assert b.get(timeout=2.0) == _JPEG_C

    # Genuine generation-2 termination still reaches B.
    gen2.end()
    assert b.get(timeout=2.0) is None

    broker.unsubscribe(a)
    broker.unsubscribe(b)


def test_stale_pump_does_not_clear_replacement_references() -> None:
    """A stale pump finishing must not clear the replacement's source/pump."""
    broker, a, b, _gen1, gen2, close_gate = _two_generations()

    close_gate.set()
    assert a.get(timeout=2.0) is None  # generation 1 fully torn down

    # Generation 2 still owns the broker: frames flow and a new consumer joins
    # the *same* live generation rather than triggering another restart.
    gen2.push(_JPEG_C)
    assert b.get(timeout=2.0) == _JPEG_C

    c = broker.subscribe()
    assert c.generation == 2
    gen2.push(_JPEG_C)
    assert c.get(timeout=2.0) == _JPEG_C

    broker.unsubscribe(a)
    broker.unsubscribe(b)
    broker.unsubscribe(c)


# --- genuine end-of-stream still reaches the right consumers ---------------


def test_natural_exhaustion_delivers_eos_to_its_consumer() -> None:
    """A source that runs out sends end-of-stream to its own consumer.

    The one-slot mailbox is latest-wins, so a fast-exhausting source may replace
    the single frame with the sentinel before it is read; the guarantee under
    test is that the genuine end-of-stream reaches the consumer.
    """
    broker = MjpegBroker(lambda: MockFrameSource([_JPEG_A]))
    sub = broker.subscribe()
    try:
        seen: list[bytes | None] = []
        for _ in range(3):
            seen.append(sub.get(timeout=2.0))
            if seen[-1] is None:
                break
        assert None in seen
    finally:
        broker.unsubscribe(sub)


def test_source_exception_delivers_eos_to_its_consumer() -> None:
    """A failing source still delivers a clean end-of-stream to its consumer."""
    broker = MjpegBroker(lambda: _RaisingSource([_JPEG_A]))
    sub = broker.subscribe()
    try:
        seen: list[bytes | None] = []
        for _ in range(3):
            seen.append(sub.get(timeout=2.0))
            if seen[-1] is None:
                break
        assert None in seen
    finally:
        broker.unsubscribe(sub)


# --- generation lifecycle robustness --------------------------------------


def test_each_pump_restart_advances_the_generation() -> None:
    """Every fresh pump start binds new consumers to a higher generation."""
    broker = _fresh_live_broker(_JPEG_A)

    first = broker.subscribe()
    assert first.get(timeout=2.0) == _JPEG_A
    first_generation = first.generation
    broker.unsubscribe(first)
    assert _wait_for(lambda: broker.viewer_count == 0)

    second = broker.subscribe()
    assert second.get(timeout=2.0) == _JPEG_A
    assert second.generation > first_generation
    broker.unsubscribe(second)


def test_rapid_unsubscribe_resubscribe_remains_usable() -> None:
    """Rapid churn never wedges the broker; each consumer still gets frames."""
    broker = _fresh_live_broker(_JPEG_A)

    for _ in range(5):
        sub = broker.subscribe()
        assert sub.get(timeout=2.0) == _JPEG_A
        broker.unsubscribe(sub)

    final = broker.subscribe()
    assert final.get(timeout=2.0) == _JPEG_A
    broker.unsubscribe(final)
    assert _wait_for(lambda: broker.viewer_count == 0)


def test_no_consumer_or_pump_leak_after_final_shutdown() -> None:
    """After every consumer leaves, the broker is idle yet immediately reusable."""
    broker = _fresh_live_broker(_JPEG_A)

    subscribers = [broker.subscribe() for _ in range(3)]
    for subscriber in subscribers:
        assert subscriber.get(timeout=2.0) == _JPEG_A
    for subscriber in subscribers:
        broker.unsubscribe(subscriber)

    assert _wait_for(lambda: broker.viewer_count == 0)

    # Not wedged: a later consumer starts a fresh generation and receives frames.
    again = broker.subscribe()
    assert again.get(timeout=2.0) == _JPEG_A
    assert again.generation >= 2
    broker.unsubscribe(again)
    assert _wait_for(lambda: broker.viewer_count == 0)
