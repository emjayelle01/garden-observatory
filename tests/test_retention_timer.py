"""Tests for the scheduled-retention deployment assets (Task 14.5).

Static analysis of the tracked template, timer and installer, plus real
executions of the installer under bash against a temporary unit directory:
dry run, install, idempotence, rollback, and the refusals that keep the
validation aids away from a real host. Nothing here runs ``systemctl``,
touches ``/etc`` or needs root. Where bash is unavailable the execution tests
skip and the static tests still run.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from mgo.core.config import (
    PROJECT_ROOT,
    SERVICE_ACCOUNT,
    SYSTEM_BACKUP_DIRECTORY,
    SYSTEM_CAPTURE_DIRECTORY,
    SYSTEM_CONFIG_PATH,
    SYSTEM_DATABASE_DIRECTORY,
)

DEPLOY = PROJECT_ROOT / "scripts" / "deploy"
TEMPLATE = DEPLOY / "mgo-retention.service.template"
TIMER = DEPLOY / "mgo-retention.timer"
INSTALLER = DEPLOY / "install-retention-timer.sh"
BACKUP_TEMPLATE = DEPLOY / "mgo-backup.service.template"
BACKUP_TIMER = DEPLOY / "mgo-backup.timer"


def _read(path: Path) -> str:
    return path.read_bytes().decode("utf-8")


def _directives(text: str, section: str) -> dict[str, list[str]]:
    """Return ``{key: [values]}`` for one ``[section]`` of a unit file."""
    found: dict[str, list[str]] = {}
    current: str | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            current = line
            continue
        if current == section and "=" in line:
            key, value = line.split("=", 1)
            found.setdefault(key.strip(), []).append(value.strip())
    return found


def _service() -> dict[str, list[str]]:
    return _directives(_read(TEMPLATE), "[Service]")


def _find_bash() -> str | None:
    for candidate in ("bash", "/usr/bin/bash"):
        found = shutil.which(candidate)
        if found and "System32" not in found:
            return found
    for candidate in (
        Path(r"C:\Program Files\Git\bin\bash.exe"),
        Path(r"C:\Program Files\Git\usr\bin\bash.exe"),
    ):
        if candidate.exists():
            return str(candidate)
    return None


def _posix(path: Path) -> str:
    """A path bash on this host understands (MSYS on Windows)."""
    text = path.as_posix()
    if re.match(r"^[A-Za-z]:/", text):
        return "/" + text[0].lower() + text[2:]
    return text


def _app_root(tmp_path: Path, name: str = "app") -> str:
    """A checkout stand-in carrying the one thing the installer checks for."""
    root = tmp_path / name
    entry = root / ".venv" / "bin" / "mgo-retention"
    entry.parent.mkdir(parents=True, exist_ok=True)
    entry.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8", newline="\n")
    entry.chmod(0o755)
    return _posix(root)


def _run_installer(
    *arguments: str, cwd: Path | None = None
) -> subprocess.CompletedProcess[str]:
    bash = _find_bash()
    assert bash is not None
    return subprocess.run(
        [bash, _posix(INSTALLER), *arguments],
        cwd=str(cwd or PROJECT_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        env={**os.environ, "LC_ALL": "C"},
        timeout=120,
    )


# --- line endings and tracking --------------------------------------------------------


@pytest.mark.parametrize("asset", [TEMPLATE, TIMER, INSTALLER])
def test_assets_use_lf_line_endings(asset: Path) -> None:
    assert b"\r" not in asset.read_bytes(), asset.name


def test_the_installer_is_executable_in_git() -> None:
    result = subprocess.run(
        ["git", "ls-files", "-s", "--", INSTALLER.relative_to(PROJECT_ROOT).as_posix()],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        pytest.skip("git metadata unavailable")
    assert result.stdout.startswith("100755"), result.stdout


def test_the_installer_is_strict_bash() -> None:
    text = _read(INSTALLER)
    assert text.startswith("#!/usr/bin/env bash\n")
    assert "set -euo pipefail" in text


# --- the service template -----------------------------------------------------


def test_the_service_is_a_one_shot_running_the_real_entry_point() -> None:
    service = _service()
    assert service["Type"] == ["oneshot"]
    assert service["ExecStart"] == [
        "@APP_ROOT@/.venv/bin/mgo-retention scheduled-run --execute "
        "--backup-directory @BACKUP_DIR@"
    ]
    assert service["Restart"] == ["no"]
    assert service["User"] == ["@SERVICE_USER@"]
    assert service["Group"] == ["@SERVICE_GROUP@"]
    assert "SupplementaryGroups" not in service


def test_the_service_never_uses_a_shell_or_a_relative_path() -> None:
    for line in _service()["ExecStart"]:
        assert line.startswith("@APP_ROOT@/"), line
        assert "sh -c" not in line
        assert "bash" not in line


def test_the_service_carries_the_backup_units_sandbox() -> None:
    backup = _directives(_read(BACKUP_TEMPLATE), "[Service]")
    retention = _service()
    for key in (
        "NoNewPrivileges",
        "ProtectSystem",
        "ProtectHome",
        "PrivateTmp",
        "PrivateDevices",
        "ProtectProc",
        "ProtectClock",
        "ProtectControlGroups",
        "ProtectHostname",
        "ProtectKernelLogs",
        "ProtectKernelModules",
        "ProtectKernelTunables",
        "LockPersonality",
        "RestrictNamespaces",
        "RestrictRealtime",
        "RestrictSUIDSGID",
        "SystemCallArchitectures",
        "CapabilityBoundingSet",
        "AmbientCapabilities",
        "UMask",
        "Nice",
        "IOSchedulingClass",
        "CPUSchedulingPolicy",
    ):
        assert retention.get(key) == backup.get(key), key


def test_the_service_makes_no_network_connection() -> None:
    assert _service()["RestrictAddressFamilies"] == ["AF_UNIX"]


def test_the_service_writes_only_the_database_capture_and_backup_lock_paths() -> None:
    """Task 14.5A: the backup directory is writable so the run can HOLD the
    backup's O_EXCL lock for its duration; nothing else was added."""
    assert _service()["ReadWritePaths"] == [
        "@DATABASE_DIR@ @CAPTURE_DIR@ @BACKUP_DIR@"
    ]
    for forbidden in ("@CONFIG_PATH@", "@APP_ROOT@", "/etc", "/usr", "/opt"):
        assert forbidden not in _service()["ReadWritePaths"][0]


def test_the_service_run_is_bounded() -> None:
    assert int(_service()["TimeoutStartSec"][0]) <= 900


def test_the_service_has_no_install_section() -> None:
    """Only the timer is ever enabled; the service cannot be enabled at boot."""
    assert "[Install]" not in _read(TEMPLATE)


def test_the_service_is_ordered_after_the_backup_without_depending_on_it() -> None:
    unit = _directives(_read(TEMPLATE), "[Unit]")
    assert "mgo-backup.service" in unit["After"][0]
    for key in ("Requires", "Wants", "BindsTo", "PartOf", "Conflicts"):
        assert key not in unit, key


def test_every_template_placeholder_is_substituted_by_the_installer() -> None:
    placeholders = set(re.findall(r"@[A-Z_]+@", _read(TEMPLATE)))
    installer = _read(INSTALLER)
    for placeholder in placeholders:
        assert f"s|{placeholder}|" in installer, placeholder


def test_the_service_points_at_the_canonical_paths_by_default() -> None:
    installer = _read(INSTALLER)
    assert f'config_path="{SYSTEM_CONFIG_PATH}"' in installer
    assert f'backup_dir="{SYSTEM_BACKUP_DIRECTORY}"' in installer
    assert f'service_user="{SERVICE_ACCOUNT}"' in installer
    assert str(SYSTEM_DATABASE_DIRECTORY).endswith("/db")
    assert str(SYSTEM_CAPTURE_DIRECTORY).endswith("/media/captures")


# --- the timer ----------------------------------------------------------------


def test_the_timer_runs_daily_after_the_backup_window() -> None:
    timer = _directives(_read(TIMER), "[Timer]")
    assert timer["OnCalendar"] == ["*-*-* 04:00:00"]
    assert timer["RandomizedDelaySec"] == ["15m"]
    assert timer["AccuracySec"] == ["1m"]
    assert timer["Persistent"] == ["true"]
    assert timer["Unit"] == ["mgo-retention.service"]
    backup = _directives(_read(BACKUP_TIMER), "[Timer]")
    assert backup["OnCalendar"] == ["*-*-* 02:30:00"]


def test_the_timer_is_enabled_at_boot_only_through_its_install_section() -> None:
    install = _directives(_read(TIMER), "[Install]")
    assert install["WantedBy"] == ["timers.target"]


def test_the_timer_has_no_placeholder() -> None:
    assert not re.findall(r"@[A-Z_]+@", _read(TIMER))


# --- the installer, statically ------------------------------------------------


def test_the_installer_touches_no_application_state() -> None:
    text = _read(INSTALLER)
    for forbidden in (
        "claude-approved-sha",
        "git ",
        "uv ",
        "mgo.db",
        "sqlite",
        "rm -rf /",
        "restart mgo",
        "deploy-main",
        "restart-api",
    ):
        assert forbidden not in text, forbidden
    # The configuration path is only ever substituted, never written.
    assert not re.search(r">\s*\"?\$\{config_path\}", text)


def test_the_installer_enables_only_on_explicit_request() -> None:
    text = _read(INSTALLER)
    marker = "if (( enable_timer )); then  # the only path that schedules anything"
    enable_block = text[text.index(marker) :]
    assert "systemctl enable" in enable_block
    before = text[: text.index(marker)]
    assert "systemctl enable" not in before
    assert "systemctl start" not in before
    assert 'systemctl start "${service_unit}"' not in text


def test_the_installer_seeds_the_stamp_before_enabling() -> None:
    text = _read(INSTALLER)
    assert text.index("timer_stamp}") < text.index('systemctl enable "${timer_unit}"')


def test_the_installer_reloads_only_when_something_changed() -> None:
    text = _read(INSTALLER)
    assert "if (( changed )) && (( ! developer_directory )); then" in text
    assert "systemctl daemon-reload" in text


def test_the_installer_publishes_atomically_with_rollback() -> None:
    text = _read(INSTALLER)
    assert "mv -T --" in text
    assert 'mktemp -p "${unit_directory}"' in text
    assert "restore_one" in text
    assert "previous_service" in text


# --- the installer, executed --------------------------------------------------


needs_bash = pytest.mark.skipif(_find_bash() is None, reason="bash is not available")


@needs_bash
def test_a_dry_run_renders_and_writes_nothing(tmp_path: Path) -> None:
    units = tmp_path / "units"
    units.mkdir()

    result = _run_installer("--dry-run", "--unit-directory", _posix(units))

    assert result.returncode == 0, result.stdout + result.stderr
    assert "would publish" in result.stdout
    assert "would NOT enable" in result.stdout
    assert "every placeholder substituted" in result.stdout
    assert list(units.iterdir()) == []
    assert "@APP_ROOT@" not in result.stdout.split("--- rendered")[1]


@needs_bash
def test_an_install_publishes_the_pair_and_a_rerun_changes_nothing(
    tmp_path: Path,
) -> None:
    units = tmp_path / "units"
    units.mkdir()
    app_root = _app_root(tmp_path)
    arguments = ("--unit-directory", _posix(units), "--app-root", app_root)

    first = _run_installer(*arguments)

    assert first.returncode == 0, first.stdout + first.stderr
    service = units / "mgo-retention.service"
    timer = units / "mgo-retention.timer"
    assert service.is_file() and timer.is_file()
    rendered = service.read_text(encoding="utf-8")
    assert not re.findall(r"@[A-Z_]+@", rendered)
    assert f"ExecStart={app_root}/.venv/bin/mgo-retention" in rendered
    assert "--backup-directory /var/backups/garden-observatory" in rendered
    assert timer.read_bytes() == TIMER.read_bytes()
    assert "NOT enabled" in first.stdout
    assert sorted(p.name for p in units.iterdir()) == [
        "mgo-retention.service",
        "mgo-retention.timer",
    ]

    second = _run_installer(*arguments)

    assert second.returncode == 0, second.stdout + second.stderr
    assert "already up to date" in second.stdout
    assert "nothing published" in second.stdout
    assert service.read_text(encoding="utf-8") == rendered


@needs_bash
def test_a_changed_input_replaces_the_service_and_keeps_the_timer(
    tmp_path: Path,
) -> None:
    units = tmp_path / "units"
    units.mkdir()
    base = ("--unit-directory", _posix(units))
    one = _app_root(tmp_path, "one")
    two = _app_root(tmp_path, "two")

    assert _run_installer(*base, "--app-root", one).returncode == 0
    assert _run_installer(*base, "--app-root", two).returncode == 0

    rendered = (units / "mgo-retention.service").read_text(encoding="utf-8")
    assert f"{two}/.venv/bin/mgo-retention" in rendered
    assert f"{one}/" not in rendered
    assert sorted(p.name for p in units.iterdir()) == [
        "mgo-retention.service",
        "mgo-retention.timer",
    ]


@needs_bash
def test_a_failed_second_publication_restores_the_previous_pair(
    tmp_path: Path,
) -> None:
    units = tmp_path / "units"
    units.mkdir()
    base = ("--unit-directory", _posix(units))
    one = _app_root(tmp_path, "one")
    two = _app_root(tmp_path, "two")
    assert _run_installer(*base, "--app-root", one).returncode == 0
    previous_service = (units / "mgo-retention.service").read_bytes()
    previous_timer = (units / "mgo-retention.timer").read_bytes()

    result = _run_installer(*base, "--app-root", two, "--fail-after-first-publish")

    assert result.returncode == 70, result.stdout + result.stderr
    assert "rolling back" in result.stderr
    assert (units / "mgo-retention.service").read_bytes() == previous_service
    assert (units / "mgo-retention.timer").read_bytes() == previous_timer
    assert sorted(p.name for p in units.iterdir()) == [
        "mgo-retention.service",
        "mgo-retention.timer",
    ]


@needs_bash
def test_a_failed_first_ever_publication_leaves_the_directory_empty(
    tmp_path: Path,
) -> None:
    units = tmp_path / "units"
    units.mkdir()

    result = _run_installer(
        "--unit-directory",
        _posix(units),
        "--app-root",
        _app_root(tmp_path),
        "--fail-after-first-publish",
    )

    assert result.returncode == 70
    assert list(units.iterdir()) == []


@needs_bash
def test_the_validation_aids_are_refused_on_the_default_directory() -> None:
    seam = _run_installer("--fail-after-first-publish", "--dry-run")
    assert seam.returncode == 65
    assert "validation aid" in seam.stderr


@needs_bash
def test_enable_is_refused_with_a_developer_directory(tmp_path: Path) -> None:
    result = _run_installer("--enable", "--unit-directory", _posix(tmp_path))
    assert result.returncode == 65
    assert "--enable is only valid" in result.stderr


@needs_bash
def test_a_relative_path_is_refused(tmp_path: Path) -> None:
    result = _run_installer("--dry-run", "--unit-directory", "relative/units")
    assert result.returncode == 65
    assert "absolute" in result.stderr


@needs_bash
def test_an_unknown_option_is_a_usage_error() -> None:
    result = _run_installer("--bogus")
    assert result.returncode == 2


@needs_bash
def test_a_symlinked_existing_unit_is_refused(tmp_path: Path) -> None:
    units = tmp_path / "units"
    units.mkdir()
    target = tmp_path / "elsewhere.service"
    target.write_text("[Unit]\n")
    try:
        (units / "mgo-retention.service").symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available to this account")

    result = _run_installer(
        "--unit-directory", _posix(units), "--app-root", _app_root(tmp_path)
    )

    assert result.returncode == 65
    assert "symlink" in result.stderr
    assert target.read_text() == "[Unit]\n"


@needs_bash
def test_installed_units_carry_the_expected_mode(tmp_path: Path) -> None:
    units = tmp_path / "units"
    units.mkdir()
    assert (
        _run_installer(
            "--unit-directory", _posix(units), "--app-root", _app_root(tmp_path)
        ).returncode
        == 0
    )
    if os.name == "nt":
        pytest.skip("POSIX modes are not represented on this host")
    for name in ("mgo-retention.service", "mgo-retention.timer"):
        assert (units / name).stat().st_mode & 0o777 == 0o644
