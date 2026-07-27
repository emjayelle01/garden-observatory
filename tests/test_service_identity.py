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
import shutil
import subprocess
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


# --- stale virtual environment detection -----------------------------------
#
# A Python virtual environment is location-dependent: every launcher in
# .venv/bin hard-codes the interpreter's absolute path. Relocating a checkout
# therefore leaves a .venv that systemd cannot execute (status=203/EXEC). The
# installer must detect that and refuse to install a unit that could never
# start.
#
# The tests below execute the *real* detection logic rather than asserting on
# its text: the block is delimited by markers in the script, reads only
# ``venv_dir``/``venv_launcher`` and sets ``venv_problem``, so it can be run in
# isolation. The fixture tree is built by the harness inside a POSIX temporary
# directory so the shebang paths are absolute POSIX paths on every platform.

DETECTION_START = "# >>> venv-detection >>>"
DETECTION_END = "# <<< venv-detection <<<"

#: The real-world shebang observed on the Pi after the checkout was relocated
#: from a home directory to /opt.
STALE_SHEBANG = "#!/home/pi/Projects/garden-observatory/.venv/bin/python3"

#: Fixture builders. Each is bash that populates "$root" as one scenario.
SCENARIOS: dict[str, str] = {
    "missing_venv": "",
    "missing_launcher": 'mkdir -p "$root/.venv/bin"',
    "launcher_not_executable": (
        # Deliberately NOT a shebang file: MSYS2 (Git Bash on Windows) treats
        # any file beginning with "#!" as executable regardless of its mode, so
        # a plain file is the only portable way to exercise this branch.
        'mkdir -p "$root/.venv/bin"\n'
        'printf \'launcher\\n\' > "$root/.venv/bin/uvicorn"\n'
        'chmod 644 "$root/.venv/bin/uvicorn"'
    ),
    "stale_launcher": (
        'mkdir -p "$root/.venv/bin"\n'
        f"printf '{STALE_SHEBANG}\\n' > \"$root/.venv/bin/uvicorn\"\n"
        'chmod 755 "$root/.venv/bin/uvicorn"'
    ),
    "interpreter_missing": (
        'mkdir -p "$root/.venv/bin"\n'
        'printf \'#!%s/.venv/bin/python\\n\' "$root" > "$root/.venv/bin/uvicorn"\n'
        'chmod 755 "$root/.venv/bin/uvicorn"'
    ),
    "relative_interpreter": (
        'mkdir -p "$root/.venv/bin"\n'
        'printf \'#!python\\n\' > "$root/.venv/bin/uvicorn"\n'
        'chmod 755 "$root/.venv/bin/uvicorn"'
    ),
    "healthy": (
        'mkdir -p "$root/.venv/bin"\n'
        'printf \'#!/bin/sh\\n\' > "$root/.venv/bin/python"\n'
        'chmod 755 "$root/.venv/bin/python"\n'
        'printf \'#!%s/.venv/bin/python\\n\' "$root" > "$root/.venv/bin/uvicorn"\n'
        'chmod 755 "$root/.venv/bin/uvicorn"'
    ),
}


def _find_bash() -> str | None:
    """Locate a bash interpreter, including one not on ``PATH``.

    On the Raspberry Pi bash is always on ``PATH``. On the Windows development
    machine Git Bash ships with Git but is usually not on ``PATH``, so it is
    derived from the ``git`` executable's own location rather than skipping the
    tests on the very platform they are written on.
    """
    found = shutil.which("bash")
    if found is not None:
        return found

    git = shutil.which("git")
    if git is None:  # pragma: no cover - git is present wherever this repo is
        return None

    git_root = Path(git).resolve().parent.parent
    for candidate in (
        git_root / "bin" / "bash.exe",
        git_root / "usr" / "bin" / "bash.exe",
    ):
        if candidate.is_file():
            return str(candidate)
    return None  # pragma: no cover - Git without bash is not a supported setup


def _detection_block() -> str:
    """Extract the self-contained detection logic from the install script."""
    text = _read(INSTALL_SCRIPT)
    _, start, remainder = text.partition(DETECTION_START)
    block, end, _ = remainder.partition(DETECTION_END)

    assert start, f"{DETECTION_START} marker is missing from the install script"
    assert end, f"{DETECTION_END} marker is missing from the install script"
    return block


def _detect(scenario: str) -> str:
    """Run the real detection logic against ``scenario`` and return its verdict.

    Returns the ``venv_problem`` description, or an empty string when the
    environment is usable.
    """
    program = "\n".join(
        (
            "set -u",
            'root="$(mktemp -d)"',
            SCENARIOS[scenario],
            'venv_dir="$root/.venv"',
            'venv_launcher="$root/.venv/bin/uvicorn"',
            _detection_block(),
            'printf \'%s\' "$venv_problem"',
            'rm -rf "$root"',
        )
    )
    bash = _find_bash()
    if bash is None:  # pragma: no cover - bash is present on Windows and the Pi
        pytest.skip("bash is unavailable")

    completed = subprocess.run(
        [bash, "-c", program],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout.strip()


def test_stale_launcher_from_a_relocated_checkout_is_detected() -> None:
    """The production failure: a launcher still pointing at the old checkout."""
    problem = _detect("stale_launcher")

    assert "belongs to another checkout" in problem
    # The offending interpreter path is named so the operator can see the cause.
    assert "/home/pi/Projects/garden-observatory/.venv/bin/python3" in problem


def test_missing_virtual_environment_is_detected() -> None:
    """A checkout that was never synced must not yield a unit."""
    problem = _detect("missing_venv")

    assert "no virtual environment at" in problem


def test_missing_launcher_is_detected() -> None:
    """A .venv without the uvicorn entry point is unusable."""
    problem = _detect("missing_launcher")

    assert "no launcher at" in problem


def test_non_executable_launcher_is_detected() -> None:
    """A launcher without the execute bit would fail with status=203/EXEC."""
    problem = _detect("launcher_not_executable")

    assert "is not executable" in problem


def test_missing_interpreter_is_detected() -> None:
    """A launcher pointing at an interpreter that no longer exists is invalid."""
    problem = _detect("interpreter_missing")

    assert "missing or non-executable interpreter" in problem


def test_relative_interpreter_is_detected() -> None:
    """A shebang without an absolute interpreter cannot be validated."""
    problem = _detect("relative_interpreter")

    assert "no absolute interpreter" in problem


def test_healthy_virtual_environment_is_accepted() -> None:
    """A .venv belonging to this checkout must not be reported as a problem."""
    assert _detect("healthy") == ""


def test_install_script_prints_the_remediation_steps() -> None:
    """The operator must be told exactly how to recover."""
    text = _read(INSTALL_SCRIPT)

    assert "LOCATION-DEPENDENT" in text
    assert "rm -rf .venv" in text
    assert "uv sync" in text
    assert "status=203/EXEC" in text
    # Remediation runs in the checkout, as the administrative user.
    assert "cd ${app_root}" in text


def test_install_script_refuses_to_install_a_broken_unit() -> None:
    """Detection must stop provisioning, not merely warn."""
    text = _read(INSTALL_SCRIPT)

    assert 'fail "refusing to install ${service_unit}' in text

    # The check has to run before the unit is written, or a broken unit would
    # already be installed by the time the problem is reported.
    assert text.index(DETECTION_START) < text.index('step "systemd unit"')


def test_install_script_does_not_repair_the_virtual_environment() -> None:
    """Shebangs are never rewritten and the environment is never auto-deleted.

    Recreating it requires ``uv sync`` as the administrative user; doing either
    from a root-run installer would be a surprising, unrequested action.
    """
    text = _read(INSTALL_SCRIPT)

    assert "sed -i" not in text
    # "rm -rf .venv" appears only inside the operator-facing remediation text,
    # never as a command the script runs on the operator's behalf.
    assert 'rm -rf "${venv_dir}"' not in text
    assert "rm -rf ${venv_dir}" not in text


# --- executable bits -------------------------------------------------------


def test_deployment_scripts_are_executable_in_git() -> None:
    """Operators run these directly; mode 100644 breaks ``./script.sh``.

    The mode is what Git records, not what the working tree happens to show:
    Windows checkouts cannot represent the execute bit at all.
    """
    git = shutil.which("git")
    if git is None:  # pragma: no cover - git is present wherever this repo is
        pytest.skip("git is unavailable")

    completed = subprocess.run(
        [
            git,
            "ls-files",
            "--stage",
            "scripts/deploy/install-service-identity.sh",
            "scripts/deploy/verify-service-identity.sh",
            "scripts/deploy/update-main.sh",
            "scripts/ssh/verify-key-auth.sh",
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr

    modes = {
        line.split()[3]: line.split()[0]
        for line in completed.stdout.splitlines()
        if line.strip()
    }
    assert modes, "expected git to report the staged scripts"

    for path, mode in modes.items():
        assert mode == "100755", f"{path} is {mode}, expected 100755"


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
