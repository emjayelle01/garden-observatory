"""Tests for the MJPEG streaming layer (broker, demuxer, multipart framing).

No Raspberry Pi hardware is required: the frame source is mocked and the MJPEG
demuxer is exercised with in-memory byte streams. These cover multipart framing,
frame parsing, viewer connect/disconnect, multi-viewer fan-out, source cleanup
and the preview-process frame source's single-owner behaviour.
"""

from __future__ import annotations

import io
import threading
import time

from mgo.camera.streaming import (
    MJPEG_BOUNDARY,
    MjpegBroker,
    MockFrameSource,
    PreviewProcessFrameSource,
    Subscriber,
    encode_multipart_frame,
    parse_mjpeg_frames,
)

_JPEG_A = b"\xff\xd8" + b"AAAA" + b"\xff\xd9"
_JPEG_B = b"\xff\xd8" + b"BBBB" + b"\xff\xd9"
_JPEG_C = b"\xff\xd8" + b"CCCC" + b"\xff\xd9"


# --- multipart framing ----------------------------------------------------


def test_encode_multipart_frame_has_headers_and_boundary() -> None:
    """A frame is wrapped with the boundary and JPEG content headers."""
    part = encode_multipart_frame(_JPEG_A)

    assert part.startswith(b"--" + MJPEG_BOUNDARY.encode())
    assert b"Content-Type: image/jpeg\r\n" in part
    assert b"Content-Length: " + str(len(_JPEG_A)).encode() in part
    assert part.endswith(_JPEG_A + b"\r\n")


# --- MJPEG demuxer --------------------------------------------------------


def test_parse_mjpeg_frames_splits_on_markers() -> None:
    """Concatenated JPEGs are split into individual frames."""
    stream = io.BytesIO(_JPEG_A + _JPEG_B + _JPEG_C)

    frames = list(parse_mjpeg_frames(stream, chunk_size=4))

    assert frames == [_JPEG_A, _JPEG_B, _JPEG_C]


def test_parse_mjpeg_frames_discards_leading_noise() -> None:
    """Bytes before the first start marker are ignored."""
    stream = io.BytesIO(b"garbage-preamble" + _JPEG_A)

    frames = list(parse_mjpeg_frames(stream))

    assert frames == [_JPEG_A]


def test_parse_mjpeg_frames_ends_on_exhausted_stream() -> None:
    """A partial trailing frame yields nothing and the parser ends."""
    stream = io.BytesIO(_JPEG_A + b"\xff\xd8partial-no-end")

    frames = list(parse_mjpeg_frames(stream))

    assert frames == [_JPEG_A]


# --- subscriber mailbox ---------------------------------------------------


def test_subscriber_keeps_only_latest_frame() -> None:
    """The one-slot mailbox drops an unread frame in favour of the newest."""
    subscriber = Subscriber()

    subscriber.offer(_JPEG_A)
    subscriber.offer(_JPEG_B)

    assert subscriber.get(timeout=1.0) == _JPEG_B


# --- broker ---------------------------------------------------------------


def _wait_for(predicate: object, timeout: float = 2.0) -> bool:
    """Poll ``predicate`` until true or the timeout elapses."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():  # type: ignore[operator]
            return True
        time.sleep(0.01)
    return False


def test_broker_delivers_frames_to_a_viewer() -> None:
    """A subscribed viewer receives frames published by the pump."""
    hold = threading.Event()
    source = MockFrameSource([_JPEG_A, _JPEG_B], hold_open=hold)
    broker = MjpegBroker(lambda: source)

    subscriber = broker.subscribe()
    try:
        received = subscriber.get(timeout=2.0)
        assert received in (_JPEG_A, _JPEG_B)
    finally:
        broker.unsubscribe(subscriber)


def test_broker_tracks_viewer_count() -> None:
    """Viewer count reflects subscribe/unsubscribe."""
    hold = threading.Event()
    broker = MjpegBroker(lambda: MockFrameSource([_JPEG_A], hold_open=hold))

    assert broker.viewer_count == 0
    first = broker.subscribe()
    second = broker.subscribe()
    assert broker.viewer_count == 2

    broker.unsubscribe(first)
    broker.unsubscribe(second)
    assert broker.viewer_count == 0


def test_broker_fans_out_to_multiple_viewers() -> None:
    """Every connected viewer receives frames from the single source."""
    hold = threading.Event()
    broker = MjpegBroker(
        lambda: MockFrameSource([_JPEG_A, _JPEG_B, _JPEG_C], hold_open=hold)
    )

    first = broker.subscribe()
    second = broker.subscribe()
    try:
        assert first.get(timeout=2.0) is not None
        assert second.get(timeout=2.0) is not None
    finally:
        broker.unsubscribe(first)
        broker.unsubscribe(second)


def test_broker_closes_source_when_last_viewer_leaves() -> None:
    """The single frame source is closed once no viewers remain."""
    hold = threading.Event()
    source = MockFrameSource([_JPEG_A], hold_open=hold)
    broker = MjpegBroker(lambda: source)

    subscriber = broker.subscribe()
    broker.unsubscribe(subscriber)

    assert _wait_for(lambda: source.closed)


def test_broker_sends_end_sentinel_when_source_ends() -> None:
    """When the source ends, connected viewers receive the end sentinel."""
    # No hold_open: the source ends immediately after its frames.
    source = MockFrameSource([_JPEG_A])
    broker = MjpegBroker(lambda: source)

    subscriber = broker.subscribe()
    try:
        seen: list[bytes | None] = []
        for _ in range(3):
            seen.append(subscriber.get(timeout=2.0))
            if seen[-1] is None:
                break
        assert None in seen
    finally:
        broker.unsubscribe(subscriber)


# --- preview-process frame source (single owner) --------------------------


class _Provider:
    """A stand-in preview service exposing a frame stream."""

    def __init__(self, stream: io.BytesIO | None) -> None:
        self._stream = stream

    def frame_stream(self) -> io.BytesIO | None:
        return self._stream


def test_preview_process_frame_source_reads_provider_stream() -> None:
    """The source demuxes frames from the provider's stream."""
    provider = _Provider(io.BytesIO(_JPEG_A + _JPEG_B))
    source = PreviewProcessFrameSource(provider)

    assert list(source.frames()) == [_JPEG_A, _JPEG_B]


def test_preview_process_frame_source_yields_nothing_without_stream() -> None:
    """When no frame stream is exposed, the source ends immediately."""
    source = PreviewProcessFrameSource(_Provider(None))

    assert list(source.frames()) == []
