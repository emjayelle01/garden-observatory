"""Tests for persistent MGO health monitoring."""

from pathlib import Path
from unittest.mock import patch

from mgo.core.config import MGOConfig, StorageConfig, load_config
from mgo.core.database import apply_migrations
from mgo.core.health_monitor import record_health_observation
from mgo.core.observations import list_observations


def _config_with_database(database_path: Path) -> MGOConfig:
    """Return the default configuration with isolated test storage."""
    config = load_config()
    return MGOConfig(
        application=config.application,
        storage=StorageConfig(
            data_directory=database_path.parent,
            log_directory=database_path.parent / "logs",
            database_path=database_path,
        ),
        camera=config.camera,
        preview=config.preview,
        motion=config.motion,
        notifications=config.notifications,
        health=config.health,
    )


def test_health_snapshot_is_persisted(tmp_path: Path) -> None:
    """A collected health snapshot should become an observation."""
    database_path = tmp_path / "test.db"
    apply_migrations(database_path)
    config = _config_with_database(database_path)
    fake_health = {
        "status": "healthy",
        "hostname": "mgo-test",
        "temperature": {"celsius": 45.2, "status": "healthy"},
        "memory": {"used_percent": 25.0, "status": "healthy"},
        "disk": {"used_percent": 10.0, "status": "healthy"},
        "camera": {"enabled": False, "status": "waiting_for_hardware"},
    }

    with patch(
        "mgo.core.health_monitor.collect_health",
        return_value=fake_health,
    ):
        recorded = record_health_observation(config)

    observations = list_observations(database_path)
    assert recorded.kind == "health_snapshot"
    assert recorded.source == "mgo-health"
    assert recorded.status == "healthy"
    assert recorded.payload == fake_health
    assert observations == [recorded]


def test_missing_health_status_is_recorded_as_unknown(tmp_path: Path) -> None:
    """Missing overall status should be reported honestly as unknown."""
    database_path = tmp_path / "test.db"
    apply_migrations(database_path)
    config = _config_with_database(database_path)

    with patch(
        "mgo.core.health_monitor.collect_health",
        return_value={"temperature": None},
    ):
        recorded = record_health_observation(config)

    assert recorded.status == "unknown"
    assert recorded.summary == "System health: unknown"
