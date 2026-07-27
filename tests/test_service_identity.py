"""Tests for the production service identity and filesystem layout.

These lock the deployment contract together: the canonical path constants in
:mod:`mgo.core.config`, the production example configuration, the systemd unit
template and the provisioning scripts must all describe the *same* identity and
layout. A drift in any one of them is a deployment defect that would only show
up on the Raspberry Pi, so it is caught here instead.

Nothing here touches the real ``/etc`` or ``/var`` — the constants are
:class:`~pathlib.PurePosixPath` descriptions of the deployment host and the
scripts are read as text.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path, PurePosixPath

import pytest

from mgo.core.config import (
    CAMERA_GROUP,
    CONFIG_PATH_ENV,
    DEFAULT_CONFIG_PATH,
    PROJECT_ROOT,
    SERVICE_ACCOUNT,
    SERVICE_GROUP,
    SERVICE_UNIT_NAME,
    SYSTEM_CAPTURE_DIRECTORY,
    SYSTEM_CONFIG_DIRECTORY,
    SYSTEM_CONFIG_PATH,
    SYSTEM_DATABASE_DIRECTORY,
    SYSTEM_DATABASE_PATH,
    SYSTEM_LOG_DIRECTORY,
    SYSTEM_MEDIA_DIRECTORY,
    SYSTEM_QUEUE_DIRECTORY,
    SYSTEM_RUNTIME_STATE_DIRECTORY,
    SYSTEM_STATE_DIRECTORY,
    SYSTEM_STATE_SUBDIRECTORIES,
    load_config,
    resolve_config_path,
)

DEPLOY_DIRECTORY = PROJECT_ROOT / "scripts" / "deploy"
UNIT_TEMPLATE = DEPLOY_DIRECTORY / "mgo.service.template"
INSTALL_SCRIPT = DEPLOY_DIRECTORY / "install-service-identity.sh"
VERIFY_SCRIPT = DEPLOY_DIRECTORY / "verify-service-identity.sh"
PRODUCTION_EXAMPLE = PROJECT_ROOT / "config" / "mgo.production.example.toml"


def _read(path: Path) -> str:
    """Read a deployment asset as text, preserving its raw bytes' line endings."""
    return path.read_text(encoding="utf-8")


def _service_directives(unit_text: str) -> list[tuple[str, str]]:
    """Parse ``[Service]`` into ordered key/value pairs.

    A hand-rolled parser rather than :mod:`configparser`: systemd allows a
    directive to be repeated (``Environment=`` is, here) and only the ordered
    pair list preserves every occurrence.
    """
    directives: list[tuple[str, str]] = []
    in_service = False
    for raw_line in unit_text.splitlines():
        line = raw_line.strip()
        if line.startswith("[") and line.endswith("]"):
            in_service = line == "[Service]"
            continue
        if not in_service or not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if separator:
            directives.append((key.strip(), value.strip()))
    return directives


def _directive_values(unit_text: str, key: str) -> list[str]:
    """Return every value configured for ``key`` in ``[Service]``."""
    return [value for name, value in _service_directives(unit_text) if name == key]


# --- canonical path constants ----------------------------------------------


def test_configuration_lives_under_etc_garden_observatory() -> None:
    """Configuration must live in the project's own /etc directory."""
    assert SYSTEM_CONFIG_DIRECTORY.as_posix() == "/etc/garden-observatory"
    assert SYSTEM_CONFIG_PATH.parent == SYSTEM_CONFIG_DIRECTORY


def test_persistent_data_lives_under_var_lib_garden_observatory() -> None:
    """Persistent application data must live under /var/lib, not a home dir."""
    assert SYSTEM_STATE_DIRECTORY.as_posix() == "/var/lib/garden-observatory"

    for directory in SYSTEM_STATE_SUBDIRECTORIES:
        assert directory.is_relative_to(SYSTEM_STATE_DIRECTORY)


def test_state_subdirectories_cover_the_required_roles() -> None:
    """Database, media, queue and runtime-state directories are all provisioned."""
    assert set(SYSTEM_STATE_SUBDIRECTORIES) == {
        SYSTEM_DATABASE_DIRECTORY,
        SYSTEM_MEDIA_DIRECTORY,
        SYSTEM_CAPTURE_DIRECTORY,
        SYSTEM_QUEUE_DIRECTORY,
        SYSTEM_RUNTIME_STATE_DIRECTORY,
    }


def test_state_subdirectories_are_ordered_parents_first() -> None:
    """Creation order must never place a child before its parent."""
    created: list[PurePosixPath] = [SYSTEM_STATE_DIRECTORY]
    for directory in SYSTEM_STATE_SUBDIRECTORIES:
        assert directory.parent in created
        created.append(directory)


def test_database_and_captures_sit_in_their_directories() -> None:
    """The database file and capture directory belong to the layout above."""
    assert SYSTEM_DATABASE_PATH.parent == SYSTEM_DATABASE_DIRECTORY
    assert SYSTEM_CAPTURE_DIRECTORY.parent == SYSTEM_MEDIA_DIRECTORY


def test_system_paths_are_absolute_posix_paths() -> None:
    """Deployment paths describe the Linux host, so they are absolute POSIX."""
    paths = (
        SYSTEM_CONFIG_DIRECTORY,
        SYSTEM_CONFIG_PATH,
        SYSTEM_STATE_DIRECTORY,
        SYSTEM_LOG_DIRECTORY,
        SYSTEM_DATABASE_PATH,
        SYSTEM_CAPTURE_DIRECTORY,
        *SYSTEM_STATE_SUBDIRECTORIES,
    )

    for path in paths:
        assert isinstance(path, PurePosixPath)
        assert path.is_absolute()


def test_config_path_resolution_is_unchanged_by_the_new_layout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The production layout must not change how configuration is selected.

    Backwards compatibility: the loader still resolves to the repository default
    when nothing is set. The new location is reached only because the systemd
    unit sets ``MGO_CONFIG_PATH``.
    """
    monkeypatch.delenv(CONFIG_PATH_ENV, raising=False)

    assert resolve_config_path() == DEFAULT_CONFIG_PATH
    assert Path(str(SYSTEM_CONFIG_PATH)) != DEFAULT_CONFIG_PATH


# --- production example configuration --------------------------------------


@pytest.fixture(scope="module")
def production_example() -> dict[str, object]:
    """The tracked production example configuration, parsed as raw TOML."""
    with PRODUCTION_EXAMPLE.open("rb") as handle:
        return tomllib.load(handle)


def test_production_example_uses_the_canonical_layout(
    production_example: dict[str, object],
) -> None:
    """The example must place all persistent data in the provisioned layout."""
    storage = production_example["storage"]
    camera = production_example["camera"]
    assert isinstance(storage, dict)
    assert isinstance(camera, dict)

    assert storage["data_directory"] == SYSTEM_STATE_DIRECTORY.as_posix()
    assert storage["log_directory"] == SYSTEM_LOG_DIRECTORY.as_posix()
    assert storage["database_path"] == SYSTEM_DATABASE_PATH.as_posix()
    assert camera["capture_directory"] == SYSTEM_CAPTURE_DIRECTORY.as_posix()


def test_production_example_keeps_no_data_in_a_home_directory(
    production_example: dict[str, object],
) -> None:
    """No production path may live under an operator's home directory."""
    storage = production_example["storage"]
    camera = production_example["camera"]
    assert isinstance(storage, dict)
    assert isinstance(camera, dict)

    configured = [
        str(storage["data_directory"]),
        str(storage["log_directory"]),
        str(storage["database_path"]),
        str(camera["capture_directory"]),
    ]

    for value in configured:
        assert value.startswith("/var/"), value
        assert "/home/" not in value


def test_production_example_is_a_valid_configuration() -> None:
    """The example must still pass the application's own validation."""
    config = load_config(PRODUCTION_EXAMPLE)

    assert config.application.environment == "production"
    assert config.camera.enabled is True


# --- systemd unit template -------------------------------------------------


def test_unit_template_runs_as_the_dedicated_account() -> None:
    """The unit must run as the service account, never as root."""
    unit = _read(UNIT_TEMPLATE)

    assert _directive_values(unit, "User") == ["@SERVICE_USER@"]
    assert _directive_values(unit, "Group") == ["@SERVICE_GROUP@"]
    assert "User=root" not in unit


def test_unit_template_grants_only_the_camera_group() -> None:
    """The camera group is the only supplementary privilege granted."""
    unit = _read(UNIT_TEMPLATE)

    assert _directive_values(unit, "SupplementaryGroups") == ["@CAMERA_GROUP@"]


def test_unit_template_grants_no_linux_capabilities() -> None:
    """No capabilities are retained: the service needs none."""
    unit = _read(UNIT_TEMPLATE)

    assert _directive_values(unit, "CapabilityBoundingSet") == [""]
    assert _directive_values(unit, "AmbientCapabilities") == [""]
    assert _directive_values(unit, "NoNewPrivileges") == ["yes"]


def test_unit_template_applies_filesystem_least_privilege() -> None:
    """The filesystem is read-only apart from the provisioned state."""
    unit = _read(UNIT_TEMPLATE)

    assert _directive_values(unit, "ProtectSystem") == ["strict"]
    # Administrative home directories stay unreachable from the runtime identity.
    assert _directive_values(unit, "ProtectHome") == ["yes"]
    assert _directive_values(unit, "PrivateTmp") == ["yes"]
    assert _directive_values(unit, "RestrictSUIDSGID") == ["yes"]


def test_unit_template_provisions_every_state_directory() -> None:
    """systemd creates each persistent directory, so a fresh host needs no setup."""
    unit = _read(UNIT_TEMPLATE)
    declared = set(_directive_values(unit, "StateDirectory")[0].split())

    expected = {
        SYSTEM_STATE_DIRECTORY.name,
        *(
            directory.relative_to(SYSTEM_STATE_DIRECTORY.parent).as_posix()
            for directory in SYSTEM_STATE_SUBDIRECTORIES
        ),
    }
    assert declared == expected


def test_unit_template_directory_modes_are_not_world_accessible() -> None:
    """Runtime directories must never be readable or writable by others."""
    unit = _read(UNIT_TEMPLATE)

    for key in ("StateDirectoryMode", "LogsDirectoryMode"):
        for mode in _directive_values(unit, key):
            assert mode == "0750"


def test_unit_template_points_at_the_production_configuration() -> None:
    """The unit is what selects the new configuration location."""
    unit = _read(UNIT_TEMPLATE)

    assert "Environment=MGO_CONFIG_PATH=@CONFIG_PATH@" in unit


def test_unit_template_starts_the_virtualenv_entry_point_directly() -> None:
    """No package manager runs at start-up, so the account needs no write access."""
    unit = _read(UNIT_TEMPLATE)
    exec_start = _directive_values(unit, "ExecStart")

    assert len(exec_start) == 1
    assert exec_start[0].startswith("@APP_ROOT@/.venv/bin/uvicorn ")
    assert "uv run" not in unit


def test_unit_template_does_not_hide_the_camera_devices() -> None:
    """PrivateDevices would cut the camera off; it must stay unset."""
    unit = _read(UNIT_TEMPLATE)

    assert _directive_values(unit, "PrivateDevices") == []


# --- provisioning scripts --------------------------------------------------


@pytest.mark.parametrize(
    "script", [INSTALL_SCRIPT, VERIFY_SCRIPT], ids=["install", "verify"]
)
def test_deployment_scripts_use_lf_line_endings(script: Path) -> None:
    """CRLF would break these under bash on the Raspberry Pi."""
    assert b"\r\n" not in script.read_bytes()


@pytest.mark.parametrize(
    "script", [INSTALL_SCRIPT, VERIFY_SCRIPT], ids=["install", "verify"]
)
def test_deployment_scripts_are_bash_with_strict_mode(script: Path) -> None:
    """Both scripts fail fast rather than continuing past an error."""
    text = _read(script)

    assert text.startswith("#!/usr/bin/env bash")
    assert "set -euo pipefail" in text or "set -uo pipefail" in text


def test_install_script_creates_a_non_login_system_account() -> None:
    """The runtime account must be a system account with no login shell."""
    text = _read(INSTALL_SCRIPT)

    assert "useradd" in text
    assert "--system" in text
    assert "nologin" in text
    assert "--no-create-home" in text
    assert "usermod --lock" in text


def test_install_script_provisions_every_layout_directory() -> None:
    """Every canonical directory is created by the provisioning script."""
    text = _read(INSTALL_SCRIPT)

    # The three roots are written out in full...
    for root in (
        SYSTEM_CONFIG_DIRECTORY,
        SYSTEM_STATE_DIRECTORY,
        SYSTEM_LOG_DIRECTORY,
    ):
        assert f'"{root.as_posix()}"' in text, root

    # ...and every subdirectory is provisioned relative to the state root.
    for directory in SYSTEM_STATE_SUBDIRECTORIES:
        relative = directory.relative_to(SYSTEM_STATE_DIRECTORY).as_posix()
        assert f'"${{state_dir}}/{relative}"' in text, directory


def test_install_script_never_grants_world_access() -> None:
    """Least privilege: nothing may be made world-readable or world-writable."""
    text = _read(INSTALL_SCRIPT)

    for forbidden in ("777", "o+w", "a+w", "0777", "chmod -R 777"):
        assert forbidden not in text, forbidden


def test_install_script_uses_the_expected_modes() -> None:
    """Directories are 0750 and the configuration file is 0640."""
    text = _read(INSTALL_SCRIPT)

    assert "-m 0750" in text
    assert "-m 0640" in text


def test_install_script_substitutes_every_unit_placeholder() -> None:
    """A placeholder left unsubstituted would produce an invalid unit."""
    unit = _read(UNIT_TEMPLATE)
    install = _read(INSTALL_SCRIPT)

    placeholders = set(re.findall(r"@[A-Z_]+@", unit))
    assert placeholders, "the template should contain placeholders"

    for placeholder in placeholders:
        assert f"s|{placeholder}|" in install, placeholder


def test_install_script_never_overwrites_an_existing_configuration() -> None:
    """A real production configuration must survive re-provisioning."""
    text = _read(INSTALL_SCRIPT)

    assert "already exists — left unchanged" in text


def test_scripts_reference_the_expected_identity_names() -> None:
    """The scripts and the application agree on the account and group names."""
    for script in (INSTALL_SCRIPT, VERIFY_SCRIPT):
        text = _read(script)
        assert f'service_user="{SERVICE_ACCOUNT}"' in text
        assert f'service_group="{SERVICE_GROUP}"' in text
        assert f'camera_group="{CAMERA_GROUP}"' in text
        assert f'service_unit="{SERVICE_UNIT_NAME}"' in text


def test_verify_script_is_read_only() -> None:
    """The verification script must never mutate the deployment."""
    text = _read(VERIFY_SCRIPT)

    for mutating in (
        "useradd",
        "usermod",
        "groupadd",
        "chown",
        "chmod",
        "chgrp",
        "systemctl restart",
        "systemctl start",
        "rm ",
    ):
        assert mutating not in text, mutating
