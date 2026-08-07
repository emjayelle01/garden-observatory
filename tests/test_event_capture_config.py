"""Tests for the ``[event_capture]`` configuration section.

Two things are being protected here. The first is backwards compatibility: an
`mgo.toml` written before this section existed must load unchanged, with the
feature off -- a deployment does not acquire an automatic camera behaviour by
being upgraded. The second is that an *impossible* enabled configuration is
refused at load time rather than accepted and silently ineffective, because a
feature that quietly never runs is worse than one that refuses to start.
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
enabled = {camera}
backend = "simulator"
detection_interval_seconds = 60
capture_directory = "data/captures"

[preview]
enabled = {preview}
auto_start = {auto_start}
restore_after_capture = {restore}
width = 640
height = 480
fps = 15
startup_timeout_seconds = 2.0
shutdown_timeout_seconds = 2.0

[motion]
enabled = {motion}

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


def _configuration(
    *,
    camera: bool = True,
    preview: bool = True,
    auto_start: bool = True,
    restore: bool = True,
    motion: bool = True,
    event_capture: bool | None = True,
) -> str:
    """Render a configuration, optionally omitting ``[event_capture]``."""
    text = _BASE.format(
        camera=str(camera).lower(),
        preview=str(preview).lower(),
        auto_start=str(auto_start).lower(),
        restore=str(restore).lower(),
        motion=str(motion).lower(),
    )
    if event_capture is None:
        return text
    return f"{text}\n[event_capture]\nenabled = {str(event_capture).lower()}\n"


# --- default-disabled behaviour ---------------------------------------------


def test_an_absent_section_means_disabled() -> None:
    """A configuration file that predates the feature loads with it off."""
    config = parse_config_text(_configuration(event_capture=None))

    assert config.event_capture.enabled is False


def test_an_explicit_false_means_disabled() -> None:
    """Writing the section out and switching it off is the same thing."""
    config = parse_config_text(_configuration(event_capture=False))

    assert config.event_capture.enabled is False


def test_an_empty_section_means_disabled() -> None:
    """A present-but-empty section is not an accidental enablement."""
    config = parse_config_text(
        _configuration(event_capture=None) + "\n[event_capture]\n"
    )

    assert config.event_capture.enabled is False


def test_the_repository_default_configuration_disables_event_capture() -> None:
    """The shipped `config/mgo.toml` never turns the camera on by itself."""
    from mgo.core.config import DEFAULT_CONFIG_PATH, load_config

    config = load_config(DEFAULT_CONFIG_PATH)

    assert config.event_capture.enabled is False


# --- enablement ---------------------------------------------------------------


def test_a_fully_compatible_enabled_configuration_loads() -> None:
    """With the whole camera/preview/motion chain on, enabling is accepted."""
    config = parse_config_text(_configuration(event_capture=True))

    assert config.event_capture.enabled is True
    assert config.camera.enabled is True
    assert config.preview.enabled is True
    assert config.preview.auto_start is True
    assert config.preview.restore_after_capture is True
    assert config.motion.enabled is True


@pytest.mark.parametrize(
    ("overrides", "requirement"),
    [
        # ``preview`` off also switches the two managed policies off, because
        # the pre-existing managed-preview validation would otherwise reject the
        # configuration first and this test would prove nothing about event
        # capture.
        ({"camera": False, "preview": False, "auto_start": False, "restore": False},
         "camera.enabled"),
        ({"preview": False, "auto_start": False, "restore": False},
         "preview.enabled"),
        ({"auto_start": False}, "preview.auto_start"),
        ({"restore": False}, "preview.restore_after_capture"),
        ({"motion": False}, "motion.enabled"),
    ],
)
def test_an_impossible_enabled_configuration_is_rejected(
    overrides: dict[str, bool], requirement: str
) -> None:
    """Each dependency is enforced, and named, at load time."""
    with pytest.raises(ValueError) as excinfo:
        parse_config_text(_configuration(event_capture=True, **overrides))

    message = str(excinfo.value)
    assert message == (
        f"event_capture.enabled = true requires {requirement} = true"
    )


def test_the_camera_dependency_is_reported_first() -> None:
    """With everything off, the most fundamental conflict is the one named."""
    with pytest.raises(ValueError) as excinfo:
        parse_config_text(
            _configuration(
                camera=False,
                preview=False,
                auto_start=False,
                restore=False,
                motion=False,
                event_capture=True,
            )
        )

    assert "camera.enabled" in str(excinfo.value)


# --- what the message may not contain ---------------------------------------


def test_a_rejection_leaks_no_path_or_unrelated_value() -> None:
    """A validation message names two settings and nothing else."""
    with pytest.raises(ValueError) as excinfo:
        parse_config_text(_configuration(motion=False, event_capture=True))

    message = str(excinfo.value)
    for leak in (
        "mgo.toml",
        "data/mgo.db",
        "data/captures",
        "127.0.0.1",
        "8080",
        "simulator",
        "[event_capture]",
        "temperature",
        "\n",
    ):
        assert leak not in message, leak
    # Short enough to be a diagnosis rather than a dump of the file.
    assert len(message) < 120


def test_a_disabled_feature_never_rejects_anything() -> None:
    """The dependencies exist to make an enabled feature work, not to police."""
    config = parse_config_text(
        _configuration(
            camera=False,
            preview=False,
            auto_start=False,
            restore=False,
            motion=False,
            event_capture=False,
        )
    )

    assert config.event_capture.enabled is False


# --- the rest of configuration validation is untouched -----------------------


def test_existing_validation_still_applies() -> None:
    """Adding a section did not weaken the checks that were already there."""
    broken = _configuration(event_capture=False).replace(
        "[motion]\nenabled = true",
        "[motion]\nenabled = true\ncooldown_seconds = -1.0",
    )

    with pytest.raises(ValueError) as excinfo:
        parse_config_text(broken)

    assert "cooldown" in str(excinfo.value).lower()


def test_the_managed_preview_policy_check_is_unchanged() -> None:
    """The Task 12 cross-section rule still fires on its own terms."""
    with pytest.raises(ValueError) as excinfo:
        parse_config_text(
            _configuration(preview=False, restore=False, event_capture=False)
        )

    assert str(excinfo.value) == (
        "preview.auto_start = true requires preview.enabled = true"
    )


def test_an_older_configuration_loads_unchanged() -> None:
    """Every pre-Task-13.1 section keeps the exact values it had."""
    config = parse_config_text(
        _configuration(
            camera=False,
            preview=False,
            auto_start=False,
            restore=False,
            motion=False,
            event_capture=None,
        )
    )

    assert config.application.name == "Matt's Garden Observatory"
    assert config.camera.enabled is False
    assert config.camera.backend == "simulator"
    assert config.preview.enabled is False
    assert config.preview.auto_start is False
    assert config.preview.restore_after_capture is False
    assert config.motion.enabled is False
    assert config.notifications.enabled is False
    assert config.database.busy_timeout_seconds == 5.0
    assert config.health.enabled is True
    assert config.event_capture.enabled is False
