"""Configuration loading for Matt's Garden Observatory."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "mgo.toml"


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


@dataclass(frozen=True)
class CameraConfig:
    """Camera runtime settings."""

    enabled: bool
    capture_directory: Path


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


def load_config(path: Path = DEFAULT_CONFIG_PATH) -> MGOConfig:
    """Load and validate MGO configuration from TOML."""
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
        camera=CameraConfig(
            enabled=bool(camera_data["enabled"]),
            capture_directory=_project_path(str(camera_data["capture_directory"])),
        ),
        health=health,
    )
