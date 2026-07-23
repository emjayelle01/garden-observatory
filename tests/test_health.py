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
    assert "camera" in health


def test_camera_is_waiting_for_hardware() -> None:
    """Disabled camera configuration should report a waiting state."""
    health = collect_health(load_config())

    assert health["camera"] == {
        "enabled": False,
        "status": "waiting_for_hardware",
    }
