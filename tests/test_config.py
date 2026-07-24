"""Tests for MGO configuration loading."""

import tomllib
from pathlib import Path

import pytest

from mgo.core.config import (
    CONFIG_PATH_ENV,
    DEFAULT_CONFIG_PATH,
    load_config,
    resolve_config_path,
)

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
enabled = {enabled}
backend = "{backend}"
detection_interval_seconds = {interval}
capture_directory = "data/captures"
{extra}

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


def _write_config(
    tmp_path: Path,
    *,
    enabled: str = "false",
    backend: str = "rpicam",
    interval: str = "60",
    extra: str = "",
) -> Path:
    """Write a temporary config file with a customised camera section."""
    path = tmp_path / "mgo.toml"
    path.write_text(
        _BASE_CONFIG.format(
            enabled=enabled,
            backend=backend,
            interval=interval,
            extra=extra,
        ),
        encoding="utf-8",
    )
    return path


def test_default_configuration_loads() -> None:
    """The repository's default configuration should load successfully."""
    config = load_config()

    assert config.application.name == "Matt's Garden Observatory"
    assert config.application.port == 8080
    assert config.camera.enabled is False
    assert config.storage.database_path.name == "mgo.db"


def test_default_camera_configuration_is_safe() -> None:
    """The default camera configuration must be disabled and well-formed."""
    camera = load_config().camera

    assert camera.enabled is False
    assert camera.backend == "rpicam"
    assert camera.detection_interval_seconds > 0
    assert camera.device_index is None


def test_camera_device_index_is_parsed(tmp_path: Path) -> None:
    """A configured device index should be loaded as an integer."""
    config = load_config(_write_config(tmp_path, extra="device_index = 2"))

    assert config.camera.device_index == 2


def test_invalid_camera_interval_is_rejected(tmp_path: Path) -> None:
    """A non-positive detection interval must raise a clear error."""
    with pytest.raises(ValueError, match="positive"):
        load_config(_write_config(tmp_path, interval="0"))


def test_unsupported_camera_backend_is_rejected(tmp_path: Path) -> None:
    """An unknown backend must be rejected with a clear error."""
    with pytest.raises(ValueError, match="Unsupported camera backend"):
        load_config(_write_config(tmp_path, backend="webcam"))


def test_negative_camera_device_index_is_rejected(tmp_path: Path) -> None:
    """A negative device index must be rejected."""
    with pytest.raises(ValueError, match="device index"):
        load_config(_write_config(tmp_path, extra="device_index = -1"))


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


def test_health_monitor_configuration_is_valid() -> None:
    """Health monitoring should use safe default settings."""
    health = load_config().health

    assert health.enabled is True
    assert health.collection_interval_seconds >= 10
    assert health.temperature_warning_celsius < health.temperature_critical_celsius
    assert health.disk_warning_percent < health.disk_critical_percent
    assert health.memory_warning_percent < health.memory_critical_percent


# --- External production configuration selection (MGO_CONFIG_PATH) ---------


def _write_external_config(path: Path) -> Path:
    """Write a distinctive external config that proves it is not the default."""
    path.write_text(
        _BASE_CONFIG.format(
            enabled="false",
            backend="rpicam",
            interval="60",
            extra="",
        ).replace('environment = "test"', 'environment = "external-production"'),
        encoding="utf-8",
    )
    return path


def test_no_explicit_path_and_no_env_loads_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Case A: with nothing set, the repository default is loaded (camera off)."""
    monkeypatch.delenv(CONFIG_PATH_ENV, raising=False)

    assert resolve_config_path() == DEFAULT_CONFIG_PATH

    config = load_config()
    assert config.application.environment == "development"
    assert config.camera.enabled is False


def test_env_variable_selects_external_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Case B: a valid MGO_CONFIG_PATH is loaded instead of the default."""
    external = _write_external_config(tmp_path / "external.toml")
    monkeypatch.setenv(CONFIG_PATH_ENV, str(external))

    config = load_config()

    # Proves it did not load the repository default (which is "development").
    assert config.application.environment == "external-production"


def test_explicit_path_overrides_env_variable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Case C: an explicit path always wins over the environment variable."""
    explicit = _write_config(tmp_path, backend="libcamera")
    env_path = _write_external_config(tmp_path / "env.toml")
    monkeypatch.setenv(CONFIG_PATH_ENV, str(env_path))

    assert resolve_config_path(explicit) == explicit

    config = load_config(explicit)
    # The explicit file uses environment "test"; the env file would be
    # "external-production", so this proves the explicit path won.
    assert config.application.environment == "test"
    assert config.camera.backend == "libcamera"


def test_missing_env_config_raises_with_resolved_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Case D: a missing selected file fails with the resolved path, no fallback."""
    missing = tmp_path / "does-not-exist.toml"
    monkeypatch.setenv(CONFIG_PATH_ENV, str(missing))

    with pytest.raises(FileNotFoundError) as excinfo:
        load_config()

    assert str(missing) in str(excinfo.value)


def test_empty_env_config_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """Case E: an empty MGO_CONFIG_PATH raises a clear ValueError."""
    monkeypatch.setenv(CONFIG_PATH_ENV, "")

    with pytest.raises(ValueError, match=CONFIG_PATH_ENV):
        load_config()


def test_whitespace_only_env_config_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Case F: a whitespace-only MGO_CONFIG_PATH raises a clear ValueError."""
    monkeypatch.setenv(CONFIG_PATH_ENV, "   \t  ")

    with pytest.raises(ValueError, match=CONFIG_PATH_ENV):
        load_config()


def test_surrounding_whitespace_is_stripped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Case G: surrounding whitespace is stripped and the trimmed path loads."""
    external = _write_external_config(tmp_path / "padded.toml")
    monkeypatch.setenv(CONFIG_PATH_ENV, f"  {external}  ")

    config = load_config()

    assert config.application.environment == "external-production"


def test_relative_env_config_resolves_against_cwd(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Case H: a relative MGO_CONFIG_PATH resolves against the current dir."""
    _write_external_config(tmp_path / "relative.toml")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(CONFIG_PATH_ENV, "relative.toml")

    assert resolve_config_path() == tmp_path / "relative.toml"

    config = load_config()
    assert config.application.environment == "external-production"


def test_home_expansion_uses_controlled_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Case I: "~" is expanded deterministically against a controlled home."""
    home = tmp_path / "home"
    home.mkdir()
    _write_external_config(home / "mgo.toml")

    # Control home expansion deterministically across platforms.
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv(CONFIG_PATH_ENV, "~/mgo.toml")

    assert resolve_config_path() == home / "mgo.toml"

    config = load_config()
    assert config.application.environment == "external-production"


def test_invalid_config_via_env_propagates(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Case J: an invalid config selected via the env var still fails clearly."""
    invalid = _write_config(tmp_path, backend="webcam")
    monkeypatch.setenv(CONFIG_PATH_ENV, str(invalid))

    with pytest.raises(ValueError, match="Unsupported camera backend"):
        load_config()


def test_invalid_toml_via_env_propagates(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Case J: malformed TOML selected via the env var propagates as an error."""
    broken = tmp_path / "broken.toml"
    broken.write_text("this is = not valid toml [[[", encoding="utf-8")
    monkeypatch.setenv(CONFIG_PATH_ENV, str(broken))

    with pytest.raises(tomllib.TOMLDecodeError):
        load_config()
