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

import re
import shutil
import subprocess
from pathlib import Path, PurePosixPath

import pytest

from mgo.core.config import (
    PROJECT_ROOT,
    SERVICE_ACCOUNT,
    SERVICE_GROUP,
    SERVICE_UNIT_NAME,
    SYSTEM_BACKUP_DIRECTORY,
    SYSTEM_CONFIG_PATH,
    SYSTEM_DATABASE_DIRECTORY,
    SYSTEM_LOG_DIRECTORY,
    SYSTEM_STATE_DIRECTORY,
)
from mgo.operations.backup import DEFAULT_RETENTION_COUNT, MAX_RETENTION_COUNT
from mgo.operations.errors import OperationError

DEPLOY = PROJECT_ROOT / "scripts" / "deploy"
OPERATIONS = PROJECT_ROOT / "scripts" / "operations"

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
