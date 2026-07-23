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


def load_config(path: Path = DEFAULT_CONFIG_PATH) -> MGOConfig:
    """Load and validate MGO configuration from TOML."""
    if not path.exists():
        raise FileNotFoundError(f"Configuration file not found: {path}")

    with path.open("rb") as config_file:
        raw = tomllib.load(config_file)

    application = raw["application"]
    storage = raw["storage"]
    camera = raw["camera"]
    health = raw["health"]

    return MGOConfig(
        application=ApplicationConfig(
            name=str(application["name"]),
            environment=str(application["environment"]),
            host=str(application["host"]),
            port=int(application["port"]),
        ),
        storage=StorageConfig(
            data_directory=_project_path(str(storage["data_directory"])),
            log_directory=_project_path(str(storage["log_directory"])),
            database_path=_project_path(str(storage["database_path"])),
        ),
        camera=CameraConfig(
            enabled=bool(camera["enabled"]),
            capture_directory=_project_path(str(camera["capture_directory"])),
        ),
        health=HealthConfig(
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
        ),
    )

if health.collection_interval_seconds < 10:
    raise ValueError(
        "Health collection interval must be at least 10 seconds"
    )
