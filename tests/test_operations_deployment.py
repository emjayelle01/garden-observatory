"""Tests for the Task 10 deployment assets.

These lock the *deployment contract* together the way
``tests/test_service_identity.py`` already does for the runtime identity: the
canonical constants in :mod:`mgo.core.config`, the backup service template, the
timer, the logrotate policy, the installer and the verifier must all describe
the same operations layout. Drift between them is a defect that would only
appear on the Raspberry Pi, so it is caught here instead.

Everything is static analysis of tracked text plus Git metadata. Nothing runs
``systemctl``, ``journalctl`` or ``logrotate``, nothing touches ``/etc`` or
``/var``, and nothing needs root.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path, PurePosixPath

import pytest

from mgo.core.config import (
    CONFIG_PATH_ENV,
    DEFAULT_CONFIG_PATH,
    PROJECT_ROOT,
    SERVICE_ACCOUNT,
    SERVICE_GROUP,
    SERVICE_UNIT_NAME,
    SYSTEM_BACKUP_DIRECTORY,
    SYSTEM_CONFIG_PATH,
    SYSTEM_DATABASE_DIRECTORY,
    SYSTEM_LOG_DIRECTORY,
    SYSTEM_STATE_DIRECTORY,
    resolve_config_path,
)
from mgo.operations.backup import DEFAULT_RETENTION_COUNT, MAX_RETENTION_COUNT
from mgo.operations.backup_cli import _build_parser
from mgo.operations.errors import OperationError

DEPLOY = PROJECT_ROOT / "scripts" / "deploy"
OPERATIONS = PROJECT_ROOT / "scripts" / "operations"
DOCUMENTATION = PROJECT_ROOT / "docs" / "Operations.md"

API_UNIT = DEPLOY / "mgo.service.template"
BACKUP_UNIT = DEPLOY / "mgo-backup.service.template"
BACKUP_TIMER = DEPLOY / "mgo-backup.timer"
LOGROTATE = DEPLOY / "garden-observatory.logrotate"
INSTALL_SCRIPT = DEPLOY / "install-service-identity.sh"
VERIFY_IDENTITY = DEPLOY / "verify-service-identity.sh"
VERIFY_OPERATIONS = DEPLOY / "verify-operations.sh"
BACKUP_WRAPPER = OPERATIONS / "backup-database.sh"
BUNDLE_WRAPPER = OPERATIONS / "create-support-bundle.sh"

TRACKED_SCRIPTS = (
    "scripts/deploy/install-service-identity.sh",
    "scripts/deploy/verify-service-identity.sh",
    "scripts/deploy/verify-operations.sh",
    "scripts/deploy/update-main.sh",
    "scripts/ssh/verify-key-auth.sh",
    "scripts/operations/backup-database.sh",
    "scripts/operations/create-support-bundle.sh",
)


def _read(path: Path) -> str:
    """Read a deployment asset as text."""
    return path.read_text(encoding="utf-8")


def _directives(unit_text: str, section: str = "[Service]") -> list[tuple[str, str]]:
    """Parse one unit section into ordered key/value pairs."""
    directives: list[tuple[str, str]] = []
    inside = False
    for raw_line in unit_text.splitlines():
        line = raw_line.strip()
        if line.startswith("[") and line.endswith("]"):
            inside = line == section
            continue
        if not inside or not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if separator:
            directives.append((key.strip(), value.strip()))
    return directives


def _values(unit_text: str, key: str, section: str = "[Service]") -> list[str]:
    """Return every value configured for ``key`` in ``section``."""
    return [value for name, value in _directives(unit_text, section) if name == key]


def _code(text: str) -> str:
    """Return only the executable/directive lines of a script or unit.

    Comment lines and here-document bodies are both stripped. Every one of
    these assets *documents* itself heavily -- the backup unit explains why it
    needs no camera access, the wrappers' ``--help`` output tells an operator to
    inspect a bundle with ``tar -tzf`` -- so matching against raw text would
    fail on the very sentences that promise the behaviour being asserted. What
    matters is what the file *does*.
    """
    lines: list[str] = []
    terminator: str | None = None

    for line in text.splitlines():
        if terminator is not None:
            if line.strip() == terminator:
                terminator = None
            continue
        if line.lstrip().startswith("#"):
            continue

        opening = re.search(r"<<-?'?([A-Za-z_][A-Za-z0-9_]*)'?\s*$", line)
        if opening is not None:
            terminator = opening.group(1)
            # Keep the opening line itself: it is real shell.
            lines.append(line)
            continue
        lines.append(line)

    return "\n".join(lines)


def _runs(body: str, command: str) -> bool:
    """Return whether ``body`` invokes ``command`` in a statement position.

    Word boundaries matter more than they look: a plain substring search for
    ``rm`` matches ``-perm``, and for ``tar`` matches ``.tar.gz``. A command is
    only a command at the start of a statement.
    """
    pattern = rf"(?:^|[|;&]\s*|\$\(\s*|\bexec\s+){re.escape(command)}\b"
    return re.search(pattern, body, re.MULTILINE) is not None


def _git() -> str:
    """Locate git, skipping the test when it is unavailable."""
    found = shutil.which("git")
    if found is None:  # pragma: no cover - git is present wherever this repo is
        pytest.skip("git is unavailable")
    return found


# --- the canonical backup constant ------------------------------------------


def test_the_backup_root_is_the_architecture_plan_location() -> None:
    """The plan names /var/backups/garden-observatory; nothing may drift."""
    assert SYSTEM_BACKUP_DIRECTORY.as_posix() == "/var/backups/garden-observatory"
    assert isinstance(SYSTEM_BACKUP_DIRECTORY, PurePosixPath)
    assert SYSTEM_BACKUP_DIRECTORY.is_absolute()


def test_the_backup_root_is_outside_the_tree_it_protects() -> None:
    """A backup inside the state tree is lost to the same accident."""
    assert not SYSTEM_BACKUP_DIRECTORY.is_relative_to(SYSTEM_STATE_DIRECTORY)
    assert not SYSTEM_STATE_DIRECTORY.is_relative_to(SYSTEM_BACKUP_DIRECTORY)


def test_the_backup_location_has_one_authority() -> None:
    """The literal must not be re-spelled across the tracked assets.

    Each file may state it once (a shell default, a unit placeholder default);
    what would be a defect is the path being *derived* differently in two
    places, so the assertion is that everything agrees with the constant.
    """
    literal = SYSTEM_BACKUP_DIRECTORY.as_posix()

    assert f'backup_dir="{literal}"' in _read(INSTALL_SCRIPT)
    assert f'backup_dir="{literal}"' in _read(VERIFY_OPERATIONS)
    # The unit template uses a placeholder rather than hard-coding the path.
    # (Its comment header may name the default; only the directives matter.)
    assert literal not in _code(_read(BACKUP_UNIT))
    assert "@BACKUP_DIR@" in _read(BACKUP_UNIT)


# --- the existing API service is unchanged ----------------------------------


def test_the_api_unit_is_byte_for_byte_unchanged_from_main() -> None:
    """Task 10 found no defect in the API unit, so it must not have edited it.

    This is the assertion behind the architecture decision recorded in
    ``docs/tasks/Task-010-Operations.md``: the existing restart policy and
    sandbox already satisfy OPS-01, and changing them purely so the task
    appears to have touched the service would be a regression risk taken for
    cosmetic reasons.
    """
    git = _git()
    completed = subprocess.run(
        [git, "diff", "--quiet", "main", "--", "scripts/deploy/mgo.service.template"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if completed.returncode not in (0, 1):  # pragma: no cover - no 'main' ref
        pytest.skip("the 'main' ref is unavailable for comparison")

    assert completed.returncode == 0, (
        "scripts/deploy/mgo.service.template differs from main; Task 10 "
        "records that no API unit change was required"
    )


def test_the_api_unit_keeps_its_restart_policy() -> None:
    """OPS-01's restart requirement, asserted directly rather than by trust."""
    unit = _read(API_UNIT)

    assert _values(unit, "Restart") == ["on-failure"]
    assert _values(unit, "RestartSec") == ["5"]
    assert _values(unit, "TimeoutStopSec") == ["20"]
    assert _values(unit, "WantedBy", "[Install]") == ["multi-user.target"]


def test_the_api_unit_keeps_its_identity_and_camera_access() -> None:
    """Nothing in Task 10 may cost the API its runtime identity or camera."""
    unit = _read(API_UNIT)

    assert _values(unit, "User") == ["@SERVICE_USER@"]
    assert _values(unit, "Group") == ["@SERVICE_GROUP@"]
    assert _values(unit, "SupplementaryGroups") == ["@CAMERA_GROUP@"]
    # PrivateDevices would cut the camera off; it must stay unset on the API.
    assert _values(unit, "PrivateDevices") == []
    assert "--port @PORT@" in unit


# --- no fictional worker ----------------------------------------------------


def test_no_worker_service_was_invented() -> None:
    """The recorded decision: a worker unit needs a real worker first."""
    units = sorted(path.name for path in DEPLOY.glob("*.service.template"))

    assert units == ["mgo-backup.service.template", "mgo.service.template"]


def test_no_unit_is_a_placeholder_that_merely_stays_alive() -> None:
    """An empty service, a sleeping process or a stub is worse than nothing."""
    for unit in (API_UNIT, BACKUP_UNIT):
        text = _read(unit)
        exec_lines = _values(text, "ExecStart")
        assert exec_lines, f"{unit.name} has no ExecStart"
        for line in exec_lines:
            assert "sleep" not in line, unit.name
            assert "/bin/true" not in line, unit.name
            assert line.strip() != "", unit.name


def test_the_backup_unit_runs_real_delivered_functionality() -> None:
    """The one operations unit added maps to code this task actually ships."""
    exec_start = _values(_read(BACKUP_UNIT), "ExecStart")[0]

    assert "mgo.operations.backup_cli" in exec_start
    assert (PROJECT_ROOT / "src" / "mgo" / "operations" / "backup_cli.py").is_file()


# --- backup service ---------------------------------------------------------


def test_the_backup_service_is_a_one_shot_job() -> None:
    """A scheduled backup must not be a long-running service."""
    unit = _read(BACKUP_UNIT)

    assert _values(unit, "Type") == ["oneshot"]
    assert _values(unit, "Restart") == ["no"]


def test_the_backup_service_runs_as_the_unprivileged_account() -> None:
    """Nothing in Task 10 runs the application as root."""
    unit = _read(BACKUP_UNIT)

    assert _values(unit, "User") == ["@SERVICE_USER@"]
    assert _values(unit, "Group") == ["@SERVICE_GROUP@"]
    assert "User=root" not in unit


def test_the_backup_service_needs_no_camera_access() -> None:
    """It reads a database; it has no business near the camera."""
    unit = _read(BACKUP_UNIT)

    assert _values(unit, "SupplementaryGroups") == []
    # Unlike the API, this unit CAN hide the device nodes.
    assert _values(unit, "PrivateDevices") == ["yes"]


def test_the_backup_service_uses_the_canonical_configuration() -> None:
    """One configuration authority, selected the same way the API selects it."""
    unit = _read(BACKUP_UNIT)

    assert "Environment=MGO_CONFIG_PATH=@CONFIG_PATH@" in unit
    assert "--config @CONFIG_PATH@" in _values(unit, "ExecStart")[0]


def test_the_backup_service_applies_the_security_sandbox() -> None:
    """Least privilege, matching the API unit's model."""
    unit = _read(BACKUP_UNIT)

    assert _values(unit, "CapabilityBoundingSet") == [""]
    assert _values(unit, "AmbientCapabilities") == [""]
    assert _values(unit, "NoNewPrivileges") == ["yes"]
    assert _values(unit, "RestrictSUIDSGID") == ["yes"]
    assert _values(unit, "ProtectSystem") == ["strict"]
    assert _values(unit, "ProtectHome") == ["yes"]
    assert _values(unit, "PrivateTmp") == ["yes"]
    assert _values(unit, "RestrictNamespaces") == ["yes"]
    assert _values(unit, "UMask") == ["0027"]


def test_the_backup_service_makes_no_network_connection() -> None:
    """A backup is local. Nothing here may reach the network."""
    assert _values(_read(BACKUP_UNIT), "RestrictAddressFamilies") == ["AF_UNIX"]


def test_the_backup_service_run_is_bounded() -> None:
    """A wedged job must not hold the lock until the stale timeout."""
    assert _values(_read(BACKUP_UNIT), "TimeoutStartSec") == ["900"]


def test_the_backup_service_uses_low_impact_scheduling() -> None:
    """The API is serving from the same SD card."""
    unit = _read(BACKUP_UNIT)

    assert _values(unit, "IOSchedulingClass") == ["idle"]
    assert _values(unit, "CPUSchedulingPolicy") == ["idle"]
    assert _values(unit, "Nice") == ["10"]


def test_the_backup_service_can_write_only_where_it_must() -> None:
    """ProtectSystem=strict plus exactly two named writable paths."""
    writable = _values(_read(BACKUP_UNIT), "ReadWritePaths")

    assert len(writable) == 1
    paths = writable[0].split()
    assert "@BACKUP_DIR@" in paths
    # Required for SQLite WAL shared memory, even for a read-only reader.
    assert "@DATABASE_DIR@" in paths
    assert len(paths) == 2


def test_the_backup_service_never_manages_the_api() -> None:
    """A backup must not be able to start, stop or restart the API."""
    body = _code(_read(BACKUP_UNIT))

    assert "systemctl" not in body
    assert "Requires=" not in body
    assert "BindsTo=" not in body
    assert "Wants=" not in body
    # After= is ordering only, and is permitted.
    assert f"After={SERVICE_UNIT_NAME}" in body


def test_the_backup_service_never_touches_media() -> None:
    """The media archive is not backed up and not read."""
    directives = _code(_read(BACKUP_UNIT))

    assert "media" not in directives
    assert "captures" not in directives


# --- backup timer -----------------------------------------------------------


def test_the_timer_runs_daily() -> None:
    """The plan requires a scheduled daily backup."""
    schedule = _values(_read(BACKUP_TIMER), "OnCalendar", "[Timer]")

    assert schedule == ["*-*-* 02:30:00"]


def test_the_timer_survives_downtime() -> None:
    """A Pi is not always powered; a missed backup must be caught up."""
    assert _values(_read(BACKUP_TIMER), "Persistent", "[Timer]") == ["true"]


def test_the_timer_spreads_its_start() -> None:
    """A rigid write spike at the same second every day is avoidable."""
    assert _values(_read(BACKUP_TIMER), "RandomizedDelaySec", "[Timer]") == ["30m"]


def test_the_timer_triggers_the_real_backup_service() -> None:
    """The timer must invoke the delivered job, not something else."""
    assert _values(_read(BACKUP_TIMER), "Unit", "[Timer]") == ["mgo-backup.service"]


def test_the_timer_is_enabled_at_boot() -> None:
    """Scheduling that does not survive a reboot is not scheduling."""
    assert _values(_read(BACKUP_TIMER), "WantedBy", "[Install]") == ["timers.target"]


def test_the_timer_has_no_placeholder_to_substitute() -> None:
    """It is installed verbatim, so an unsubstituted placeholder would ship."""
    assert not re.findall(r"@[A-Z_]+@", _read(BACKUP_TIMER))


def test_the_installer_seeds_the_timer_stamp_before_enabling() -> None:
    """Installing a schedule must never be the thing that starts a backup.

    With ``Persistent=true`` and no stamp file, enabling the timer after
    02:30 would fire an immediate catch-up run.
    """
    text = _read(INSTALL_SCRIPT)

    assert "timer_stamp=" in text
    assert 'if [[ ! -e "${timer_stamp}" ]]; then' in text
    assert text.index("timer_stamp}") < text.index(
        'systemctl enable "${backup_timer}"'
    )


def test_the_installer_does_not_run_a_backup() -> None:
    """Provisioning and taking a backup are separate operator actions."""
    text = _read(INSTALL_SCRIPT)

    assert "systemctl start \"${backup_unit}\"" not in text
    assert "backup_cli backup" not in text
    assert "--now" not in text


# --- logrotate policy -------------------------------------------------------


def _logrotate_targets(text: str) -> list[str]:
    """Return the rotation target globs (the lines outside the brace block)."""
    return [
        line.strip().rstrip("{").strip()
        for line in text.splitlines()
        if line.startswith("/")
    ]


def test_the_logrotate_target_is_confined_to_mgo_logs() -> None:
    """A broad glob would hand another service's logs to this policy."""
    targets = _logrotate_targets(_read(LOGROTATE))

    assert targets == [f"{SYSTEM_LOG_DIRECTORY.as_posix()}/*.log"]


def test_the_logrotate_policy_touches_no_data_path() -> None:
    """Rotation must never be pointed at the database, media or backups."""
    for target in _logrotate_targets(_read(LOGROTATE)):
        assert SYSTEM_STATE_DIRECTORY.as_posix() not in target
        assert SYSTEM_DATABASE_DIRECTORY.as_posix() not in target
        assert SYSTEM_BACKUP_DIRECTORY.as_posix() not in target
        assert ".db" not in target
        assert "captures" not in target


def test_the_logrotate_retention_is_bounded() -> None:
    """Unbounded rotation would defeat the point of rotating."""
    match = re.search(r"^\s*rotate\s+(\d+)\s*$", _read(LOGROTATE), re.MULTILINE)

    assert match is not None
    assert int(match.group(1)) == 14


def test_the_logrotate_policy_rotates_daily() -> None:
    """The documented cadence."""
    assert re.search(r"^\s*daily\s*$", _read(LOGROTATE), re.MULTILINE)


def test_the_logrotate_policy_creates_secure_files() -> None:
    """A rotated log must never become world-readable."""
    text = _read(LOGROTATE)
    match = re.search(
        rf"^\s*create\s+(\d+)\s+{SERVICE_ACCOUNT}\s+{SERVICE_GROUP}\s*$",
        text,
        re.MULTILINE,
    )

    assert match is not None
    mode = int(match.group(1), 8)
    assert mode == 0o640
    assert not mode & 0o007


def test_the_logrotate_policy_rotates_as_the_runtime_account() -> None:
    """The log directory is owned by mgo:mgo and is not world-readable."""
    assert re.search(
        rf"^\s*su\s+{SERVICE_ACCOUNT}\s+{SERVICE_GROUP}\s*$",
        _read(LOGROTATE),
        re.MULTILINE,
    )


def test_the_logrotate_policy_is_safe_when_no_log_exists() -> None:
    """journald is primary, so today the glob matches nothing."""
    text = _read(LOGROTATE)

    assert re.search(r"^\s*missingok\s*$", text, re.MULTILINE)
    assert re.search(r"^\s*notifempty\s*$", text, re.MULTILINE)


def test_the_logrotate_policy_compresses_but_delays() -> None:
    """A writer holding the old descriptor must not lose its final lines."""
    text = _read(LOGROTATE)

    assert re.search(r"^\s*compress\s*$", text, re.MULTILINE)
    assert re.search(r"^\s*delaycompress\s*$", text, re.MULTILINE)


def test_copytruncate_is_not_used_without_a_documented_writer() -> None:
    """copytruncate trades a lost-lines race for a convenience nobody needs."""
    text = _read(LOGROTATE)
    directives = [
        line.strip()
        for line in text.splitlines()
        if not line.strip().startswith("#")
    ]

    assert "copytruncate" not in " ".join(directives)


def test_the_policy_states_that_it_does_not_rotate_the_journal() -> None:
    """Truthfulness: it would be easy to imply this rotates everything."""
    text = _read(LOGROTATE)

    assert "journal" in text.lower()
    assert "logrotate cannot rotate the journal" in text


# --- installer --------------------------------------------------------------


def test_the_installer_provisions_the_backup_directory() -> None:
    """Ownership and mode are asserted, not assumed."""
    text = _read(INSTALL_SCRIPT)

    assert '-m 0750 "${backup_dir}"' in text
    assert '-o "${service_user}" -g "${service_group}" -m 0750 "${backup_dir}"' in text


def test_the_installer_installs_the_timer_and_logrotate_policy() -> None:
    """Both operations assets are provisioned by the one installer."""
    text = _read(INSTALL_SCRIPT)

    assert "backup_timer_destination" in text
    assert "logrotate_destination" in text
    assert "/etc/logrotate.d/garden-observatory" in text


def test_the_installer_substitutes_every_backup_unit_placeholder() -> None:
    """An unsubstituted placeholder would produce an invalid unit."""
    unit = _read(BACKUP_UNIT)
    install = _read(INSTALL_SCRIPT)

    placeholders = set(re.findall(r"@[A-Z_]+@", unit))
    assert placeholders, "the template should contain placeholders"

    for placeholder in placeholders:
        assert f"s|{placeholder}|" in install, placeholder


def test_the_installer_avoids_replacing_an_identical_file() -> None:
    """Re-running provisioning must be quiet and must not churn files."""
    text = _read(INSTALL_SCRIPT)

    assert "cmp -s" in text
    assert "is already up to date" in text


def test_the_installer_backs_up_a_replaced_file() -> None:
    """A local edit must never be silently discarded."""
    text = _read(INSTALL_SCRIPT)

    assert "backed up the existing ${label}" in text
    assert ".bak-$(date" in text


def test_the_installer_validates_the_logrotate_policy_before_installing() -> None:
    """A syntax error in /etc/logrotate.d breaks rotation for the whole host."""
    text = _read(INSTALL_SCRIPT)

    assert "logrotate --debug" in text
    assert "refusing to install a logrotate policy that does not parse" in text
    # It validates the staged copy, never the checkout source. The behavioural
    # proof is below; this guards the shape against a careless revert.
    assert 'logrotate --debug "${staged}"' in text
    assert 'logrotate --debug "${logrotate_source}"' not in text


def test_the_installer_validates_the_retention_argument() -> None:
    """``--keep`` reaches a unit file; it must be a positive integer."""
    text = _read(INSTALL_SCRIPT)

    assert '[[ "${backup_keep}" =~ ^[1-9][0-9]*$ ]]' in text


def test_the_installer_default_retention_matches_the_application() -> None:
    """One documented retention policy, not two that agree by luck."""
    assert f'backup_keep="{DEFAULT_RETENTION_COUNT}"' in _read(INSTALL_SCRIPT)


def test_the_installer_supports_dry_run_for_the_new_work() -> None:
    """Every mutating step must be describable without doing it."""
    text = _read(INSTALL_SCRIPT)

    assert "--dry-run" in text
    assert "[dry-run] would write ${backup_unit_destination}" in text or (
        "[dry-run] would write %s" in text
    )
    assert "[dry-run] would install %s -> %s" in text
    assert (
        "would reload systemd, seed the timer stamp, enable" in text
        and "verify both states" in text
    )


def test_the_installer_never_restarts_the_api_for_operations_work() -> None:
    """Installing a timer or a rotation rule must not interrupt serving.

    The installer *tells* the operator to restart the service as a next step;
    what it must never do is restart it itself, so the assertion is on the
    commands it runs rather than the text it prints.
    """
    commands = [
        line.strip()
        for line in _code(_read(INSTALL_SCRIPT)).splitlines()
        if not line.strip().startswith(("note ", "printf ", "warn ", "fail "))
    ]
    body = "\n".join(commands)

    assert "systemctl restart" not in body
    assert 'systemctl start "${service_unit}"' not in body
    assert "systemctl stop" not in body
    # The only unit the installer starts is the timer.
    started = re.findall(r"systemctl start \"?\$\{(\w+)\}\"?", body)
    assert started == ["backup_timer"]


def test_the_installer_never_grants_world_access() -> None:
    """Extended for the new paths; the original rule still holds."""
    text = _read(INSTALL_SCRIPT)

    for forbidden in ("777", "o+w", "a+w", "0777", "chmod -R 777", "chmod 666"):
        assert forbidden not in text, forbidden


def test_the_installer_never_deletes_a_backup() -> None:
    """Provisioning must never be able to destroy recovery data."""
    text = _read(INSTALL_SCRIPT)

    assert 'rm -rf "${backup_dir}"' not in text
    assert "rm -rf ${backup_dir}" not in text
    assert 'rm -f "${backup_dir}' not in text


# --- verifier ---------------------------------------------------------------


def test_the_operations_verifier_is_read_only() -> None:
    """Verification must never mutate the deployment it inspects."""
    body = _code(_read(VERIFY_OPERATIONS))

    for mutating in (
        "useradd",
        "usermod",
        "groupadd",
        "chown",
        "chmod",
        "chgrp",
        "install",
        "rm",
        "mv",
        "cp",
        "touch",
        "tee",
        "truncate",
        "dd",
    ):
        assert not _runs(body, mutating), mutating

    for forbidden in (
        "systemctl restart",
        "systemctl start",
        "systemctl enable",
        "systemctl stop",
        "logrotate --force",
        "logrotate -f",
        "> /etc",
        "> /var",
    ):
        assert forbidden not in body, forbidden

    # ``systemctl show``/``is-enabled``/``is-active`` are the only systemctl
    # verbs permitted, and all three are read-only. Reporting lines are
    # excluded: "skip \"systemctl unavailable\"" is a message, not a command.
    commands = "\n".join(
        line
        for line in body.splitlines()
        if not line.strip().startswith(("pass ", "skip ", "fail ", "printf ", "note "))
    )
    for verb in re.findall(r"systemctl ([a-z][\w-]*)", commands):
        assert verb in {"show", "is-enabled", "is-active"}, verb


def test_the_verifier_does_not_create_a_backup() -> None:
    """Checking that backups are configured must not produce one."""
    text = _read(VERIFY_OPERATIONS)

    assert "backup-database.sh" not in text
    assert "backup_cli" not in text


def test_the_verifier_checks_the_required_properties() -> None:
    """The verification contract, asserted so a check cannot quietly vanish."""
    text = _read(VERIFY_OPERATIONS)

    for expected in (
        "backup_dir",
        "Type=oneshot",
        "NoNewPrivileges",
        "CapabilityBoundingSet",
        "ProtectSystem",
        "ReadWritePaths",
        "Persistent=true",
        "OnCalendar",
        "RandomizedDelaySec",
        "is-enabled",
        "is-active",
        "NextElapseUSecRealtime",
        "logrotate",
        "world-readable",
        "journalctl --disk-usage",
    ):
        assert expected in text, expected


def test_the_verifier_reports_a_failed_scheduled_backup() -> None:
    """A silently failing timer is the failure mode that matters most."""
    text = _read(VERIFY_OPERATIONS)

    assert 'if [[ "${backup_state}" == "failed" ]]' in text


def test_the_identity_verifier_is_still_read_only() -> None:
    """The pre-existing guarantee must survive Task 10."""
    text = _read(VERIFY_IDENTITY)

    for mutating in ("useradd", "usermod", "groupadd", "chown", "chmod", "chgrp"):
        assert mutating not in text, mutating


# --- shell hygiene ----------------------------------------------------------


@pytest.mark.parametrize(
    "script",
    [VERIFY_OPERATIONS, BACKUP_WRAPPER, BUNDLE_WRAPPER],
    ids=["verify-operations", "backup-wrapper", "bundle-wrapper"],
)
def test_new_scripts_are_bash_with_strict_mode(script: Path) -> None:
    """Fail fast rather than continuing past an error."""
    text = _read(script)

    assert text.startswith("#!/usr/bin/env bash")
    assert "set -euo pipefail" in text or "set -uo pipefail" in text


@pytest.mark.parametrize(
    "asset",
    [
        BACKUP_UNIT,
        BACKUP_TIMER,
        LOGROTATE,
        VERIFY_OPERATIONS,
        BACKUP_WRAPPER,
        BUNDLE_WRAPPER,
    ],
    ids=["unit", "timer", "logrotate", "verify", "backup-sh", "bundle-sh"],
)
def test_new_deployment_assets_use_lf_line_endings(asset: Path) -> None:
    """CRLF would break these under bash and systemd on the Raspberry Pi."""
    assert b"\r\n" not in asset.read_bytes()


def test_the_wrappers_contain_no_business_logic() -> None:
    """Logic belongs in Python where it is typed and unit-tested."""
    for wrapper in (BACKUP_WRAPPER, BUNDLE_WRAPPER):
        body = _code(_read(wrapper))

        for forbidden in ("sqlite3", "tar", "sha256sum", "find", "rm", "gzip", "jq"):
            assert not _runs(body, forbidden), f"{wrapper.name}: {forbidden}"
        assert "exec " in body, "the wrapper should hand off to Python"


def test_the_wrappers_preserve_the_python_exit_status() -> None:
    """systemd and the operator both depend on the exit code."""
    for wrapper in (BACKUP_WRAPPER, BUNDLE_WRAPPER):
        assert re.search(
            r'exec "\$\{python_bin\}" -m mgo\.operations\.\w+ "\$@"', _read(wrapper)
        )


def test_the_wrappers_use_the_checked_in_virtual_environment() -> None:
    """No package manager runs, and no system Python is assumed."""
    for wrapper in (BACKUP_WRAPPER, BUNDLE_WRAPPER):
        text = _read(wrapper)

        assert 'python_bin="${app_root}/.venv/bin/python"' in text
        assert "uv run" not in text
        assert "no Python interpreter at" in text


def test_the_wrappers_do_not_use_sudo_or_upload() -> None:
    """An operator tool must not escalate or send anything anywhere."""
    for wrapper in (BACKUP_WRAPPER, BUNDLE_WRAPPER):
        body = _code(_read(wrapper))

        for forbidden in ("sudo", "curl", "wget", "scp", "rsync", "nc", "ssh"):
            assert not _runs(body, forbidden), f"{wrapper.name}: {forbidden}"


def test_the_wrappers_support_help() -> None:
    """An operator on the Pi must be able to discover the commands."""
    for wrapper in (BACKUP_WRAPPER, BUNDLE_WRAPPER):
        text = _read(wrapper)

        assert "--help" in text
        assert "usage()" in text


def test_every_tracked_script_is_executable_in_git() -> None:
    """Mode 100644 breaks ``./script.sh``; Windows cannot show the bit."""
    git = _git()
    completed = subprocess.run(
        [git, "ls-files", "--stage", *TRACKED_SCRIPTS],
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
    assert len(modes) == len(TRACKED_SCRIPTS), modes

    for path, mode in modes.items():
        assert mode == "100755", f"{path} is {mode}, expected 100755"


# --- regression: target-specific virtual-environment validation --------------
#
# The installer originally failed a broken virtual environment only when the API
# unit was selected. Once Task 10 could install mgo-backup.service, "--no-unit"
# skipped mgo.service while still installing a backup service against an
# unusable interpreter. Validation is now per-target, and the tests below run
# the REAL bash logic rather than asserting on its text.

PYTHON_DETECTION_START = "# >>> python-detection >>>"
PYTHON_DETECTION_END = "# <<< python-detection <<<"

#: Fixture builders for the interpreter check. Each is bash populating "$root".
PYTHON_SCENARIOS: dict[str, str] = {
    "missing_venv": "",
    "missing_python": 'mkdir -p "$root/.venv/bin"',
    "python_not_executable": (
        # Not a shebang file: MSYS2 treats any "#!" file as executable
        # regardless of its mode, so a plain file is the only portable way to
        # exercise this branch.
        'mkdir -p "$root/.venv/bin"\n'
        'printf \'interpreter\\n\' > "$root/.venv/bin/python"\n'
        'chmod 644 "$root/.venv/bin/python"'
    ),
    "healthy": (
        'mkdir -p "$root/.venv/bin"\n'
        'printf \'#!/bin/sh\\n\' > "$root/.venv/bin/python"\n'
        'chmod 755 "$root/.venv/bin/python"'
    ),
}


def _find_bash() -> str | None:
    """Locate bash, including a Git Bash not on ``PATH``.

    Mirrors the helper in ``tests/test_service_identity.py``: on this Windows
    box bash ships with Git but is usually not on ``PATH``, and skipping would
    mean never running these tests on the platform they are written on.
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


def _python_detection_block() -> str:
    """Extract the self-contained interpreter check from the install script."""
    text = _read(INSTALL_SCRIPT)
    _, start, remainder = text.partition(PYTHON_DETECTION_START)
    block, end, _ = remainder.partition(PYTHON_DETECTION_END)

    assert start, f"{PYTHON_DETECTION_START} marker is missing"
    assert end, f"{PYTHON_DETECTION_END} marker is missing"
    return block


def _detect_python(scenario: str) -> str:
    """Run the real interpreter-detection logic and return its verdict."""
    program = "\n".join(
        (
            "set -u",
            'root="$(mktemp -d)"',
            PYTHON_SCENARIOS[scenario],
            'venv_dir="$root/.venv"',
            'venv_python="$root/.venv/bin/python"',
            _python_detection_block(),
            'printf \'%s\' "$python_problem"',
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


def test_the_backup_service_interpreter_is_validated() -> None:
    """The backup service runs python directly, so python is what is checked."""
    assert "no virtual environment at" in _detect_python("missing_venv")
    assert "no interpreter at" in _detect_python("missing_python")
    assert "is not executable" in _detect_python("python_not_executable")


def test_a_healthy_interpreter_is_accepted() -> None:
    """A usable interpreter must not be reported as a problem."""
    assert _detect_python("healthy") == ""


def test_the_installer_validates_the_executable_each_target_uses() -> None:
    """The API unit runs uvicorn; the backup unit runs python."""
    text = _read(INSTALL_SCRIPT)

    assert 'venv_launcher="${venv_dir}/bin/uvicorn"' in text
    assert 'venv_python="${venv_dir}/bin/python"' in text
    # The backup unit's ExecStart must use the interpreter that is validated.
    assert "/.venv/bin/python -m mgo.operations.backup_cli" in _read(BACKUP_UNIT)


@pytest.mark.parametrize(
    ("install_unit", "install_operations", "should_fail"),
    [
        (1, 1, True),   # default: both targets selected
        (0, 1, True),   # --no-unit: the backup service is still selected
        (1, 0, True),   # --no-operations: the API service is still selected
        (0, 0, False),  # neither: no systemd executable is being installed
    ],
    ids=["default", "no-unit", "no-operations", "no-unit-no-operations"],
)
def test_flag_combinations_decide_whether_a_broken_venv_is_fatal(
    install_unit: int, install_operations: int, should_fail: bool
) -> None:
    """The selection logic, executed rather than asserted on as text.

    ``--no-unit`` must still fail: it skips ``mgo.service`` but Task 10 would
    otherwise install ``mgo-backup.service`` against the same broken
    environment.
    """
    text = _read(INSTALL_SCRIPT)
    _, _, remainder = text.partition(PYTHON_DETECTION_END)
    selection, _, _ = remainder.partition('if [[ -n "${selected_problem}" ]]')
    assert "selected_problem=" in selection

    program = "\n".join(
        (
            "set -u",
            f"install_unit={install_unit}",
            f"install_operations={install_operations}",
            'venv_problem="uvicorn is broken"',
            'python_problem="python is broken"',
            'service_unit="mgo.service"',
            'backup_unit="mgo-backup.service"',
            'venv_launcher="/opt/app/.venv/bin/uvicorn"',
            'venv_python="/opt/app/.venv/bin/python"',
            selection,
            'printf \'%s|%s\' "${selected_problem}" "${selected_target}"',
        )
    )
    bash = _find_bash()
    if bash is None:  # pragma: no cover
        pytest.skip("bash is unavailable")

    completed = subprocess.run(
        [bash, "-c", program],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    problem, _, target = completed.stdout.partition("|")

    if should_fail:
        assert problem, "a selected target with a broken environment must fail"
        assert target
    else:
        assert problem == ""


def test_no_unit_no_longer_claims_nothing_will_use_the_environment() -> None:
    """The old message was untrue once the backup service existed."""
    text = _read(INSTALL_SCRIPT)

    assert "no systemd unit will be pointed at this virtual environment" not in text
    assert 'fail "refusing to install ${selected_target}' in text


# --- regression: installer retention validation ------------------------------


def test_the_installer_shares_the_runtime_retention_bound() -> None:
    """One documented maximum, not two that agree by luck."""
    text = _read(INSTALL_SCRIPT)

    assert f'backup_keep_max="{MAX_RETENTION_COUNT}"' in text
    assert "(( backup_keep <= backup_keep_max ))" in text


def _installer_accepts_keep(keep: str) -> bool:
    """Run the installer's real ``--keep`` guards against a candidate value."""
    text = _read(INSTALL_SCRIPT)
    bash = _find_bash()
    if bash is None:  # pragma: no cover
        pytest.skip("bash is unavailable")

    # The guards are lifted verbatim from the installer, and their presence is
    # asserted, so this test cannot drift away from the script it describes.
    assert '[[ "${backup_keep}" =~ ^[1-9][0-9]*$ ]]' in text
    assert "(( backup_keep <= backup_keep_max ))" in text

    program = "\n".join(
        (
            "set -u",
            f'backup_keep="{keep}"',
            f'backup_keep_max="{MAX_RETENTION_COUNT}"',
            '[[ "${backup_keep}" =~ ^[1-9][0-9]*$ ]] || exit 1',
            "(( backup_keep <= backup_keep_max )) || exit 1",
            "exit 0",
        )
    )
    completed = subprocess.run(
        [bash, "-c", program],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    return completed.returncode == 0


def _python_accepts_keep(keep: str) -> bool:
    """Run the runtime retention validation against the same candidate."""
    from mgo.operations.backup import _validated_keep

    try:
        _validated_keep(int(keep))
    except (ValueError, OperationError):
        return False
    return True


@pytest.mark.parametrize(
    "keep", ["0", "3651", "-1", "1.5", "abc", "", "999999"]
)
def test_the_installer_rejects_every_unusable_retention_value(keep: str) -> None:
    """Installing a unit that fails every scheduled run helps nobody."""
    assert not _installer_accepts_keep(keep)


@pytest.mark.parametrize("keep", ["1", "14", "3650"])
def test_the_installer_accepts_the_documented_retention_range(keep: str) -> None:
    """The boundary values named in the operations policy."""
    assert _installer_accepts_keep(keep)
    assert _python_accepts_keep(keep)


@pytest.mark.parametrize(
    "keep",
    ["0", "1", "14", "3650", "3651", "-1", "+5", "07", "1.5", "abc", "", "999999"],
)
def test_the_installer_never_accepts_what_the_runtime_would_reject(
    keep: str,
) -> None:
    """The invariant that actually matters, in the direction that matters.

    The installer writes a ``--keep`` into a unit file that the Python CLI then
    parses on every scheduled run, so the installer accepting something Python
    rejects would produce a service guaranteed to fail nightly. The reverse is
    harmless: the installer refuses ``+5`` and ``07`` where ``int()`` would
    coerce them, and being stricter than the runtime costs nothing.
    """
    if _installer_accepts_keep(keep):
        assert _python_accepts_keep(keep), (
            f"the installer accepts {keep!r} but the runtime rejects it"
        )


# --- regression: timer activation truthfulness -------------------------------
#
# Every timer step originally ended in "|| true" and the script then announced
# success unconditionally, so a timer that failed to enable was reported as
# enabled. A silently unscheduled backup is the worst failure this task has.


def test_no_timer_operation_is_allowed_to_fail_silently() -> None:
    """No ``|| true`` may guard a Task 10 timer command."""
    text = _read(INSTALL_SCRIPT)
    operations_section = text.partition("step \"Enabling the backup timer\"")[2]

    assert operations_section, "the timer section should exist"
    for line in operations_section.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if "systemctl" in stripped and "backup_timer" in stripped:
            assert "|| true" not in stripped, stripped


@pytest.mark.parametrize(
    "step",
    ["daemon-reload", "enable", "start", "verify-enabled", "verify-active"],
)
def test_every_timer_step_can_fail_the_installer(step: str) -> None:
    """Each step names itself in a failure message and exits non-zero."""
    text = _read(INSTALL_SCRIPT)

    assert f"step '{step}' failed" in text, step


def test_the_timer_state_is_verified_not_inferred() -> None:
    """enable/start can both succeed while the state is not what was asked."""
    text = _read(INSTALL_SCRIPT)

    assert 'systemctl is-enabled --quiet "${backup_timer}"' in text
    assert 'systemctl is-active --quiet "${backup_timer}"' in text
    # Verification must come after the actions it verifies.
    assert text.index('systemctl start "${backup_timer}"') < text.index(
        'systemctl is-active --quiet "${backup_timer}"'
    )


def test_success_is_only_claimed_after_every_check() -> None:
    """The old code printed "enabled and started" regardless of the outcome."""
    text = _read(INSTALL_SCRIPT)

    assert 'note "enabled and started ${backup_timer}' not in text
    claim = 'note "${backup_timer} is enabled and active'
    assert claim in text
    assert text.index('systemctl is-active --quiet "${backup_timer}"') < text.index(
        claim
    )


def test_a_missing_systemctl_is_a_failure_not_a_warning() -> None:
    """Files installed with no scheduler is not a successful installation."""
    text = _read(INSTALL_SCRIPT)

    assert (
        'fail "systemctl is unavailable, so ${backup_timer} cannot be enabled'
        in text
    )
    assert "NO BACKUP IS SCHEDULED" in text


def test_a_failed_activation_does_not_run_the_backup_or_touch_the_api() -> None:
    """A failure path must not escalate into starting or restarting anything.

    Assertions are on the commands the section *runs*, not on the operator
    guidance it prints: the closing summary legitimately tells the operator to
    restart the service themselves.
    """
    text = _read(INSTALL_SCRIPT)
    section = text.partition('step "Enabling the backup timer"')[2]
    commands = "\n".join(
        line
        for line in _code(section).splitlines()
        if not line.strip().startswith(("note ", "printf ", "warn ", "fail "))
    )

    assert 'systemctl start "${backup_unit}"' not in commands
    assert "systemctl restart" not in commands
    assert "backup_cli" not in commands


# --- regression: the operator wrappers select the production configuration ---
#
# mgo-backup.service always set MGO_CONFIG_PATH *and* passed --config, so the
# scheduled backup was correct. The manual wrappers supplied neither, and
# resolve_config_path() deliberately falls back to the tracked development
# configuration, so a documented bare command --
#
#   sudo -u mgo /opt/garden-observatory/scripts/operations/backup-database.sh backup
#
# -- backed up a development database path and snapshotted the repository's
# configuration. sudo clears the environment, so this was not a corner case: it
# was what every documented manual command did.
#
# These tests run the REAL wrappers under bash against a fake interpreter that
# reports the argument vector and environment it was handed. Nothing touches
# /etc, /var, /opt or the production virtual environment.

#: A stand-in for ``.venv/bin/python`` that prints what it was invoked with.
#: It writes to stdout rather than to a file so no Windows path has to be
#: translated into an MSYS path to be read back.
FAKE_INTERPRETER = """#!/bin/sh
for arg in "$@"; do
  printf 'ARG %s\\n' "$arg"
done
if [ -n "${{{env}+set}}" ]; then
  printf 'ENV [%s]\\n' "${env}"
else
  printf 'ENV-UNSET\\n'
fi
exit {exit_code}
"""

#: Exit status each wrapper uses for "the virtual environment is unusable".
MISSING_VENV_STATUS = {"backup-database.sh": 1, "create-support-bundle.sh": 2}

#: Python module each wrapper hands off to.
WRAPPER_MODULE = {
    "backup-database.sh": "mgo.operations.backup_cli",
    "create-support-bundle.sh": "mgo.operations.support_bundle_cli",
}

#: Smallest argument vector each wrapper accepts without printing its usage.
#: ``backup-database.sh`` exits 2 on no arguments, so "no arguments" is not a
#: neutral way to exercise the rest of the script.
WRAPPER_ARGUMENTS: dict[str, list[str]] = {
    "backup-database.sh": ["backup"],
    "create-support-bundle.sh": [],
}

WRAPPERS = (BACKUP_WRAPPER, BUNDLE_WRAPPER)
WRAPPER_IDS = ("backup", "bundle")

#: The canonical production configuration, as the wrappers spell it.
PRODUCTION_CONFIG = SYSTEM_CONFIG_PATH.as_posix()


def _sandbox(
    tmp_path: Path, wrapper: Path, *, exit_code: int = 0, interpreter: bool = True
) -> Path:
    """Copy one wrapper into a throwaway application root and return its path.

    The layout has to be real: the wrapper derives ``app_root`` from its own
    location, so the fake interpreter must sit at ``<root>/.venv/bin/python``
    exactly as the deployed one does.
    """
    root = tmp_path / "app"
    operations = root / "scripts" / "operations"
    operations.mkdir(parents=True)
    copied = operations / wrapper.name
    copied.write_bytes(wrapper.read_bytes())

    if interpreter:
        bin_directory = root / ".venv" / "bin"
        bin_directory.mkdir(parents=True)
        fake = bin_directory / "python"
        fake.write_text(
            FAKE_INTERPRETER.format(env=CONFIG_PATH_ENV, exit_code=exit_code),
            encoding="utf-8",
            newline="\n",
        )
        fake.chmod(0o755)

    return copied


def _run_wrapper(
    script: Path,
    args: Sequence[str] = (),
    *,
    config_env: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a sandboxed wrapper with ``MGO_CONFIG_PATH`` in a known state.

    ``config_env=None`` means genuinely unset -- the case the defect was in.
    """
    bash = _find_bash()
    if bash is None:  # pragma: no cover - bash is present on Windows and the Pi
        pytest.skip("bash is unavailable")

    environment = dict(os.environ)
    environment.pop(CONFIG_PATH_ENV, None)
    if config_env is not None:
        environment[CONFIG_PATH_ENV] = config_env

    return subprocess.run(
        [bash, str(script).replace("\\", "/"), *args],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        env=environment,
    )


def _observed(
    completed: subprocess.CompletedProcess[str],
) -> tuple[list[str], str | None]:
    """Return the argument vector and configuration value Python was handed."""
    assert completed.returncode == 0, completed.stderr

    lines = completed.stdout.splitlines()
    arguments = [line[len("ARG ") :] for line in lines if line.startswith("ARG ")]

    reports = [line for line in lines if line.startswith(("ENV [", "ENV-UNSET"))]
    assert len(reports) == 1, completed.stdout
    if reports[0] == "ENV-UNSET":
        return arguments, None
    return arguments, reports[0][len("ENV [") : -1]


def _tree_digest(root: Path) -> dict[str, str]:
    """Hash every file under ``root`` so a wrapper's writes cannot go unseen."""
    return {
        str(path.relative_to(root)).replace("\\", "/"): hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


@pytest.mark.parametrize("wrapper", WRAPPERS, ids=WRAPPER_IDS)
def test_an_unset_configuration_becomes_the_production_configuration(
    tmp_path: Path, wrapper: Path
) -> None:
    """The defect itself: a bare operator command must mean production."""
    script = _sandbox(tmp_path, wrapper)

    _, selected = _observed(_run_wrapper(script, WRAPPER_ARGUMENTS[wrapper.name]))

    assert selected == PRODUCTION_CONFIG
    assert selected == "/etc/garden-observatory/mgo.toml"


@pytest.mark.parametrize("wrapper", WRAPPERS, ids=WRAPPER_IDS)
def test_the_production_default_is_not_the_development_configuration(
    tmp_path: Path, wrapper: Path
) -> None:
    """Stated the other way round, because that is the failure being fixed."""
    script = _sandbox(tmp_path, wrapper)

    _, selected = _observed(_run_wrapper(script, WRAPPER_ARGUMENTS[wrapper.name]))

    # Leaving it unset is the defect, not an acceptable alternative: Python
    # would then fall through to the tracked development configuration.
    assert selected is not None
    assert Path(selected) != DEFAULT_CONFIG_PATH
    assert "config/mgo.toml" not in selected
    assert not Path(selected).is_relative_to(PROJECT_ROOT)


@pytest.mark.parametrize("wrapper", WRAPPERS, ids=WRAPPER_IDS)
def test_a_caller_supplied_configuration_is_preserved(
    tmp_path: Path, wrapper: Path
) -> None:
    """An operator who sets the variable meant it."""
    script = _sandbox(tmp_path, wrapper)

    _, selected = _observed(
        _run_wrapper(
            script,
            WRAPPER_ARGUMENTS[wrapper.name],
            config_env="/srv/other/mgo.toml",
        )
    )

    assert selected == "/srv/other/mgo.toml"


@pytest.mark.parametrize("wrapper", WRAPPERS, ids=WRAPPER_IDS)
@pytest.mark.parametrize(
    "value",
    ["", " ", "\t", "   \t "],
    ids=["empty", "space", "tab", "blank"],
)
def test_a_set_but_blank_configuration_is_not_repaired(
    tmp_path: Path,
    wrapper: Path,
    value: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``: "${VAR:=default}"`` would silently replace these; ``-v`` must not.

    An empty or whitespace-only ``MGO_CONFIG_PATH`` is an error the application
    reports, and it must keep reporting it. Substituting production for a value
    the operator deliberately set would hide a broken unit or a typo in a
    profile, and would do so by pointing the tooling at the live system.
    """
    script = _sandbox(tmp_path, wrapper)

    _, selected = _observed(
        _run_wrapper(script, WRAPPER_ARGUMENTS[wrapper.name], config_env=value)
    )

    assert selected == value
    # The value the wrapper preserved is one the application still rejects.
    monkeypatch.setenv(CONFIG_PATH_ENV, value)
    with pytest.raises(ValueError):
        resolve_config_path()


@pytest.mark.parametrize("wrapper", WRAPPERS, ids=WRAPPER_IDS)
def test_an_explicit_config_argument_is_forwarded_untouched(
    tmp_path: Path, wrapper: Path
) -> None:
    """``--config`` beats the environment, and shell must not parse it."""
    script = _sandbox(tmp_path, wrapper)

    arguments, selected = _observed(
        _run_wrapper(script, ["--config", "/srv/explicit.toml"])
    )

    assert arguments[-2:] == ["--config", "/srv/explicit.toml"]
    # The environment default is still supplied; Python's precedence decides.
    assert selected == PRODUCTION_CONFIG
    assert resolve_config_path(Path("/srv/explicit.toml")) == Path("/srv/explicit.toml")


@pytest.mark.parametrize("wrapper", WRAPPERS, ids=WRAPPER_IDS)
def test_every_argument_reaches_python_unchanged(
    tmp_path: Path, wrapper: Path
) -> None:
    """Including empty strings, spaces and dashes -- nothing is re-split."""
    script = _sandbox(tmp_path, wrapper)
    forwarded = [
        "backup",
        "--output-directory",
        "/tmp/a directory",
        "--keep",
        "3",
        "",
        "--database",
        "/var/lib/garden-observatory/db/mgo.db",
    ]

    arguments, _ = _observed(_run_wrapper(script, forwarded))

    assert arguments == ["-m", WRAPPER_MODULE[wrapper.name], *forwarded]


@pytest.mark.parametrize("wrapper", WRAPPERS, ids=WRAPPER_IDS)
@pytest.mark.parametrize("status", [0, 1, 2, 7])
def test_the_python_exit_status_is_still_preserved(
    tmp_path: Path, wrapper: Path, status: int
) -> None:
    """The bundle wrapper's 0/1/2 contract depends on this exactly."""
    script = _sandbox(tmp_path, wrapper, exit_code=status)

    completed = _run_wrapper(script, WRAPPER_ARGUMENTS[wrapper.name])

    assert completed.returncode == status


@pytest.mark.parametrize("wrapper", WRAPPERS, ids=WRAPPER_IDS)
def test_the_missing_interpreter_error_is_unchanged(
    tmp_path: Path, wrapper: Path
) -> None:
    """The default must not run before, or instead of, the existing check."""
    script = _sandbox(tmp_path, wrapper, interpreter=False)

    completed = _run_wrapper(script, WRAPPER_ARGUMENTS[wrapper.name])

    assert completed.returncode == MISSING_VENV_STATUS[wrapper.name]
    assert "no Python interpreter at" in completed.stderr
    assert "/.venv/bin/python" in completed.stderr
    assert "uv sync" in completed.stderr
    assert completed.stdout == ""


@pytest.mark.parametrize("wrapper", WRAPPERS, ids=WRAPPER_IDS)
def test_the_wrapper_writes_nothing(tmp_path: Path, wrapper: Path) -> None:
    """An operator wrapper that edits its own checkout would be a defect."""
    script = _sandbox(tmp_path, wrapper)
    root = script.parents[2]
    before = _tree_digest(root)

    _observed(_run_wrapper(script, WRAPPER_ARGUMENTS[wrapper.name]))

    assert _tree_digest(root) == before


@pytest.mark.parametrize("wrapper", WRAPPERS, ids=WRAPPER_IDS)
def test_the_help_text_names_the_file_that_will_be_used(wrapper: Path) -> None:
    """An operator must be able to discover which file they are about to use."""
    _, _, remainder = _read(wrapper).partition("<<'USAGE'")
    help_text, terminator, _ = remainder.partition("\nUSAGE")

    assert terminator, f"{wrapper.name} should print a usage heredoc"
    assert PRODUCTION_CONFIG in help_text
    assert CONFIG_PATH_ENV in help_text


@pytest.mark.parametrize("wrapper", WRAPPERS, ids=WRAPPER_IDS)
def test_the_default_distinguishes_unset_from_empty(wrapper: Path) -> None:
    """Asserted on the source too, so the intent cannot be lost in a rewrite."""
    body = _code(_read(wrapper))

    assert f"[[ ! -v {CONFIG_PATH_ENV} ]]" in body
    assert f'export {CONFIG_PATH_ENV}="{PRODUCTION_CONFIG}"' in body
    # The idiom that would have replaced a deliberately empty value.
    assert f'"${{{CONFIG_PATH_ENV}:=' not in body
    assert f"${{{CONFIG_PATH_ENV}:-" not in body


@pytest.mark.parametrize("wrapper", WRAPPERS, ids=WRAPPER_IDS)
def test_the_default_is_a_plain_assignment(wrapper: Path) -> None:
    """No escalation, no substitution, no command runs to produce the value."""
    body = _code(_read(wrapper))
    assignment = [
        line.strip()
        for line in body.splitlines()
        if line.strip().startswith(f"export {CONFIG_PATH_ENV}=")
    ]

    assert len(assignment) == 1
    assert assignment[0] == f'export {CONFIG_PATH_ENV}="{PRODUCTION_CONFIG}"'
    # The only variable a wrapper exports is the configuration path.
    assert re.findall(r"^\s*export\s+(\w+)=", body, re.MULTILINE) == [CONFIG_PATH_ENV]


@pytest.mark.parametrize("wrapper", WRAPPERS, ids=WRAPPER_IDS)
def test_the_wrappers_no_longer_claim_to_be_environment_neutral(
    wrapper: Path,
) -> None:
    """Truthfulness: they now make one execution-environment decision."""
    text = _read(wrapper)

    assert "contains NO business logic" not in text
    assert "Contains NO business logic" not in text
    assert "execution-environment decision" in text


# --- regression: the Python default is untouched -----------------------------


def test_direct_python_execution_still_uses_the_development_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fix belongs at the operator boundary, not in the library.

    Moving the production default into ``resolve_config_path`` would make every
    developer test run, and every ``uv run`` on this machine, reach for a path
    that does not exist here.
    """
    monkeypatch.delenv(CONFIG_PATH_ENV, raising=False)

    assert resolve_config_path() == DEFAULT_CONFIG_PATH
    assert resolve_config_path() != Path(PRODUCTION_CONFIG)
    assert DEFAULT_CONFIG_PATH.is_relative_to(PROJECT_ROOT)


def test_the_python_default_does_not_depend_on_the_operating_system() -> None:
    """A platform-conditional default would be untestable on one of them."""
    source = (PROJECT_ROOT / "src" / "mgo" / "core" / "config.py").read_text(
        encoding="utf-8"
    )
    resolver = source.partition("def resolve_config_path")[2].partition("\ndef ")[0]

    assert resolver, "resolve_config_path should exist"
    for forbidden in ("sys.platform", "os.name", "platform.system", "SYSTEM_CONFIG"):
        assert forbidden not in resolver, forbidden


# --- regression: the scheduled service stays independently explicit ----------


def test_the_scheduled_service_does_not_rely_on_the_wrapper_default() -> None:
    """The unit runs python directly; it must carry its own configuration."""
    unit = _read(BACKUP_UNIT)
    exec_start = _values(unit, "ExecStart")[0]

    assert "Environment=MGO_CONFIG_PATH=@CONFIG_PATH@" in unit
    assert "--config @CONFIG_PATH@" in exec_start
    assert "backup-database.sh" not in unit
    assert "scripts/operations" not in unit


def test_the_scheduled_service_template_is_unchanged_by_this_correction() -> None:
    """The unit was already correct, so the fix must not have edited it."""
    git = _git()
    completed = subprocess.run(
        [
            git,
            "diff",
            "--quiet",
            "52c6d31419b04a8ea8cdc486180acec857db2ee3",
            "--",
            "scripts/deploy/mgo-backup.service.template",
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if completed.returncode not in (0, 1):  # pragma: no cover - shallow clone
        pytest.skip("the recorded Task 10 commit is unavailable for comparison")

    assert completed.returncode == 0, (
        "scripts/deploy/mgo-backup.service.template changed; the scheduled unit "
        "was already explicit and this correction is confined to the wrappers"
    )


# --- regression: the documented bare commands are now safe -------------------


def _bash_commands(text: str) -> list[str]:
    """Return every command inside a ```bash fence, continuations joined.

    Joining matters: several documented commands are wrapped with a trailing
    backslash, and treating each physical line as its own command would let a
    ``--config`` on the second line go unseen.
    """
    commands: list[str] = []
    pending = ""
    inside = False

    for line in text.splitlines():
        if line.startswith("```"):
            inside = line.strip() == "```bash"
            pending = ""
            continue
        stripped = line.strip()
        if not inside or not stripped:
            continue
        if stripped.endswith("\\"):
            pending += stripped[:-1].strip() + " "
            continue
        commands.append((pending + stripped).strip())
        pending = ""

    return commands


def _documented_wrapper_commands() -> list[str]:
    """Documented commands that invoke an operator wrapper."""
    commands = _bash_commands(_read(DOCUMENTATION))
    return [
        command
        for command in commands
        if any(wrapper.name in command for wrapper in WRAPPERS)
    ]


def test_the_documentation_still_shows_bare_wrapper_commands() -> None:
    """The point of the fix is that these did not have to grow a --config.

    Requiring every documented command to repeat ``--config
    /etc/garden-observatory/mgo.toml`` would have papered over the operator
    boundary instead of fixing it, and the first command an operator typed from
    memory would still have been wrong.
    """
    bare = [
        command
        for command in _documented_wrapper_commands()
        if "--config" not in command
    ]

    assert bare, "the documented operator commands should not all need --config"
    assert any("backup-database.sh backup" in command for command in bare)
    assert any("create-support-bundle.sh" in command for command in bare)


def test_every_bare_documented_command_reaches_a_wrapper_that_defaults() -> None:
    """A bare example is only safe because the wrapper supplies the default."""
    for command in _documented_wrapper_commands():
        if "--config" in command:
            continue
        named = [wrapper for wrapper in WRAPPERS if wrapper.name in command]
        assert len(named) == 1, command
        body = _code(_read(named[0]))
        assert f"[[ ! -v {CONFIG_PATH_ENV} ]]" in body, command


def test_no_documented_operator_command_bypasses_the_wrapper() -> None:
    """``python -m mgo.operations.backup_cli`` would get no default at all."""
    for command in _bash_commands(_read(DOCUMENTATION)):
        for module in WRAPPER_MODULE.values():
            if module not in command:
                continue
            assert "--config" in command, command


def test_the_documentation_states_the_override_order() -> None:
    """An operator has to be able to predict which file will be used."""
    text = _read(DOCUMENTATION)

    assert "wrapper-supplied production" in text
    assert f"defaults `{CONFIG_PATH_ENV}`" in text or (
        f"default `{CONFIG_PATH_ENV}`" in text
    )
    assert "set-but-empty" in text or "set but empty" in text


# --- regression: the logrotate policy is validated as what it will become -----
#
# The installer used to run "logrotate --debug" against the tracked file inside
# the checkout. Modern logrotate refuses a configuration file that is group- or
# other-writable, and also one whose owner is neither root nor the invoking
# user -- and a checkout is DELIBERATELY owned by the administrative operator so
# the runtime account can only read it. The check was therefore unsatisfiable on
# a correctly provisioned host: it failed on the Raspberry Pi with "the file
# owner is wrong (should be root or user with uid 0)" while the policy content
# was perfectly valid.
#
# Making the checkout root-owned would contradict the service-identity model,
# and dropping the validation would remove the guard that stops a malformed
# policy breaking rotation for the WHOLE host. The boundary is a transient
# root-owned staged copy: validate the file as it will exist at the destination,
# and install those same bytes.
#
# These tests execute the real extracted shell logic against stubs rather than
# grepping for words, so a mutation that validates the staged copy and then
# installs the source again fails them.

LOGROTATE_STAGING_START = "# >>> logrotate-staging >>>"
LOGROTATE_STAGING_END = "# <<< logrotate-staging <<<"
MANAGED_FILE_START = "# >>> managed-file >>>"
MANAGED_FILE_END = "# <<< managed-file <<<"
RUN_HELPER_START = "# >>> run-helper >>>"
RUN_HELPER_END = "# <<< run-helper <<<"


def _marked_block(text: str, start: str, end: str) -> str:
    """Extract a self-contained, executable region of the install script."""
    _, opening, remainder = text.partition(start)
    block, closing, _ = remainder.partition(end)

    assert opening, f"{start} marker is missing"
    assert closing, f"{end} marker is missing"
    return block


def _logrotate_block() -> str:
    """The staging, validation and installation logic, verbatim."""
    return _marked_block(
        _read(INSTALL_SCRIPT), LOGROTATE_STAGING_START, LOGROTATE_STAGING_END
    )


def _managed_file_block() -> str:
    """The managed-file installer, verbatim."""
    return _marked_block(
        _read(INSTALL_SCRIPT), MANAGED_FILE_START, MANAGED_FILE_END
    )


def _run_helper_block() -> str:
    """The dry-run-aware command runner, verbatim.

    Extracted rather than reimplemented: ``install_managed_file`` backs up an
    existing destination through it, so a test that stubbed it could not prove
    the real backup failure path.
    """
    return _marked_block(_read(INSTALL_SCRIPT), RUN_HELPER_START, RUN_HELPER_END)


def _bash_path(path: Path) -> str:
    """Render a path the way MSYS bash accepts it on either platform."""
    return str(path).replace("\\", "/")


def _run_logrotate_staging(
    tmp_path: Path,
    *,
    policy_parses: bool = True,
    logrotate_available: bool = True,
    dry_run: int = 0,
    as_root: bool = True,
    install_fails: bool = False,
    real_helper: bool = False,
    destination_install_fails: bool = False,
) -> tuple[int, dict[str, list[str]], str]:
    """Execute the real staging block against recording stubs.

    Everything the block touches is stubbed at the shell level -- ``note``,
    ``warn``, ``fail``, ``install``, ``logrotate``, ``id`` and, where needed,
    ``command`` -- so the test observes exactly which paths were handed to which
    operation.

    ``real_helper`` swaps the ``install_managed_file`` stub for the real
    extracted implementation, so an end-to-end run exercises staging,
    validation, installation and cleanup as one piece. The ``install`` double
    then distinguishes the two call sites by destination: the staging copy
    succeeds, and the managed destination can be made to fail.
    """
    bash = _find_bash()
    if bash is None:  # pragma: no cover - bash is present on Windows and the Pi
        pytest.skip("bash is unavailable")

    root = _bash_path(tmp_path)
    source = f"{root}/checkout/garden-observatory.logrotate"
    destination = f"{root}/etc/logrotate.d/garden-observatory"
    record = f"{root}/record"

    uid = "0" if as_root else "1000"
    install_status = "1" if install_fails else "0"

    stubs = [
        "set -uo pipefail",
        f'mkdir -p "{root}/checkout" "{root}/etc/logrotate.d"',
        f'printf "/var/log/x/*.log {{\\n  daily\\n  rotate 14\\n}}\\n" > "{source}"',
        f'chmod 0644 "{source}"',
        f'logrotate_source="{source}"',
        f'logrotate_destination="{destination}"',
        f"dry_run={dry_run}",
        f'record="{record}"',
        ': > "$record"',
        # --- helper stubs, each recording what it was given -----------------
        'note() { printf "NOTE %s\\n" "$*" >> "$record"; }',
        'warn() { printf "WARN %s\\n" "$*" >> "$record"; }',
        'fail() { printf "FAIL %s\\n" "$*" >> "$record"; exit 1; }',
        # id -u decides whether the root ownership flags are requested.
        f'id() {{ if [ "${{1:-}}" = "-u" ]; then printf "{uid}"; '
        'else builtin command id "$@"; fi; }',
    ]

    if real_helper:
        # The real helper, so a failure inside it has to propagate for real.
        stubs.extend([_run_helper_block(), _managed_file_block()])
    else:
        stubs.append(
            "install_managed_file() { "
            'printf "MANAGED_SOURCE %s\\n" "$1" >> "$record"; '
            'printf "MANAGED_DEST %s\\n" "$2" >> "$record"; '
            'printf "MANAGED_MODE %s\\n" "$3" >> "$record"; '
            'printf "MANAGED_DISPLAY %s\\n" "${5:-$1}" >> "$record"; '
            f"return {install_status}; }}"
        )

    # Record the exact argument vector, then really create the file so the rest
    # of the block behaves as it would in production. The staging copy and the
    # managed destination are told apart by their target.
    destination_branch = (
        f'if [ "${{@: -1}}" = "{destination}" ]; then '
        'printf "DESTINATION_INSTALL_ATTEMPTED\\n" >> "$record"; return 1; fi; '
        if destination_install_fails
        else ""
    )
    stubs.append(
        "install() { "
        'printf "INSTALL_ARGS %s\\n" "$*" >> "$record"; '
        'printf "INSTALL_TARGET %s\\n" "${@: -1}" >> "$record"; '
        f"{destination_branch}"
        f'if [ "${{@: -1}}" != "{destination}" ]; then '
        'printf "STAGED_PATH %s\\n" "${@: -1}" >> "$record"; fi; '
        'builtin command install -m 0644 "${@: -2:1}" "${@: -1}"; }'
    )

    if logrotate_available:
        stubs.append(
            "logrotate() { "
            'printf "LOGROTATE_ARG %s\\n" "$2" >> "$record"; '
            + (
                'printf "parsed\\n"; return 0; }'
                if policy_parses
                else 'printf "error: simulated syntax error\\n"; return 1; }'
            )
        )
    else:
        # Deterministic on a host that does have logrotate installed.
        stubs.append(
            'command() { if [ "${1:-}" = "-v" ] && [ "${2:-}" = "logrotate" ]; '
            'then return 1; fi; builtin command "$@"; }'
        )

    program = "\n".join(
        [
            *stubs,
            # The block defines the function and calls it. Wrapping it lets the
            # harness observe the status and then inspect the filesystem, which
            # a bare top-level "exit 1" would prevent.
            "run_block() {",
            _logrotate_block(),
            "}",
            "run_block",
            'printf "STATUS %s\\n" "$?" >> "$record"',
            'staged="$(sed -n "s/^STAGED_PATH //p" "$record" | head -1)"',
            'if [ -n "$staged" ]; then',
            '  if [ -e "$staged" ]; then remains=yes; else remains=no; fi',
            '  printf "STAGED_REMAINS %s\\n" "$remains" >> "$record"',
            '  if [ -d "$(dirname "$staged")" ]; then dir=yes; else dir=no; fi',
            '  printf "STAGING_DIR_REMAINS %s\\n" "$dir" >> "$record"',
            "fi",
        ]
    )

    completed = subprocess.run(
        [bash, "-c", program],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    entries: dict[str, list[str]] = {}
    record_file = tmp_path / "record"
    if record_file.is_file():
        for line in record_file.read_text(encoding="utf-8").splitlines():
            key, _, value = line.partition(" ")
            entries.setdefault(key, []).append(value)

    status = int(entries.get("STATUS", ["-1"])[0])
    return status, entries, completed.stderr


def test_logrotate_is_given_a_staged_copy_not_the_checkout_source(
    tmp_path: Path,
) -> None:
    """The defect itself: the checkout can never satisfy logrotate's owner check."""
    status, entries, _ = _run_logrotate_staging(tmp_path)

    assert status == 0
    validated = entries["LOGROTATE_ARG"][0]
    source = entries["MANAGED_DISPLAY"][0]

    assert validated != source
    assert "/checkout/" not in validated
    assert validated.startswith("/tmp/mgo-logrotate-")


def test_the_staged_policy_carries_the_destination_metadata(
    tmp_path: Path,
) -> None:
    """root:root 0644 -- exactly what /etc/logrotate.d will hold."""
    _, entries, _ = _run_logrotate_staging(tmp_path, as_root=True)
    arguments = entries["INSTALL_ARGS"][0].split()

    assert "-m" in arguments
    assert arguments[arguments.index("-m") + 1] == "0644"
    assert "-o" in arguments
    assert arguments[arguments.index("-o") + 1] == "root"
    assert "-g" in arguments
    assert arguments[arguments.index("-g") + 1] == "root"


def test_the_staging_directory_is_private_and_unpredictable(
    tmp_path: Path,
) -> None:
    """A fixed name in /tmp is a symlink attack waiting for a slow day."""
    block = _logrotate_block()

    assert "mktemp -d /tmp/mgo-logrotate-XXXXXXXX" in block
    assert "chmod 0700" in block
    # Never a fixed filename, and never inside the repository.
    assert "/tmp/garden-observatory.logrotate" not in block
    assert "${app_root}" not in block
    assert "${script_dir}" not in block

    _, entries, _ = _run_logrotate_staging(tmp_path)
    first = entries["STAGED_PATH"][0]
    _, second_entries, _ = _run_logrotate_staging(tmp_path / "again")

    assert first != second_entries["STAGED_PATH"][0], "the path must vary"


def test_the_validated_bytes_are_the_installed_bytes(tmp_path: Path) -> None:
    """A mutation that re-installs the source instead must fail here."""
    status, entries, _ = _run_logrotate_staging(tmp_path)

    assert status == 0
    assert entries["LOGROTATE_ARG"][0] == entries["MANAGED_SOURCE"][0]
    assert entries["MANAGED_SOURCE"][0] != entries["MANAGED_DISPLAY"][0]


def test_a_valid_policy_is_validated_then_installed(tmp_path: Path) -> None:
    """The whole success path, end to end."""
    status, entries, _ = _run_logrotate_staging(tmp_path)

    assert status == 0
    assert any("parses cleanly" in note for note in entries["NOTE"])
    assert entries["MANAGED_DEST"][0].endswith("/etc/logrotate.d/garden-observatory")
    assert entries["MANAGED_MODE"] == ["0644"]
    assert "WARN" not in entries
    assert "FAIL" not in entries


def test_a_malformed_policy_fails_closed(tmp_path: Path) -> None:
    """Nothing reaches the destination when logrotate refuses the policy."""
    status, entries, stderr = _run_logrotate_staging(tmp_path, policy_parses=False)

    assert status == 1
    assert "MANAGED_SOURCE" not in entries, "installation must not be attempted"
    assert any("could not parse" in warning for warning in entries["WARN"])
    assert any("does not parse" in failure for failure in entries["FAIL"])
    # The operator is shown why, not merely that.
    assert "simulated syntax error" in stderr


def test_staging_is_removed_after_successful_validation(tmp_path: Path) -> None:
    """Success must not litter /tmp with copies of the policy."""
    _, entries, _ = _run_logrotate_staging(tmp_path)

    assert entries["STAGED_REMAINS"] == ["no"]
    assert entries["STAGING_DIR_REMAINS"] == ["no"]


def test_staging_is_removed_after_failed_validation(tmp_path: Path) -> None:
    """The refusal path exits through the same trap."""
    _, entries, _ = _run_logrotate_staging(tmp_path, policy_parses=False)

    assert entries["STAGED_REMAINS"] == ["no"]
    assert entries["STAGING_DIR_REMAINS"] == ["no"]


def test_staging_is_removed_when_installation_fails(tmp_path: Path) -> None:
    """An unexpected failure after validation still cleans up, and fails."""
    status, entries, _ = _run_logrotate_staging(tmp_path, install_fails=True)

    assert status == 1
    assert entries["STAGING_DIR_REMAINS"] == ["no"]


def test_a_dry_run_validates_but_leaves_nothing_behind(tmp_path: Path) -> None:
    """--dry-run may stage privately; it may not leave a trace."""
    status, entries, _ = _run_logrotate_staging(tmp_path, dry_run=1, as_root=False)

    assert status == 0
    # Validation genuinely happened.
    assert entries["LOGROTATE_ARG"][0] == entries["MANAGED_SOURCE"][0]
    assert entries["STAGED_REMAINS"] == ["no"]
    assert entries["STAGING_DIR_REMAINS"] == ["no"]
    # A non-root dry run cannot chown, and must not pretend to.
    assert "-o root" not in entries["INSTALL_ARGS"][0]
    assert "-m 0644" in entries["INSTALL_ARGS"][0]


def test_a_dry_run_reports_the_tracked_policy_not_the_staging_path(
    tmp_path: Path,
) -> None:
    """An operator must see the file they would edit, not a temporary name."""
    _, entries, _ = _run_logrotate_staging(tmp_path, dry_run=1, as_root=False)
    display = entries["MANAGED_DISPLAY"][0]

    assert display.endswith("/checkout/garden-observatory.logrotate")
    assert not display.startswith("/tmp/mgo-logrotate-")


def test_a_missing_logrotate_still_installs_the_staged_policy(
    tmp_path: Path,
) -> None:
    """The documented behaviour, and one code path decides the bytes."""
    status, entries, _ = _run_logrotate_staging(
        tmp_path, logrotate_available=False
    )

    assert status == 0
    assert any("skipping policy validation" in note for note in entries["NOTE"])
    assert "LOGROTATE_ARG" not in entries
    # Still the staged copy, not the source.
    assert entries["MANAGED_SOURCE"][0].startswith("/tmp/mgo-logrotate-")
    assert entries["STAGING_DIR_REMAINS"] == ["no"]


def test_the_whole_staging_flow_fails_when_the_destination_install_fails(
    tmp_path: Path,
) -> None:
    """Staging, validation, real helper and cleanup, exercised as one piece.

    This is the end-to-end version of the finding: everything up to the
    destination write succeeds, the write fails, and the run must still fail.
    A stubbed helper could not prove this.
    """
    status, entries, _ = _run_logrotate_staging(
        tmp_path, real_helper=True, destination_install_fails=True
    )

    # Staging succeeded and validation ran against the staged copy.
    staged = entries["STAGED_PATH"][0]
    assert staged.startswith("/tmp/mgo-logrotate-")
    assert entries["LOGROTATE_ARG"] == [staged]
    assert any("parses cleanly" in note for note in entries["NOTE"])

    # The destination write was attempted, and failed.
    assert "DESTINATION_INSTALL_ATTEMPTED" in entries
    assert entries["INSTALL_TARGET"][-1].endswith(
        "/etc/logrotate.d/garden-observatory"
    )

    # The failure reached the caller rather than being announced as success.
    assert status == 1
    assert not any("installed" in note for note in entries["NOTE"])
    assert any("could not install the logrotate policy" in f for f in entries["FAIL"])

    # And the staging directory still went away.
    assert entries["STAGING_DIR_REMAINS"] == ["no"]
    assert entries["STAGED_REMAINS"] == ["no"]


def test_the_whole_staging_flow_succeeds_through_the_real_helper(
    tmp_path: Path,
) -> None:
    """The same wiring with nothing failing, so the failure test means something."""
    status, entries, _ = _run_logrotate_staging(tmp_path, real_helper=True)

    assert status == 0
    assert "DESTINATION_INSTALL_ATTEMPTED" not in entries
    assert any("installed" in note for note in entries["NOTE"])
    # The staged copy is what reached the destination.
    assert entries["INSTALL_ARGS"][-1].endswith(
        f"{entries['STAGED_PATH'][0]} {entries['INSTALL_TARGET'][-1]}"
    )
    assert entries["STAGING_DIR_REMAINS"] == ["no"]


def test_every_staging_step_is_explicitly_checked(tmp_path: Path) -> None:
    """Fail-closed: no required operation may be allowed to fail quietly."""
    block = _logrotate_block()

    for required in (
        "mktemp -d",
        "chmod 0700",
        'install "${stage_flags[@]}"',
        "install_managed_file",
    ):
        assert required in block, required

    for line in block.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        assert "|| true" not in stripped, stripped

    # Each mutating step names its own failure.
    assert block.count("|| fail") >= 4


# --- regression: managed-file installation behaviour -------------------------


def _run_managed_file(
    tmp_path: Path,
    *,
    existing: str | None,
    dry_run: int = 0,
    display: str | None = None,
) -> tuple[int, str]:
    """Execute the real install_managed_file against a temporary destination."""
    bash = _find_bash()
    if bash is None:  # pragma: no cover
        pytest.skip("bash is unavailable")

    root = _bash_path(tmp_path)
    source = f"{root}/source.conf"
    destination = f"{root}/dest/garden-observatory"

    setup = [
        "set -uo pipefail",
        f'mkdir -p "{root}/dest"',
        f'printf "policy-v2\\n" > "{source}"',
        f"dry_run={dry_run}",
        'note() { printf "NOTE %s\\n" "$*"; }',
        'run() { if (( dry_run )); then printf "  [dry-run] %s\\n" "$*"; '
        'else "$@"; fi; }',
        # The real coreutils install, minus the ownership flags an unprivileged
        # test user cannot set. The last three arguments are always mode,
        # source, destination. Ownership is asserted separately, statically.
        'install() { builtin command install -m "${@: -3:1}" "${@: -2:1}" '
        '"${@: -1}"; }',
    ]
    if existing is not None:
        setup.append(f'printf "{existing}\\n" > "{destination}"')

    call = f'install_managed_file "{source}" "{destination}" 0644 "logrotate policy"'
    if display is not None:
        call += f' "{display}"'

    program = "\n".join([*setup, _managed_file_block(), call])
    completed = subprocess.run(
        [bash, "-c", program],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    return completed.returncode, completed.stdout


def test_an_identical_destination_is_left_alone(tmp_path: Path) -> None:
    """Re-running provisioning must be quiet and must not churn files."""
    status, output = _run_managed_file(tmp_path, existing="policy-v2")

    assert status == 0
    assert "already up to date" in output
    assert "backed up" not in output


def test_a_different_destination_is_backed_up_before_replacement(
    tmp_path: Path,
) -> None:
    """A local edit must never be silently discarded."""
    status, output = _run_managed_file(tmp_path, existing="locally-edited")

    assert status == 0
    assert "backed up the existing logrotate policy" in output
    backups = list((tmp_path / "dest").glob("garden-observatory.bak-*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8").strip() == "locally-edited"
    assert (tmp_path / "dest" / "garden-observatory").read_text(
        encoding="utf-8"
    ).strip() == "policy-v2"


def test_a_new_destination_is_installed(tmp_path: Path) -> None:
    """The first install writes the policy and says so."""
    status, output = _run_managed_file(tmp_path, existing=None)

    assert status == 0
    assert "(root:root 0644)" in output
    assert (tmp_path / "dest" / "garden-observatory").is_file()


def test_the_destination_metadata_is_always_root_root_0644() -> None:
    """The one place the installed ownership and mode are decided."""
    block = _managed_file_block()

    assert 'install -o root -g root -m "${mode}" "${source}" "${destination}"' in (
        block
    )
    assert 'note "installed ${destination} (root:root ${mode})"' in block
    # Every caller for Task 10 assets asks for 0644.
    text = _read(INSTALL_SCRIPT)
    assert text.count("0644 \"logrotate policy\"") == 1
    assert "0644 \"backup timer\"" in text


def test_the_reported_path_defaults_to_the_installed_path(
    tmp_path: Path,
) -> None:
    """The display argument is optional; omitting it must change nothing."""
    _, without = _run_managed_file(tmp_path, existing=None, dry_run=1)
    _, with_display = _run_managed_file(
        tmp_path / "b", existing=None, dry_run=1, display="/tracked/policy"
    )

    assert "source.conf ->" in without
    assert "/tracked/policy ->" in with_display


# --- regression: a failure inside the helper must reach the caller -----------
#
# The staged-policy caller is written as
#
#     install_managed_file ... || fail "could not install the logrotate policy."
#
# and bash suppresses errexit for the WHOLE BODY of a function called on the
# left of "||". So a failing "install" inside the helper did not stop it: control
# fell through to the "installed ..." note, the note succeeded, the function
# returned zero, and the "|| fail" never ran. The installer would have announced
# a policy it had not written.
#
# The previous coverage could not have caught this. It replaced
# install_managed_file with a stub that returned the requested status, which
# proves the CALLER reacts to a non-zero return but says nothing about whether a
# failure inside the REAL helper produces one. These tests run the real helper.


def _run_real_managed_file(
    tmp_path: Path,
    *,
    install_fails: bool = False,
    backup_fails: bool = False,
    existing: str | None = None,
    dry_run: int = 0,
) -> tuple[int, str, str]:
    """Run the real helper in the exact ``helper || fail`` context."""
    bash = _find_bash()
    if bash is None:  # pragma: no cover
        pytest.skip("bash is unavailable")

    root = _bash_path(tmp_path)
    source = f"{root}/source.conf"
    destination = f"{root}/dest/garden-observatory"

    lines = [
        "set -euo pipefail",
        f'mkdir -p "{root}/dest"',
        f'printf "policy-v2\\n" > "{source}"',
        f"dry_run={dry_run}",
        'note() { printf "NOTE %s\\n" "$*"; }',
        'fail() { printf "FAIL %s\\n" "$*"; exit 1; }',
    ]
    if existing is not None:
        lines.append(f'printf "{existing}\\n" > "{destination}"')

    if install_fails:
        lines.append('install() { printf "INSTALL-ATTEMPTED\\n"; return 1; }')
    else:
        lines.append(
            'install() { builtin command install -m "${@: -3:1}" '
            '"${@: -2:1}" "${@: -1}"; }'
        )

    if backup_fails:
        lines.append('cp() { printf "BACKUP-ATTEMPTED\\n"; return 1; }')

    lines.extend(
        [
            _run_helper_block(),
            _managed_file_block(),
            f'install_managed_file "{source}" "{destination}" 0644 '
            '"logrotate policy" \\',
            '  || fail "could not install the logrotate policy."',
            'printf "REACHED-END\\n"',
        ]
    )

    completed = subprocess.run(
        [bash, "-c", "\n".join(lines)],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    return completed.returncode, completed.stdout, completed.stderr


def test_a_failed_destination_install_reaches_the_caller(
    tmp_path: Path,
) -> None:
    """The defect itself, against the real helper in the real call shape."""
    status, output, _ = _run_real_managed_file(tmp_path, install_fails=True)

    assert status == 1
    assert "INSTALL-ATTEMPTED" in output
    assert "NOTE installed" not in output, "success was announced after a failure"
    assert "FAIL could not install the logrotate policy." in output
    assert "REACHED-END" not in output
    assert not (tmp_path / "dest" / "garden-observatory").exists()


def test_a_failed_destination_backup_reaches_the_caller(
    tmp_path: Path,
) -> None:
    """A lost backup is a lost local edit, so it must stop the replacement."""
    status, output, _ = _run_real_managed_file(
        tmp_path, backup_fails=True, existing="locally-edited"
    )

    assert status == 1
    assert "BACKUP-ATTEMPTED" in output
    assert "NOTE backed up" not in output
    assert "NOTE installed" not in output, "the destination must not be replaced"
    assert "FAIL could not install the logrotate policy." in output
    assert "REACHED-END" not in output
    # The operator's edit is still there, untouched.
    assert (tmp_path / "dest" / "garden-observatory").read_text(
        encoding="utf-8"
    ).strip() == "locally-edited"


def test_the_helper_still_succeeds_when_nothing_fails(tmp_path: Path) -> None:
    """The corrections must not have made the success path fail closed too."""
    status, output, _ = _run_real_managed_file(tmp_path)

    assert status == 0
    assert "NOTE installed" in output
    assert "REACHED-END" in output
    assert (tmp_path / "dest" / "garden-observatory").is_file()


def test_a_dry_run_through_the_real_helper_still_succeeds(
    tmp_path: Path,
) -> None:
    """--dry-run writes nothing, so it cannot fail on a write."""
    status, output, _ = _run_real_managed_file(tmp_path, dry_run=1)

    assert status == 0
    assert "would install" in output
    assert "REACHED-END" in output
    assert not (tmp_path / "dest" / "garden-observatory").exists()


def test_errexit_does_not_protect_a_helper_called_in_an_or_list() -> None:
    """The bash behaviour behind the finding, pinned so it cannot be forgotten.

    This documents *why* the explicit ``|| return 1`` lines exist. It is
    supplementary: the production proof is the real-helper tests above.
    """
    bash = _find_bash()
    if bash is None:  # pragma: no cover
        pytest.skip("bash is unavailable")

    program = "\n".join(
        (
            "set -euo pipefail",
            "helper() { false; printf 'CONTINUED\\n'; }",
            "helper || printf 'HANDLER\\n'",
            "printf 'END\\n'",
        )
    )
    completed = subprocess.run(
        [bash, "-c", program],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    # errexit did not stop the function, and the handler never ran.
    assert completed.returncode == 0
    assert "CONTINUED" in completed.stdout
    assert "HANDLER" not in completed.stdout
    assert "END" in completed.stdout


def test_every_mutating_step_in_the_helper_returns_on_failure() -> None:
    """Structural guard: the explicit returns are what make the above work."""
    block = _managed_file_block()

    assert 'run cp -a "${destination}" "${backup}" \\\n      || return 1' in block
    assert (
        'install -o root -g root -m "${mode}" "${source}" "${destination}" \\\n'
        "      || return 1" in block
    )
    # Each success note comes only after its guarded command.
    assert block.index("|| return 1") < block.index('note "backed up')
    assert block.rindex("|| return 1") < block.index('note "installed')


# --- regression: pre-merge validation removes what it installed --------------
#
# The Pi validation procedure installed mgo-backup.service, installed AND
# ENABLED mgo-backup.timer, installed the logrotate policy, proved the timer
# survives a reboot -- and then told the operator to "git checkout main".
#
# Installed units live in /etc and are OUTSIDE GIT. A checkout reverts the code
# they point at, not the units themselves, so that sequence ended with an
# enabled timer whose ExecStart names mgo.operations.backup_cli -- a module that
# does not exist on pre-Task-10 main. The next 02:30 run would have failed, on a
# Pi deliberately returned to a known-good state.
#
# These tests assert the ORDER of the documented procedure, not just that the
# right words appear somewhere in it: cleanup before checkout is the whole
# finding, and a correctly-worded cleanup placed after the checkout would be
# exactly as broken as no cleanup at all.

PI_PROCEDURE_HEADING = "## 13. Raspberry Pi validation"
ROLLBACK_HEADING = "## 14. Rollback"
TROUBLESHOOTING_HEADING = "## 15. Troubleshooting"

CLEANUP_HEADING = "### 13.14 Pre-merge operational cleanup"
SERVICE_STATE_HEADING = "### 13.15 Record the feature-branch runtime"
RESTART_HEADING = "### 13.17 Restart the API onto `main`"
PREVIEW_HEADING = "### 13.18 Restore preview to its original state"
FINAL_CHECK_HEADING = "### 13.19 Final API check"

RESTART_API = "sudo systemctl restart mgo.service"
ENDPOINT_SWEEP = "/notifications/status /captures /observations /dashboard"
DASHBOARD_CHECK = "http://mgo-core:8080/dashboard"
PREVIEW_START = "curl -fsS -X POST http://127.0.0.1:8080/camera/preview/start"

#: The checkout as a *command* -- alone on its line inside a fence. Matching the
#: bare string would also hit the prose "must happen before `git checkout main`",
#: which appears earlier and would make every ordering assertion pass wrongly.
CHECKOUT_MAIN = "\ngit checkout main\n"
DISABLE_TIMER = "sudo systemctl disable --now mgo-backup.timer"
DAEMON_RELOAD = "sudo systemctl daemon-reload"
REMOVE_UNITS = (
    "sudo rm -f /etc/systemd/system/mgo-backup.timer "
    "/etc/systemd/system/mgo-backup.service"
)
REMOVE_LOGROTATE = "sudo rm -f /etc/logrotate.d/garden-observatory"
ORPHAN_CHECK = "systemctl list-timers --all | grep -F mgo-backup"

#: Paths a documented ``rm`` must never name. Four of the five are production
#: data; ``mgo.service`` is the API unit, which Task 10 never installed and
#: therefore must never remove.
PROTECTED_PATHS = (
    "/var/backups/garden-observatory",
    "/var/lib/garden-observatory",
    "/var/log/garden-observatory",
    "/etc/garden-observatory",
)

#: The one deletion under a protected path the procedure legitimately performs:
#: the synthetic log file §13.11 creates itself to exercise rotation. It is
#: allowed by exact spelling, so a widened glob would still be rejected.
PERMITTED_DELETIONS = ("/var/log/garden-observatory/task10-validation.log*",)


def _deletes_protected_data(command: str) -> str | None:
    """Return the protected path a documented ``rm`` would destroy, if any."""
    if not re.search(r"(?:^|\s|\|)(?:sudo\s+)?rm\b", command):
        return None

    remainder = command
    for permitted in PERMITTED_DELETIONS:
        remainder = remainder.replace(permitted, "")

    for path in PROTECTED_PATHS:
        if path in remainder:
            return path
    return None


def _span(text: str, start: str, end: str) -> str:
    """Return the documentation between two headings, exclusive of both."""
    _, opening, remainder = text.partition(start)
    assert opening, f"{start} is missing"
    body, closing, _ = remainder.partition(end)
    assert closing, f"{end} is missing"
    return body


def _procedure() -> str:
    """The Raspberry Pi validation procedure."""
    return _span(_read(DOCUMENTATION), PI_PROCEDURE_HEADING, ROLLBACK_HEADING)


def _rollback() -> str:
    """The rollback contract."""
    return _span(_read(DOCUMENTATION), ROLLBACK_HEADING, TROUBLESHOOTING_HEADING)


def _cleanup() -> str:
    """The mandatory pre-merge cleanup step."""
    return _span(_procedure(), CLEANUP_HEADING, SERVICE_STATE_HEADING)


def test_the_timer_is_disabled_before_the_checkout_to_main() -> None:
    """The ordering IS the finding: cleanup afterwards would be too late."""
    procedure = _procedure()

    assert DISABLE_TIMER in procedure
    assert CHECKOUT_MAIN in procedure
    assert procedure.index(DISABLE_TIMER) < procedure.index(CHECKOUT_MAIN)


@pytest.mark.parametrize(
    "removal",
    [REMOVE_UNITS, REMOVE_LOGROTATE],
    ids=["units", "logrotate"],
)
def test_installed_artefacts_are_removed_before_the_checkout_to_main(
    removal: str,
) -> None:
    """A unit file in /etc does not disappear when the checkout changes."""
    procedure = _procedure()

    assert removal in procedure, removal
    assert procedure.index(removal) < procedure.index(CHECKOUT_MAIN)


def test_the_daemon_reload_follows_the_unit_removal() -> None:
    """Reloading before the files are gone would reload them back in."""
    cleanup = _cleanup()

    assert DAEMON_RELOAD in cleanup
    assert cleanup.index(REMOVE_UNITS) < cleanup.index(DAEMON_RELOAD)
    assert cleanup.index(REMOVE_LOGROTATE) < cleanup.index(DAEMON_RELOAD)
    assert "reset-failed mgo-backup.service mgo-backup.timer" in cleanup


def test_the_removed_state_is_verified_not_assumed() -> None:
    """``rm -f`` succeeds whether or not it removed anything."""
    cleanup = _cleanup()

    for path in (
        "/etc/systemd/system/mgo-backup.timer",
        "/etc/systemd/system/mgo-backup.service",
        "/etc/logrotate.d/garden-observatory",
    ):
        assert f"test ! -e {path}" in cleanup, path

    assert "systemctl is-enabled mgo-backup.timer" in cleanup
    assert "systemctl is-active mgo-backup.timer" in cleanup


def test_the_reboot_test_still_precedes_the_cleanup() -> None:
    """Persistence must be proven before the thing proving it is removed."""
    procedure = _procedure()

    assert "sudo reboot" in procedure
    assert procedure.index("sudo reboot") < procedure.index(CLEANUP_HEADING)


def test_the_cleanup_deletes_no_recovery_data() -> None:
    """Removing the scheduled tooling is not deleting the backups."""
    for command in _bash_commands(_cleanup()):
        assert _deletes_protected_data(command) is None, command


def test_no_documented_command_anywhere_deletes_production_data() -> None:
    """The same rule, applied to the whole operations document."""
    for command in _bash_commands(_read(DOCUMENTATION)):
        assert _deletes_protected_data(command) is None, command


def test_the_one_permitted_deletion_is_the_artefact_it_created() -> None:
    """The synthetic rotation log, named exactly -- not a glob over the logs.

    Allowing it by exact spelling is what keeps the rule above meaningful: a
    later widening to ``*.log`` or to the directory itself would be rejected
    rather than quietly inheriting the exemption.
    """
    procedure = _procedure()
    artefact = PERMITTED_DELETIONS[0]

    assert f"sudo rm -f {artefact}" in procedure
    # The procedure must have created the file it removes.
    assert artefact.rstrip("*") in procedure.partition(f"sudo rm -f {artefact}")[0]
    assert _deletes_protected_data(f"sudo rm -f {artefact}") is None
    assert _deletes_protected_data("sudo rm -f /var/log/garden-observatory/*.log")


@pytest.mark.parametrize(
    "dangerous",
    [
        "rm -rf /var/backups/garden-observatory",
        "rm -f /var/backups/garden-observatory/*",
        "rm -rf /var/lib/garden-observatory",
        "rm -f /var/backups/garden-observatory/mgo-*",
    ],
)
def test_the_dangerous_cleanup_commands_are_absent(dangerous: str) -> None:
    """Named explicitly, because these are the ones that would look plausible."""
    assert dangerous not in _read(DOCUMENTATION)


def test_the_cleanup_names_what_it_must_not_touch() -> None:
    """The protected list is stated, not left to the operator's judgement."""
    cleanup = _cleanup()

    for path in (*PROTECTED_PATHS, "mgo.service"):
        assert path in cleanup, path
    assert "not the same as deleting recovery data" in cleanup


def test_the_cleanup_never_touches_the_api_service() -> None:
    """mgo.service was never installed by Task 10, so it is never removed."""
    for command in _bash_commands(_cleanup()):
        assert "mgo.service" not in command, command


def test_the_validated_recovery_set_is_kept_deliberately() -> None:
    """Evidence that the backup path worked on real hardware."""
    cleanup = _cleanup()

    assert "validated recovery set" in cleanup
    assert "/var/backups/garden-observatory" in cleanup


def test_the_pre_existing_artefact_check_precedes_installation() -> None:
    """Validation may remove only what it proved was absent and installed."""
    procedure = _procedure()

    assert "### 13.1a Record the pre-existing operational state" in procedure
    assert procedure.index("### 13.1a") < procedure.index("### 13.4 Install")

    for path in (
        "/etc/systemd/system/mgo-backup.timer",
        "/etc/systemd/system/mgo-backup.service",
        "/etc/logrotate.d/garden-observatory",
    ):
        assert f"test -e {path}" in procedure, path

    assert "do **not** delete it" in procedure


def test_the_procedure_explains_that_units_are_outside_git() -> None:
    """The category error behind the defect, named where it is corrected."""
    procedure = _procedure()

    assert "outside Git" in procedure
    assert "mgo.operations" in procedure


def test_the_orphaned_timer_is_checked_after_returning_to_main() -> None:
    """The one check that would have caught the original defect."""
    procedure = _procedure()

    assert ORPHAN_CHECK in procedure
    assert procedure.index(CHECKOUT_MAIN) < procedure.index(ORPHAN_CHECK)


def test_the_api_is_re_verified_after_returning_to_main() -> None:
    """Cleanup must be proven not to have disturbed the service."""
    procedure = _procedure()
    sweep = procedure.rindex("/notifications/status")

    assert procedure.index(CHECKOUT_MAIN) < sweep
    assert "systemctl is-active mgo.service" in procedure
    assert "/camera/preview/status" in procedure


def test_the_procedure_keeps_every_existing_validation_step() -> None:
    """The correction adds steps; it must not have removed any."""
    procedure = _procedure()

    for heading in (
        "### 13.1 Start from a clean tree",
        "### 13.2 Record the \"before\" state",
        "### 13.3 Confirm the existing service is unaffected",
        "### 13.3a Record the configuration state",
        "### 13.4 Install",
        "### 13.5 Check what was provisioned",
        "### 13.5a Prove the manual wrapper selects the production configuration",
        "### 13.6 Take a backup while the API is serving",
        "### 13.6a Source-identity checks",
        "### 13.7 Confirm production data is untouched",
        "### 13.8 Scheduled run",
        "### 13.9 Backup failure does not affect the API",
        "### 13.10 Support bundle",
        "### 13.11 Journal and rotation",
        "### 13.12 Restart and boot persistence",
    ):
        assert heading in procedure, heading

    for command in ("uv run ruff check .", "uv run mypy src", "uv run pytest"):
        assert command in procedure, command


# --- regression: the runtime is restored, not just the checkout --------------
#
# Finding 17 was "installed state is not Git state". This is the same error one
# level deeper: RUNTIME state is not Git state either.
#
# Validation deliberately restarts and reboots the API while the feature branch
# is checked out, so the serving process has feature-branch modules in memory.
# Python reads a module once, at import, and keeps it for the life of the
# interpreter -- `git checkout main` replaces the files and changes nothing
# about the running process. The procedure ended with a clean `main` checkout
# serving code that was no longer anywhere in the checkout, and nothing about
# that state looks wrong from the outside.
#
# As with §13.14, ORDER is the finding. A restart that happens before the
# checkout -- which validation already performs, twice -- satisfies every
# "is the service restarted?" assertion while leaving the defect in place.


def test_the_checkout_to_main_precedes_the_api_restart() -> None:
    """A restart before the checkout is what the defect already did.

    §13.12 legitimately restarts the API *during* validation, on the feature
    branch, so "the document contains a restart" proves nothing. What has to be
    true is that a restart appears **after** the checkout.
    """
    after_checkout = _procedure().partition(CHECKOUT_MAIN)[2]

    assert after_checkout, "the checkout should not be the last step"
    assert RESTART_API in after_checkout


def test_the_restart_follows_the_operational_cleanup() -> None:
    """Units first, then checkout, then runtime -- in that order."""
    procedure = _procedure()

    assert procedure.index(CLEANUP_HEADING) < procedure.index(CHECKOUT_MAIN)
    assert procedure.index(CHECKOUT_MAIN) < procedure.index(RESTART_HEADING)


def _after_main_restart() -> str:
    """Everything documented after the post-checkout restart.

    Anchored to the checkout first: §13.12 restarts the API during validation,
    so ``index(RESTART_API)`` alone would find the feature-branch restart and
    every ordering assertion below would pass for the wrong reason.
    """
    after_checkout = _procedure().partition(CHECKOUT_MAIN)[2]
    _, restart, remainder = after_checkout.partition(RESTART_API)

    assert restart, "no restart is documented after the checkout"
    return remainder


def test_the_final_endpoint_sweep_follows_the_restart() -> None:
    """Sweeping before the restart would be describing the wrong process."""
    assert ENDPOINT_SWEEP in _after_main_restart()


def test_the_dashboard_confirmation_follows_the_restart() -> None:
    """The browser check must exercise the main-branch process."""
    assert DASHBOARD_CHECK in _after_main_restart()


def test_preview_restoration_follows_the_restart() -> None:
    """The restart stops preview, so restoring it earlier would be undone."""
    after_restart = _after_main_restart()

    assert PREVIEW_HEADING in after_restart
    assert PREVIEW_START in after_restart
    # And the sweep reports the restored state, not the state during the restart.
    assert after_restart.index(PREVIEW_HEADING) < after_restart.index(
        FINAL_CHECK_HEADING
    )


def test_preview_is_restored_to_its_original_recorded_state() -> None:
    """§13.2's recording, not whatever preview was doing during validation."""
    preview = _span(_procedure(), PREVIEW_HEADING, FINAL_CHECK_HEADING)

    assert "§13.2" in preview
    assert "was running" in preview
    assert "originally **stopped**, leave it stopped" in preview
    assert "Do **not** capture an image" in preview


def test_the_feature_branch_runtime_is_recorded_before_the_checkout() -> None:
    """Without a recorded "before", "it was replaced" is not provable."""
    procedure = _procedure()

    assert "FEATURE_RUNTIME_PID=" in procedure
    assert "FEATURE_RUNTIME_STARTED=" in procedure
    assert procedure.index("FEATURE_RUNTIME_PID=") < procedure.index(CHECKOUT_MAIN)
    assert procedure.index("FEATURE_RUNTIME_STARTED=") < procedure.index(
        CHECKOUT_MAIN
    )


def test_the_main_runtime_is_recorded_after_the_restart() -> None:
    """MainPID and ActiveEnterTimestamp, taken from the replacement process."""
    restart = _span(_procedure(), RESTART_HEADING, PREVIEW_HEADING)

    assert "MAIN_RUNTIME_PID=" in restart
    assert "MAIN_RUNTIME_STARTED=" in restart
    assert "--property=MainPID" in restart
    assert "--property=ActiveEnterTimestamp" in restart
    assert restart.index(RESTART_API) < restart.index("MAIN_RUNTIME_PID=")


def test_the_timestamp_is_the_authoritative_evidence_not_the_pid() -> None:
    """An operating system may reuse a PID; it cannot reuse a start time."""
    restart = _span(_procedure(), RESTART_HEADING, PREVIEW_HEADING)

    assert "reuse a PID" in restart
    assert "authoritative evidence" in restart
    # A reload would leave the same interpreter holding the same modules.
    assert "never `systemctl reload`" in restart
    assert "systemctl reload mgo.service" not in _read(DOCUMENTATION)


def test_the_restarted_process_is_traced_to_the_checkout() -> None:
    """/proc is what ties the live process to the directory on disk."""
    restart = _span(_procedure(), RESTART_HEADING, PREVIEW_HEADING)

    assert '/proc/${MAIN_RUNTIME_PID}/cmdline' in restart
    assert '/proc/${MAIN_RUNTIME_PID}/cwd' in restart
    assert "/opt/garden-observatory/.venv/bin/uvicorn" in restart
    assert "mgo.api.app:app" in restart


def test_the_fresh_import_check_is_documented_as_a_supplement() -> None:
    """It shows what the checkout would import, not what is already loaded."""
    restart = _span(_procedure(), RESTART_HEADING, PREVIEW_HEADING)

    assert "import mgo.api.app, mgo.core.config" in restart
    assert "/opt/garden-observatory/src/mgo/" in restart
    assert "supplements" in restart


def test_the_procedure_explains_why_a_checkout_does_not_reload_modules() -> None:
    """The reasoning has to be in the document, not only in the commit."""
    procedure = _procedure()

    assert "does not reload modules" in procedure
    assert "not** a complete rollback" in procedure


def test_the_restart_journal_is_reviewed() -> None:
    """A restart that fails silently would leave no API at all."""
    procedure = _procedure()

    assert 'journalctl -u mgo.service --since "10 minutes ago" --no-pager' in (
        _after_main_restart()
    )
    assert "mgo.operations" in procedure


def test_the_required_final_state_names_runtime_as_well_as_git() -> None:
    """A clean checkout on its own was exactly the insufficient evidence."""
    final_state = _span(
        _procedure(), "### 13.21 Required final state", "### 13.22 What happens next"
    )

    assert "not** sufficient" in final_state
    for requirement in (
        "Git checkout:              main",
        "mgo.service:               active",
        "mgo.service process:       started after checkout to main",
        "Preview:                   restored to original state",
        "mgo-backup.timer:          absent",
        "mgo-backup.service:        absent",
        "Task 10 logrotate policy:  absent",
        "Validated recovery set:    preserved",
    ):
        assert requirement in final_state, requirement


def test_the_merge_sequence_states_the_runtime_matches_main() -> None:
    """The reason the restart exists, carried into what happens next."""
    _, heading, sequence = _procedure().partition("### 13.22 What happens next")

    assert heading
    assert "restarted after that checkout" in sequence
    assert "corresponds to `main`" in sequence
    assert "**Only then** may a pull request be created." in sequence
    assert "disagreeing with the running process" in sequence


def test_the_restart_reinstalls_no_task_10_artefact() -> None:
    """Restoring the runtime must not undo the cleanup that preceded it."""
    restoration = _procedure().partition(CLEANUP_HEADING)[2].partition(
        CHECKOUT_MAIN
    )[2]

    assert restoration, "there should be steps after the checkout"
    for command in _bash_commands(restoration):
        assert "install-service-identity.sh" not in command, command
        assert "systemctl enable" not in command, command
        assert "mgo-backup" not in command or command.startswith(
            "systemctl list-timers"
        ), command


# --- regression: the rollback contract has three states ----------------------


@pytest.mark.parametrize(
    "heading",
    [
        "### 14.1 Before any Pi installation validation",
        "### 14.2 After pre-merge Pi installation validation, before merge",
        "### 14.3 After Task 10 is merged and deployed",
    ],
    ids=["before-install", "pre-merge-post-install", "post-merge"],
)
def test_the_rollback_distinguishes_three_states(heading: str) -> None:
    """One sentence covered all three, and was wrong for two of them."""
    assert heading in _rollback()


def test_the_inaccurate_pre_merge_rollback_claim_is_gone() -> None:
    """The exact wording that was false once installation had happened."""
    text = _read(DOCUMENTATION)

    assert "nothing on `main` was touched; rollback is returning to" not in text
    assert "There are **three** distinct states" in _rollback()


def test_the_pre_merge_rollback_state_requires_the_full_cleanup() -> None:
    """Returning to main is the last step of that rollback, not the whole of it."""
    state = _span(
        _rollback(),
        "### 14.2 After pre-merge Pi installation validation, before merge",
        "### 14.3 After Task 10 is merged and deployed",
    )

    assert "systemctl disable --now mgo-backup.timer" in state
    assert "/etc/systemd/system/mgo-backup.timer" in state
    assert "/etc/systemd/system/mgo-backup.service" in state
    assert "/etc/logrotate.d/garden-observatory" in state
    assert "systemctl daemon-reload" in state
    assert "preserve every recovery set" in state
    assert "mgo.service" in state
    assert "external to Git" in state


def test_the_pre_merge_rollback_restarts_after_the_checkout() -> None:
    """Every step before the restart leaves a main checkout on feature code."""
    state = _span(
        _rollback(),
        "### 14.2 After pre-merge Pi installation validation, before merge",
        "### 14.3 After Task 10 is merged and deployed",
    )

    assert "`git checkout main`" in state
    assert RESTART_API in state
    assert state.index("`git checkout main`") < state.index(RESTART_API)
    assert "feature-branch runtime" in state
    assert "restore preview" in state

    # The prose names the step the rollback is not complete without. Tie it to
    # the list rather than to a literal: inserting a step renumbers the list but
    # would silently leave the sentence pointing at the wrong one.
    claimed = re.search(r"not complete until step (\d+)", state)
    restart_step = re.search(
        rf"^(\d+)\. `{re.escape(RESTART_API)}`", state, re.MULTILINE
    )

    assert claimed is not None, "the rollback must name its completing step"
    assert restart_step is not None, "the restart must be a numbered step"
    assert claimed.group(1) == restart_step.group(1)


def test_the_post_merge_rollback_also_restarts_the_service() -> None:
    """The unit never changed; the code the API imports did."""
    _, heading, state = _rollback().partition(
        "### 14.3 After Task 10 is merged and deployed"
    )

    assert heading
    assert RESTART_API in state
    assert state.index("git revert") < state.index(RESTART_API)
    assert "src/mgo/core/config.py" in state
    # The old claim was true of the unit file and false of the code.
    assert "needs **no restart**, because it never changed" not in _read(
        DOCUMENTATION
    )


def test_the_before_install_rollback_state_stays_simple() -> None:
    """Where nothing was installed, nothing has to be uninstalled."""
    state = _span(
        _rollback(),
        "### 14.1 Before any Pi installation validation",
        "### 14.2 After pre-merge Pi installation validation, before merge",
    )

    assert CHECKOUT_MAIN in state
    assert "no system artefact was installed" in state
    assert "systemctl" not in state


def test_the_post_merge_rollback_state_reverts_the_code_too() -> None:
    """The only state where Git history is involved."""
    _, heading, state = _rollback().partition(
        "### 14.3 After Task 10 is merged and deployed"
    )

    assert heading
    assert "git revert" in state
    assert "install-service-identity.sh --no-operations" in state
    assert "left intact" in state


def test_permanent_installation_is_documented_as_a_post_merge_act() -> None:
    """A feature branch must not leave a permanent installation behind."""
    procedure = _procedure()

    assert "**Only then** may a pull request be created." in procedure
    assert "after merge" in procedure.lower()
    assert "must not be left running indefinitely from the feature branch" in (
        procedure
    )


# --- generated artefacts stay untracked -------------------------------------


def test_generated_operations_artefacts_are_ignored() -> None:
    """A committed backup or bundle would publish real observation data."""
    git = _git()
    candidates = [
        "mgo-20260728T023000Z.db",
        "mgo-20260728T023000Z.manifest.json",
        "mgo-support-20260728T023000Z.tar.gz",
        ".mgo-backup-abcd.tmp",
        ".mgo-backup.lock",
        "var/log/garden-observatory/mgo.log",
    ]
    completed = subprocess.run(
        [git, "check-ignore", "--no-index", *candidates],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    ignored = set(completed.stdout.split())

    for candidate in candidates:
        assert candidate in ignored, f"{candidate} is not ignored"


def test_the_ignore_rules_do_not_hide_tracked_content() -> None:
    """An over-broad rule would hide migrations, fixtures or documentation."""
    git = _git()
    completed = subprocess.run(
        [git, "ls-files", "--ignored", "--exclude-standard", "--cached"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr

    assert completed.stdout.strip() == "", (
        "these tracked files are now matched by an ignore rule: "
        f"{completed.stdout}"
    )


def test_the_documentation_and_migrations_are_still_tracked() -> None:
    """A direct guard on the two things a broad rule would most likely hide."""
    git = _git()
    completed = subprocess.run(
        [git, "ls-files", "migrations", "docs"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    tracked = completed.stdout.splitlines()

    assert any(name.endswith(".sql") for name in tracked)
    assert any(name.endswith(".md") for name in tracked)


# --- deployment constants used consistently ---------------------------------


def test_the_verifier_and_installer_agree_on_every_location() -> None:
    """Two scripts describing the same deployment must not drift.

    The installer composes the configuration path from its directory
    (``config_path="${config_dir}/mgo.toml"``) rather than repeating the
    literal, which is the right shape; the assertion therefore checks the
    roots each script spells out and the composition rule separately.
    """
    install = _read(INSTALL_SCRIPT)
    verify = _read(VERIFY_OPERATIONS)

    for literal in (
        SYSTEM_CONFIG_PATH.parent.as_posix(),
        SYSTEM_STATE_DIRECTORY.as_posix(),
        SYSTEM_LOG_DIRECTORY.as_posix(),
        SYSTEM_BACKUP_DIRECTORY.as_posix(),
    ):
        assert literal in install, literal
        assert literal in verify, literal

    assert 'config_path="${config_dir}/mgo.toml"' in install
    assert f'config_path="{SYSTEM_CONFIG_PATH.as_posix()}"' in verify

    for name in ("mgo-backup.service", "mgo-backup.timer"):
        assert name in install, name
        assert name in verify, name


# --- finding 23: ignored bytecode must not survive the return to main --------
#
# Direct Pi validation returned to `main` and left src/mgo/operations/ behind: a
# directory holding nothing but git-ignored .pyc files. Git does not remove
# ignored files on checkout, and it will not delete a directory that still holds
# them, so the package directory outlived the branch that created it.
#
# `git status --porcelain` reported the tree CLEAN throughout -- not mentioning
# ignored files is precisely its job -- so the procedure's own cleanliness check
# could not see the residue. And the survivor was importable: a directory with
# no __init__.py is a PEP 420 implicit namespace package, so
# find_spec("mgo.operations") answered on a checkout meant to predate Task 10.
#
# These tests assert the ORDER (cleanup before the checkout, proofs after it)
# and then prove the behaviour against a real temporary Git repository. Order is
# the whole finding: a correct cleanup placed after the checkout is exactly as
# useless as no cleanup, because by then the tracked sources are gone and only
# the bytecode remains to identify.

BYTECODE_HEADING = "#### Remove Task 10 compiled bytecode"
MUST_NOT_TOUCH_HEADING = "#### What this cleanup must not touch"
PACKAGE_PROOF_HEADING = "#### Prove the Task 10 package is gone"
FINAL_STATE_HEADING = "### 13.21 Required final state"
WHAT_NEXT_HEADING = "### 13.22 What happens next"

TASK_10_PACKAGE = "src/mgo/operations"
IGNORED_STATUS = f"git status --ignored --porcelain -- {TASK_10_PACKAGE}"
PACKAGE_RESIDUE_STATE = (
    "Task 10 Python package residue: absent and not import-discoverable"
)


def _bytecode_cleanup() -> str:
    """The mandatory bytecode-removal step, which precedes the checkout."""
    return _span(_procedure(), BYTECODE_HEADING, MUST_NOT_TOUCH_HEADING)


def _package_proof() -> str:
    """The three post-checkout absence proofs."""
    return _span(_procedure(), PACKAGE_PROOF_HEADING, RESTART_HEADING)


def test_the_bytecode_cleanup_precedes_the_checkout() -> None:
    """Removing bytecode after the checkout would be too late to be scoped."""
    procedure = _procedure()

    assert BYTECODE_HEADING in procedure
    assert procedure.index(BYTECODE_HEADING) < procedure.index(CHECKOUT_MAIN)


def test_the_bytecode_cleanup_follows_the_system_artefact_removal() -> None:
    """Units first, then bytecode, then the checkout."""
    procedure = _procedure()

    assert procedure.index(REMOVE_LOGROTATE) < procedure.index(BYTECODE_HEADING)


def test_the_package_proofs_follow_the_checkout() -> None:
    """Absence can only be proven once the checkout has happened."""
    procedure = _procedure()

    assert PACKAGE_PROOF_HEADING in procedure
    assert procedure.index(CHECKOUT_MAIN) < procedure.index(PACKAGE_PROOF_HEADING)


def test_the_bytecode_deletion_is_confined_to_the_task_10_package() -> None:
    """Every deletion names the one package it is allowed to touch."""
    deletions = [
        command
        for command in _bash_commands(_bytecode_cleanup())
        if "-delete" in command
    ]

    assert deletions, "the cleanup deletes nothing"
    for command in deletions:
        assert command.startswith(f"find {TASK_10_PACKAGE} "), command


def test_only_compiled_artefacts_and_empty_cache_directories_are_deleted() -> None:
    """Two deletions: compiled files, then the directories left empty."""
    deletions = [
        command
        for command in _bash_commands(_bytecode_cleanup())
        if "-delete" in command
    ]

    assert len(deletions) == 2, deletions
    files, directories = deletions

    assert "-type f" in files
    assert "-name '*.pyc'" in files
    assert "-name '*.pyo'" in files

    assert "-type d" in directories
    assert "-name '__pycache__'" in directories
    # Without -empty this would remove a cache directory holding anything at all.
    assert "-empty" in directories


def test_the_bytecode_cleanup_never_names_python_source() -> None:
    """A cleanup that could match ``*.py`` would delete tracked source."""
    for command in _bash_commands(_bytecode_cleanup()):
        assert re.search(r"-name\s+'\*\.py'", command) is None, command


def test_the_cleanup_uses_no_broad_git_clean() -> None:
    """``git clean -fdx``/``-fdX`` would work, and would destroy far more."""
    for command in _bash_commands(_procedure()):
        assert "git clean" not in command, command


def test_the_procedure_never_removes_the_package_directory_wholesale() -> None:
    """On the feature branch that directory still holds tracked source."""
    for command in _bash_commands(_procedure()):
        assert f"rm -rf {TASK_10_PACKAGE}" not in command, command


def test_ignored_state_is_not_proven_by_ordinary_git_status() -> None:
    """The defect hid behind exactly the check the procedure used to trust."""
    proof = _package_proof()

    assert IGNORED_STATUS in proof

    for command in _bash_commands(proof):
        if command.startswith("git status"):
            assert "--ignored" in command, command


def test_the_filesystem_absence_is_proven_after_the_checkout() -> None:
    """The plainest check, and the one the residue defeated."""
    assert f"test ! -e {TASK_10_PACKAGE}" in _package_proof()


def test_the_import_check_uses_a_fresh_interpreter_without_bytecode() -> None:
    """A probe that writes bytecode would recreate what it is testing for."""
    probes = [
        command
        for command in _bash_commands(_package_proof())
        if "find_spec" in command
    ]

    assert probes, "no import-resolution check is documented"
    for command in probes:
        assert "python -B" in command, command
        assert 'find_spec("mgo.operations")' in command, command


def test_the_required_final_state_demands_no_package_residue() -> None:
    """A clean checkout is explicitly not sufficient."""
    state = _span(_read(DOCUMENTATION), FINAL_STATE_HEADING, WHAT_NEXT_HEADING)

    assert PACKAGE_RESIDUE_STATE in state


def test_the_pre_installation_rollback_explains_when_it_stays_simple() -> None:
    """Section 14.1 may stay simple only where nothing imported the package."""
    simple = _span(
        _rollback(),
        "### 14.1 Before any Pi installation validation",
        "### 14.2 After pre-merge Pi installation validation, before merge",
    )

    assert "never imported" in simple
    assert "*.pyc" in simple


def test_the_pre_merge_rollback_removes_bytecode_and_proves_absence() -> None:
    """The full rollback carries the cleanup and all three proofs, in order."""
    full = _span(
        _rollback(),
        "### 14.2 After pre-merge Pi installation validation, before merge",
        "### 14.3 After Task 10 is merged and deployed",
    )

    assert "bytecode" in full
    assert f"test ! -e {TASK_10_PACKAGE}" in full
    assert IGNORED_STATUS in full
    assert 'find_spec("mgo.operations")' in full
    assert full.index("bytecode") < full.index("git checkout main")
    assert full.index("git checkout main") < full.index(IGNORED_STATUS)


# --- finding 23: behavioural proof against a real repository -----------------


def _git_run(repo: Path, *args: str) -> str:
    """Run git inside a scratch repository."""
    completed = subprocess.run(
        [_git(), *args],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    return completed.stdout


def _scratch_checkout(tmp_path: Path) -> Path:
    """A repository shaped like the Pi: ``main`` without the operations package.

    The feature branch adds tracked sources under ``src/mgo/operations``, and
    ``__pycache__`` is ignored -- the same arrangement that produced the
    finding.
    """
    repo = tmp_path / "checkout"
    (repo / "src" / "mgo").mkdir(parents=True)

    _git_run(repo, "init", "--initial-branch=main")
    _git_run(repo, "config", "user.email", "validation@example.invalid")
    _git_run(repo, "config", "user.name", "Validation")

    (repo / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
    (repo / "src" / "mgo" / "__init__.py").write_text("", encoding="utf-8")
    _git_run(repo, "add", "-A")
    _git_run(repo, "commit", "-m", "main without the operations package")

    _git_run(repo, "checkout", "-b", "task-010-operations")
    package = repo / "src" / "mgo" / "operations"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "backup_cli.py").write_text("", encoding="utf-8")
    _git_run(repo, "add", "-A")
    _git_run(repo, "commit", "-m", "add the operations package")

    return repo


def _leave_bytecode(repo: Path) -> Path:
    """Reproduce what importing the package leaves behind."""
    cache = repo / "src" / "mgo" / "operations" / "__pycache__"
    cache.mkdir(exist_ok=True)
    for module in ("__init__", "backup_cli"):
        (cache / f"{module}.cpython-313.pyc").write_bytes(b"\x00\x00\x00\x00")
    return cache


def _resolves_operations(repo: Path) -> str:
    """Ask a fresh interpreter whether ``mgo.operations`` resolves in ``repo``.

    ``-S`` is load-bearing. This repository is installed in editable mode, and
    an editable install registers a meta-path finder consulted *before*
    ``sys.path`` -- so without it the probe would answer for the developer's own
    source tree and both halves of the regression would pass for entirely the
    wrong reason. ``-B`` stops the probe writing the bytecode it looks for.
    """
    probe = (
        "import sys; sys.path.insert(0, sys.argv[1]); "
        "import importlib.util; "
        "spec = importlib.util.find_spec('mgo.operations'); "
        "print('NONE' if spec is None else "
        "(spec.origin or ';'.join(spec.submodule_search_locations)))"
    )
    completed = subprocess.run(
        [sys.executable, "-B", "-S", "-c", probe, str(repo / "src")],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout.strip()


def _run_documented_cleanup(repo: Path) -> subprocess.CompletedProcess[str]:
    """Execute the documented bytecode cleanup verbatim inside ``repo``."""
    bash = _find_bash()
    if bash is None:  # pragma: no cover - bash is present on Windows and the Pi
        pytest.skip("bash is unavailable")

    commands = _bash_commands(_bytecode_cleanup())
    assert len(commands) == 3, commands

    # Git Bash's coreutils must win over C:\\Windows\\System32\\find.exe, which
    # is a text search tool and would silently do something else entirely.
    environment = dict(os.environ)
    environment["PATH"] = os.pathsep.join(
        (str(Path(bash).parent), environment.get("PATH", ""))
    )

    return subprocess.run(
        [bash, "-c", "set -u\n" + "\n".join(commands)],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        env=environment,
    )


def test_ignored_bytecode_survives_a_bare_checkout_and_stays_importable(
    tmp_path: Path,
) -> None:
    """The defect itself, reproduced before the correction is asserted."""
    repo = _scratch_checkout(tmp_path)
    _leave_bytecode(repo)
    package = repo / "src" / "mgo" / "operations"

    _git_run(repo, "checkout", "main")

    assert (
        _git_run(repo, "status", "--porcelain") == ""
    ), "ordinary git status reports a clean tree -- which is the trap"
    assert package.is_dir(), "the ignored bytecode kept the directory alive"
    assert not list(package.glob("*.py")), "no tracked source survived"

    resolved = _resolves_operations(repo)
    assert resolved != "NONE", "the residue is discoverable as a namespace package"
    assert str(tmp_path) in resolved, resolved


def test_the_documented_cleanup_removes_the_residue_before_the_checkout(
    tmp_path: Path,
) -> None:
    """The correction: cleanup on the branch, then all three proofs on main."""
    repo = _scratch_checkout(tmp_path)
    _leave_bytecode(repo)
    package = repo / "src" / "mgo" / "operations"

    completed = _run_documented_cleanup(repo)

    assert completed.returncode == 0, completed.stderr
    assert (
        completed.stdout.strip() == ""
    ), f"the residue check should print nothing, printed: {completed.stdout}"
    assert (package / "backup_cli.py").is_file(), "tracked source was destroyed"

    _git_run(repo, "checkout", "main")

    assert not package.exists()
    assert (
        _git_run(repo, "status", "--ignored", "--porcelain", "--", TASK_10_PACKAGE)
        == ""
    )
    assert _resolves_operations(repo) == "NONE"


def test_an_unexpected_cache_file_is_preserved_and_stops_the_cleanup(
    tmp_path: Path,
) -> None:
    """The cleanup must fail closed rather than widen itself to succeed."""
    repo = _scratch_checkout(tmp_path)
    cache = _leave_bytecode(repo)
    for stale in cache.glob("*.pyc"):
        stale.unlink()

    (cache / "expected_module.pyc").write_bytes(b"\x00\x00\x00\x00")
    unexpected = cache / "unexpected-preserve-me.txt"
    unexpected.write_text("someone put this here", encoding="utf-8")

    completed = _run_documented_cleanup(repo)

    assert not (cache / "expected_module.pyc").exists(), "the .pyc was not removed"
    assert unexpected.is_file(), "an unexpected file must never be deleted"
    assert unexpected.read_text(encoding="utf-8") == "someone put this here"
    assert cache.is_dir(), "a non-empty cache directory must survive"

    residue = completed.stdout.strip()
    assert residue, "the check must report the surviving directory"
    assert "__pycache__" in residue, residue


# --- finding 24: logrotate discovery must not depend on an operator's PATH ---
#
# During Pi validation the verifier printed "logrotate is not installed on this
# host" about the very binary the installer had just used successfully. It lives
# at /usr/sbin/logrotate, and /usr/sbin is not on an unprivileged account's
# PATH, so `command -v logrotate` failed. The result was a SKIP rather than a
# false PASS -- the check stayed safe -- but it drew a conclusion about the
# PACKAGE from evidence that only concerned COMMAND DISCOVERY.
#
# The discovery loop is marker-extracted and executed here against stub
# executables. The two canonical locations are absolute, so the harness rewrites
# them into tmp_path; it asserts each literal was present before substituting,
# because a substitution that silently matched nothing would make every scenario
# below pass for the wrong reason.

LOGROTATE_DISCOVERY_START = "# >>> logrotate-discovery >>>"
LOGROTATE_DISCOVERY_END = "# <<< logrotate-discovery <<<"

CANONICAL_LOGROTATE = ("/usr/sbin/logrotate", "/sbin/logrotate")
DISCOVERY_SKIP = "no logrotate executable found in PATH, /usr/sbin or /sbin"


def _logrotate_discovery_block() -> str:
    """The executable discovery and parse check, verbatim."""
    return _marked_block(
        _read(VERIFY_OPERATIONS), LOGROTATE_DISCOVERY_START, LOGROTATE_DISCOVERY_END
    )


def _exit_binary(*, parses: bool) -> Path | None:
    """Locate a real binary that exits ``0`` or ``1``.

    A shebang script is the natural stub, but Git Bash does not report one as
    executable -- ``-x`` is false for a file Python created, and ``chmod`` does
    not stick on a ``noacl`` mount. The discovery loop would then reject the
    stub for reasons that have nothing to do with the logic under test. A copied
    ``true``/``false`` binary is recognised on every supported host, so the
    scenarios below run on Windows and on the Pi alike.
    """
    name = "true" if parses else "false"
    candidates = [Path("/usr/bin") / name, Path("/bin") / name]

    bash = _find_bash()
    if bash is not None:
        directory = Path(bash).parent
        candidates.insert(0, directory / f"{name}.exe")
        candidates.insert(1, directory.parent / "usr" / "bin" / f"{name}.exe")

    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None  # pragma: no cover - every supported host ships true and false


def _stub_logrotate(target: Path, *, parses: bool) -> None:
    """Install an executable stand-in that accepts or rejects the policy.

    The scenarios below deliberately run with a minimal ``PATH`` -- that is the
    whole point of the finding -- so a copied MSYS binary cannot find its
    runtime through ``PATH``. Windows searches an executable's own directory
    first, so the sibling DLLs travel with it. On Linux the glob matches
    nothing and ordinary library resolution applies.
    """
    target.parent.mkdir(parents=True, exist_ok=True)

    source = _exit_binary(parses=parses)
    if source is None:  # pragma: no cover - fallback for an unusual host
        status = 0 if parses else 1
        target.write_text(f"#!/bin/sh\nexit {status}\n", encoding="utf-8")
    else:
        shutil.copy2(source, target)
        for runtime in source.parent.glob("msys-*.dll"):
            companion = target.parent / runtime.name
            if not companion.exists():
                shutil.copy2(runtime, companion)

    target.chmod(0o755)


def _run_logrotate_discovery(
    tmp_path: Path,
    *,
    on_path: bool = False,
    at_usr_sbin: bool = False,
    at_sbin: bool = False,
    parses: bool = True,
) -> str:
    """Run the real discovery logic against controlled stub locations."""
    bash = _find_bash()
    if bash is None:  # pragma: no cover - bash is present on Windows and the Pi
        pytest.skip("bash is unavailable")

    block = _logrotate_discovery_block()
    root = tmp_path.as_posix()
    for canonical in CANONICAL_LOGROTATE:
        assert f'"{canonical}"' in block, canonical
        block = block.replace(f'"{canonical}"', f'"{root}{canonical}"')

    path_directory = tmp_path / "pathbin"
    path_directory.mkdir(parents=True, exist_ok=True)

    if on_path:
        _stub_logrotate(path_directory / "logrotate", parses=parses)
    if at_usr_sbin:
        _stub_logrotate(tmp_path / "usr" / "sbin" / "logrotate", parses=parses)
    if at_sbin:
        _stub_logrotate(tmp_path / "sbin" / "logrotate", parses=parses)

    policy = tmp_path / "garden-observatory"
    policy.write_text("# policy\n", encoding="utf-8")

    program = "\n".join(
        (
            "set -u",
            'pass() { printf "PASS %s\\n" "$*"; }',
            'fail() { printf "FAIL %s\\n" "$*"; }',
            'skip() { printf "SKIP %s\\n" "$*"; }',
            f'logrotate_path="{policy.as_posix()}"',
            # Bash searches PATH itself, and MSYS bash needs a POSIX entry to
            # do it -- a "C:/..." element is honoured by -x but not by the PATH
            # lookup, which would make the on-PATH scenario silently skip.
            f"PATH=\"$(cygpath -u '{path_directory.as_posix()}' 2>/dev/null"
            f" || printf '%s' '{path_directory.as_posix()}')\"",
            "export PATH",
            'printf "PATH_BEFORE=%s\\n" "$PATH"',
            block,
            'printf "PATH_AFTER=%s\\n" "$PATH"',
        )
    )

    completed = subprocess.run(
        [bash, "-c", program],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout


def test_the_discovery_searches_path_then_the_canonical_locations() -> None:
    """Order matters: an operator's own logrotate should win if there is one."""
    block = _logrotate_discovery_block()

    assert "command -v logrotate" in block
    for canonical in CANONICAL_LOGROTATE:
        assert f'"{canonical}"' in block, canonical

    assert block.index("command -v logrotate") < block.index('"/usr/sbin/logrotate"')
    assert block.index('"/usr/sbin/logrotate"') < block.index('"/sbin/logrotate"')


def test_the_discovery_runs_the_executable_it_selected() -> None:
    """A bare ``logrotate`` would go straight back through PATH."""
    block = _logrotate_discovery_block()

    assert '"${logrotate_bin}" --debug "${logrotate_path}"' in block
    assert "\n    if logrotate --debug" not in block


def test_the_verifier_never_claims_logrotate_is_uninstalled() -> None:
    """The old wording asserted a fact the check could not establish."""
    verifier = _read(VERIFY_OPERATIONS)

    assert "logrotate is not installed on this host" not in verifier
    assert DISCOVERY_SKIP in verifier


def test_the_verifier_never_modifies_the_callers_path() -> None:
    """Discovery must not repair PATH for the rest of the script."""
    block = _logrotate_discovery_block()

    assert "PATH=" not in block
    assert "export PATH" not in block


def test_a_path_discovered_logrotate_is_used(tmp_path: Path) -> None:
    """The ordinary case: logrotate on PATH, policy parses."""
    output = _run_logrotate_discovery(tmp_path, on_path=True)

    assert "PASS" in output
    assert "pathbin/logrotate" in output


def test_a_canonical_usr_sbin_logrotate_is_found_without_path(
    tmp_path: Path,
) -> None:
    """The exact Pi condition: installed in /usr/sbin, absent from PATH."""
    output = _run_logrotate_discovery(tmp_path, at_usr_sbin=True)

    assert "PASS" in output
    assert "usr/sbin/logrotate" in output
    assert "SKIP" not in output


def test_a_canonical_sbin_logrotate_is_found_without_path(tmp_path: Path) -> None:
    """The second fallback, for hosts that do not split /sbin and /usr/sbin."""
    output = _run_logrotate_discovery(tmp_path, at_sbin=True)

    assert "PASS" in output
    assert "/sbin/logrotate" in output
    assert "SKIP" not in output


def test_a_discovered_logrotate_that_rejects_the_policy_fails(
    tmp_path: Path,
) -> None:
    """A real parse failure must stay a FAIL, not soften into a SKIP."""
    output = _run_logrotate_discovery(tmp_path, at_usr_sbin=True, parses=False)

    assert "FAIL" in output
    assert "cannot parse" in output
    assert "PASS" not in output


def test_a_genuinely_absent_logrotate_skips_truthfully(tmp_path: Path) -> None:
    """Nothing anywhere: skip, and say what was searched rather than guessing."""
    output = _run_logrotate_discovery(tmp_path)

    assert "SKIP" in output
    assert DISCOVERY_SKIP in output
    assert "not installed" not in output


def test_the_discovery_leaves_path_untouched(tmp_path: Path) -> None:
    """Whatever it finds, the caller's environment is unchanged.

    Compared before against after rather than against a literal: repairing
    ``PATH`` to reach ``/usr/sbin`` would be a tempting fix and a bad one, since
    it would change how every later command in the verifier resolves.
    """
    for index, scenario in enumerate(
        ({"on_path": True}, {"at_usr_sbin": True}, {})
    ):
        output = _run_logrotate_discovery(tmp_path / f"scenario{index}", **scenario)
        before = re.search(r"^PATH_BEFORE=(.*)$", output, re.MULTILINE)
        after = re.search(r"^PATH_AFTER=(.*)$", output, re.MULTILINE)

        assert before is not None and after is not None, output
        assert before.group(1) == after.group(1), scenario


# --- finding 25: wrapper help must describe options per command --------------
#
# The wrapper listed --config, --database, --output-directory and --keep under a
# single "Common options" heading. Only -h/--help is genuinely common: `list`
# reads no configuration and rejects --config, `verify` takes only a positional
# backup file. The CLI was always right; the help was not, and it invited an
# operator to type a command the parser refuses.

HELP_COMMANDS = ("backup", "verify", "restore-test", "list")


def _wrapper_help() -> str:
    """The wrapper's own ``--help`` output, produced by running it."""
    bash = _find_bash()
    if bash is None:  # pragma: no cover - bash is present on Windows and the Pi
        pytest.skip("bash is unavailable")

    completed = subprocess.run(
        [bash, str(BACKUP_WRAPPER).replace("\\", "/"), "--help"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout


def _help_sections(help_text: str) -> dict[str, str]:
    """Split the per-command option blocks out of the help.

    A section runs from its ``name:`` heading until the first line that is not
    indented, so the explanatory prose after the last block is not silently
    absorbed into it -- which would let ``--config`` appear to belong to
    ``list`` purely because a sentence mentions it.
    """
    sections: dict[str, str] = {}
    current: str | None = None

    for line in help_text.splitlines():
        heading = re.match(r"^([a-z-]+):$", line)
        if heading is not None and heading.group(1) in HELP_COMMANDS:
            current = heading.group(1)
            sections[current] = ""
            continue
        if current is None:
            continue
        if line and not line.startswith("  "):
            current = None
            continue
        sections[current] += line + "\n"

    return sections


def test_the_wrapper_help_no_longer_claims_common_options() -> None:
    """The heading that made the classification wrong is gone."""
    help_text = _wrapper_help()

    assert "Common options:" not in help_text
    assert "not shared between commands" in help_text


def test_the_wrapper_help_documents_every_command() -> None:
    """Each command gets its own block."""
    sections = _help_sections(_wrapper_help())

    for command in HELP_COMMANDS:
        assert command in sections, command


@pytest.mark.parametrize(
    ("command", "expected"),
    (
        ("backup", ("--config", "--database", "--output-directory", "--keep")),
        ("verify", ()),
        ("restore-test", ("--work-directory", "--preserve", "--config")),
        ("list", ("--output-directory", "--no-verify")),
    ),
)
def test_each_option_is_documented_under_its_own_command(
    command: str, expected: tuple[str, ...]
) -> None:
    """Every option appears beneath the command that actually defines it."""
    sections = _help_sections(_wrapper_help())
    body = sections[command]

    for option in expected:
        assert option in body, f"{command} should document {option}"


@pytest.mark.parametrize(
    ("command", "forbidden"),
    (
        ("list", ("--config", "--database", "--keep")),
        ("verify", ("--config", "--database", "--keep", "--output-directory")),
        ("backup", ("--work-directory", "--preserve", "--no-verify")),
        ("restore-test", ("--database", "--keep", "--no-verify")),
    ),
)
def test_no_command_documents_an_option_it_does_not_accept(
    command: str, forbidden: tuple[str, ...]
) -> None:
    """The defect was breadth, so the absences are what must be asserted."""
    sections = _help_sections(_wrapper_help())
    body = sections[command]

    for option in forbidden:
        assert option not in body, f"{command} must not document {option}"


def test_the_positional_backup_argument_is_documented() -> None:
    """``verify`` and ``restore-test`` take a file, not an option."""
    sections = _help_sections(_wrapper_help())

    assert "<backup>" in sections["verify"]
    assert "<backup>" in sections["restore-test"]


def test_help_remains_available_everywhere() -> None:
    """``-h/--help`` really is common, and is still offered as such."""
    help_text = _wrapper_help()

    assert "Accepted by every command:" in help_text
    assert "-h, --help" in help_text
    assert 'Run "<command> --help" for the full options of a single command.' in (
        help_text
    )


def test_the_wrapper_still_forwards_every_argument_unchanged() -> None:
    """Help text changed; the wrapper's transparency did not."""
    body = _code(_read(BACKUP_WRAPPER))

    assert 'exec "${python_bin}" -m mgo.operations.backup_cli "$@"' in body


def test_list_still_rejects_a_configuration_option() -> None:
    """The help was corrected to match the CLI, not the CLI to match the help."""
    parser = _build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args(["list", "--config", "mgo.toml"])


@pytest.mark.parametrize(
    "argv",
    (
        ["backup", "--config", "mgo.toml"],
        ["restore-test", "backup.db", "--config", "mgo.toml"],
    ),
)
def test_the_commands_that_take_a_configuration_still_accept_one(
    argv: list[str],
) -> None:
    """Nothing about argument parsing changed."""
    arguments = _build_parser().parse_args(argv)

    assert arguments.config == Path("mgo.toml")


def test_list_still_accepts_its_own_options() -> None:
    """``list`` keeps exactly the two options the help now shows for it."""
    arguments = _build_parser().parse_args(
        ["list", "--output-directory", "/var/backups/garden-observatory", "--no-verify"]
    )

    assert arguments.output_directory == Path("/var/backups/garden-observatory")
    assert arguments.no_verify is True
