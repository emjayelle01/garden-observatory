"""Tests for MGO configuration loading."""

from pathlib import Path

import pytest

from mgo.core.config import load_config

_BASE_CONFIG = """
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
enabled = {enabled}
backend = "{backend}"
detection_interval_seconds = {interval}
capture_directory = "data/captures"
{extra}

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


def _write_config(
    tmp_path: Path,
    *,
    enabled: str = "false",
    backend: str = "rpicam",
    interval: str = "60",
    extra: str = "",
) -> Path:
    """Write a temporary config file with a customised camera section."""
    path = tmp_path / "mgo.toml"
    path.write_text(
        _BASE_CONFIG.format(
            enabled=enabled,
            backend=backend,
            interval=interval,
            extra=extra,
        ),
        encoding="utf-8",
    )
    return path


def test_default_configuration_loads() -> None:
    """The repository's default configuration should load successfully."""
    config = load_config()

    assert config.application.name == "Matt's Garden Observatory"
    assert config.application.port == 8080
    assert config.camera.enabled is False
    assert config.storage.database_path.name == "mgo.db"


def test_default_camera_configuration_is_safe() -> None:
    """The default camera configuration must be disabled and well-formed."""
    camera = load_config().camera

    assert camera.enabled is False
    assert camera.backend == "rpicam"
    assert camera.detection_interval_seconds > 0
    assert camera.device_index is None


def test_camera_device_index_is_parsed(tmp_path: Path) -> None:
    """A configured device index should be loaded as an integer."""
    config = load_config(_write_config(tmp_path, extra="device_index = 2"))

    assert config.camera.device_index == 2


def test_invalid_camera_interval_is_rejected(tmp_path: Path) -> None:
    """A non-positive detection interval must raise a clear error."""
    with pytest.raises(ValueError, match="positive"):
        load_config(_write_config(tmp_path, interval="0"))


def test_unsupported_camera_backend_is_rejected(tmp_path: Path) -> None:
    """An unknown backend must be rejected with a clear error."""
    with pytest.raises(ValueError, match="Unsupported camera backend"):
        load_config(_write_config(tmp_path, backend="webcam"))


def test_negative_camera_device_index_is_rejected(tmp_path: Path) -> None:
    """A negative device index must be rejected."""
    with pytest.raises(ValueError, match="device index"):
        load_config(_write_config(tmp_path, extra="device_index = -1"))


def test_configured_paths_are_absolute() -> None:
    """Configured project paths should resolve to absolute filesystem paths."""
    config = load_config()

    paths: tuple[Path, ...] = (
        config.storage.data_directory,
        config.storage.log_directory,
        config.storage.database_path,
        config.camera.capture_directory,
    )

    assert all(path.is_absolute() for path in paths)


def test_health_monitor_configuration_is_valid() -> None:
    """Health monitoring should use safe default settings."""
    health = load_config().health

    assert health.enabled is True
    assert health.collection_interval_seconds >= 10
    assert health.temperature_warning_celsius < health.temperature_critical_celsius
    assert health.disk_warning_percent < health.disk_critical_percent
    assert health.memory_warning_percent < health.memory_critical_percent
