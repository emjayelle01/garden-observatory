"""Configuration loading for Matt's Garden Observatory."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "mgo.toml"

#: Environment variable that selects an external configuration file.
CONFIG_PATH_ENV = "MGO_CONFIG_PATH"


@dataclass(frozen=True)
class ApplicationConfig:
    """Application runtime settings."""

    name: str
    environment: str
    host: str
    port: int


@dataclass(frozen=True)
class StorageConfig:
    """Persistent storage locations."""

    data_directory: Path
    log_directory: Path
    database_path: Path


SUPPORTED_CAMERA_BACKENDS = frozenset({"rpicam", "libcamera", "null", "none"})


@dataclass(frozen=True)
class CameraConfig:
    """Camera runtime settings.

    ``enabled`` gates all camera behaviour. ``backend`` selects the detection
    adapter. ``device_index`` optionally narrows detection to a specific
    device (``None`` means "no preference"). ``detection_interval_seconds``
    controls how often the background readiness monitor re-checks hardware.
    """

    enabled: bool
    backend: str
    device_index: int | None
    detection_interval_seconds: int
    capture_directory: Path


@dataclass(frozen=True)
class PreviewConfig:
    """Live camera preview settings.

    ``enabled`` gates all preview behaviour. ``width``/``height``/``fps`` shape
    the preview pipeline. ``startup_timeout_seconds`` and
    ``shutdown_timeout_seconds`` bound process start confirmation and graceful
    shutdown respectively. Preview shares the camera hardware with capture; only
    one may own the camera at a time.
    """

    enabled: bool
    width: int
    height: int
    fps: int
    startup_timeout_seconds: float
    shutdown_timeout_seconds: float


@dataclass(frozen=True)
class HealthConfig:
    """Configuration for health monitoring."""

    enabled: bool
    collection_interval_seconds: int
    temperature_warning_celsius: float
    temperature_critical_celsius: float
    disk_warning_percent: float
    disk_critical_percent: float
    memory_warning_percent: float
    memory_critical_percent: float


@dataclass(frozen=True)
class MGOConfig:
    """Complete MGO configuration."""

    application: ApplicationConfig
    storage: StorageConfig
    camera: CameraConfig
    preview: PreviewConfig
    health: HealthConfig


def _project_path(value: str) -> Path:
    """Resolve a configured path relative to the project root."""
    path = Path(value)
    return path if path.is_absolute() else PROJECT_ROOT / path


def _validate_health_config(health: HealthConfig) -> None:
    """Validate health-monitor settings and thresholds."""
    if health.collection_interval_seconds < 10:
        raise ValueError("Health collection interval must be at least 10 seconds")

    if health.temperature_warning_celsius >= health.temperature_critical_celsius:
        raise ValueError("Temperature warning threshold must be below critical")

    if health.disk_warning_percent >= health.disk_critical_percent:
        raise ValueError("Disk warning threshold must be below critical")

    if health.memory_warning_percent >= health.memory_critical_percent:
        raise ValueError("Memory warning threshold must be below critical")


#: Sensible defaults for preview when the ``[preview]`` section is absent, so
#: pre-existing configuration files keep loading unchanged.
_PREVIEW_DEFAULTS = {
    "enabled": False,
    "width": 1280,
    "height": 720,
    "fps": 15,
    "startup_timeout_seconds": 5.0,
    "shutdown_timeout_seconds": 5.0,
}


def _validate_preview_config(preview: PreviewConfig) -> None:
    """Validate preview settings, rejecting non-positive dimensions/timings."""
    if preview.width <= 0 or preview.height <= 0:
        raise ValueError("Preview width and height must be positive")

    if preview.fps <= 0:
        raise ValueError("Preview fps must be positive")

    if preview.startup_timeout_seconds <= 0:
        raise ValueError("Preview startup timeout must be positive")

    if preview.shutdown_timeout_seconds <= 0:
        raise ValueError("Preview shutdown timeout must be positive")


def _validate_camera_config(camera: CameraConfig) -> None:
    """Validate camera settings, rejecting unsafe or unsupported values."""
    if camera.detection_interval_seconds <= 0:
        raise ValueError(
            "Camera detection interval must be a positive number of seconds"
        )

    if camera.backend.strip().lower() not in SUPPORTED_CAMERA_BACKENDS:
        supported = ", ".join(sorted(SUPPORTED_CAMERA_BACKENDS))
        raise ValueError(
            f"Unsupported camera backend {camera.backend!r}; "
            f"supported backends: {supported}"
        )

    if camera.device_index is not None and camera.device_index < 0:
        raise ValueError("Camera device index must be zero or positive")


def resolve_config_path(path: Path | None = None) -> Path:
    """Resolve the effective configuration path.

    Selection precedence is:

    1. an explicit ``path`` supplied by the caller;
    2. the :data:`CONFIG_PATH_ENV` (``MGO_CONFIG_PATH``) environment variable;
    3. the repository default, :data:`DEFAULT_CONFIG_PATH`.

    An explicit path always wins over the environment variable so that callers
    and tests remain deterministic. When the environment variable is used it is
    stripped of surrounding whitespace, has ``~`` expanded to the user's home
    directory, and — if relative — is resolved against the current working
    directory (normal operating-system path semantics for an operator-supplied
    value).

    A set-but-empty or whitespace-only environment value is treated as a
    configuration error and raises :class:`ValueError`; an unset variable is
    treated as absent.
    """
    if path is not None:
        return path

    raw = os.environ.get(CONFIG_PATH_ENV)
    if raw is None:
        return DEFAULT_CONFIG_PATH

    stripped = raw.strip()
    if not stripped:
        raise ValueError(
            f"{CONFIG_PATH_ENV} is set but empty; "
            "unset it to use the default configuration or provide a valid path"
        )

    expanded = Path(stripped).expanduser()
    if not expanded.is_absolute():
        expanded = Path.cwd() / expanded
    return expanded


def load_config(path: Path | None = None) -> MGOConfig:
    """Load and validate MGO configuration from TOML.

    When ``path`` is ``None`` the effective path is selected via
    :func:`resolve_config_path`, honouring the ``MGO_CONFIG_PATH`` environment
    variable. A missing file raises :class:`FileNotFoundError` with the resolved
    path; there is no silent fallback to the repository default.
    """
    path = resolve_config_path(path)
    if not path.exists():
        raise FileNotFoundError(f"Configuration file not found: {path}")

    with path.open("rb") as config_file:
        raw = tomllib.load(config_file)

    application_data = raw["application"]
    storage_data = raw["storage"]
    camera_data = raw["camera"]
    health_data = raw["health"]

    health = HealthConfig(
        enabled=bool(health_data.get("enabled", True)),
        collection_interval_seconds=int(
            health_data.get("collection_interval_seconds", 60)
        ),
        temperature_warning_celsius=float(
            health_data["temperature_warning_celsius"]
        ),
        temperature_critical_celsius=float(
            health_data["temperature_critical_celsius"]
        ),
        disk_warning_percent=float(health_data["disk_warning_percent"]),
        disk_critical_percent=float(health_data["disk_critical_percent"]),
        memory_warning_percent=float(health_data["memory_warning_percent"]),
        memory_critical_percent=float(health_data["memory_critical_percent"]),
    )
    _validate_health_config(health)

    device_index_raw = camera_data.get("device_index")
    camera = CameraConfig(
        enabled=bool(camera_data["enabled"]),
        backend=str(camera_data.get("backend", "rpicam")),
        device_index=(
            int(device_index_raw) if device_index_raw is not None else None
        ),
        detection_interval_seconds=int(
            camera_data.get("detection_interval_seconds", 60)
        ),
        capture_directory=_project_path(str(camera_data["capture_directory"])),
    )
    _validate_camera_config(camera)

    # The ``[preview]`` section is optional so pre-Task-2D configuration files
    # continue to load; absent keys fall back to safe defaults.
    preview_data = raw.get("preview", {})
    preview = PreviewConfig(
        enabled=bool(preview_data.get("enabled", _PREVIEW_DEFAULTS["enabled"])),
        width=int(preview_data.get("width", _PREVIEW_DEFAULTS["width"])),
        height=int(preview_data.get("height", _PREVIEW_DEFAULTS["height"])),
        fps=int(preview_data.get("fps", _PREVIEW_DEFAULTS["fps"])),
        startup_timeout_seconds=float(
            preview_data.get(
                "startup_timeout_seconds",
                _PREVIEW_DEFAULTS["startup_timeout_seconds"],
            )
        ),
        shutdown_timeout_seconds=float(
            preview_data.get(
                "shutdown_timeout_seconds",
                _PREVIEW_DEFAULTS["shutdown_timeout_seconds"],
            )
        ),
    )
    _validate_preview_config(preview)

    return MGOConfig(
        application=ApplicationConfig(
            name=str(application_data["name"]),
            environment=str(application_data["environment"]),
            host=str(application_data["host"]),
            port=int(application_data["port"]),
        ),
        storage=StorageConfig(
            data_directory=_project_path(str(storage_data["data_directory"])),
            log_directory=_project_path(str(storage_data["log_directory"])),
            database_path=_project_path(str(storage_data["database_path"])),
        ),
        camera=camera,
        preview=preview,
        health=health,
    )
