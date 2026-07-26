"""Tests for the ``[motion]`` configuration section.

No Raspberry Pi hardware is required: these exercise pure configuration loading
and validation from in-memory TOML written to a temporary file.
"""

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

_VALID_MOTION = """
[motion]
enabled = true
analysis_interval_seconds = 1.0
analysis_width = 160
analysis_height = 90
pixel_difference_threshold = 20
changed_pixel_ratio_threshold = 0.05
cooldown_seconds = 5.0
"""


def _write(tmp_path: Path, *, motion: str = "") -> Path:
    """Write a config file with an optional customised ``[motion]`` section."""
    path = tmp_path / "mgo.toml"
    path.write_text(_BASE_CONFIG + motion, encoding="utf-8")
    return path


def _motion_section(**overrides: object) -> str:
    """Render a ``[motion]`` section with the given field overrides."""
    fields: dict[str, object] = {
        "enabled": "true",
        "analysis_interval_seconds": 1.0,
        "analysis_width": 160,
        "analysis_height": 90,
        "pixel_difference_threshold": 20,
        "changed_pixel_ratio_threshold": 0.08,
        "cooldown_seconds": 5.0,
    }
    fields.update(overrides)
    lines = "\n".join(f"{key} = {value}" for key, value in fields.items())
    return f"[motion]\n{lines}\n"


def test_missing_motion_section_uses_safe_disabled_defaults(tmp_path: Path) -> None:
    """A config without a [motion] section loads with motion disabled."""
    config = load_config(_write(tmp_path))

    motion = config.motion
    assert motion.enabled is False
    assert motion.analysis_interval_seconds == 1.0
    assert motion.analysis_width == 160
    assert motion.analysis_height == 90
    assert motion.pixel_difference_threshold == 20
    # Default raised to 0.08 (rolling-reference refinement, IMX708 measurements).
    assert motion.changed_pixel_ratio_threshold == 0.08
    assert motion.cooldown_seconds == 5.0
    # The obsolete quiet-baseline refresh field is gone under rolling reference.
    assert not hasattr(motion, "baseline_refresh_seconds")


def test_enabled_motion_configuration_parses(tmp_path: Path) -> None:
    """A fully specified enabled [motion] section parses with its values."""
    config = load_config(_write(tmp_path, motion=_VALID_MOTION))

    motion = config.motion
    assert motion.enabled is True
    assert motion.analysis_interval_seconds == 1.0
    assert motion.pixel_difference_threshold == 20
    assert motion.changed_pixel_ratio_threshold == 0.05


def test_explicit_threshold_overrides_default(tmp_path: Path) -> None:
    """An explicit changed-pixel ratio threshold overrides the 0.08 default."""
    section = _motion_section(changed_pixel_ratio_threshold=0.15)
    config = load_config(_write(tmp_path, motion=section))

    assert config.motion.changed_pixel_ratio_threshold == 0.15


def test_legacy_baseline_refresh_key_is_ignored(tmp_path: Path) -> None:
    """A pre-refinement config carrying baseline_refresh_seconds still loads.

    The parser only reads the keys it knows, so the removed legacy field is
    silently ignored rather than rejected — the config loads cleanly and no such
    attribute exists on the resulting MotionConfig.
    """
    section = _motion_section() + "baseline_refresh_seconds = 30.0\n"
    config = load_config(_write(tmp_path, motion=section))

    assert config.motion.enabled is True
    assert config.motion.changed_pixel_ratio_threshold == 0.08
    assert not hasattr(config.motion, "baseline_refresh_seconds")


def test_repository_default_config_has_motion_disabled() -> None:
    """The tracked development default must ship with motion disabled."""
    config = load_config()

    assert config.motion.enabled is False


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"analysis_interval_seconds": 0}, "interval must be positive"),
        ({"analysis_interval_seconds": -1.0}, "interval must be positive"),
        ({"analysis_width": 0}, "width must be between"),
        ({"analysis_width": 100000}, "width must be between"),
        ({"analysis_height": 0}, "height must be between"),
        ({"analysis_height": 100000}, "height must be between"),
        ({"pixel_difference_threshold": -1}, "pixel difference threshold"),
        ({"pixel_difference_threshold": 256}, "pixel difference threshold"),
        ({"changed_pixel_ratio_threshold": 0.0}, "ratio threshold"),
        ({"changed_pixel_ratio_threshold": 1.5}, "ratio threshold"),
        ({"cooldown_seconds": -1.0}, "cooldown cannot be negative"),
    ],
)
def test_invalid_motion_values_are_rejected(
    tmp_path: Path, overrides: dict[str, object], message: str
) -> None:
    """Out-of-range motion values fail clearly at load time."""
    with pytest.raises(ValueError, match=message):
        load_config(_write(tmp_path, motion=_motion_section(**overrides)))
