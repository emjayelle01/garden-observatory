"""Tests for MGO system-health collection."""

from mgo.core.config import load_config
from mgo.core.health import collect_health


def test_health_response_contains_core_fields() -> None:
    """Health collection should return the required operational fields."""
    health = collect_health(load_config())

    assert health["status"] in {"healthy", "warning", "critical"}
    assert health["hostname"]
    assert health["architecture"]
    assert health["python_version"]
    assert health["uptime_seconds"] >= 0
    assert "memory" in health
    assert "disk" in health
    assert "temperature" in health


def test_camera_is_not_calculated_in_health_collection() -> None:
    """Camera readiness is composed at the API layer, not in collect_health.

    There must be a single source of camera-readiness truth (the camera
    monitor's runtime state), so system-health collection no longer computes
    its own camera section.
    """
    health = collect_health(load_config())

    assert "camera" not in health
