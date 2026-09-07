"""The retention-timer installer as hostile input (Task 14.5A, brief §16).

Every test runs the shipped installer under bash against a temporary
directory, or a private copy of it beside a deliberately malformed template.
Nothing here runs ``systemctl``, touches ``/etc`` or needs root. What is
proved:

* no spelling of the real unit directory -- trailing separator, a ``.`` or
  ``..`` component, a symlink to it -- escapes the real directory's rules,
  so the validation-only failure seam can never be armed against it;
* an account name that is not a name is refused before rendering, so a
  newline in ``--user`` cannot become a second directive;
* a backslash sequence in a path is refused before rendering, so ``\\n``
  cannot become a newline and a second directive;
* a template with an unknown placeholder, a missing hardening directive or a
  carriage return is refused and nothing is published -- proved by copying the
  installer next to a malformed template, which is how the mutation register
  detects the removal of each check.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from mgo.core.config import PROJECT_ROOT

DEPLOY = PROJECT_ROOT / "scripts" / "deploy"
TEMPLATE = DEPLOY / "mgo-retention.service.template"
TIMER = DEPLOY / "mgo-retention.timer"
INSTALLER = DEPLOY / "install-retention-timer.sh"


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


needs_bash = pytest.mark.skipif(_find_bash() is None, reason="bash is not available")


def _posix(path: Path) -> str:
    text = path.as_posix()
    if re.match(r"^[A-Za-z]:/", text):
        return "/" + text[0].lower() + text[2:]
    return text


def _app_root(tmp_path: Path) -> str:
    root = tmp_path / "app"
    entry = root / ".venv" / "bin" / "mgo-retention"
    entry.parent.mkdir(parents=True, exist_ok=True)
    entry.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8", newline="\n")
    entry.chmod(0o755)
    return _posix(root)


def _run(installer: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    bash = _find_bash()
    assert bash is not None
    return subprocess.run(
        [bash, _posix(installer), *arguments],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        env={**os.environ, "LC_ALL": "C"},
        timeout=120,
    )


def _bash_literal(value: str) -> str:
    """Encode ``value`` as a bash ``$'...'`` literal with no raw control bytes.

    A newline or a backslash inside a process argument does not survive the
    Windows command line and the MSYS argument reconstruction intact, so the
    hostile values are not passed as arguments at all: they are spelled as
    bash escape sequences inside one ``bash -c`` script, and bash builds the
    real bytes itself before it execs the installer.
    """
    escaped = value.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n")
    return f"$'{escaped}'"


def _run_hostile(installer: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    bash = _find_bash()
    assert bash is not None
    script = " ".join(
        ["exec", "bash", _bash_literal(_posix(installer))]
        + [_bash_literal(argument) for argument in arguments]
    )
    return subprocess.run(
        [bash, "-c", script],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        env={**os.environ, "LC_ALL": "C"},
        timeout=120,
    )


def _units(tmp_path: Path) -> Path:
    units = tmp_path / "units"
    units.mkdir(exist_ok=True)
    return units


def _private_copy(tmp_path: Path, template_text: str) -> Path:
    """The shipped installer and timer beside a template of the test's choosing.

    The installer resolves its companions relative to its own location, so a
    copy in a private directory renders whatever template sits next to it.
    """
    deploy = tmp_path / "deploy"
    deploy.mkdir()
    installer = deploy / INSTALLER.name
    installer.write_bytes(INSTALLER.read_bytes())
    (deploy / TIMER.name).write_bytes(TIMER.read_bytes())
    (deploy / TEMPLATE.name).write_bytes(template_text.encode("utf-8"))
    return installer


# --- the unit directory -------------------------------------------------------------


@needs_bash
@pytest.mark.parametrize(
    "spelling",
    [
        "/etc/systemd/system/",
        "/etc/systemd/system//",
        "/etc/systemd/./system",
        "/etc/systemd/../systemd/system",
        "/etc//systemd/system",
    ],
)
def test_the_validation_aid_is_refused_for_every_spelling_of_the_default_directory(
    spelling: str,
) -> None:
    result = _run(
        INSTALLER,
        "--unit-directory",
        spelling,
        "--fail-after-first-publish",
        "--dry-run",
    )

    assert result.returncode == 65, result.stdout + result.stderr
    assert "validation aid" in result.stderr


@needs_bash
@pytest.mark.parametrize("spelling", ["/etc/systemd/system/", "/etc/systemd/./system"])
def test_enable_is_still_the_default_directory_for_every_spelling(
    spelling: str,
) -> None:
    """A spelled variant does not become a developer directory: the refusal
    it meets is the root requirement, not the developer-directory refusal."""
    result = _run(INSTALLER, "--unit-directory", spelling, "--enable")

    assert result.returncode == 65
    assert "--enable is only valid" not in result.stderr
    assert "requires root" in result.stderr


@needs_bash
def test_a_symlink_to_the_default_directory_is_refused(tmp_path: Path) -> None:
    link = tmp_path / "link"
    try:
        link.symlink_to("/etc/systemd/system", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available to this account")

    result = _run(
        INSTALLER,
        "--unit-directory",
        _posix(link),
        "--fail-after-first-publish",
        "--dry-run",
    )

    assert result.returncode == 65
    assert "symlink" in result.stderr or "validation aid" in result.stderr


@needs_bash
def test_a_symlink_component_of_a_developer_directory_is_refused(
    tmp_path: Path,
) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "via"
    try:
        link.symlink_to(real, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available to this account")

    result = _run(INSTALLER, "--unit-directory", _posix(link / "units"), "--dry-run")

    assert result.returncode == 65
    assert "symlink" in result.stderr


@needs_bash
def test_the_installer_reports_the_canonical_directory(tmp_path: Path) -> None:
    units = _units(tmp_path)
    spelled = _posix(units) + "/./"

    result = _run(INSTALLER, "--unit-directory", spelled, "--dry-run")

    assert result.returncode == 0, result.stdout + result.stderr
    assert f"would publish {_posix(units)}/mgo-retention.service" in result.stdout
    assert list(units.iterdir()) == []


# --- names and paths ---------------------------------------------------------------


@needs_bash
@pytest.mark.parametrize(
    "value",
    ["mgo\nExecStartPre=/bin/evil", "mgo ExecStartPre=/bin/evil", "", "-mgo", "mgo;id"],
)
def test_an_account_name_that_is_not_a_name_is_refused(
    tmp_path: Path, value: str
) -> None:
    result = _run_hostile(
        INSTALLER,
        "--dry-run",
        "--unit-directory",
        _posix(_units(tmp_path)),
        "--user",
        value,
    )

    assert result.returncode == 65, result.stdout + result.stderr
    assert "plain account name" in result.stderr
    assert "ExecStartPre" not in result.stdout
    assert "--- rendered" not in result.stdout


@needs_bash
def test_a_group_name_that_is_not_a_name_is_refused(tmp_path: Path) -> None:
    result = _run_hostile(
        INSTALLER,
        "--dry-run",
        "--unit-directory",
        _posix(_units(tmp_path)),
        "--group",
        "mgo\nExecStartPre=/bin/evil",
    )

    assert result.returncode == 65, result.stdout + result.stderr
    assert "plain account name" in result.stderr


@needs_bash
def test_the_hostile_runner_passes_a_newline_through(tmp_path: Path) -> None:
    """Positive control for ``_run_hostile``: the newline really arrives.

    Without this, the refusals above could pass because the value was
    mangled into two harmless arguments rather than because the installer
    refused a name with a newline in it.
    """
    probe = tmp_path / "probe.sh"
    probe.write_text(
        '#!/usr/bin/env bash\nprintf "%s" "$1" | od -An -c\n',
        encoding="utf-8",
        newline="\n",
    )
    result = _run_hostile(probe, "a\nb\\c")

    assert result.returncode == 0, result.stdout + result.stderr
    # ``od -c`` prints a newline as ``\n`` and a backslash as ``\``.
    columns = result.stdout.split()
    assert columns == ["a", "\\n", "b", "\\", "c"], result.stdout


@needs_bash
@pytest.mark.parametrize(
    ("option", "value"),
    [
        ("--app-root", "/opt/a\\nExecStartPre=/bin/evil"),
        ("--config", "/etc/x\\n[Service]"),
        ("--backup-dir", "/var/b&ackups"),
        ("--capture-dir", "/var/cap tures"),
        ("--database-dir", "/var/lib/a|b"),
    ],
)
def test_a_backslash_sequence_in_a_path_cannot_inject_a_directive(
    tmp_path: Path, option: str, value: str
) -> None:
    result = _run_hostile(
        INSTALLER,
        "--dry-run",
        "--unit-directory",
        _posix(_units(tmp_path)),
        option,
        value,
    )

    assert result.returncode == 65, result.stdout + result.stderr
    assert "cannot carry" in result.stderr
    assert "ExecStartPre" not in result.stdout
    assert "--- rendered" not in result.stdout


@needs_bash
def test_a_plain_path_with_none_of_those_characters_still_renders(
    tmp_path: Path,
) -> None:
    """The refusal list is not so broad that the real paths trip it."""
    result = _run(
        INSTALLER,
        "--dry-run",
        "--unit-directory",
        _posix(_units(tmp_path)),
        "--app-root",
        "/opt/garden-observatory",
        "--config",
        "/etc/garden-observatory/mgo.toml",
        "--backup-dir",
        "/var/backups/garden-observatory",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "ExecStart=/opt/garden-observatory/.venv/bin/mgo-retention" in result.stdout


# --- malformed templates --------------------------------------------------------------


@needs_bash
def test_a_template_with_an_unknown_placeholder_is_refused(tmp_path: Path) -> None:
    template = (
        TEMPLATE.read_text(encoding="utf-8") + "\nEnvironment=EXTRA=@UNKNOWN_VALUE@\n"
    )
    installer = _private_copy(tmp_path, template)
    units = _units(tmp_path)

    result = _run(
        installer, "--unit-directory", _posix(units), "--app-root", _app_root(tmp_path)
    )

    assert result.returncode == 65, result.stdout + result.stderr
    assert "unsubstituted placeholder" in result.stderr
    assert list(units.iterdir()) == []


@needs_bash
@pytest.mark.parametrize(
    ("removed", "expected"),
    [
        ("NoNewPrivileges=yes", "NoNewPrivileges=yes is required"),
        ("ProtectSystem=strict", "ProtectSystem=strict is required"),
        ("Type=oneshot", "Type=oneshot"),
        ("ReadWritePaths=", "ReadWritePaths= is required"),
    ],
)
def test_a_template_missing_a_hardening_directive_is_refused(
    tmp_path: Path, removed: str, expected: str
) -> None:
    lines = [
        line
        for line in TEMPLATE.read_text(encoding="utf-8").splitlines()
        if not line.startswith(removed)
    ]
    installer = _private_copy(tmp_path, "\n".join(lines) + "\n")
    units = _units(tmp_path)

    result = _run(
        installer, "--unit-directory", _posix(units), "--app-root", _app_root(tmp_path)
    )

    assert result.returncode == 65, result.stdout + result.stderr
    assert expected in result.stderr
    assert list(units.iterdir()) == []


@needs_bash
def test_a_template_that_grew_an_install_section_is_refused(tmp_path: Path) -> None:
    template = (
        TEMPLATE.read_text(encoding="utf-8")
        + "\n[Install]\nWantedBy=multi-user.target\n"
    )
    installer = _private_copy(tmp_path, template)
    units = _units(tmp_path)

    result = _run(
        installer, "--unit-directory", _posix(units), "--app-root", _app_root(tmp_path)
    )

    assert result.returncode == 65
    assert "[Install]" in result.stderr
    assert list(units.iterdir()) == []


@needs_bash
def test_a_template_with_carriage_returns_is_refused(tmp_path: Path) -> None:
    """The check is GNU grep's; the MSYS build normalises CRLF before matching,
    so this is provable only on a Linux host -- the Pi validation gate."""
    if os.name == "nt":
        pytest.skip("MSYS grep normalises carriage returns; proven on Linux")
    template = TEMPLATE.read_text(encoding="utf-8").replace("\n", "\r\n")
    installer = _private_copy(tmp_path, template)
    units = _units(tmp_path)

    result = _run(
        installer, "--unit-directory", _posix(units), "--app-root", _app_root(tmp_path)
    )

    assert result.returncode == 65
    assert "carriage returns" in result.stderr
    assert list(units.iterdir()) == []


@needs_bash
def test_the_private_copy_control_installs_the_shipped_template(tmp_path: Path) -> None:
    """Positive control for the copies above: the unmodified template installs."""
    installer = _private_copy(tmp_path, TEMPLATE.read_text(encoding="utf-8"))
    units = _units(tmp_path)

    result = _run(
        installer, "--unit-directory", _posix(units), "--app-root", _app_root(tmp_path)
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert sorted(p.name for p in units.iterdir()) == [
        "mgo-retention.service",
        "mgo-retention.timer",
    ]


# --- rollback fidelity ---------------------------------------------------------


@needs_bash
def test_a_rollback_restores_the_previous_mode(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("POSIX modes are not represented on this host")
    units = _units(tmp_path)
    base = ("--unit-directory", _posix(units))
    assert _run(INSTALLER, *base, "--app-root", _app_root(tmp_path)).returncode == 0
    service = units / "mgo-retention.service"
    service.chmod(0o600)
    other = tmp_path / "other"
    (other / ".venv" / "bin").mkdir(parents=True)
    entry = other / ".venv" / "bin" / "mgo-retention"
    entry.write_text("#!/bin/sh\nexit 0\n")
    entry.chmod(0o755)

    result = _run(
        INSTALLER, *base, "--app-root", _posix(other), "--fail-after-first-publish"
    )

    assert result.returncode == 70
    assert service.stat().st_mode & 0o777 == 0o600
