"""Configuration loading for Matt's Garden Observatory."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "mgo.toml"

#: Environment variable that selects an external configuration file.
CONFIG_PATH_ENV = "MGO_CONFIG_PATH"

# --- Production service identity and filesystem layout ---------------------
#
# The constants below describe the *deployment host* (Raspberry Pi OS) layout
# under which MGO runs in production: a dedicated, non-login service account
# and a persistent filesystem layout separate from any operator's home
# directory. They are the single source of truth shared by the production
# example configuration, the systemd unit template, the provisioning scripts
# and the documentation.
#
# They are deliberately :class:`~pathlib.PurePosixPath` values rather than
# :class:`~pathlib.Path`: they name locations on the Linux deployment host and
# are never opened on the Windows development machine. Nothing in the
# application resolves configuration from them implicitly -- the effective
# configuration path is still chosen only by :func:`resolve_config_path`, so
# existing deployments keep loading exactly the file they load today.

#: Dedicated non-login runtime account the service runs as.
SERVICE_ACCOUNT = "mgo"

#: Primary group owning the persistent application data.
SERVICE_GROUP = "mgo"

#: Supplementary group granting access to the Raspberry Pi camera devices
#: (``/dev/video*``, ``/dev/media*``, ``/dev/vchiq``). It is the only
#: supplementary group the runtime account needs.
CAMERA_GROUP = "video"

#: systemd unit that runs the application under the dedicated identity.
SERVICE_UNIT_NAME = "mgo.service"

#: Read-only configuration directory (owned by ``root``, readable by the
#: runtime group, so the service cannot rewrite its own configuration).
SYSTEM_CONFIG_DIRECTORY = PurePosixPath("/etc/garden-observatory")

#: Canonical production configuration file.
SYSTEM_CONFIG_PATH = SYSTEM_CONFIG_DIRECTORY / "mgo.toml"

#: Persistent application data root, owned by the runtime account.
SYSTEM_STATE_DIRECTORY = PurePosixPath("/var/lib/garden-observatory")

#: Database directory (SQLite file plus its WAL/shm sidecars).
SYSTEM_DATABASE_DIRECTORY = SYSTEM_STATE_DIRECTORY / "db"

#: Media root for captured imagery.
SYSTEM_MEDIA_DIRECTORY = SYSTEM_STATE_DIRECTORY / "media"

#: Queue spool directory. Reserved for future asynchronous work (for example
#: pending notification deliveries); nothing writes to it yet.
SYSTEM_QUEUE_DIRECTORY = SYSTEM_STATE_DIRECTORY / "queues"

#: Volatile-but-persisted runtime state (markers, cursors) that must survive a
#: restart. Reserved; nothing writes to it yet.
SYSTEM_RUNTIME_STATE_DIRECTORY = SYSTEM_STATE_DIRECTORY / "state"

#: Log directory. Application logging goes to the journal via stdout/stderr;
#: this exists for any file-based log destination and for ``log_directory``.
SYSTEM_LOG_DIRECTORY = PurePosixPath("/var/log/garden-observatory")

#: Production database file.
SYSTEM_DATABASE_PATH = SYSTEM_DATABASE_DIRECTORY / "mgo.db"

#: Production capture directory.
SYSTEM_CAPTURE_DIRECTORY = SYSTEM_MEDIA_DIRECTORY / "captures"

#: Every directory the deployment provisions beneath the state root, in
#: creation order (parents first).
SYSTEM_STATE_SUBDIRECTORIES: tuple[PurePosixPath, ...] = (
    SYSTEM_DATABASE_DIRECTORY,
    SYSTEM_MEDIA_DIRECTORY,
    SYSTEM_CAPTURE_DIRECTORY,
    SYSTEM_QUEUE_DIRECTORY,
    SYSTEM_RUNTIME_STATE_DIRECTORY,
)


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
class MotionConfig:
    """Motion-detection settings.

    ``enabled`` gates all motion behaviour (disabled by default, so no frames
    are ever consumed unless it is turned on). ``analysis_interval_seconds``
    controls how often a frame is analysed (deliberately far slower than the
    preview frame rate). ``analysis_width``/``analysis_height`` are the small
    resolution frames are reduced to before comparison, bounding both memory and
    CPU. ``pixel_difference_threshold`` is the per-pixel luminance change (0-255)
    below which a pixel is treated as unchanged noise.
    ``changed_pixel_ratio_threshold`` is the proportion of changed pixels (0-1)
    above which the scene is considered to have meaningfully changed between the
    two most recent frames; its default (0.08) is based on real IMX708
    production measurements. ``cooldown_seconds`` suppresses recording a
    *repeated* motion event that begins again within the window, without hiding
    the eventual return to no-motion.
    """

    enabled: bool
    analysis_interval_seconds: float
    analysis_width: int
    analysis_height: int
    pixel_difference_threshold: int
    changed_pixel_ratio_threshold: float
    cooldown_seconds: float


SUPPORTED_NOTIFICATION_PROVIDERS = frozenset({"log", "null"})


@dataclass(frozen=True)
class NotificationsConfig:
    """Notification framework settings.

    ``enabled`` gates all notification delivery (disabled by default, so no
    provider is ever constructed unless it is turned on). ``provider`` selects
    the delivery provider; only the transport-free ``log`` and ``null``
    providers exist in this task. Future transports (Telegram, email, ...)
    extend this section with their own settings -- no tokens or SMTP details
    belong here yet.
    """

    enabled: bool
    provider: str


@dataclass(frozen=True)
class DatabaseConfig:
    """SQLite runtime settings.

    The database *location* deliberately does not live here: it is already
    ``storage.database_path``, and a second setting naming the same file would
    create two sources of truth for the most safety-critical path in the
    deployment. This section carries only the runtime knobs.

    ``health_check_interval_seconds`` is how often the background monitor runs
    the read-only health check. ``busy_timeout_seconds`` is the finite time
    SQLite waits for a competing writer's lock before failing -- a bound, never
    a retry loop.
    """

    health_check_interval_seconds: int
    busy_timeout_seconds: float


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
    motion: MotionConfig
    notifications: NotificationsConfig
    database: DatabaseConfig
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


#: Sensible defaults for motion when the ``[motion]`` section is absent, so
#: pre-existing configuration files keep loading unchanged with motion disabled.
#: The ``changed_pixel_ratio_threshold`` default (0.08) is based on real IMX708
#: production measurements: it sits above the quiet-scene frame-to-frame
#: variation (~0.003-0.016) and well below clear controlled motion (~0.12-0.61).
_MOTION_DEFAULTS = {
    "enabled": False,
    "analysis_interval_seconds": 1.0,
    "analysis_width": 160,
    "analysis_height": 90,
    "pixel_difference_threshold": 20,
    "changed_pixel_ratio_threshold": 0.08,
    "cooldown_seconds": 5.0,
}

#: Upper bounds for the analysis resolution. Motion analysis operates on a small
#: downscaled frame; these keep memory and per-frame CPU bounded even if a
#: configuration file requests an unreasonable size.
_MOTION_MAX_ANALYSIS_WIDTH = 1920
_MOTION_MAX_ANALYSIS_HEIGHT = 1080


def _validate_motion_config(motion: MotionConfig) -> None:
    """Validate motion settings, rejecting unsafe or nonsensical values."""
    if motion.analysis_interval_seconds <= 0:
        raise ValueError("Motion analysis interval must be positive")

    if not (0 < motion.analysis_width <= _MOTION_MAX_ANALYSIS_WIDTH):
        raise ValueError(
            "Motion analysis width must be between 1 and "
            f"{_MOTION_MAX_ANALYSIS_WIDTH}"
        )

    if not (0 < motion.analysis_height <= _MOTION_MAX_ANALYSIS_HEIGHT):
        raise ValueError(
            "Motion analysis height must be between 1 and "
            f"{_MOTION_MAX_ANALYSIS_HEIGHT}"
        )

    if not (0 <= motion.pixel_difference_threshold <= 255):
        raise ValueError(
            "Motion pixel difference threshold must be between 0 and 255"
        )

    if not (0 < motion.changed_pixel_ratio_threshold <= 1.0):
        raise ValueError(
            "Motion changed-pixel ratio threshold must be within (0, 1]"
        )

    if motion.cooldown_seconds < 0:
        raise ValueError("Motion cooldown cannot be negative")


#: Sensible defaults for notifications when the ``[notifications]`` section is
#: absent, so pre-existing configuration files keep loading unchanged with
#: notifications disabled.
_NOTIFICATIONS_DEFAULTS = {
    "enabled": False,
    "provider": "log",
}


def _validate_notifications_config(notifications: NotificationsConfig) -> None:
    """Validate notification settings, rejecting unsupported providers."""
    if (
        notifications.provider.strip().lower()
        not in SUPPORTED_NOTIFICATION_PROVIDERS
    ):
        supported = ", ".join(sorted(SUPPORTED_NOTIFICATION_PROVIDERS))
        raise ValueError(
            f"Unsupported notification provider {notifications.provider!r}; "
            f"supported providers: {supported}"
        )


#: Sensible defaults for the database when the ``[database]`` section is
#: absent, so pre-Task-7 configuration files keep loading unchanged. They match
#: the values the application used before the section existed: a five-second
#: busy timeout and the same one-minute cadence the health monitor uses.
_DATABASE_DEFAULTS = {
    "health_check_interval_seconds": 60,
    "busy_timeout_seconds": 5.0,
}

#: Upper bound for the SQLite busy timeout. A longer wait would stall a request
#: rather than surfacing a stuck writer.
_MAX_BUSY_TIMEOUT_SECONDS = 60.0


def _validate_database_config(database: DatabaseConfig) -> None:
    """Validate SQLite runtime settings, rejecting unusable values."""
    if database.health_check_interval_seconds < 10:
        raise ValueError(
            "Database health check interval must be at least 10 seconds"
        )

    if database.busy_timeout_seconds <= 0:
        raise ValueError("Database busy timeout must be positive")

    if database.busy_timeout_seconds > _MAX_BUSY_TIMEOUT_SECONDS:
        raise ValueError(
            "Database busy timeout must not exceed "
            f"{_MAX_BUSY_TIMEOUT_SECONDS} seconds"
        )


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

    # The ``[motion]`` section is optional so pre-Task-4 configuration files
    # continue to load; absent keys fall back to safe (disabled) defaults. Only
    # the keys below are read, so any legacy/unknown key (e.g. a pre-refinement
    # ``baseline_refresh_seconds``) is simply ignored rather than rejected.
    motion_data = raw.get("motion", {})
    motion = MotionConfig(
        enabled=bool(motion_data.get("enabled", _MOTION_DEFAULTS["enabled"])),
        analysis_interval_seconds=float(
            motion_data.get(
                "analysis_interval_seconds",
                _MOTION_DEFAULTS["analysis_interval_seconds"],
            )
        ),
        analysis_width=int(
            motion_data.get("analysis_width", _MOTION_DEFAULTS["analysis_width"])
        ),
        analysis_height=int(
            motion_data.get(
                "analysis_height", _MOTION_DEFAULTS["analysis_height"]
            )
        ),
        pixel_difference_threshold=int(
            motion_data.get(
                "pixel_difference_threshold",
                _MOTION_DEFAULTS["pixel_difference_threshold"],
            )
        ),
        changed_pixel_ratio_threshold=float(
            motion_data.get(
                "changed_pixel_ratio_threshold",
                _MOTION_DEFAULTS["changed_pixel_ratio_threshold"],
            )
        ),
        cooldown_seconds=float(
            motion_data.get(
                "cooldown_seconds", _MOTION_DEFAULTS["cooldown_seconds"]
            )
        ),
    )
    _validate_motion_config(motion)

    # The ``[notifications]`` section is optional so pre-Task-5 configuration
    # files continue to load; absent keys fall back to safe (disabled) defaults.
    notifications_data = raw.get("notifications", {})
    notifications = NotificationsConfig(
        enabled=bool(
            notifications_data.get(
                "enabled", _NOTIFICATIONS_DEFAULTS["enabled"]
            )
        ),
        provider=str(
            notifications_data.get(
                "provider", _NOTIFICATIONS_DEFAULTS["provider"]
            )
        ),
    )
    _validate_notifications_config(notifications)

    # The ``[database]`` section is optional so pre-Task-7 configuration files
    # continue to load; absent keys fall back to the values the application
    # already used, so an existing deployment behaves identically.
    database_data = raw.get("database", {})
    database = DatabaseConfig(
        health_check_interval_seconds=int(
            database_data.get(
                "health_check_interval_seconds",
                _DATABASE_DEFAULTS["health_check_interval_seconds"],
            )
        ),
        busy_timeout_seconds=float(
            database_data.get(
                "busy_timeout_seconds",
                _DATABASE_DEFAULTS["busy_timeout_seconds"],
            )
        ),
    )
    _validate_database_config(database)

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
        motion=motion,
        notifications=notifications,
        database=database,
        health=health,
    )
