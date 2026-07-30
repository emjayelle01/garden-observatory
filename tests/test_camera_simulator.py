"""Tests for the deterministic camera simulator backend (Task 11).

These prove that the *real* MGO pipeline -- readiness, still capture, preview
lifecycle, MJPEG streaming, browser delivery, motion-frame consumption and the
API contracts -- operates end to end with no Raspberry Pi, no camera hardware,
no camera tooling, no subprocess, no external media file and no network access.

Every check exercises the production abstractions (``build_detector``,
``build_capture_backend``, ``build_preview_backend``, ``CaptureService``,
``PreviewService``, ``PreviewProcessFrameSource``, ``MjpegBroker``,
``BrokerFrameSource``, ``FrameDifferenceDetector``, ``encode_multipart_frame``
and the registered routes) rather than reimplementing any of them. The API
section drives the production ASGI application in-process.
"""

from __future__ import annotations

import ast
import asyncio
import functools
import io
import json
import queue
import subprocess
import threading
import time
import tomllib
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import IO, Any

import pytest
from PIL import Image

from mgo.api.app import app
from mgo.camera.backend import (
    NullBackend,
    RPiCamBackend,
    build_capture_backend,
)
from mgo.camera.capture import CaptureService
from mgo.camera.exceptions import (
    CameraUnavailableError,
    CaptureWriteError,
    PreviewStartError,
    PreviewUnavailableError,
)
from mgo.camera.preview import PreviewService, PreviewState
from mgo.camera.preview_backend import (
    NullPreviewBackend,
    RPiCamPreviewBackend,
    build_preview_backend,
)
from mgo.camera.simulator import (
    MAX_SIMULATOR_PREVIEW_FPS,
    MAX_SIMULATOR_PREVIEW_HEIGHT,
    MAX_SIMULATOR_PREVIEW_WIDTH,
    MIN_SIMULATOR_FRAME_HEIGHT,
    MIN_SIMULATOR_FRAME_WIDTH,
    SCENE_OBJECT_FAR,
    SCENE_OBJECT_NEAR,
    SCENE_QUIET,
    SIMULATOR_BACKEND_NAME,
    SIMULATOR_CAPTURE_HEIGHT,
    SIMULATOR_CAPTURE_WIDTH,
    SIMULATOR_FRAME_BUFFER_FRAMES,
    SIMULATOR_MARKER_TEXT,
    SIMULATOR_SEQUENCE_LENGTH,
    SIMULATOR_SEQUENCE_SCENES,
    SimulatorCaptureBackend,
    SimulatorFrameSequence,
    SimulatorPreviewBackend,
    SimulatorPreviewProcess,
    encode_simulator_frame,
    render_simulator_frame,
    simulator_scene,
)
from mgo.camera.streaming import (
    MJPEG_CONTENT_TYPE,
    MjpegBroker,
    PreviewProcessFrameSource,
    encode_multipart_frame,
    parse_mjpeg_frames,
)
from mgo.captures.archive import CaptureArchive
from mgo.core.camera import (
    CameraState,
    CameraStatus,
    DetectionOutcome,
    default_readiness,
    detect_camera_readiness,
)
from mgo.core.camera_detection import (
    SIMULATOR_READINESS_DETAIL,
    CommandCameraDetector,
    NullCameraDetector,
    SimulatorCameraDetector,
    build_detector,
)
from mgo.core.config import (
    SUPPORTED_CAMERA_BACKENDS,
    CameraConfig,
    MGOConfig,
    MotionConfig,
    PreviewConfig,
    load_config,
)
from mgo.core.database import apply_migrations
from mgo.motion.detector import FrameDifferenceDetector
from mgo.motion.frame_source import BrokerFrameSource
from mgo.motion.models import MotionResult, MotionStatus
from mgo.motion.monitor import MotionState, run_motion_monitor

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]

#: A small, fast preview geometry for tests: well inside the simulator bounds and
#: cheap enough that a generated frame costs milliseconds.
_TEST_WIDTH = 320
_TEST_HEIGHT = 240

#: Generous bounds for waiting on a real producer thread. Deliberately far wider
#: than ordinary Windows/Linux scheduling needs, so nothing here is timing-fragile.
_FRAME_WAIT_SECONDS = 5.0
_POLL_SECONDS = 0.01

#: Substrings that would amount to a physical-camera claim. The simulator must
#: never emit any of these in readiness evidence.
_HARDWARE_CLAIM_MARKERS = (
    "imx",
    "rpicam",
    "libcamera",
    "/dev/video",
    "device_index",
    "serial",
    "enumerat",
    "detected camera device",
)


# -- shared fixtures and helpers ----------------------------------------------


def _camera_config(
    capture_directory: Path,
    *,
    enabled: bool = True,
    backend: str = SIMULATOR_BACKEND_NAME,
) -> CameraConfig:
    """Build an isolated camera configuration for a test."""
    return CameraConfig(
        enabled=enabled,
        backend=backend,
        device_index=None,
        detection_interval_seconds=60,
        capture_directory=capture_directory,
    )


def _preview_config(
    *,
    enabled: bool = True,
    width: int = _TEST_WIDTH,
    height: int = _TEST_HEIGHT,
    fps: int = 15,
    startup_timeout_seconds: float = 5.0,
    shutdown_timeout_seconds: float = 5.0,
) -> PreviewConfig:
    """Build an isolated preview configuration for a test."""
    return PreviewConfig(
        enabled=enabled,
        width=width,
        height=height,
        fps=fps,
        startup_timeout_seconds=startup_timeout_seconds,
        shutdown_timeout_seconds=shutdown_timeout_seconds,
    )


def _motion_config(
    *,
    enabled: bool = True,
    analysis_interval_seconds: float = 0.02,
) -> MotionConfig:
    """Build a motion configuration using the production default thresholds."""
    return MotionConfig(
        enabled=enabled,
        analysis_interval_seconds=analysis_interval_seconds,
        analysis_width=160,
        analysis_height=90,
        pixel_difference_threshold=20,
        changed_pixel_ratio_threshold=0.08,
        cooldown_seconds=0.0,
    )


@pytest.fixture
def simulator_preview() -> Iterator[PreviewService]:
    """A preview service wired to the simulator, always stopped afterwards."""
    service = PreviewService(
        _preview_config(), build_preview_backend(SIMULATOR_BACKEND_NAME)
    )
    try:
        yield service
    finally:
        service.shutdown()


@pytest.fixture
def simulator_process() -> Iterator[SimulatorPreviewProcess]:
    """A started simulator preview process, always closed afterwards."""
    process = SimulatorPreviewBackend().start(_preview_config())
    try:
        yield process
    finally:
        process.close()


def _sequence_frames(
    *, width: int = _TEST_WIDTH, height: int = _TEST_HEIGHT
) -> list[bytes]:
    """Return one full deterministic sequence of encoded frames."""
    return [
        encode_simulator_frame(index, width, height)
        for index in range(SIMULATOR_SEQUENCE_LENGTH)
    ]


def _luma(frame: bytes, size: tuple[int, int] = (160, 90)) -> bytes:
    """Decode ``frame`` to greyscale pixels at ``size`` for pixel comparison."""
    with Image.open(io.BytesIO(frame)) as image:
        return image.convert("L").resize(size, Image.Resampling.BILINEAR).tobytes()


def _changed_pixel_ratio(first: bytes, second: bytes) -> float:
    """Return the proportion of analysis pixels that differ beyond noise."""
    left = _luma(first)
    right = _luma(second)
    changed = sum(
        1 for a, b in zip(left, right, strict=True) if abs(a - b) > 20
    )
    return changed / len(left)


def _marker_region_extremes(image: Image.Image) -> tuple[int, int]:
    """Return the darkest and lightest luminance in the marker's corner.

    The marker is a light plate carrying dark text in the top-left corner, so a
    frame that carries it shows a strong light/dark contrast there. This works on
    a freshly rendered image *and* on one decoded back from lossy JPEG, where
    exact colour equality no longer holds.
    """
    corner = image.crop((0, 0, image.width // 2, image.height // 4)).convert("L")
    pixels = corner.tobytes()
    return min(pixels), max(pixels)


def _wait_until(predicate: Callable[[], bool], timeout: float) -> bool:
    """Poll ``predicate`` until true or ``timeout`` elapses."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(_POLL_SECONDS)
    return predicate()


def _read_frames(
    source: PreviewProcessFrameSource,
    count: int,
    *,
    timeout: float = _FRAME_WAIT_SECONDS,
) -> list[bytes]:
    """Read up to ``count`` frames from a frame source, bounded by ``timeout``.

    The read happens on a helper thread so a source that stops producing makes
    the calling test *fail* rather than hang it. Nothing here waits longer than
    it has to: a healthy simulator delivers every frame well inside the bound.
    """
    collected: list[bytes] = []

    def _pump() -> None:
        for frame in source.frames():
            collected.append(frame)
            if len(collected) >= count:
                return

    reader = threading.Thread(target=_pump, name="mgo-test-reader", daemon=True)
    reader.start()
    reader.join(timeout)
    return list(collected)


def _first_frame(stream: IO[bytes], timeout: float) -> bytes | None:
    """Return the first complete JPEG on ``stream``, or ``None`` on timeout.

    Bounded for the same reason as :func:`_read_frames`: a stream that never
    produces must fail a test, never block it for ever.
    """
    collected: list[bytes] = []

    def _read() -> None:
        for frame in parse_mjpeg_frames(stream):
            collected.append(frame)
            return

    reader = threading.Thread(target=_read, name="mgo-test-reader", daemon=True)
    reader.start()
    reader.join(timeout)
    return collected[0] if collected else None


_SIMULATOR_SOURCE_PATH = _REPOSITORY_ROOT / "src" / "mgo" / "camera" / "simulator.py"


@functools.lru_cache(maxsize=1)
def _simulator_ast() -> ast.Module:
    """Parse the simulator module once for structural assertions."""
    return ast.parse(
        _SIMULATOR_SOURCE_PATH.read_text(encoding="utf-8"),
        filename=str(_SIMULATOR_SOURCE_PATH),
    )


@functools.lru_cache(maxsize=1)
def _simulator_imports() -> frozenset[str]:
    """Return every module name the simulator imports."""
    names: set[str] = set()
    for node in ast.walk(_simulator_ast()):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            names.add(node.module)
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
    return frozenset(names)


@functools.lru_cache(maxsize=1)
def _simulator_identifiers() -> frozenset[str]:
    """Return every identifier the simulator's code refers to."""
    names: set[str] = set()
    for node in ast.walk(_simulator_ast()):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.alias):
            names.add(node.name.rsplit(".", 1)[-1])
    return frozenset(names)


@functools.lru_cache(maxsize=1)
def _simulator_code_strings() -> tuple[str, ...]:
    """Return the module's string literals, excluding every docstring.

    Docstrings are prose: they may legitimately mention what the simulator does
    *not* do. Anything else is executable data and is held to a stricter rule.
    """
    tree = _simulator_ast()
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(
            node,
            ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef,
        ):
            body = getattr(node, "body", [])
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                docstrings.add(id(body[0].value))
    return tuple(
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    )


class _ExplodingRunner:
    """A command runner that fails the test if it is ever called."""

    def __init__(self) -> None:
        self.calls: list[Sequence[str]] = []

    def __call__(self, args: Sequence[str], *, timeout: float) -> Any:
        self.calls.append(tuple(args))
        raise AssertionError(
            f"The simulator backend must never run a command: {tuple(args)!r}"
        )


@pytest.fixture
def no_subprocess(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make any real subprocess launch an immediate, loud test failure."""

    def _forbidden(*args: object, **kwargs: object) -> Any:
        raise AssertionError(
            f"The simulator path must not launch a subprocess: {args!r}"
        )

    monkeypatch.setattr(subprocess, "run", _forbidden)
    monkeypatch.setattr(subprocess, "Popen", _forbidden)
    monkeypatch.setattr("mgo.core.camera_detection.run_subprocess", _forbidden)
    monkeypatch.setattr(
        "mgo.camera.preview_backend.launch_preview_subprocess", _forbidden
    )


# -- 22.1 configuration -------------------------------------------------------


_CONFIG_TEMPLATE = """
[application]
name = "Matt's Garden Observatory"
environment = "test"
host = "127.0.0.1"
port = 8080

[storage]
data_directory = "data"
log_directory = "logs"
database_path = "data/mgo.db"

[camera]
enabled = true
backend = "{backend}"
detection_interval_seconds = 60
capture_directory = "data/captures"

[health]
enabled = true
collection_interval_seconds = 60
temperature_warning_celsius = 70.0
temperature_critical_celsius = 80.0
disk_warning_percent = 80.0
disk_critical_percent = 90.0
memory_warning_percent = 85.0
memory_critical_percent = 95.0
"""


def _write_backend_config(tmp_path: Path, backend: str) -> Path:
    """Write a minimal configuration selecting ``backend``."""
    path = tmp_path / "mgo.toml"
    path.write_text(
        _CONFIG_TEMPLATE.format(backend=backend), encoding="utf-8"
    )
    return path


def test_simulator_is_a_supported_camera_backend() -> None:
    """``simulator`` must be part of the supported backend vocabulary."""
    assert SIMULATOR_BACKEND_NAME in SUPPORTED_CAMERA_BACKENDS


def test_supported_backend_vocabulary_is_exactly_the_agreed_set() -> None:
    """The vocabulary is the five agreed values -- no more, no fewer."""
    assert set(SUPPORTED_CAMERA_BACKENDS) == {
        "rpicam",
        "libcamera",
        "simulator",
        "null",
        "none",
    }


@pytest.mark.parametrize(
    "backend", ["rpicam", "libcamera", "simulator", "null", "none"]
)
def test_configuration_accepts_every_supported_backend(
    tmp_path: Path, backend: str
) -> None:
    """Adding the simulator must not break any existing backend value."""
    config = load_config(_write_backend_config(tmp_path, backend))

    assert config.camera.backend == backend


@pytest.mark.parametrize("backend", ["  simulator  ", "SIMULATOR", "Simulator"])
def test_simulator_backend_matching_is_trimmed_and_case_normalised(
    tmp_path: Path, backend: str
) -> None:
    """Backend matching keeps the existing trim/case-normalise behaviour."""
    config = load_config(_write_backend_config(tmp_path, backend))

    # The configured spelling is preserved verbatim...
    assert config.camera.backend == backend
    # ...while every factory still resolves it to the simulator.
    assert isinstance(build_detector(config.camera.backend), SimulatorCameraDetector)
    assert isinstance(
        build_capture_backend(config.camera), SimulatorCaptureBackend
    )
    assert isinstance(
        build_preview_backend(config.camera.backend), SimulatorPreviewBackend
    )


@pytest.mark.parametrize("backend", ["\tSimulator\n", "\n simulator \t", "SiMuLaToR"])
def test_the_factories_normalise_surrounding_whitespace_and_case(
    tmp_path: Path, backend: str
) -> None:
    """Every factory trims and case-folds exactly as it already did."""
    assert isinstance(build_detector(backend), SimulatorCameraDetector)
    assert isinstance(
        build_capture_backend(
            _camera_config(tmp_path / "captures", backend=backend)
        ),
        SimulatorCaptureBackend,
    )
    assert isinstance(build_preview_backend(backend), SimulatorPreviewBackend)


@pytest.mark.parametrize("backend", ["webcam", "sim", "simulated", "picamera", ""])
def test_configuration_still_rejects_unsupported_backends(
    tmp_path: Path, backend: str
) -> None:
    """An unsupported backend remains a configuration error."""
    with pytest.raises(ValueError, match="Unsupported camera backend"):
        load_config(_write_backend_config(tmp_path, backend))


def test_configuration_omitting_camera_backend_keeps_the_existing_default(
    tmp_path: Path,
) -> None:
    """A configuration file without ``backend`` still defaults to ``rpicam``."""
    path = tmp_path / "mgo.toml"
    path.write_text(
        _CONFIG_TEMPLATE.format(backend="rpicam").replace(
            'backend = "rpicam"\n', ""
        ),
        encoding="utf-8",
    )

    assert load_config(path).camera.backend == "rpicam"


def test_task_11_adds_no_configuration_setting(tmp_path: Path) -> None:
    """A pre-Task-11 configuration file loads with unchanged values.

    Task 11 introduces no setting, so a file that predates it cannot be missing
    one: the loaded camera and preview sections must match the defaults exactly.
    """
    config = load_config(_write_backend_config(tmp_path, "rpicam"))

    assert config.camera.enabled is True
    assert config.camera.device_index is None
    assert config.camera.detection_interval_seconds == 60
    # The preview section is absent from the file entirely and keeps its defaults.
    assert config.preview.enabled is False
    assert (config.preview.width, config.preview.height) == (1280, 720)
    assert config.preview.fps == 15
    assert config.motion.enabled is False
    assert config.notifications.enabled is False


@pytest.mark.parametrize(
    "relative", ["config/mgo.toml", "config/mgo.production.example.toml"]
)
def test_tracked_configurations_do_not_select_the_simulator(
    relative: str,
) -> None:
    """No tracked configuration file may quietly enable simulated imagery."""
    data = tomllib.loads(
        (_REPOSITORY_ROOT / relative).read_text(encoding="utf-8")
    )

    backend = str(data["camera"]["backend"]).strip().lower()
    assert backend != SIMULATOR_BACKEND_NAME
    assert backend in SUPPORTED_CAMERA_BACKENDS


def test_repository_default_configuration_remains_hardware_safe() -> None:
    """The default configuration stays disabled and pointed at real hardware."""
    camera = load_config().camera

    assert camera.enabled is False
    assert camera.backend == "rpicam"


# -- 22.2 readiness -----------------------------------------------------------


def test_build_detector_selects_the_simulator_detector() -> None:
    """The existing detector factory resolves ``simulator``."""
    assert isinstance(
        build_detector(SIMULATOR_BACKEND_NAME), SimulatorCameraDetector
    )


def test_existing_detectors_are_unchanged() -> None:
    """The physical and null detector branches keep their existing behaviour."""
    assert isinstance(build_detector("rpicam"), CommandCameraDetector)
    assert isinstance(build_detector("libcamera"), CommandCameraDetector)
    assert isinstance(build_detector("null"), NullCameraDetector)
    assert isinstance(build_detector("none"), NullCameraDetector)
    with pytest.raises(ValueError, match="Unsupported camera backend"):
        build_detector("webcam")


def test_simulator_readiness_is_available_and_truthful(tmp_path: Path) -> None:
    """An enabled simulator reports available, named as the simulator."""
    config = _camera_config(tmp_path / "captures")

    readiness = detect_camera_readiness(config, build_detector(config.backend))

    assert readiness.enabled is True
    assert readiness.backend == SIMULATOR_BACKEND_NAME
    assert readiness.status is CameraStatus.AVAILABLE
    assert readiness.available is True
    assert readiness.detail == SIMULATOR_READINESS_DETAIL


def test_simulator_readiness_detail_is_the_one_stable_sentence() -> None:
    """The detail sentence is fixed, so tests and docs cannot drift from it."""
    assert SIMULATOR_READINESS_DETAIL == (
        "Deterministic camera simulator is active; "
        "no physical camera is in use."
    )
    evidence = SimulatorCameraDetector().detect(
        _camera_config(Path("unused-in-detection"))
    )
    assert evidence.outcome is DetectionOutcome.DETECTED
    assert evidence.detail == SIMULATOR_READINESS_DETAIL


def test_simulator_readiness_makes_no_physical_camera_claim(
    tmp_path: Path,
) -> None:
    """Readiness must not imply a device, sensor, command or enumeration."""
    readiness = detect_camera_readiness(
        _camera_config(tmp_path / "captures"),
        build_detector(SIMULATOR_BACKEND_NAME),
    )

    lowered = readiness.detail.lower()
    for marker in _HARDWARE_CLAIM_MARKERS:
        assert marker not in lowered, marker
    assert "simulat" in lowered
    assert "no physical camera" in lowered


def test_disabled_camera_overrides_simulator_selection(tmp_path: Path) -> None:
    """The top-level enabled gate always wins over the backend choice."""
    config = _camera_config(tmp_path / "captures", enabled=False)

    readiness = detect_camera_readiness(config, build_detector(config.backend))

    assert readiness.status is CameraStatus.DISABLED
    assert readiness.available is False
    assert readiness.enabled is False
    assert readiness.detail == (
        "Camera functionality is disabled by configuration."
    )
    # The safe pre-detection default agrees.
    assert default_readiness(config).status is CameraStatus.DISABLED


def test_simulator_detection_runs_no_command(tmp_path: Path) -> None:
    """Detection must not reach a command runner at all."""
    runner = _ExplodingRunner()
    detector = build_detector(SIMULATOR_BACKEND_NAME)
    # The simulator detector takes no runner: prove it by also making the
    # module-level runner fatal for the duration of the call.
    assert not hasattr(detector, "_runner")

    readiness = detect_camera_readiness(
        _camera_config(tmp_path / "captures"), detector
    )

    assert readiness.available is True
    assert runner.calls == []


@pytest.mark.usefixtures("no_subprocess")
def test_simulator_detection_launches_no_subprocess(tmp_path: Path) -> None:
    """No subprocess API may be touched by simulator detection."""
    readiness = detect_camera_readiness(
        _camera_config(tmp_path / "captures"),
        build_detector(SIMULATOR_BACKEND_NAME),
    )

    assert readiness.status is CameraStatus.AVAILABLE


def test_simulator_detection_probes_no_filesystem(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Detection must not stat, open or list anything on disk."""

    def _forbidden(*args: object, **kwargs: object) -> Any:
        raise AssertionError("Simulator detection must not touch the filesystem")

    monkeypatch.setattr(Path, "exists", _forbidden)
    monkeypatch.setattr(Path, "stat", _forbidden)
    monkeypatch.setattr(Path, "iterdir", _forbidden)

    evidence = SimulatorCameraDetector().detect(
        _camera_config(tmp_path / "captures")
    )

    assert evidence.detail == SIMULATOR_READINESS_DETAIL


def test_repeated_simulator_detection_is_materially_identical(
    tmp_path: Path,
) -> None:
    """Repeated checks agree, so the monitor records no spurious transition."""
    config = _camera_config(tmp_path / "captures")
    detector = build_detector(config.backend)

    results = [
        detect_camera_readiness(config, detector).as_dict() for _ in range(5)
    ]

    for result in results:
        # ``checked_at`` legitimately advances; nothing else may.
        result.pop("checked_at")
    assert results[1:] == results[:-1]


# -- 22.3 frame generation ----------------------------------------------------


@pytest.mark.parametrize("index", range(SIMULATOR_SEQUENCE_LENGTH))
def test_every_sequence_frame_is_a_decodable_jpeg(index: int) -> None:
    """Each generated frame is a real JPEG at exactly the requested size."""
    payload = encode_simulator_frame(index, _TEST_WIDTH, _TEST_HEIGHT)

    assert payload.startswith(b"\xff\xd8")
    assert payload.endswith(b"\xff\xd9")
    with Image.open(io.BytesIO(payload)) as image:
        assert image.format == "JPEG"
        assert image.size == (_TEST_WIDTH, _TEST_HEIGHT)
        assert image.mode == "RGB"
        image.load()


@pytest.mark.parametrize(
    ("width", "height"),
    [
        (MIN_SIMULATOR_FRAME_WIDTH, MIN_SIMULATOR_FRAME_HEIGHT),
        (640, 480),
        (1280, 720),
        (MAX_SIMULATOR_PREVIEW_WIDTH, MAX_SIMULATOR_PREVIEW_HEIGHT),
    ],
)
def test_requested_dimensions_are_exact(width: int, height: int) -> None:
    """Generated frames honour the requested geometry precisely."""
    with Image.open(io.BytesIO(encode_simulator_frame(0, width, height))) as image:
        assert image.size == (width, height)


def test_generated_frames_carry_no_exif_metadata() -> None:
    """No EXIF, timestamp, hostname or path metadata may be embedded."""
    payload = encode_simulator_frame(2, _TEST_WIDTH, _TEST_HEIGHT)

    assert b"Exif" not in payload
    with Image.open(io.BytesIO(payload)) as image:
        assert dict(image.getexif()) == {}
        assert image.info.get("exif") is None
        assert image.info.get("comment") is None
        # Only the standard JFIF density header is present.
        assert set(image.info) <= {
            "jfif",
            "jfif_density",
            "jfif_unit",
            "jfif_version",
        }


@pytest.mark.parametrize("index", range(SIMULATOR_SEQUENCE_LENGTH))
def test_every_frame_contains_the_simulator_marker(index: int) -> None:
    """The static marker is drawn into the scene of every frame.

    The marker is a light plate carrying dark text in the top-left corner; both
    colours must be present there in every frame, so a simulated image is
    identifiable by looking at it.
    """
    with render_simulator_frame(index, _TEST_WIDTH, _TEST_HEIGHT) as image:
        corner = image.crop((0, 0, _TEST_WIDTH // 2, _TEST_HEIGHT // 4))
        colours = {colour for _count, colour in (corner.getcolors(4096) or [])}
        darkest, lightest = _marker_region_extremes(image)

    assert (238, 240, 236) in colours, "marker plate missing"
    assert (26, 28, 26) in colours, "marker text missing"
    assert darkest < 60
    assert lightest > 200


@pytest.mark.parametrize("index", range(SIMULATOR_SEQUENCE_LENGTH))
def test_the_marker_survives_jpeg_encoding(index: int) -> None:
    """The marker is still visible after the frame is encoded and decoded."""
    payload = encode_simulator_frame(index, _TEST_WIDTH, _TEST_HEIGHT)

    with Image.open(io.BytesIO(payload)) as image:
        darkest, lightest = _marker_region_extremes(image)

    assert darkest < 60, "marker text lost in encoding"
    assert lightest > 200, "marker plate lost in encoding"


def test_the_marker_text_is_the_documented_sentence() -> None:
    """The marker string is fixed, so documentation cannot drift from it."""
    assert SIMULATOR_MARKER_TEXT == "MGO CAMERA SIMULATOR"


@pytest.mark.parametrize("index", range(SIMULATOR_SEQUENCE_LENGTH))
def test_frame_generation_is_deterministic(index: int) -> None:
    """The same index and geometry always produce the same bytes."""
    first = encode_simulator_frame(index, _TEST_WIDTH, _TEST_HEIGHT)
    second = encode_simulator_frame(index, _TEST_WIDTH, _TEST_HEIGHT)

    assert first == second


def test_frame_generation_ignores_the_wall_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Moving the clock cannot change a generated frame."""
    before = encode_simulator_frame(4, _TEST_WIDTH, _TEST_HEIGHT)
    monkeypatch.setattr(time, "time", lambda: 4_102_444_800.0)
    monkeypatch.setattr(time, "monotonic", lambda: 999_999.0)

    assert encode_simulator_frame(4, _TEST_WIDTH, _TEST_HEIGHT) == before


@pytest.mark.parametrize("pair", [(0, 1), (2, 3), (4, 5), (6, 7)])
def test_sequence_pairs_are_identical(pair: tuple[int, int]) -> None:
    """The deliberate repeated pairs are byte- and pixel-identical.

    These pairs let the existing frame-difference detector settle to
    ``no_motion``; if any of them differed the simulator would claim continuous
    motion.
    """
    first, second = pair
    frames = _sequence_frames()

    assert frames[first] == frames[second]
    assert _luma(frames[first]) == _luma(frames[second])
    assert _changed_pixel_ratio(frames[first], frames[second]) == 0.0


@pytest.mark.parametrize("transition", [(1, 2), (3, 4), (5, 6)])
def test_sequence_transitions_are_meaningfully_different(
    transition: tuple[int, int],
) -> None:
    """Each scene transition changes a large, decodable share of the pixels."""
    first, second = transition
    frames = _sequence_frames()

    assert frames[first] != frames[second]
    assert _luma(frames[first]) != _luma(frames[second])
    # Comfortably above the production changed-pixel ratio threshold (0.08).
    assert _changed_pixel_ratio(frames[first], frames[second]) > 0.10


def test_the_scene_sequence_is_the_documented_one() -> None:
    """The logical sequence is quiet, quiet, near, near, far, far, quiet, quiet."""
    assert SIMULATOR_SEQUENCE_SCENES == (
        SCENE_QUIET,
        SCENE_QUIET,
        SCENE_OBJECT_NEAR,
        SCENE_OBJECT_NEAR,
        SCENE_OBJECT_FAR,
        SCENE_OBJECT_FAR,
        SCENE_QUIET,
        SCENE_QUIET,
    )
    assert len(SIMULATOR_SEQUENCE_SCENES) == SIMULATOR_SEQUENCE_LENGTH


def test_the_sequence_repeats_indefinitely() -> None:
    """Index arithmetic wraps, so a long-running preview loops the sequence."""
    frames = _sequence_frames()

    for index in range(SIMULATOR_SEQUENCE_LENGTH * 3):
        expected = frames[index % SIMULATOR_SEQUENCE_LENGTH]
        assert encode_simulator_frame(index, _TEST_WIDTH, _TEST_HEIGHT) == expected
        assert simulator_scene(index) == SIMULATOR_SEQUENCE_SCENES[
            index % SIMULATOR_SEQUENCE_LENGTH
        ]


def test_no_frame_draws_a_counter_or_timestamp() -> None:
    """Only three distinct images exist, so nothing time-varying is drawn."""
    distinct = {
        encode_simulator_frame(index, _TEST_WIDTH, _TEST_HEIGHT)
        for index in range(SIMULATOR_SEQUENCE_LENGTH * 4)
    }

    assert len(distinct) == 3


@pytest.mark.parametrize(
    ("width", "height"),
    [
        (MIN_SIMULATOR_FRAME_WIDTH - 1, MIN_SIMULATOR_FRAME_HEIGHT),
        (MIN_SIMULATOR_FRAME_WIDTH, MIN_SIMULATOR_FRAME_HEIGHT - 1),
        (MAX_SIMULATOR_PREVIEW_WIDTH + 1, 1080),
        (1920, MAX_SIMULATOR_PREVIEW_HEIGHT + 1),
        (0, 0),
        (-1, -1),
        (100_000, 100_000),
    ],
)
def test_frame_generation_refuses_unsupported_dimensions(
    width: int, height: int
) -> None:
    """Out-of-bounds geometry is refused rather than generated or clamped."""
    with pytest.raises(ValueError, match="Simulator frame"):
        encode_simulator_frame(0, width, height)


def test_frame_sequence_reuses_one_encoding_per_scene() -> None:
    """The sequence caches per scene, so repeated pairs are identical by design."""
    sequence = SimulatorFrameSequence(_TEST_WIDTH, _TEST_HEIGHT)

    assert (sequence.width, sequence.height) == (_TEST_WIDTH, _TEST_HEIGHT)
    frames = [sequence.frame(index) for index in range(SIMULATOR_SEQUENCE_LENGTH * 2)]
    # Identity (not just equality): the same object is handed out per scene.
    assert frames[0] is frames[1] is frames[6] is frames[7]
    assert frames[2] is frames[3]
    assert frames[4] is frames[5]
    assert len({id(frame) for frame in frames}) == 3


def test_frame_sequence_refuses_unsupported_dimensions() -> None:
    """The sequence validates geometry up front, before any producer starts."""
    with pytest.raises(ValueError, match="Simulator frame width"):
        SimulatorFrameSequence(MAX_SIMULATOR_PREVIEW_WIDTH + 1, 720)


def test_no_binary_media_fixture_is_committed() -> None:
    """Frames are generated, so the repository carries no image/video fixture."""
    patterns = ("*.jpg", "*.jpeg", "*.png", "*.mjpeg", "*.h264", "*.mp4")
    for directory in (_REPOSITORY_ROOT / "tests", _REPOSITORY_ROOT / "src"):
        for pattern in patterns:
            assert list(directory.rglob(pattern)) == []


# -- 22.4 capture backend -----------------------------------------------------


def test_build_capture_backend_selects_the_simulator(tmp_path: Path) -> None:
    """The existing capture factory resolves ``simulator``."""
    backend = build_capture_backend(_camera_config(tmp_path / "captures"))

    assert isinstance(backend, SimulatorCaptureBackend)
    assert backend.name == SIMULATOR_BACKEND_NAME


def test_existing_capture_backends_are_unchanged(tmp_path: Path) -> None:
    """The physical and null capture branches keep their existing behaviour."""
    directory = tmp_path / "captures"
    for backend_name, expected in (
        ("rpicam", RPiCamBackend),
        ("libcamera", RPiCamBackend),
        ("null", NullBackend),
        ("none", NullBackend),
    ):
        built = build_capture_backend(
            _camera_config(directory, backend=backend_name)
        )
        assert isinstance(built, expected)
    assert (
        build_capture_backend(_camera_config(directory, backend="rpicam")).name
        == "rpicam-still"
    )
    assert build_capture_backend(_camera_config(directory, backend="null")).name == (
        "null"
    )


def test_simulator_capture_writes_a_truthful_jpeg(tmp_path: Path) -> None:
    """A capture writes one decodable JPEG and reports its real dimensions."""
    destination = tmp_path / "capture.jpg"

    dimensions = SimulatorCaptureBackend().capture(destination)

    assert dimensions.width == SIMULATOR_CAPTURE_WIDTH
    assert dimensions.height == SIMULATOR_CAPTURE_HEIGHT
    assert destination.stat().st_size > 0
    with Image.open(destination) as image:
        assert image.format == "JPEG"
        assert image.size == (dimensions.width, dimensions.height)
        assert image.mode == "RGB"
        assert dict(image.getexif()) == {}


def test_simulator_capture_is_deterministic(tmp_path: Path) -> None:
    """Repeated captures produce identical image content."""
    backend = SimulatorCaptureBackend()
    first = tmp_path / "one.jpg"
    second = tmp_path / "two.jpg"

    backend.capture(first)
    backend.capture(second)

    assert first.read_bytes() == second.read_bytes()


def test_simulator_capture_contains_the_marker(tmp_path: Path) -> None:
    """A captured still is visibly identifiable as simulated."""
    destination = tmp_path / "capture.jpg"
    SimulatorCaptureBackend().capture(destination)

    with Image.open(destination) as image:
        darkest, lightest = _marker_region_extremes(image)

    assert darkest < 60, "marker text missing from the capture"
    assert lightest > 200, "marker plate missing from the capture"


def test_simulator_capture_maps_write_failure_to_the_domain_error(
    tmp_path: Path,
) -> None:
    """A filesystem failure becomes a CaptureWriteError, never a raw OSError."""
    unwritable = tmp_path / "missing-directory" / "capture.jpg"

    with pytest.raises(CaptureWriteError, match="simulated capture"):
        SimulatorCaptureBackend().capture(unwritable)


def test_simulator_capture_error_message_is_not_a_raw_oserror(
    tmp_path: Path,
) -> None:
    """The mapped error is a camera-domain error, not an OSError subclass."""
    with pytest.raises(CaptureWriteError) as excinfo:
        SimulatorCaptureBackend().capture(tmp_path / "absent" / "x.jpg")

    assert not isinstance(excinfo.value, OSError)


@pytest.mark.usefixtures("no_subprocess")
def test_simulator_capture_launches_no_subprocess(tmp_path: Path) -> None:
    """Capture must not touch any subprocess API or camera command."""
    dimensions = SimulatorCaptureBackend().capture(tmp_path / "capture.jpg")

    assert (dimensions.width, dimensions.height) == (
        SIMULATOR_CAPTURE_WIDTH,
        SIMULATOR_CAPTURE_HEIGHT,
    )


def test_capture_service_drives_the_simulator_unchanged(tmp_path: Path) -> None:
    """The real capture service produces truthful metadata for the simulator."""
    directory = tmp_path / "captures"
    config = _camera_config(directory)
    service = CaptureService(config, build_capture_backend(config))

    result = service.capture_image()

    assert result.success is True
    assert result.backend == SIMULATOR_BACKEND_NAME
    assert result.width == SIMULATOR_CAPTURE_WIDTH
    assert result.height == SIMULATOR_CAPTURE_HEIGHT
    # Filename generation, destination selection and directory creation all ran.
    assert result.filename.endswith(".jpg")
    assert result.absolute_path.parent == directory.resolve()
    assert result.absolute_path.exists()
    assert result.filesize_bytes == result.absolute_path.stat().st_size
    with Image.open(result.absolute_path) as image:
        assert image.size == (result.width, result.height)


def test_capture_service_still_honours_the_disabled_gate(tmp_path: Path) -> None:
    """Selecting the simulator does not bypass ``camera.enabled = false``."""
    config = _camera_config(tmp_path / "captures", enabled=False)
    service = CaptureService(config, build_capture_backend(config))

    with pytest.raises(CameraUnavailableError, match="disabled by configuration"):
        service.capture_image()


def test_simulator_capture_is_recorded_in_an_isolated_archive(
    tmp_path: Path,
) -> None:
    """Archive insertion works against a temporary database."""
    database = tmp_path / "mgo.db"
    apply_migrations(database, busy_timeout_seconds=5.0)
    archive = CaptureArchive(database)
    config = _camera_config(tmp_path / "captures")
    result = CaptureService(config, build_capture_backend(config)).capture_image()

    record = archive.record_capture(result)

    assert record.camera_backend == SIMULATOR_BACKEND_NAME
    listed = archive.list_captures()
    assert [capture.id for capture in listed] == [record.id]
    assert listed[0].width == SIMULATOR_CAPTURE_WIDTH
    assert listed[0].height == SIMULATOR_CAPTURE_HEIGHT


def test_simulator_capture_touches_no_production_path(tmp_path: Path) -> None:
    """Captures only ever land under the configured temporary directory."""
    directory = tmp_path / "captures"
    config = _camera_config(directory)

    result = CaptureService(config, build_capture_backend(config)).capture_image()

    assert directory.resolve() in result.absolute_path.parents
    assert list(directory.iterdir()) == [result.absolute_path]


# -- 22.5 preview process -----------------------------------------------------


def test_build_preview_backend_selects_the_simulator() -> None:
    """The existing preview factory resolves ``simulator``."""
    backend = build_preview_backend(SIMULATOR_BACKEND_NAME)

    assert isinstance(backend, SimulatorPreviewBackend)
    assert backend.name == SIMULATOR_BACKEND_NAME


def test_existing_preview_backends_are_unchanged() -> None:
    """The physical and null preview branches keep their existing behaviour."""
    assert isinstance(build_preview_backend("rpicam"), RPiCamPreviewBackend)
    assert isinstance(build_preview_backend("libcamera"), RPiCamPreviewBackend)
    assert isinstance(build_preview_backend("null"), NullPreviewBackend)
    assert isinstance(build_preview_backend("none"), NullPreviewBackend)
    assert build_preview_backend("rpicam").name == "rpicam-vid"
    with pytest.raises(ValueError, match="Unsupported camera backend"):
        build_preview_backend("webcam")


def test_building_the_backend_starts_no_producer() -> None:
    """Nothing is produced merely because the application wired the backend up."""
    before = {thread.name for thread in threading.enumerate()}

    backend = build_preview_backend(SIMULATOR_BACKEND_NAME)
    PreviewService(_preview_config(), backend)

    after = {thread.name for thread in threading.enumerate()}
    assert "mgo-simulator-preview" not in after
    assert after <= before


def test_start_returns_a_truthful_process_handle(
    simulator_process: SimulatorPreviewProcess,
) -> None:
    """The handle admits it is not an operating-system process."""
    assert simulator_process.pid is None
    assert simulator_process.poll() is None
    assert simulator_process.read_error() == ""
    assert simulator_process.frame_stream() is not None


def test_the_first_complete_frame_arrives_promptly(
    simulator_process: SimulatorPreviewProcess,
) -> None:
    """A complete JPEG is readable well inside the startup window."""
    stream = simulator_process.frame_stream()
    assert stream is not None

    started = time.monotonic()
    frame = _first_frame(stream, _FRAME_WAIT_SECONDS)
    elapsed = time.monotonic() - started

    assert frame is not None, "no complete frame arrived within the bound"
    assert elapsed < _FRAME_WAIT_SECONDS
    with Image.open(io.BytesIO(frame)) as image:
        assert image.size == (_TEST_WIDTH, _TEST_HEIGHT)


def test_preview_service_reaches_running_with_the_simulator(
    simulator_preview: PreviewService,
) -> None:
    """The real preview service validates startup against a generated frame."""
    assert simulator_preview.status().state is PreviewState.STOPPED

    status = simulator_preview.start()

    assert status.state is PreviewState.RUNNING
    assert status.backend == SIMULATOR_BACKEND_NAME
    assert status.owner == "preview"
    assert status.last_error is None
    assert status.resolution == f"{_TEST_WIDTH}x{_TEST_HEIGHT}"


def test_preview_start_is_idempotent(simulator_preview: PreviewService) -> None:
    """A second start never launches a second producer."""
    simulator_preview.start()
    before = [
        thread
        for thread in threading.enumerate()
        if thread.name == "mgo-simulator-preview"
    ]

    status = simulator_preview.start()

    after = [
        thread
        for thread in threading.enumerate()
        if thread.name == "mgo-simulator-preview"
    ]
    assert status.state is PreviewState.RUNNING
    assert len(after) == len(before) == 1


def test_running_preview_streams_multiple_valid_jpegs(
    simulator_preview: PreviewService,
) -> None:
    """Frames keep arriving after the startup validation consumed the first."""
    simulator_preview.start()

    frames = _read_frames(PreviewProcessFrameSource(simulator_preview), 6)

    assert len(frames) == 6
    for frame in frames:
        with Image.open(io.BytesIO(frame)) as image:
            assert image.format == "JPEG"
            assert image.size == (_TEST_WIDTH, _TEST_HEIGHT)


def test_streamed_frames_come_from_the_looping_sequence(
    simulator_preview: PreviewService,
) -> None:
    """Every streamed frame is one of the three deterministic scenes."""
    simulator_preview.start()
    known = set(_sequence_frames())

    frames = _read_frames(PreviewProcessFrameSource(simulator_preview), 10)

    assert all(frame in known for frame in frames)
    # Over ten frames the sequence must have moved on from a single scene.
    assert len(set(frames)) >= 2


def test_the_frame_buffer_stays_bounded(
    simulator_process: SimulatorPreviewProcess,
) -> None:
    """A slow consumer costs dropped frames, never growing memory."""
    assert _wait_until(
        lambda: simulator_process.buffered_frames >= 1, _FRAME_WAIT_SECONDS
    )
    # Let the producer run well ahead of any reader.
    deadline = time.monotonic() + 0.5
    while time.monotonic() < deadline:
        assert simulator_process.buffered_frames <= SIMULATOR_FRAME_BUFFER_FRAMES
        time.sleep(_POLL_SECONDS)

    assert simulator_process.buffered_frames <= SIMULATOR_FRAME_BUFFER_FRAMES


def test_the_producer_cadence_follows_the_configured_frame_rate() -> None:
    """The producer paces itself; it does not spin out frames as fast as it can."""
    process = SimulatorPreviewBackend().start(_preview_config(fps=1))
    try:
        stream = process.frame_stream()
        assert stream is not None
        frames = parse_mjpeg_frames(stream)
        next(frames)  # the immediate first frame

        second: list[bytes] = []

        def _await_next_frame() -> None:
            # The stream ends when the process is closed in the ``finally``
            # below; that is a clean end, not a frame.
            for frame in frames:
                second.append(frame)
                return

        reader = threading.Thread(target=_await_next_frame, daemon=True)
        reader.start()
        reader.join(0.25)

        # At one frame per second the next frame cannot have arrived yet; a
        # stop-event wait never returns early, so this direction is never flaky.
        assert second == []
    finally:
        process.close()


@pytest.mark.parametrize(
    "config",
    [
        _preview_config(width=MIN_SIMULATOR_FRAME_WIDTH - 1),
        _preview_config(width=MAX_SIMULATOR_PREVIEW_WIDTH + 1),
        _preview_config(width=7680),
    ],
)
def test_unsafe_preview_width_is_rejected(config: PreviewConfig) -> None:
    """An unsafe width is refused through the preview-domain failure model."""
    with pytest.raises(PreviewStartError, match="Simulator preview width"):
        SimulatorPreviewBackend().start(config)


@pytest.mark.parametrize(
    "config",
    [
        _preview_config(height=MIN_SIMULATOR_FRAME_HEIGHT - 1),
        _preview_config(height=MAX_SIMULATOR_PREVIEW_HEIGHT + 1),
        _preview_config(height=4320),
    ],
)
def test_unsafe_preview_height_is_rejected(config: PreviewConfig) -> None:
    """An unsafe height is refused through the preview-domain failure model."""
    with pytest.raises(PreviewStartError, match="Simulator preview height"):
        SimulatorPreviewBackend().start(config)


@pytest.mark.parametrize("fps", [0, -1, MAX_SIMULATOR_PREVIEW_FPS + 1, 240])
def test_unsafe_preview_frame_rate_is_rejected(fps: int) -> None:
    """An unsafe frame rate is refused, not silently clamped."""
    with pytest.raises(PreviewStartError, match="Simulator preview frame rate"):
        SimulatorPreviewBackend().start(_preview_config(fps=fps))


def test_an_excessive_frame_rate_is_never_clamped() -> None:
    """A rejected request must not quietly become a supported one."""
    service = PreviewService(
        _preview_config(fps=120), build_preview_backend(SIMULATOR_BACKEND_NAME)
    )
    with pytest.raises(PreviewStartError):
        service.start()

    status = service.status()
    assert status.state is PreviewState.FAILED
    assert status.last_error is not None
    assert "frame rate" in status.last_error
    # The status still reports what was *asked for*, not a clamped substitute.
    assert status.fps == 120
    assert "mgo-simulator-preview" not in {
        thread.name for thread in threading.enumerate()
    }
    service.shutdown()


def test_the_rejection_message_leaks_no_environment_detail() -> None:
    """A refusal names the setting and its bounds, nothing else."""
    with pytest.raises(PreviewStartError) as excinfo:
        SimulatorPreviewBackend().start(_preview_config(fps=999))

    message = str(excinfo.value)
    assert message == (
        "Simulator preview frame rate must be between 1 and "
        f"{MAX_SIMULATOR_PREVIEW_FPS}."
    )
    for fragment in ("\\", "/", "C:", "AppData", "Users"):
        assert fragment not in message


def test_terminate_stops_the_producer() -> None:
    """A graceful stop settles exit code zero and ends the producer."""
    process = SimulatorPreviewBackend().start(_preview_config())

    process.terminate()

    assert process.poll() == 0
    assert _wait_until(lambda: not process.producer_alive, _FRAME_WAIT_SECONDS)
    process.close()


def test_kill_stops_the_producer() -> None:
    """A forced stop settles the forced-kill exit code and ends the producer."""
    process = SimulatorPreviewBackend().start(_preview_config())

    process.kill()

    assert process.poll() == -9
    assert _wait_until(lambda: not process.producer_alive, _FRAME_WAIT_SECONDS)
    process.close()


def test_wait_returns_the_expected_codes() -> None:
    """``wait`` reports None while running and the exit code afterwards."""
    process = SimulatorPreviewBackend().start(_preview_config())
    try:
        assert process.wait(0.05) is None

        process.terminate()
        assert process.wait(_FRAME_WAIT_SECONDS) == 0
        assert process.wait(0.01) == 0
    finally:
        process.close()


def test_wait_reports_the_forced_kill_code() -> None:
    """A killed process reports -9 from ``wait`` as well as ``poll``."""
    process = SimulatorPreviewBackend().start(_preview_config())
    try:
        process.kill()

        assert process.wait(_FRAME_WAIT_SECONDS) == -9
    finally:
        process.close()


def test_the_first_exit_code_wins() -> None:
    """A later terminate cannot rewrite a forced kill as a clean exit."""
    process = SimulatorPreviewBackend().start(_preview_config())
    try:
        process.kill()
        process.terminate()
        process.close()

        assert process.poll() == -9
    finally:
        process.close()


def test_close_unblocks_a_blocked_read() -> None:
    """A reader waiting for the next frame is released promptly by close."""
    process = SimulatorPreviewBackend().start(_preview_config(fps=1))
    stream = process.frame_stream()
    assert stream is not None
    outcome: list[bytes] = []

    def _read_until_eof() -> None:
        # The first frame is immediate; the second would be a second away, so
        # this thread is genuinely blocked when close() arrives.
        stream.read(65536)
        outcome.append(stream.read(65536))

    reader = threading.Thread(target=_read_until_eof, daemon=True)
    reader.start()
    assert _wait_until(lambda: reader.is_alive(), 1.0)

    process.close()
    reader.join(_FRAME_WAIT_SECONDS)

    assert not reader.is_alive()
    assert outcome == [b""], "a blocked read must end at EOF, not hang"


def test_terminate_unblocks_a_blocked_read() -> None:
    """Terminating also releases a reader, so preview stop is never stuck."""
    process = SimulatorPreviewBackend().start(_preview_config(fps=1))
    stream = process.frame_stream()
    assert stream is not None
    outcome: list[bytes] = []

    def _read_until_eof() -> None:
        stream.read(65536)
        outcome.append(stream.read(65536))

    reader = threading.Thread(target=_read_until_eof, daemon=True)
    reader.start()
    assert _wait_until(lambda: reader.is_alive(), 1.0)

    process.terminate()
    reader.join(_FRAME_WAIT_SECONDS)

    assert outcome == [b""]
    process.close()


def test_terminate_kill_and_close_are_idempotent() -> None:
    """Repeated lifecycle calls are safe in any order and any number."""
    process = SimulatorPreviewBackend().start(_preview_config())

    for _ in range(3):
        process.terminate()
        process.wait(0.01)
        process.close()
        process.kill()
        process.close()

    assert process.poll() == 0
    assert not process.producer_alive
    assert process.read_error() == ""
    stream = process.frame_stream()
    assert stream is not None
    assert stream.read(16) == b""


def test_unexpected_exit_reconciliation_stays_truthful(
    simulator_preview: PreviewService,
) -> None:
    """A producer that dies while running is reconciled to a truthful failure."""
    status = simulator_preview.start()
    assert status.state is PreviewState.RUNNING

    # Simulate the producer dying without the service asking it to.
    stream = simulator_preview.frame_stream()
    assert stream is not None
    process = SimulatorPreviewBackend().start(_preview_config())
    process.kill()
    process.close()

    # The service's own process is still healthy; reconciliation says running.
    assert simulator_preview.status().state is PreviewState.RUNNING


def test_a_dead_producer_is_reported_as_failed() -> None:
    """Killing the service's own process surfaces as FAILED with its code."""
    backend = SimulatorPreviewBackend()
    started: list[SimulatorPreviewProcess] = []

    class _RecordingBackend:
        @property
        def name(self) -> str:
            return backend.name

        def start(self, config: PreviewConfig) -> SimulatorPreviewProcess:
            process = backend.start(config)
            started.append(process)
            return process

    service = PreviewService(_preview_config(), _RecordingBackend())
    try:
        assert service.start().state is PreviewState.RUNNING
        started[0].kill()

        status = service.status()

        assert status.state is PreviewState.FAILED
        assert status.last_error is not None
        assert "-9" in status.last_error
    finally:
        service.shutdown()


def test_no_producer_thread_survives_preview_stop(
    simulator_preview: PreviewService,
) -> None:
    """Stopping preview leaves no orphan producer behind."""
    simulator_preview.start()
    assert any(
        thread.name == "mgo-simulator-preview" for thread in threading.enumerate()
    )

    simulator_preview.stop()

    assert _wait_until(
        lambda: not any(
            thread.name == "mgo-simulator-preview"
            for thread in threading.enumerate()
        ),
        _FRAME_WAIT_SECONDS,
    )


def test_repeated_start_stop_cycles_leak_no_threads(
    simulator_preview: PreviewService,
) -> None:
    """Many preview generations never accumulate producers."""
    for _ in range(4):
        assert simulator_preview.start().state is PreviewState.RUNNING
        assert simulator_preview.stop().state is PreviewState.STOPPED

    assert _wait_until(
        lambda: not any(
            thread.name == "mgo-simulator-preview"
            for thread in threading.enumerate()
        ),
        _FRAME_WAIT_SECONDS,
    )


def test_the_producer_thread_is_a_daemon(
    simulator_process: SimulatorPreviewProcess,
) -> None:
    """No simulator thread can keep the interpreter alive."""
    producers = [
        thread
        for thread in threading.enumerate()
        if thread.name == "mgo-simulator-preview"
    ]

    assert producers
    assert all(thread.daemon for thread in producers)


def test_preview_disabled_still_wins_over_the_simulator() -> None:
    """Selecting the simulator does not override ``preview.enabled = false``."""
    service = PreviewService(
        _preview_config(enabled=False),
        build_preview_backend(SIMULATOR_BACKEND_NAME),
    )

    with pytest.raises(PreviewUnavailableError, match="disabled by configuration"):
        service.start()
    assert service.status().state is PreviewState.STOPPED


# -- 22.6 streaming integration ----------------------------------------------


def test_generated_mjpeg_is_parsed_by_the_existing_parser() -> None:
    """Concatenated generated frames satisfy the existing SOI/EOI parser."""
    frames = _sequence_frames()
    raw = io.BytesIO(b"".join(frames))

    parsed = list(parse_mjpeg_frames(raw, chunk_size=997))

    assert parsed == frames


def test_the_broker_delivers_simulator_frames(
    simulator_preview: PreviewService,
) -> None:
    """A viewer subscribed to the real broker receives generated frames."""
    simulator_preview.start()
    broker = MjpegBroker(lambda: PreviewProcessFrameSource(simulator_preview))
    subscriber = broker.subscribe()
    try:
        frame = subscriber.get(_FRAME_WAIT_SECONDS)

        assert frame is not None
        with Image.open(io.BytesIO(frame)) as image:
            assert image.size == (_TEST_WIDTH, _TEST_HEIGHT)
    finally:
        broker.unsubscribe(subscriber)


def test_multiple_viewers_share_one_producer(
    simulator_preview: PreviewService,
) -> None:
    """Three viewers share the single simulator producer and one broker pump."""
    simulator_preview.start()
    broker = MjpegBroker(lambda: PreviewProcessFrameSource(simulator_preview))
    viewers = [broker.subscribe() for _ in range(3)]
    try:
        for viewer in viewers:
            assert viewer.get(_FRAME_WAIT_SECONDS) is not None

        assert broker.viewer_count == 3
        producers = [
            thread
            for thread in threading.enumerate()
            if thread.name == "mgo-simulator-preview"
        ]
        pumps = [
            thread
            for thread in threading.enumerate()
            if thread.name == "mgo-preview-stream"
        ]
        assert len(producers) == 1
        assert len(pumps) == 1
    finally:
        for viewer in viewers:
            broker.unsubscribe(viewer)


def test_a_slow_viewer_does_not_grow_memory(
    simulator_preview: PreviewService,
) -> None:
    """An unread mailbox holds at most one frame, whatever the producer does."""
    simulator_preview.start()
    broker = MjpegBroker(lambda: PreviewProcessFrameSource(simulator_preview))
    slow = broker.subscribe()
    fast = broker.subscribe()
    try:
        # Drive the pump for a while, reading only with the fast viewer.
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline:
            fast.get(_FRAME_WAIT_SECONDS)

        # The slow viewer's one-slot mailbox yields exactly one (latest) frame,
        # then blocks again -- it never accumulated the backlog.
        assert slow.get(_FRAME_WAIT_SECONDS) is not None
        with pytest.raises(queue.Empty):
            slow.get(0.001)
    finally:
        broker.unsubscribe(slow)
        broker.unsubscribe(fast)


def test_last_viewer_disconnect_closes_only_the_broker_source(
    simulator_preview: PreviewService,
) -> None:
    """Losing the last viewer must not stop preview or create a second owner."""
    simulator_preview.start()
    broker = MjpegBroker(lambda: PreviewProcessFrameSource(simulator_preview))
    subscriber = broker.subscribe()
    assert subscriber.get(_FRAME_WAIT_SECONDS) is not None

    broker.unsubscribe(subscriber)

    assert broker.viewer_count == 0
    status = simulator_preview.status()
    assert status.state is PreviewState.RUNNING
    assert status.owner == "preview"
    assert (
        len(
            [
                thread
                for thread in threading.enumerate()
                if thread.name == "mgo-simulator-preview"
            ]
        )
        == 1
    )
    # A later viewer still receives frames from the same simulator producer.
    again = broker.subscribe()
    try:
        assert again.get(_FRAME_WAIT_SECONDS) is not None
    finally:
        broker.unsubscribe(again)


def test_preview_stop_ends_the_stream_cleanly(
    simulator_preview: PreviewService,
) -> None:
    """Stopping preview ends a connected viewer's stream with the sentinel."""
    simulator_preview.start()
    broker = MjpegBroker(lambda: PreviewProcessFrameSource(simulator_preview))
    subscriber = broker.subscribe()
    try:
        assert subscriber.get(_FRAME_WAIT_SECONDS) is not None

        simulator_preview.stop()

        deadline = time.monotonic() + _FRAME_WAIT_SECONDS
        ended = False
        while not ended and time.monotonic() < deadline:
            try:
                ended = subscriber.get(_FRAME_WAIT_SECONDS) is None
            except queue.Empty:  # pragma: no cover - defensive
                break
        assert ended, "the stream never signalled end-of-stream"
    finally:
        broker.unsubscribe(subscriber)


def test_a_new_generation_does_not_receive_a_stale_end_of_stream(
    simulator_preview: PreviewService,
) -> None:
    """A restarted preview's viewer is never ended by the previous generation."""
    broker = MjpegBroker(lambda: PreviewProcessFrameSource(simulator_preview))
    simulator_preview.start()
    first = broker.subscribe()
    assert first.get(_FRAME_WAIT_SECONDS) is not None
    simulator_preview.stop()
    broker.unsubscribe(first)

    simulator_preview.start()
    second = broker.subscribe()
    try:
        # The replacement generation must deliver a real frame, not the dead
        # generation's sentinel.
        assert second.get(_FRAME_WAIT_SECONDS) is not None
    finally:
        broker.unsubscribe(second)


def test_simulator_frames_encode_as_multipart_parts() -> None:
    """The existing multipart encoder wraps a generated frame unchanged."""
    frame = encode_simulator_frame(2, _TEST_WIDTH, _TEST_HEIGHT)

    part = encode_multipart_frame(frame)

    assert part.startswith(b"--mgopreviewframe\r\nContent-Type: image/jpeg")
    assert f"Content-Length: {len(frame)}".encode("ascii") in part
    assert part.endswith(frame + b"\r\n")


# -- 22.7 motion integration --------------------------------------------------


def test_the_real_detector_sees_no_motion_across_an_identical_pair() -> None:
    """The quiet identical pair scores exactly zero through the real detector."""
    detector = FrameDifferenceDetector(_motion_config())
    frames = _sequence_frames()

    score = detector.score(
        detector.decode(frames[0]), detector.decode(frames[1])
    )

    assert score == 0.0
    assert detector.is_motion(score) is False


def test_the_real_detector_sees_motion_across_a_transition() -> None:
    """Each scene transition exceeds the configured threshold."""
    config = _motion_config()
    detector = FrameDifferenceDetector(config)
    frames = _sequence_frames()

    for first, second in ((1, 2), (3, 4), (5, 6)):
        score = detector.score(
            detector.decode(frames[first]), detector.decode(frames[second])
        )

        assert 0.0 <= score <= 1.0
        assert score > config.changed_pixel_ratio_threshold
        assert detector.is_motion(score) is True


def test_motion_scores_stay_finite_and_bounded() -> None:
    """Every consecutive pair in the sequence scores within [0, 1]."""
    detector = FrameDifferenceDetector(_motion_config())
    analysed = [detector.decode(frame) for frame in _sequence_frames()]

    for index in range(len(analysed) - 1):
        score = detector.score(analysed[index], analysed[index + 1])

        assert score == score  # not NaN
        assert 0.0 <= score <= 1.0


def test_the_configured_threshold_is_unchanged_by_the_simulator() -> None:
    """The simulator does not retune the motion threshold."""
    config = _motion_config()
    detector = FrameDifferenceDetector(config)

    assert config.changed_pixel_ratio_threshold == 0.08
    assert detector.is_motion(0.08) is False
    assert detector.is_motion(0.0801) is True
    assert load_config().motion.changed_pixel_ratio_threshold == 0.08


def test_real_camera_threshold_behaviour_is_unaffected() -> None:
    """Hand-written frames still behave exactly as they did before Task 11."""
    detector = FrameDifferenceDetector(_motion_config())
    with io.BytesIO() as buffer:
        Image.new("RGB", (64, 48), (10, 10, 10)).save(buffer, format="JPEG")
        dark = buffer.getvalue()
    with io.BytesIO() as buffer:
        Image.new("RGB", (64, 48), (250, 250, 250)).save(buffer, format="JPEG")
        light = buffer.getvalue()

    identical = detector.score(detector.decode(dark), detector.decode(dark))
    opposite = detector.score(detector.decode(dark), detector.decode(light))

    assert identical == 0.0
    assert opposite == pytest.approx(1.0)


class _RecordingMotionState(MotionState):
    """A motion state that remembers every result it was given."""

    def __init__(self) -> None:
        super().__init__()
        self.results: list[MotionResult] = []

    def set(self, result: MotionResult) -> None:
        self.results.append(result)
        super().set(result)


def _run_motion_monitor_over_broker(
    config: MGOConfig,
    broker: MjpegBroker,
    *,
    wanted: set[MotionStatus],
    timeout: float,
) -> _RecordingMotionState:
    """Run the real motion monitor over a broker until ``wanted`` is observed."""
    state = _RecordingMotionState()

    async def _drive() -> None:
        stop_event = asyncio.Event()
        source = BrokerFrameSource(broker)
        task = asyncio.create_task(
            run_motion_monitor(
                config,
                state,
                source,
                FrameDifferenceDetector(config.motion),
                stop_event,
                recorder=lambda *args, **kwargs: SimpleNamespace(),  # type: ignore[arg-type,return-value]
            )
        )
        deadline = asyncio.get_running_loop().time() + timeout
        try:
            while asyncio.get_running_loop().time() < deadline:
                observed = {result.status for result in state.results}
                if wanted <= observed:
                    break
                await asyncio.sleep(0.02)
        finally:
            stop_event.set()
            await task

    asyncio.run(_drive())
    return state


def test_the_motion_monitor_consumes_simulator_frames_through_the_broker(
    simulator_preview: PreviewService,
) -> None:
    """The real monitor, source, broker and detector produce truthful states.

    Nothing here is simulator-specific: the frames travel the production path
    (preview process -> ``PreviewProcessFrameSource`` -> ``MjpegBroker`` ->
    ``BrokerFrameSource``) and are scored by the real ``FrameDifferenceDetector``.
    """
    simulator_preview.start()
    broker = MjpegBroker(lambda: PreviewProcessFrameSource(simulator_preview))
    config = load_config()
    config = MGOConfig(
        application=config.application,
        storage=config.storage,
        camera=config.camera,
        preview=config.preview,
        motion=_motion_config(analysis_interval_seconds=0.05),
        notifications=config.notifications,
        database=config.database,
        health=config.health,
    )

    state = _run_motion_monitor_over_broker(
        config,
        broker,
        wanted={MotionStatus.NO_MOTION, MotionStatus.MOTION_DETECTED},
        timeout=15.0,
    )

    statuses = [result.status for result in state.results]
    assert statuses[0] is MotionStatus.ESTABLISHING_BASELINE
    assert MotionStatus.NO_MOTION in statuses
    assert MotionStatus.MOTION_DETECTED in statuses
    assert MotionStatus.ERROR not in statuses
    for result in state.results:
        assert 0.0 <= result.score <= 1.0
        assert result.threshold == 0.08
    detected = [
        result
        for result in state.results
        if result.status is MotionStatus.MOTION_DETECTED
    ]
    assert all(result.detected for result in detected)
    assert all(result.frames_available for result in detected)


def test_the_simulator_owns_no_motion_machinery() -> None:
    """The simulator imports and names no motion or persistence component."""
    imported = _simulator_imports()

    assert not any(name.startswith("mgo.motion") for name in imported)
    assert "mgo.core.observations" not in imported
    for forbidden in (
        "MotionResult",
        "MotionStatus",
        "MotionDetector",
        "FrameDifferenceDetector",
        "record_observation",
        "MotionState",
    ):
        assert forbidden not in _simulator_identifiers(), forbidden


# -- 22.8 API integration -----------------------------------------------------


@pytest.fixture
def api_state(tmp_path: Path) -> Iterator[SimpleNamespace]:
    """Attach isolated simulator-backed services to the production application.

    The registered routes and the real ASGI stack are exercised; only the
    services behind them are isolated, and the previous application state is
    restored afterwards so no test can leak into another.
    """
    database = tmp_path / "mgo.db"
    apply_migrations(database, busy_timeout_seconds=5.0)
    camera_config = _camera_config(tmp_path / "captures")
    preview_service = PreviewService(
        _preview_config(), build_preview_backend(SIMULATOR_BACKEND_NAME)
    )
    camera_state = CameraState()
    camera_state.set(
        detect_camera_readiness(
            camera_config, build_detector(camera_config.backend)
        )
    )
    attached = {
        "camera_state": camera_state,
        "capture_service": CaptureService(
            camera_config, build_capture_backend(camera_config)
        ),
        "capture_archive": CaptureArchive(database),
        "preview_service": preview_service,
        "preview_broker": MjpegBroker(
            lambda: PreviewProcessFrameSource(preview_service)
        ),
    }
    previous = dict(app.state._state)
    app.state._state.update(attached)
    try:
        yield SimpleNamespace(**attached, database=database)
    finally:
        preview_service.shutdown()
        app.state._state.clear()
        app.state._state.update(previous)


def _dispatch(
    method: str, path: str, *, min_body: int | None = None
) -> tuple[int, dict[str, str], bytes]:
    """Drive the production ASGI application in-process for one request.

    ``min_body`` models a browser that disconnects once it has received that
    many body bytes, which is how an endless MJPEG stream is consumed.
    """

    async def _call() -> tuple[int, dict[str, str], bytes]:
        scope: dict[str, Any] = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": path,
            "raw_path": path.encode("ascii"),
            "query_string": b"",
            "root_path": "",
            "headers": [(b"host", b"mgo-test")],
            "client": ("127.0.0.1", 50000),
            "server": ("mgo-test", 80),
        }
        disconnected = asyncio.Event()
        chunks: list[bytes] = []
        captured: dict[str, Any] = {"status": 0, "headers": {}}

        async def receive() -> dict[str, Any]:
            await disconnected.wait()
            return {"type": "http.disconnect"}

        async def send(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                captured["status"] = message["status"]
                captured["headers"] = {
                    key.decode("latin-1").lower(): value.decode("latin-1")
                    for key, value in message.get("headers", [])
                }
            elif message["type"] == "http.response.body":
                chunks.append(message.get("body", b""))
                if min_body is not None and sum(map(len, chunks)) >= min_body:
                    disconnected.set()

        if min_body is None:
            disconnected.set()
        await app(scope, receive, send)
        return captured["status"], captured["headers"], b"".join(chunks)

    return asyncio.run(_call())


def _json(path: str, *, method: str = "GET") -> Any:
    """Dispatch a request and return its decoded JSON body."""
    status, _headers, body = _dispatch(method, path)
    assert status == 200, (status, body)
    return json.loads(body)


@pytest.mark.usefixtures("api_state")
def test_camera_status_endpoint_reports_the_simulator() -> None:
    """``GET /camera/status`` surfaces the truthful simulator readiness."""
    payload = _json("/camera/status")

    assert payload["backend"] == SIMULATOR_BACKEND_NAME
    assert payload["status"] == "available"
    assert payload["available"] is True
    assert payload["enabled"] is True
    assert payload["detail"] == SIMULATOR_READINESS_DETAIL
    # No response field was added.
    assert set(payload) == {
        "enabled",
        "backend",
        "status",
        "available",
        "detail",
        "checked_at",
    }


@pytest.mark.usefixtures("api_state")
def test_preview_lifecycle_over_the_api() -> None:
    """Start, status, stream and stop all work through the real endpoints."""
    assert _json("/camera/preview/status")["state"] == "stopped"

    started = _json("/camera/preview/start", method="POST")
    assert started["state"] == "running"
    assert started["backend"] == SIMULATOR_BACKEND_NAME

    status = _json("/camera/preview/status")
    assert status["state"] == "running"
    assert status["backend"] == SIMULATOR_BACKEND_NAME
    assert status["owner"] == "preview"

    code, headers, body = _dispatch(
        "GET", "/camera/preview/stream", min_body=1
    )
    assert code == 200
    assert headers["content-type"] == MJPEG_CONTENT_TYPE
    assert body.startswith(b"--mgopreviewframe\r\nContent-Type: image/jpeg")
    jpeg = body.split(b"\r\n\r\n", 1)[1].rstrip(b"\r\n")
    with Image.open(io.BytesIO(jpeg)) as image:
        assert image.format == "JPEG"
        assert image.size == (_TEST_WIDTH, _TEST_HEIGHT)

    stopped = _json("/camera/preview/stop", method="POST")
    assert stopped["state"] == "stopped"


@pytest.mark.usefixtures("api_state")
def test_stream_endpoint_still_refuses_when_preview_is_stopped() -> None:
    """The existing 409 gate is unchanged: streaming never starts preview."""
    code, _headers, body = _dispatch("GET", "/camera/preview/stream")

    assert code == 409
    assert b"start it before streaming" in body
    assert _json("/camera/preview/status")["state"] == "stopped"


def test_capture_endpoint_returns_simulator_metadata(
    api_state: SimpleNamespace,
) -> None:
    """``POST /camera/capture`` returns the existing metadata, truthfully."""
    payload = _json("/camera/capture", method="POST")

    assert payload["backend"] == SIMULATOR_BACKEND_NAME
    assert payload["success"] is True
    assert payload["width"] == SIMULATOR_CAPTURE_WIDTH
    assert payload["height"] == SIMULATOR_CAPTURE_HEIGHT
    assert payload["filesize_bytes"] > 0
    assert "capture_id" in payload
    with Image.open(Path(payload["absolute_path"])) as image:
        assert image.format == "JPEG"
        assert image.size == (payload["width"], payload["height"])

    listed = _json("/captures")
    assert [item["capture_id"] for item in listed] == [payload["capture_id"]]
    assert listed[0]["backend"] == SIMULATOR_BACKEND_NAME
    single = _json(f"/captures/{payload['capture_id']}")
    assert single["backend"] == SIMULATOR_BACKEND_NAME
    assert api_state.database.exists()


@pytest.mark.usefixtures("api_state")
def test_capture_releases_a_running_simulator_preview() -> None:
    """The existing capture-priority policy is preserved, with no auto-restart."""
    assert _json("/camera/preview/start", method="POST")["state"] == "running"

    _json("/camera/capture", method="POST")

    assert _json("/camera/preview/status")["state"] == "stopped"


@pytest.mark.usefixtures("api_state")
def test_health_remains_truthful_with_the_simulator() -> None:
    """``GET /health`` reports the simulator without inventing a new field."""
    payload = _json("/health")

    assert payload["camera"]["backend"] == SIMULATOR_BACKEND_NAME
    assert payload["camera"]["available"] is True
    assert payload["preview"]["state"] == "stopped"
    assert set(payload["preview"]) == {
        "enabled",
        "state",
        "owner",
        "uptime_seconds",
    }


@pytest.mark.usefixtures("api_state")
def test_dashboard_loads_without_starting_the_simulator() -> None:
    """Opening the dashboard must not start a producer or touch preview."""
    code, headers, body = _dispatch("GET", "/dashboard")

    assert code == 200
    assert headers["content-type"].startswith("text/html")
    assert len(body) > 0
    assert _json("/camera/preview/status")["state"] == "stopped"
    assert not any(
        thread.name == "mgo-simulator-preview" for thread in threading.enumerate()
    )


@pytest.mark.usefixtures("api_state")
def test_preview_page_loads_without_starting_the_simulator() -> None:
    """Opening the preview page must not start a producer either."""
    code, headers, body = _dispatch("GET", "/preview")

    assert code == 200
    assert headers["content-type"].startswith("text/html")
    assert len(body) > 0
    assert _json("/camera/preview/status")["state"] == "stopped"
    assert not any(
        thread.name == "mgo-simulator-preview" for thread in threading.enumerate()
    )


@pytest.mark.usefixtures("api_state")
def test_no_endpoint_was_added_or_renamed() -> None:
    """The registered route set is exactly the documented contract."""
    paths = {
        route.path  # type: ignore[attr-defined]
        for route in app.routes
        if getattr(route, "path", "").startswith(("/", ""))
        and not getattr(route, "path", "").startswith("/openapi")
    }

    assert {
        "/",
        "/version",
        "/health",
        "/database/status",
        "/camera/status",
        "/camera/capture",
        "/camera/preview/status",
        "/camera/preview/start",
        "/camera/preview/stop",
        "/camera/preview/stream",
        "/preview",
        "/dashboard",
        "/motion/status",
        "/notifications/status",
        "/captures",
        "/captures/{capture_id}",
        "/observations",
    } <= paths


# -- 22.9 negative hardware boundary -----------------------------------------


@pytest.mark.usefixtures("no_subprocess")
def test_the_whole_simulator_pipeline_launches_no_subprocess(
    tmp_path: Path,
) -> None:
    """Readiness, capture, preview, streaming and stop touch no process API."""
    camera_config = _camera_config(tmp_path / "captures")

    readiness = detect_camera_readiness(
        camera_config, build_detector(camera_config.backend)
    )
    assert readiness.available is True

    capture = CaptureService(
        camera_config, build_capture_backend(camera_config)
    ).capture_image()
    assert capture.backend == SIMULATOR_BACKEND_NAME

    service = PreviewService(
        _preview_config(), build_preview_backend(SIMULATOR_BACKEND_NAME)
    )
    try:
        assert service.start().state is PreviewState.RUNNING
        frames = _read_frames(PreviewProcessFrameSource(service), 3)
        assert len(frames) == 3
    finally:
        service.shutdown()


def test_the_simulator_module_imports_no_process_or_network_facility() -> None:
    """A narrow structural check backing the behavioural guarantees above.

    The behavioural tests prove no subprocess is *called*; this proves the
    module cannot, because it imports nothing capable of it. It is deliberately
    structural (imports and executable string literals via the AST) rather than
    a raw text search, so the module's own prose may still explain what it does
    *not* do.
    """
    imported = _simulator_imports()

    for forbidden in (
        "subprocess",
        "multiprocessing",
        "os",
        "shutil",
        "socket",
        "urllib",
        "http",
        "asyncio",
        "requests",
    ):
        assert not any(
            name == forbidden or name.startswith(f"{forbidden}.")
            for name in imported
        ), forbidden


def test_the_simulator_module_names_no_camera_command() -> None:
    """No executable string in the module is (or contains) a camera command."""
    literals = " ".join(_simulator_code_strings()).lower()

    for forbidden in (
        "rpicam",
        "libcamera",
        "rpicam-hello",
        "rpicam-still",
        "rpicam-vid",
        "libcamera-hello",
        "libcamera-still",
        "libcamera-vid",
        "/dev/video",
        "imx",
        "serial",
        "shell",
    ):
        assert forbidden not in literals, forbidden


def test_the_simulator_states_plainly_that_it_is_not_a_camera() -> None:
    """The module documents its own non-hardware nature."""
    source = (
        _REPOSITORY_ROOT / "src" / "mgo" / "camera" / "simulator.py"
    ).read_text(encoding="utf-8").lower()

    assert "no physical camera" in source


def test_the_existing_mock_doubles_remain_available() -> None:
    """The simulator is additive: every test double still exists, unrenamed."""
    from mgo.camera import (
        MockBackend,
        MockFrameSource,
        MockPreviewBackend,
        MockPreviewProcess,
    )

    assert MockBackend().name == "mock"
    assert MockPreviewBackend().name == "mock"
    assert MockPreviewProcess().pid == 4242
    assert MockFrameSource([]).closed is False
