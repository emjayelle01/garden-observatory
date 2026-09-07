"""Tests for the ``[retention]`` configuration section.

Two things are being protected here. The first is backwards compatibility: an
`mgo.toml` written before this section existed must load unchanged, with
retention off -- a deployment does not acquire a destructive media behaviour by
being upgraded. The second is that an *unbounded* enabled configuration is
refused at load time rather than accepted, because retention with no bound is a
deletion subsystem with nothing telling it when to stop.
"""

from __future__ import annotations

import pytest

from mgo.core.config import parse_config_text

_BASE = """
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
backend = "simulator"
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


def _configuration(section: str = "") -> str:
    """Return the base configuration with an optional retention section."""
    return _BASE + section


# --- an absent or empty section is safe -------------------------------------


def test_absent_retention_section_loads_disabled() -> None:
    """A configuration predating the section loads with retention off.

    This is the backwards-compatibility contract in one assertion: an existing
    deployment upgraded to this build does not acquire a subsystem that deletes
    its media.
    """
    config = parse_config_text(_configuration())

    assert config.retention.enabled is False
    assert config.retention.max_age_days is None
    assert config.retention.max_managed_bytes is None


def test_empty_retention_section_uses_the_safe_defaults() -> None:
    """``[retention]`` with no keys is identical to the section being absent."""
    config = parse_config_text(_configuration("\n[retention]\n"))

    assert config.retention.enabled is False
    assert config.retention.max_age_days is None
    assert config.retention.max_managed_bytes is None
    assert config.retention.minimum_keep_count == 100
    assert config.retention.max_deletions_per_run == 25


def test_absent_and_empty_sections_produce_the_same_configuration() -> None:
    """No key of a missing section differs from an explicitly empty one."""
    assert (
        parse_config_text(_configuration()).retention
        == parse_config_text(_configuration("\n[retention]\n")).retention
    )


def test_explicitly_disabled_retention_accepts_no_bounds() -> None:
    """``enabled = false`` needs no bound: nothing will ever act on one."""
    config = parse_config_text(
        _configuration("\n[retention]\nenabled = false\n")
    )

    assert config.retention.enabled is False


# --- enabled configurations -------------------------------------------------


def test_retention_enabled_with_age_only_is_accepted() -> None:
    """An age bound alone is a complete policy."""
    config = parse_config_text(
        _configuration("\n[retention]\nenabled = true\nmax_age_days = 14\n")
    )

    assert config.retention.enabled is True
    assert config.retention.max_age_days == 14
    assert config.retention.max_managed_bytes is None


def test_retention_enabled_with_managed_bytes_only_is_accepted() -> None:
    """A managed-byte bound alone is a complete policy."""
    config = parse_config_text(
        _configuration(
            "\n[retention]\nenabled = true\nmax_managed_bytes = 1073741824\n"
        )
    )

    assert config.retention.enabled is True
    assert config.retention.max_age_days is None
    assert config.retention.max_managed_bytes == 1073741824


def test_retention_enabled_with_both_bounds_is_accepted() -> None:
    """Both bounds together are accepted and both are carried through."""
    config = parse_config_text(
        _configuration(
            "\n[retention]\n"
            "enabled = true\n"
            "max_age_days = 30\n"
            "max_managed_bytes = 2147483648\n"
            "minimum_keep_count = 50\n"
            "max_deletions_per_run = 10\n"
        )
    )

    assert config.retention.enabled is True
    assert config.retention.max_age_days == 30
    assert config.retention.max_managed_bytes == 2147483648
    assert config.retention.minimum_keep_count == 50
    assert config.retention.max_deletions_per_run == 10


def test_retention_enabled_with_neither_bound_is_rejected() -> None:
    """An enabled policy with no bound at all is refused at load time.

    Accepting it would leave an operator believing retention was managing their
    media while nothing would ever be selected -- and the alternative reading,
    that "no bound" means "delete everything", is worse.
    """
    with pytest.raises(ValueError, match="max_age_days"):
        parse_config_text(_configuration("\n[retention]\nenabled = true\n"))


def test_retention_does_not_require_event_capture_or_camera() -> None:
    """Retention is a storage concern, not a camera one.

    It must be able to reclaim media a now-disabled capture feature already
    produced, so it depends on neither ``camera.enabled`` nor
    ``event_capture.enabled``. The base configuration has both off.
    """
    config = parse_config_text(
        _configuration("\n[retention]\nenabled = true\nmax_age_days = 7\n")
    )

    assert config.retention.enabled is True
    assert config.camera.enabled is False
    assert config.event_capture.enabled is False


# --- rejected values --------------------------------------------------------


@pytest.mark.parametrize("value", ["0", "-1"])
def test_non_positive_max_age_days_is_rejected(value: str) -> None:
    """An age bound of zero or less is a mistake, enabled or not."""
    with pytest.raises(ValueError, match="max_age_days"):
        parse_config_text(
            _configuration(f"\n[retention]\nmax_age_days = {value}\n")
        )


@pytest.mark.parametrize("value", ["0", "-1"])
def test_non_positive_max_managed_bytes_is_rejected(value: str) -> None:
    """A byte budget of zero or less would make every capture eligible."""
    with pytest.raises(ValueError, match="max_managed_bytes"):
        parse_config_text(
            _configuration(f"\n[retention]\nmax_managed_bytes = {value}\n")
        )


@pytest.mark.parametrize("value", ["0", "-5"])
def test_minimum_keep_count_below_one_is_rejected(value: str) -> None:
    """The preservation floor must preserve something.

    Zero would mean "every managed capture is eligible", which removes the one
    protection that holds regardless of what the policy computes.
    """
    with pytest.raises(ValueError, match="minimum_keep_count"):
        parse_config_text(
            _configuration(f"\n[retention]\nminimum_keep_count = {value}\n")
        )


@pytest.mark.parametrize("value", ["0", "-3"])
def test_max_deletions_per_run_below_one_is_rejected(value: str) -> None:
    """A run bound below one is not a safety bound; it is a broken one."""
    with pytest.raises(ValueError, match="max_deletions_per_run"):
        parse_config_text(
            _configuration(f"\n[retention]\nmax_deletions_per_run = {value}\n")
        )


def test_rejection_messages_carry_no_paths_or_unrelated_values() -> None:
    """A configuration error names the setting at fault and nothing else."""
    with pytest.raises(ValueError) as error:
        parse_config_text(_configuration("\n[retention]\nenabled = true\n"))

    message = str(error.value)
    assert "data/captures" not in message
    assert "data/mgo.db" not in message
    assert "Matt's Garden Observatory" not in message


# --- neighbouring configuration is unchanged --------------------------------


def test_existing_pre_retention_configuration_still_loads() -> None:
    """The tracked repository configuration loads with retention disabled."""
    from mgo.core.config import load_config

    config = load_config()

    assert config.retention.enabled is False
    assert config.retention.max_age_days is None
    assert config.retention.max_managed_bytes is None


def test_event_capture_configuration_behaviour_is_unchanged() -> None:
    """Adding retention did not relax the event-capture policy rules.

    ``event_capture.enabled`` still requires its whole camera/preview/motion
    chain, and a retention section present alongside it changes nothing.
    """
    with pytest.raises(ValueError, match=r"camera\.enabled"):
        parse_config_text(
            _configuration(
                # The Task 14.5 hard limits are supplied so the policy rule
                # under test is the one that fires, not the limit rule.
                "\n[event_capture]\nenabled = true\n"
                "max_captures_per_hour = 10\nmax_captures_per_day = 50\n"
                "minimum_free_bytes = 1\n"
                "\n[retention]\nenabled = true\nmax_age_days = 7\n"
            )
        )
