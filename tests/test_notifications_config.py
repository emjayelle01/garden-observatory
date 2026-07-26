"""Tests for the ``[notifications]`` configuration section."""

from __future__ import annotations

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
enabled = false
backend = "rpicam"
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


def _write_config(tmp_path: Path, notifications: str = "") -> Path:
    """Write a base config with an optional ``[notifications]`` section."""
    path = tmp_path / "mgo.toml"
    path.write_text(_BASE_CONFIG + notifications, encoding="utf-8")
    return path


def test_default_configuration_has_safe_notifications() -> None:
    """The repository default config keeps notifications disabled."""
    notifications = load_config().notifications

    assert notifications.enabled is False
    assert notifications.provider == "log"


def test_defaults_apply_when_section_absent(tmp_path: Path) -> None:
    """A config without a [notifications] section loads with safe defaults."""
    notifications = load_config(_write_config(tmp_path)).notifications

    assert notifications.enabled is False
    assert notifications.provider == "log"


def test_notifications_section_is_loaded(tmp_path: Path) -> None:
    """A provided [notifications] section overrides the defaults."""
    section = '\n[notifications]\nenabled = true\nprovider = "null"\n'
    notifications = load_config(_write_config(tmp_path, section)).notifications

    assert notifications.enabled is True
    assert notifications.provider == "null"


def test_partial_section_keeps_remaining_defaults(tmp_path: Path) -> None:
    """Absent keys within the section fall back to their defaults."""
    section = "\n[notifications]\nenabled = true\n"
    notifications = load_config(_write_config(tmp_path, section)).notifications

    assert notifications.enabled is True
    assert notifications.provider == "log"


def test_unsupported_provider_is_rejected(tmp_path: Path) -> None:
    """An unknown provider must be rejected with a clear error at startup."""
    section = '\n[notifications]\nenabled = true\nprovider = "telegram"\n'
    with pytest.raises(ValueError, match="Unsupported notification provider"):
        load_config(_write_config(tmp_path, section))


def test_provider_validation_applies_even_when_disabled(tmp_path: Path) -> None:
    """A misconfigured provider fails fast even if notifications are off."""
    section = '\n[notifications]\nenabled = false\nprovider = "smtp"\n'
    with pytest.raises(ValueError, match="Unsupported notification provider"):
        load_config(_write_config(tmp_path, section))


def test_provider_name_is_matched_case_insensitively(tmp_path: Path) -> None:
    """Validation normalises the provider name like camera backends."""
    section = '\n[notifications]\nenabled = true\nprovider = " LOG "\n'
    notifications = load_config(_write_config(tmp_path, section)).notifications

    # The raw configured value is preserved; validation accepted it.
    assert notifications.provider == " LOG "
