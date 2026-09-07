"""Configuration tests for the Task 14.5 automatic-capture hard limits.

Every case in the brief's §14.1 for event capture: defaults, absent sections,
an enabled feature with a missing limit, zero, negative and excessive values,
storage-reserve overflow, a valid minimum configuration, and backward
compatibility with the shape production runs today. Everything goes through
the real parser and validator; nothing touches a file outside ``tmp_path``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mgo.core.config import (
    DEFAULT_MAXIMUM_CAPTURE_BYTES,
    MAX_CAPTURES_PER_DAY_LIMIT,
    MAX_CAPTURES_PER_HOUR_LIMIT,
    PROJECT_ROOT,
    EventCaptureConfig,
    load_config,
    parse_config_text,
)

_BASE = """
[application]
name = "MGO"
environment = "test"
host = "127.0.0.1"
port = 8080

[storage]
data_directory = "data"
log_directory = "logs"
database_path = "data/mgo.db"

[camera]
enabled = true
backend = "simulator"
detection_interval_seconds = 60
capture_directory = "data/captures"

[preview]
enabled = true
auto_start = true
restore_after_capture = true
width = 640
height = 480
fps = 15
startup_timeout_seconds = 2.0
shutdown_timeout_seconds = 2.0

[motion]
enabled = true

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

_FULL_SECTION = """
[event_capture]
enabled = {enabled}
max_captures_per_hour = {hourly}
max_captures_per_day = {daily}
minimum_free_bytes = {floor}
maximum_capture_bytes = {reserve}
"""


def _text(
    *,
    enabled: bool = True,
    hourly: str = "10",
    daily: str = "50",
    floor: str = "1024",
    reserve: str = "16777216",
) -> str:
    return _BASE + _FULL_SECTION.format(
        enabled=str(enabled).lower(),
        hourly=hourly,
        daily=daily,
        floor=floor,
        reserve=reserve,
    )


# --- defaults and absence -------------------------------------------------------


def test_the_dataclass_defaults_are_no_limits_and_the_reservation() -> None:
    config = EventCaptureConfig(enabled=False)

    assert config.max_captures_per_hour is None
    assert config.max_captures_per_day is None
    assert config.minimum_free_bytes is None
    assert config.maximum_capture_bytes == DEFAULT_MAXIMUM_CAPTURE_BYTES
    assert DEFAULT_MAXIMUM_CAPTURE_BYTES == 16 * 1024 * 1024


def test_an_absent_section_loads_with_no_limits() -> None:
    config = parse_config_text(_BASE).event_capture

    assert config.enabled is False
    assert (
        config.max_captures_per_hour,
        config.max_captures_per_day,
        config.minimum_free_bytes,
    ) == (None, None, None)
    assert config.maximum_capture_bytes == DEFAULT_MAXIMUM_CAPTURE_BYTES


def test_a_disabled_section_without_limits_loads() -> None:
    config = parse_config_text(_BASE + "\n[event_capture]\nenabled = false\n")

    assert config.event_capture.enabled is False
    assert config.event_capture.max_captures_per_hour is None


def test_the_repository_and_production_examples_stay_disabled_with_limits() -> None:
    for name in ("mgo.toml", "mgo.production.example.toml"):
        config = load_config(PROJECT_ROOT / "config" / name).event_capture
        assert config.enabled is False, name
        assert config.max_captures_per_hour == 20, name
        assert config.max_captures_per_day == 120, name
        assert config.minimum_free_bytes == 2 * 1024**3, name
        assert config.maximum_capture_bytes == 16 * 1024**2, name


def test_the_production_shape_without_the_section_still_parses(tmp_path: Path) -> None:
    """The configuration production runs today has no [event_capture] at all."""
    path = tmp_path / "mgo.toml"
    path.write_text(_BASE, encoding="utf-8")

    config = load_config(path)

    assert config.event_capture == EventCaptureConfig(enabled=False)


# --- enabled requires every limit ------------------------------------------------


def test_a_valid_minimum_enabled_configuration_loads() -> None:
    config = parse_config_text(
        _BASE
        + "\n[event_capture]\nenabled = true\nmax_captures_per_hour = 1\n"
        "max_captures_per_day = 1\nminimum_free_bytes = 1\n"
    ).event_capture

    assert config.enabled is True
    assert config.max_captures_per_hour == 1
    assert config.max_captures_per_day == 1
    assert config.minimum_free_bytes == 1
    assert config.maximum_capture_bytes == DEFAULT_MAXIMUM_CAPTURE_BYTES


@pytest.mark.parametrize(
    "missing",
    ["max_captures_per_hour", "max_captures_per_day", "minimum_free_bytes"],
)
def test_enabling_without_a_mandatory_limit_is_refused(missing: str) -> None:
    lines = {
        "max_captures_per_hour": "max_captures_per_hour = 10",
        "max_captures_per_day": "max_captures_per_day = 50",
        "minimum_free_bytes": "minimum_free_bytes = 1024",
    }
    del lines[missing]
    text = _BASE + "\n[event_capture]\nenabled = true\n" + "\n".join(lines.values())

    with pytest.raises(ValueError, match=rf"requires event_capture\.{missing}"):
        parse_config_text(text)


def test_enabling_with_only_the_flag_is_refused() -> None:
    with pytest.raises(ValueError, match="requires event_capture"):
        parse_config_text(_BASE + "\n[event_capture]\nenabled = true\n")


def test_the_limit_rule_fires_before_the_dependency_rule() -> None:
    """An unbounded enablement is refused for being unbounded, first."""
    text = (
        _BASE.replace("[motion]\nenabled = true", "[motion]\nenabled = false")
        + "\n[event_capture]\nenabled = true\n"
    )
    with pytest.raises(ValueError, match="requires event_capture"):
        parse_config_text(text)


# --- value bounds ---------------------------------------------------------------------


@pytest.mark.parametrize("value", ["0", "-1", str(MAX_CAPTURES_PER_HOUR_LIMIT + 1)])
def test_an_out_of_range_hourly_limit_is_refused(value: str) -> None:
    with pytest.raises(ValueError, match="max_captures_per_hour must be between"):
        parse_config_text(_text(hourly=value))


@pytest.mark.parametrize("value", ["0", "-5", str(MAX_CAPTURES_PER_DAY_LIMIT + 1)])
def test_an_out_of_range_daily_limit_is_refused(value: str) -> None:
    with pytest.raises(ValueError, match="max_captures_per_day must be between"):
        parse_config_text(_text(daily=value))


@pytest.mark.parametrize("value", ["0", "-1024"])
def test_a_non_positive_floor_is_refused(value: str) -> None:
    with pytest.raises(ValueError, match="minimum_free_bytes must be at least 1"):
        parse_config_text(_text(floor=value))


@pytest.mark.parametrize("value", ["0", "-1"])
def test_a_non_positive_reservation_is_refused(value: str) -> None:
    with pytest.raises(ValueError, match="maximum_capture_bytes must be at least 1"):
        parse_config_text(_text(reserve=value))


def test_the_upper_bounds_themselves_are_accepted() -> None:
    config = parse_config_text(
        _text(
            hourly=str(MAX_CAPTURES_PER_HOUR_LIMIT),
            daily=str(MAX_CAPTURES_PER_DAY_LIMIT),
        )
    ).event_capture

    assert config.max_captures_per_hour == MAX_CAPTURES_PER_HOUR_LIMIT
    assert config.max_captures_per_day == MAX_CAPTURES_PER_DAY_LIMIT


def test_a_storage_reserve_that_overflows_is_refused() -> None:
    """floor + reservation must stay within a signed 64-bit integer."""
    with pytest.raises(ValueError, match="exceeds the supported range"):
        parse_config_text(
            _text(floor=str(2**63 - 1), reserve="1")
        )


def test_a_storage_reserve_at_the_range_edge_is_accepted() -> None:
    config = parse_config_text(
        _text(floor=str(2**63 - 2), reserve="1")
    ).event_capture

    assert config.minimum_free_bytes + config.maximum_capture_bytes == 2**63 - 1


def test_limits_are_validated_even_when_disabled() -> None:
    """A mistake is caught when it is written, not when the feature is enabled."""
    with pytest.raises(ValueError, match="max_captures_per_hour must be between"):
        parse_config_text(_text(enabled=False, hourly="0"))


# --- the motion ceiling ----------------------------------------------------------


def test_the_global_ceiling_must_exceed_the_motion_threshold() -> None:
    motion = (
        "[motion]\nenabled = true\nchanged_pixel_ratio_threshold = 0.2\n"
        "global_change_ratio_threshold = 0.2\n"
    )
    with pytest.raises(ValueError, match="global_change_ratio_threshold must be"):
        parse_config_text(_BASE.replace("[motion]\nenabled = true\n", motion))


def test_a_ceiling_just_above_the_threshold_is_accepted() -> None:
    motion = (
        "[motion]\nenabled = true\nchanged_pixel_ratio_threshold = 0.2\n"
        "global_change_ratio_threshold = 0.2000001\n"
    )
    config = parse_config_text(_BASE.replace("[motion]\nenabled = true\n", motion))
    assert config.motion.global_change_ratio_threshold == 0.2000001


@pytest.mark.parametrize("value", ["0", "-0.1", "1.0001"])
def test_an_out_of_range_ceiling_is_refused(value: str) -> None:
    motion = f"[motion]\nenabled = true\nglobal_change_ratio_threshold = {value}\n"
    with pytest.raises(ValueError, match="global change ratio threshold must be"):
        parse_config_text(_BASE.replace("[motion]\nenabled = true\n", motion))


def test_the_ceiling_defaults_to_one_half() -> None:
    assert parse_config_text(_BASE).motion.global_change_ratio_threshold == 0.5


def test_error_messages_name_only_the_setting() -> None:
    try:
        parse_config_text(_text(floor="0"))
    except ValueError as exc:
        message = str(exc)
    else:  # pragma: no cover - the assertion above is the test
        raise AssertionError("expected a refusal")

    assert message == "event_capture.minimum_free_bytes must be at least 1"
    assert "data/" not in message
    assert "/" not in message
