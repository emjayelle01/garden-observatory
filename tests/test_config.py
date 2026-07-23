"""Tests for MGO configuration loading."""

from pathlib import Path

from mgo.core.config import load_config


def test_default_configuration_loads() -> None:
    """The repository's default configuration should load successfully."""
    config = load_config()

    assert config.application.name == "Matt's Garden Observatory"
    assert config.application.port == 8080
    assert config.camera.enabled is False
    assert config.storage.database_path.name == "mgo.db"


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
