"""Tests for MGO configuration loading."""

import tomllib
from pathlib import Path

import pytest

from mgo.core.config import (
    CONFIG_PATH_ENV,
    DEFAULT_CONFIG_PATH,
    SUPPORTED_CAMERA_BACKENDS,
    PreviewConfig,
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


# --- preview configuration -------------------------------------------------

_PREVIEW_SECTION = """
[preview]
enabled = true
width = 640
height = 480
fps = 30
startup_timeout_seconds = 3.0
shutdown_timeout_seconds = 4.0
"""


def _write_config_with_preview(tmp_path: Path, preview: str) -> Path:
    """Write a base config with an appended ``[preview]`` section."""
    path = tmp_path / "mgo.toml"
    base = _BASE_CONFIG.format(
        enabled="false", backend="rpicam", interval="60", extra=""
    )
    path.write_text(base + preview, encoding="utf-8")
    return path


def test_default_configuration_includes_preview_defaults() -> None:
    """The repository default config exposes safe preview settings."""
    preview = load_config().preview

    assert preview.enabled is False
    assert preview.width == 1280
    assert preview.height == 720
    assert preview.fps == 15
    assert preview.startup_timeout_seconds == 5.0
    assert preview.shutdown_timeout_seconds == 5.0


def test_preview_defaults_apply_when_section_absent(tmp_path: Path) -> None:
    """A config without a [preview] section still loads with safe defaults."""
    preview = load_config(_write_config(tmp_path)).preview

    assert preview.enabled is False
    assert preview.width == 1280
    assert preview.height == 720
    assert preview.fps == 15


def test_preview_section_is_loaded(tmp_path: Path) -> None:
    """A provided [preview] section overrides the defaults."""
    preview = load_config(
        _write_config_with_preview(tmp_path, _PREVIEW_SECTION)
    ).preview

    assert preview.enabled is True
    assert preview.width == 640
    assert preview.height == 480
    assert preview.fps == 30
    assert preview.startup_timeout_seconds == 3.0
    assert preview.shutdown_timeout_seconds == 4.0


def test_invalid_preview_fps_is_rejected(tmp_path: Path) -> None:
    """A non-positive preview fps must raise a clear error."""
    section = "\n[preview]\nenabled = true\nfps = 0\n"
    with pytest.raises(ValueError, match="fps must be positive"):
        load_config(_write_config_with_preview(tmp_path, section))


def test_invalid_preview_dimensions_are_rejected(tmp_path: Path) -> None:
    """Non-positive preview dimensions must raise a clear error."""
    section = "\n[preview]\nenabled = true\nwidth = 0\n"
    with pytest.raises(ValueError, match="width and height must be positive"):
        load_config(_write_config_with_preview(tmp_path, section))


def test_invalid_preview_timeout_is_rejected(tmp_path: Path) -> None:
    """A non-positive preview timeout must raise a clear error."""
    section = "\n[preview]\nenabled = true\nshutdown_timeout_seconds = 0\n"
    with pytest.raises(ValueError, match="shutdown timeout must be positive"):
        load_config(_write_config_with_preview(tmp_path, section))


# --- managed preview lifecycle (Task 12) -----------------------------------
#
# ``auto_start`` and ``restore_after_capture`` change what the application does
# to the camera without anyone asking, so the tests below pin two things: the
# defaults are OFF everywhere, and a policy that could never run is refused
# rather than silently ignored.


def _write_managed_preview_config(
    tmp_path: Path,
    *,
    camera_enabled: str = "true",
    preview_enabled: str = "true",
    auto_start: str | None = None,
    restore_after_capture: str | None = None,
) -> Path:
    """Write a config whose ``[preview]`` section carries the managed policies.

    Keys left as ``None`` are omitted entirely, which is how a configuration
    written before Task 12 looks.
    """
    path = tmp_path / "mgo.toml"
    base = _BASE_CONFIG.format(
        enabled=camera_enabled, backend="rpicam", interval="60", extra=""
    )
    lines = ["", "[preview]", f"enabled = {preview_enabled}"]
    if auto_start is not None:
        lines.append(f"auto_start = {auto_start}")
    if restore_after_capture is not None:
        lines.append(f"restore_after_capture = {restore_after_capture}")
    path.write_text(base + "\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_configuration_without_managed_preview_keys_loads(
    tmp_path: Path,
) -> None:
    """A pre-Task-12 configuration file still loads, with both policies off."""
    preview = load_config(
        _write_managed_preview_config(tmp_path)
    ).preview

    assert preview.enabled is True
    assert preview.auto_start is False
    assert preview.restore_after_capture is False


def test_managed_preview_dataclass_defaults_are_off() -> None:
    """Constructing a PreviewConfig without the policies leaves them off.

    The dataclass default is the second place the "off unless asked for"
    guarantee lives (the parser's ``_PREVIEW_DEFAULTS`` is the first); both are
    pinned so neither can drift on its own.
    """
    preview = PreviewConfig(
        enabled=True,
        width=1280,
        height=720,
        fps=15,
        startup_timeout_seconds=5.0,
        shutdown_timeout_seconds=5.0,
    )

    assert preview.auto_start is False
    assert preview.restore_after_capture is False


def test_managed_preview_defaults_apply_when_section_absent(
    tmp_path: Path,
) -> None:
    """A config with no [preview] section at all leaves both policies off."""
    preview = load_config(_write_config(tmp_path)).preview

    assert preview.auto_start is False
    assert preview.restore_after_capture is False


def test_explicit_false_managed_preview_values_load(tmp_path: Path) -> None:
    """Writing the policies out explicitly as false is accepted."""
    preview = load_config(
        _write_managed_preview_config(
            tmp_path, auto_start="false", restore_after_capture="false"
        )
    ).preview

    assert preview.auto_start is False
    assert preview.restore_after_capture is False


def test_explicit_true_managed_preview_values_load(tmp_path: Path) -> None:
    """Both policies may be enabled together when preview and camera are on."""
    preview = load_config(
        _write_managed_preview_config(
            tmp_path, auto_start="true", restore_after_capture="true"
        )
    ).preview

    assert preview.auto_start is True
    assert preview.restore_after_capture is True


def test_auto_start_may_be_enabled_without_restoration(tmp_path: Path) -> None:
    """The policies are independent: auto-start alone is valid.

    This is "start preview with the process, but keep the Task 11 behaviour
    where a capture leaves preview stopped".
    """
    preview = load_config(
        _write_managed_preview_config(
            tmp_path, auto_start="true", restore_after_capture="false"
        )
    ).preview

    assert preview.auto_start is True
    assert preview.restore_after_capture is False


def test_restoration_may_be_enabled_without_auto_start(tmp_path: Path) -> None:
    """The policies are independent: restoration alone is valid.

    This is "preview is started by hand, but should survive a capture".
    """
    preview = load_config(
        _write_managed_preview_config(
            tmp_path, auto_start="false", restore_after_capture="true"
        )
    ).preview

    assert preview.auto_start is False
    assert preview.restore_after_capture is True


def test_auto_start_with_preview_disabled_is_rejected(tmp_path: Path) -> None:
    """Auto-starting a disabled preview is a configuration error."""
    with pytest.raises(
        ValueError, match=r"preview\.auto_start = true requires preview\.enabled"
    ):
        load_config(
            _write_managed_preview_config(
                tmp_path, preview_enabled="false", auto_start="true"
            )
        )


def test_restoration_with_preview_disabled_is_rejected(tmp_path: Path) -> None:
    """Restoring a disabled preview is a configuration error."""
    with pytest.raises(
        ValueError,
        match=r"preview\.restore_after_capture = true requires preview\.enabled",
    ):
        load_config(
            _write_managed_preview_config(
                tmp_path, preview_enabled="false", restore_after_capture="true"
            )
        )


def test_auto_start_with_camera_disabled_is_rejected(tmp_path: Path) -> None:
    """A managed policy with the camera disabled is a configuration error."""
    with pytest.raises(
        ValueError, match=r"preview\.auto_start = true requires camera\.enabled"
    ):
        load_config(
            _write_managed_preview_config(
                tmp_path, camera_enabled="false", auto_start="true"
            )
        )


def test_restoration_with_camera_disabled_is_rejected(tmp_path: Path) -> None:
    """Restoration with the camera disabled is a configuration error."""
    with pytest.raises(
        ValueError,
        match=r"preview\.restore_after_capture = true requires camera\.enabled",
    ):
        load_config(
            _write_managed_preview_config(
                tmp_path, camera_enabled="false", restore_after_capture="true"
            )
        )


def test_managed_preview_rejection_discloses_only_the_conflict(
    tmp_path: Path,
) -> None:
    """A refusal names the two conflicting settings and nothing else."""
    path = _write_managed_preview_config(
        tmp_path, preview_enabled="false", auto_start="true"
    )

    with pytest.raises(ValueError) as excinfo:
        load_config(path)

    message = str(excinfo.value)
    assert message == "preview.auto_start = true requires preview.enabled = true"
    # No configuration path, no unrelated value.
    assert str(path) not in message
    assert "capture_directory" not in message
    assert "database" not in message


def test_tracked_configurations_keep_managed_preview_off() -> None:
    """Neither tracked configuration file may switch managed mode on.

    Enabling managed preview is a deliberate edit to an *external* production
    configuration, never something a deployment inherits by copying a file from
    the repository.
    """
    tracked = (
        DEFAULT_CONFIG_PATH,
        DEFAULT_CONFIG_PATH.parent / "mgo.production.example.toml",
    )

    for path in tracked:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
        preview = raw.get("preview", {})
        assert preview.get("auto_start", False) is False, path.name
        assert preview.get("restore_after_capture", False) is False, path.name


def test_tracked_configurations_document_the_managed_keys() -> None:
    """Both keys are present (as false) so an operator can see the choice."""
    tracked = (
        DEFAULT_CONFIG_PATH,
        DEFAULT_CONFIG_PATH.parent / "mgo.production.example.toml",
    )

    for path in tracked:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
        assert "auto_start" in raw["preview"], path.name
        assert "restore_after_capture" in raw["preview"], path.name


def test_existing_preview_settings_are_unchanged_by_task_12() -> None:
    """The dimensions and timing defaults keep their pre-Task-12 values."""
    preview = load_config().preview

    assert preview.width == 1280
    assert preview.height == 720
    assert preview.fps == 15
    assert preview.startup_timeout_seconds == 5.0
    assert preview.shutdown_timeout_seconds == 5.0
    # The default preview enabled state is untouched.
    assert preview.enabled is False


def test_camera_backend_vocabulary_is_unchanged_by_task_12() -> None:
    """Task 12 adds no backend name and removes none."""
    assert frozenset(
        {"rpicam", "libcamera", "simulator", "null", "none"}
    ) == SUPPORTED_CAMERA_BACKENDS


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
