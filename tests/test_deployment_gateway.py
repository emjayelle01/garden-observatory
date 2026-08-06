"""Tests for the approved production deployment gateway.

These execute the *shipped* shell logic rather than describing it. The gateway
is written to be a library when sourced and a program when executed, so each
test sources ``scripts/deploy/mgo-validate`` in a real Bash process and calls
the real function against temporary directories, temporary Git repositories and
recorded command doubles.

Nothing here executes a production path, a real ``sudo``, a real ``systemd``,
the network or the Raspberry Pi. That claim is not a promise any more: it is
enforced by ``test_no_test_can_reach_the_host_control_plane``, which parses this
module and fails on the shapes that would break it. The claim used to be false.
``test_the_wrapper_reports_a_missing_gateway_and_stops`` executed the tracked
``scripts/deploy/update-main.sh`` directly, and that script names
``/usr/local/sbin/mgo-validate``; on a workstation the path is absent so the
test reached the missing-gateway branch and looked correct, but on the
Raspberry Pi the path exists and the same test ran
``sudo -n /usr/local/sbin/mgo-validate deploy-main`` against the real control
plane. Wrapper entry points now run as a disposable copy behind a fake ``sudo``.

Where a boundary genuinely cannot be executed safely — the root and systemd
calls, and the symlink refusal on a filesystem that will not create symlinks —
the shipped text is asserted instead, and the test says so.
"""

from __future__ import annotations

import ast
import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

import pytest

from mgo.core.config import PROJECT_ROOT

DEPLOY_DIRECTORY = PROJECT_ROOT / "scripts" / "deploy"
GATEWAY = DEPLOY_DIRECTORY / "mgo-validate"
SUDOERS = DEPLOY_DIRECTORY / "mgo-validate.sudoers"
INSTALLER = DEPLOY_DIRECTORY / "install-mgo-validate.sh"
UPDATE_MAIN = DEPLOY_DIRECTORY / "update-main.sh"

APPROVED_SHA = "1aec2245010a1bd971d028be235c1864af6b46b3"

# Exit codes the gateway promises to distinguish.
EX_REQUEST = 64
EX_PRECONDITION = 65
EX_DEPLOY = 70


def _bash() -> str:
    """Locate Bash on any supported development host.

    Deliberately not a skip. Git for Windows ships Bash but does not put it on
    ``PATH``, so it is resolved from the Git executable this repository already
    requires; a skip here would quietly delete the entire executed-behaviour
    half of this suite on the workstation where it is written.
    """
    found = shutil.which("bash")
    if found is not None:
        return found

    git = shutil.which("git")
    if git is not None:
        git_root = Path(git).resolve().parent.parent
        for candidate in (
            git_root / "bin" / "bash.exe",
            git_root / "usr" / "bin" / "bash.exe",
            git_root / "bin" / "bash",
        ):
            if candidate.exists():
                return str(candidate)

    raise AssertionError("bash is required to test the deployment gateway")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _posix(path: Path) -> str:
    """Render a path the way the Bash process will see it.

    Git Bash on Windows accepts forward slashes, so the drive-letter form is
    kept and only the separators are normalised.
    """
    return str(path).replace("\\", "/")


def run_bash(script: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    """Run a snippet in Bash with the repository's gateway available."""
    return subprocess.run(
        [_bash(), "-c", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(cwd) if cwd is not None else None,
        check=False,
    )


def call_gateway_function(
    call: str,
    *,
    preamble: str = "",
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """Source the shipped gateway and invoke one of its functions.

    ``preamble`` runs after sourcing, which is where a test installs command
    doubles by redefining a function the gateway calls.
    """
    script = f'set +e\nsource "{_posix(GATEWAY)}"\n{preamble}\n{call}\n'
    return run_bash(script, cwd=cwd)


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------


def _write_approval(path: Path, content: str) -> Path:
    """Write approval content byte-exactly.

    ``write_text`` translates ``\\n`` to CRLF on Windows, which the gateway
    correctly refuses — so every approval fixture writes bytes and the line
    endings under test are the ones the test intended.
    """
    path.write_bytes(content.encode("utf-8"))
    return path


@pytest.fixture
def approval_file(tmp_path: Path) -> Path:
    """A well-formed approval file: one line, exactly forty lowercase hex."""
    return _write_approval(tmp_path / "claude-approved-sha", f"{APPROVED_SHA}\n")


def _function_body(name: str, next_name: str) -> str:
    """Slice one shipped function out of the gateway, exactly."""
    source = _read(GATEWAY)
    start = source.index(name)
    return source[start : source.index(next_name, start)]


def _stat_double(owner: str = "0", mode: str = "644") -> str:
    """Replace ``stat`` so ownership and mode can be driven on any filesystem.

    Windows has no meaningful POSIX owner or mode, and the real values on a
    developer's machine are irrelevant to the contract being tested: what
    matters is that the gateway refuses the unsafe combinations it is shown.
    """
    return (
        "stat() {\n"
        '    case "$2" in\n'
        f"        '%u') printf '{owner}\\n' ;;\n"
        f"        '%a') printf '{mode}\\n' ;;\n"
        "        *) printf '\\n' ;;\n"
        "    esac\n"
        "}\n"
    )


def _validate_approval(
    path: Path, *, owner: str = "0", mode: str = "644"
) -> subprocess.CompletedProcess[str]:
    return call_gateway_function(
        f'validate_approval_file "{_posix(path)}"',
        preamble=_stat_double(owner=owner, mode=mode),
    )


# --------------------------------------------------------------------------
# approval-file contract
# --------------------------------------------------------------------------


def test_a_well_formed_approval_file_is_accepted(approval_file: Path) -> None:
    """The happy path prints the SHA and nothing else."""
    result = _validate_approval(approval_file)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == APPROVED_SHA
    assert result.stdout.count("\n") == 1


def test_a_missing_approval_file_is_rejected(tmp_path: Path) -> None:
    """No approval means no authority, not a default."""
    result = _validate_approval(tmp_path / "absent")

    assert result.returncode == EX_REQUEST
    assert "no approval file" in result.stderr


def test_a_short_sha_is_rejected(tmp_path: Path) -> None:
    """An abbreviated SHA is ambiguous; the authority must be exact."""
    path = _write_approval(tmp_path / "approval", f"{APPROVED_SHA[:12]}\n")

    result = _validate_approval(path)

    assert result.returncode == EX_REQUEST
    assert "approval file" in result.stderr or "SHA" in result.stderr


def test_a_long_sha_is_rejected(tmp_path: Path) -> None:
    """Forty characters exactly, so trailing junk cannot ride along."""
    path = _write_approval(tmp_path / "approval", f"{APPROVED_SHA}ab\n")

    assert _validate_approval(path).returncode == EX_REQUEST


def test_an_uppercase_sha_is_rejected(tmp_path: Path) -> None:
    """Lowercase only: comparison against Git output is textual."""
    path = _write_approval(tmp_path / "approval", f"{APPROVED_SHA.upper()}\n")

    assert _validate_approval(path).returncode == EX_REQUEST


@pytest.mark.parametrize(
    "content",
    [
        f" {APPROVED_SHA}\n",
        f"{APPROVED_SHA} \n",
        f"\t{APPROVED_SHA}\n",
        f"{APPROVED_SHA}\t\n",
    ],
)
def test_surrounding_whitespace_is_rejected(tmp_path: Path, content: str) -> None:
    """Whitespace is refused, never trimmed.

    Silently normalising the deployment authority would mean the thing deciding
    what may be deployed is whatever the parser was willing to salvage.
    """
    path = _write_approval(tmp_path / "approval", content)

    assert _validate_approval(path).returncode == EX_REQUEST


@pytest.mark.parametrize(
    "content",
    [
        f"{APPROVED_SHA}\n{APPROVED_SHA}\n",
        f"{APPROVED_SHA}\n# a comment\n",
        f"{APPROVED_SHA}\nmain\n",
        f"{APPROVED_SHA}\n\n",
    ],
)
def test_a_second_line_is_rejected(tmp_path: Path, content: str) -> None:
    """One logical line. A second line is a refusal, not a hint."""
    path = _write_approval(tmp_path / "approval", content)

    assert _validate_approval(path).returncode == EX_REQUEST


@pytest.mark.parametrize(
    "extra",
    [f"{APPROVED_SHA} main\n", f"{APPROVED_SHA} /opt/garden-observatory\n"],
)
def test_an_extra_token_on_the_line_is_rejected(tmp_path: Path, extra: str) -> None:
    """A branch name or path beside the SHA is not authority, it is noise."""
    path = _write_approval(tmp_path / "approval", extra)

    assert _validate_approval(path).returncode == EX_REQUEST


def test_a_valid_sha_without_a_final_newline_is_accepted(tmp_path: Path) -> None:
    """Forty bytes and nothing else is the minimal valid file."""
    path = _write_approval(tmp_path / "approval", APPROVED_SHA)

    result = _validate_approval(path)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == APPROVED_SHA


@pytest.mark.parametrize(
    ("label", "payload"),
    [
        ("terminated second line", f"{APPROVED_SHA}\nmain\n"),
        ("unterminated second line", f"{APPROVED_SHA}\nmain"),
        ("empty second line", f"{APPROVED_SHA}\n\n"),
        ("crlf", f"{APPROVED_SHA}\r\n"),
        ("bare cr", f"{APPROVED_SHA}\r"),
        ("trailing nul", f"{APPROVED_SHA}\0"),
        ("embedded nul", f"{APPROVED_SHA[:20]}\0{APPROVED_SHA[21:]}\n"),
        ("trailing bytes", f"{APPROVED_SHA}xyz"),
        ("trailing space", f"{APPROVED_SHA} "),
        ("trailing tab after newline", f"{APPROVED_SHA}\n\t"),
        ("control byte", f"{APPROVED_SHA[:20]}\x07{APPROVED_SHA[21:]}\n"),
    ],
)
def test_byte_exact_approval_parsing_refuses_trailing_data(
    tmp_path: Path, label: str, payload: str
) -> None:
    """The regression this closes: one newline is not one logical line.

    ``<sha>\\nmain`` with no final newline contains exactly one newline, so a
    line count passes and reading the first line returns a valid SHA — while a
    whole trailing line is ignored. Anchoring on the file's own length refuses
    that and every relative of it.
    """
    path = tmp_path / "approval"
    path.write_bytes(payload.encode("utf-8", errors="surrogateescape"))

    result = _validate_approval(path)

    assert result.returncode == EX_REQUEST, label


def test_an_empty_approval_file_is_rejected(tmp_path: Path) -> None:
    """Zero bytes is not an approval."""
    path = tmp_path / "approval"
    path.write_bytes(b"")

    assert _validate_approval(path).returncode == EX_REQUEST


def test_the_approval_parser_does_not_rely_on_a_line_count(tmp_path: Path) -> None:
    """The corrected parser reconciles the file's length, not its newlines."""
    body = _function_body("validate_approval_file()", "# --- repository")

    assert "wc -c" in body
    assert "head -c 40" in body
    assert "tail -c 1" in body
    assert "wc -l" not in body


def test_a_non_root_owner_is_rejected(approval_file: Path) -> None:
    """Authority owned by anyone else is authority anyone else can rewrite."""
    result = _validate_approval(approval_file, owner="1001")

    assert result.returncode == EX_REQUEST
    assert "owned by root" in result.stderr


@pytest.mark.parametrize("mode", ["664", "646", "666", "620", "602", "0664"])
def test_a_group_or_other_writable_approval_file_is_rejected(
    approval_file: Path, mode: str
) -> None:
    """Write access outside root moves the authority outside root's control.

    ``0664`` is included because ``stat`` reports a fourth leading digit when a
    special bit is set, and the write bits must be read from a known position.
    """
    result = _validate_approval(approval_file, mode=mode)

    assert result.returncode == EX_REQUEST
    assert "writable by" in result.stderr


@pytest.mark.parametrize("mode", ["644", "640", "600", "444", "0644"])
def test_a_safely_permissioned_approval_file_is_accepted(
    approval_file: Path, mode: str
) -> None:
    """The refusal is targeted at write access, not at readability."""
    result = _validate_approval(approval_file, mode=mode)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == APPROVED_SHA


def test_a_directory_is_not_a_valid_approval_file(tmp_path: Path) -> None:
    """Only a regular file can carry the authority."""
    directory = tmp_path / "approval"
    directory.mkdir()

    result = _validate_approval(directory)

    assert result.returncode == EX_REQUEST
    assert "regular file" in result.stderr


def test_the_symlink_refusal_precedes_the_regular_file_check() -> None:
    """A symlink to a valid file passes ``-f``, so ``-L`` must come first.

    Asserted against the shipped text: this repository's suite runs on hosts
    that cannot create symlinks, and a skip would hide the check entirely.
    """
    body = _function_body("validate_approval_file()", "# --- repository preconditions")
    symlink_index = body.index('[[ ! -L "$path" ]]')
    regular_index = body.index('[[ -f "$path" ]]')

    assert symlink_index < regular_index
    following = body[symlink_index : symlink_index + 200]
    assert "die" in following
    assert "symlink" in following


# --------------------------------------------------------------------------
# caller and privilege boundary
# --------------------------------------------------------------------------


ACCOUNT_EXISTS = 'id() { return 0; }\n'


def test_a_non_root_caller_is_rejected() -> None:
    """The gateway refuses to run without the privilege it exists to hold."""
    result = call_gateway_function(
        'require_root_caller "claude" 1001 "claude"', preamble=ACCOUNT_EXISTS
    )

    assert result.returncode == EX_REQUEST
    assert "as root" in result.stderr


def test_a_missing_sudo_caller_is_rejected() -> None:
    """Root with no recorded caller is an unattributable invocation."""
    result = call_gateway_function(
        'require_root_caller "claude" 0 ""', preamble=ACCOUNT_EXISTS
    )

    assert result.returncode == EX_REQUEST
    assert "sudo caller" in result.stderr


def test_another_caller_is_rejected() -> None:
    """Exactly one account may deploy, and it is named in the sudoers rule."""
    result = call_gateway_function(
        'require_root_caller "claude" 0 "pi"', preamble=ACCOUNT_EXISTS
    )

    assert result.returncode == EX_REQUEST
    assert "claude" in result.stderr


def test_the_authorised_caller_is_accepted() -> None:
    """The positive case, so the three refusals above mean something."""
    result = call_gateway_function(
        'require_root_caller "claude" 0 "claude"', preamble=ACCOUNT_EXISTS
    )

    assert result.returncode == 0, result.stderr


def test_a_caller_account_that_does_not_exist_is_rejected() -> None:
    """A sudo record naming an account this host has never had is not trust."""
    result = call_gateway_function(
        'require_root_caller "claude" 0 "claude"',
        preamble="id() { return 1; }\n",
    )

    assert result.returncode == EX_PRECONDITION
    assert "does not exist" in result.stderr


def test_the_production_call_sites_pass_the_real_uid_and_sudo_caller() -> None:
    """The parameters exist for testability, not to become configurable."""
    source = _read(GATEWAY)
    call = 'require_root_caller "$MGO_ADMIN_ACCOUNT" "$EUID" "${SUDO_USER:-}"'

    assert source.count(call) == 3


def test_git_is_routed_through_the_unprivileged_runner() -> None:
    """Every Git call is executed back down as the administrative account."""
    result = call_gateway_function(
        'git_admin "claude" "/tmp/repo" rev-parse HEAD',
        preamble='run_as_admin() { printf "run_as_admin %s\\n" "$*"; }\n',
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == (
        "run_as_admin claude git -C /tmp/repo rev-parse HEAD"
    )


def test_the_unprivileged_runner_drops_privilege_before_the_command() -> None:
    """``run_as_admin`` hands off through ``runuser``, never running as root."""
    result = call_gateway_function(
        'run_as_admin "claude" git status',
        preamble=(
            'account_home() { printf "/home/claude\\n"; }\n'
            'runuser() { printf "runuser %s\\n" "$*"; }\n'
        ),
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().startswith("runuser -u claude -- env -i ")
    assert result.stdout.strip().endswith("git status")


HOSTILE_ENVIRONMENT = (
    'export GIT_DIR="/tmp/evil.git"\n'
    'export GIT_WORK_TREE="/tmp/evil"\n'
    'export GIT_INDEX_FILE="/tmp/evil.index"\n'
    'export GIT_CONFIG="/tmp/evil.gitconfig"\n'
    'export GIT_SSH_COMMAND="ssh -i /tmp/evil"\n'
    'export UV_PROJECT="/tmp/evil"\n'
    'export UV_CONFIG_FILE="/tmp/evil.toml"\n'
    'export VIRTUAL_ENV="/tmp/evil-venv"\n'
    'export PYTHONPATH="/tmp/evil"\n'
    'export PYTHONHOME="/tmp/evil"\n'
    'export CURL_HOME="/tmp/evil"\n'
    'export TMPDIR="/tmp/evil-tmp"\n'
    'export HOME="/tmp/evil-home"\n'
    'export HTTP_PROXY="http://evil"\n'
    'export ALL_PROXY="http://evil"\n'
)


def test_the_admin_environment_is_constructed_not_inherited() -> None:
    """``env -i`` empties it, so nothing survives by omission.

    GIT_DIR, GIT_WORK_TREE, UV_PROJECT, PYTHONPATH and the rest could each
    redirect a root-invoked deployment at a different repository, index,
    configuration or interpreter.
    """
    log_marker = "runuser"
    result = call_gateway_function(
        'run_as_admin "claude" git status',
        preamble=(
            HOSTILE_ENVIRONMENT
            + 'account_home() { printf "/home/claude\\n"; }\n'
            + 'runuser() { printf "%s\\n" "$*"; }\n'
        ),
    )

    assert result.returncode == 0, result.stderr
    line = result.stdout.strip()
    assert line.startswith("-u claude -- env -i "), line
    assert "HOME=/home/claude" in line
    assert "USER=claude" in line
    assert "LC_ALL=C" in line
    for hostile in ("evil", "GIT_DIR", "PYTHONPATH", "UV_PROJECT", "TMPDIR"):
        assert hostile not in line, hostile
    assert log_marker not in line


def test_the_admin_home_comes_from_the_account_database() -> None:
    """Never the caller's HOME: Git finds ~/.ssh and ~/.gitconfig through it."""
    body = _function_body("account_home()", "# Run a command as the unprivileged")

    assert "getent passwd" in body
    assert '"$home" == /*' in body


def test_an_unusable_account_home_stops_the_command() -> None:
    """A relative or missing home is refused rather than guessed at."""
    for home in ("", "relative/path"):
        result = call_gateway_function(
            'run_as_admin "claude" git status',
            preamble=(
                f'getent() {{ printf "claude:x:1001:1001::{home}:/bin/bash\\n"; }}\n'
                'runuser() { printf "ran\\n"; }\n'
            ),
        )
        assert result.returncode != 0, home


def test_the_runtime_probe_environment_is_constructed_not_inherited(
    tmp_path: Path,
) -> None:
    """PYTHONPATH or VIRTUAL_ENV could make the probe import another app.

    The double logs to a file rather than stdout: the import probe redirects
    its own output to /dev/null, so a stdout double would never see it.
    """
    log_file = tmp_path / "runuser.log"
    result = call_gateway_function(
        'require_runtime_can_execute "mgo" "/opt/garden-observatory"',
        preamble=(
            HOSTILE_ENVIRONMENT
            + f'PROBE_LOG="{_posix(log_file)}"\n'
            + 'account_home() { printf "/var/lib/garden-observatory\\n"; }\n'
            + 'runuser() { printf "%s\\n" "$*" >> "$PROBE_LOG"; return 0; }\n'
        ),
    )
    lines = log_file.read_text(encoding="utf-8").splitlines()
    probe = [line for line in lines if "python" in line and " -c " in line]

    assert result.returncode == 0, result.stderr
    assert probe, lines
    assert "env -i" in probe[0]
    assert "HOME=/var/lib/garden-observatory" in probe[0]
    assert "PATH=/usr/bin:/bin" in probe[0]
    assert "MGO_CONFIG_PATH=/etc/garden-observatory/mgo.toml" in probe[0]
    for hostile in ("evil", "PYTHONPATH", "VIRTUAL_ENV"):
        assert hostile not in probe[0], hostile


def _main_body() -> str:
    """The entry point alone.

    Sliced on the leading newline, because ``main()`` also matches inside
    ``action_deploy_main()``.
    """
    source = _read(GATEWAY)
    start = source.index("\nmain() {")
    return source[start : source.index("# Program when executed", start)]


def test_the_root_environment_is_constructed_before_anything_else() -> None:
    """curl, stat, systemctl, flock, mktemp, python3, git and uv all read it."""
    body = _main_body()
    construction = body.index("require_constructed_environment")
    # The first case validates the request; the dispatch case follows it, so an
    # unsupported action is refused before the host is touched at all.
    validation = body.index('case "$action"')

    assert construction < body.index("unset GIT_DIR") < validation
    # Nothing is read from the request beyond assigning its first word, and
    # nothing is written anywhere, before the environment is constructed.
    assert construction < body.index("die ")
    assert construction < body.index("prepare_root_tmpdir")


def test_an_unsupported_action_is_refused_before_the_host_is_touched() -> None:
    """A bad request must not make this process create a directory."""
    body = _function_body("main()", "# Program when executed")

    assert body.index("unsupported action") < body.index("$MGO_ROOT_TMPDIR")


@pytest.mark.parametrize(
    "variable",
    [
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_CONFIG",
        "GIT_CONFIG_GLOBAL",
        "GIT_CONFIG_SYSTEM",
        "GIT_SSH",
        "GIT_SSH_COMMAND",
        "UV_PROJECT",
        "UV_CONFIG_FILE",
        "VIRTUAL_ENV",
        "PYTHONPATH",
        "PYTHONHOME",
        "CURL_HOME",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "BASH_ENV",
        "ENV",
        "LD_LIBRARY_PATH",
        "LD_PRELOAD",
        "PYTHONINSPECT",
        "PYTHONSTARTUP",
        "PYTHONWARNINGS",
        "TMPDIR",
    ],
)
def test_every_behaviour_altering_variable_is_unset_at_the_root_boundary(
    variable: str,
) -> None:
    """Named explicitly, so adding one to the list is a deliberate act.

    Defence in depth rather than the mechanism: the constructed environment
    already contains none of them. The register is kept because an explicit
    list of what is known to steer Git, uv, Python, curl or the loader is worth
    reviewing on its own.
    """
    body = _function_body("main()", "# Program when executed")
    unset_block = body[body.index("unset GIT_DIR") : body.index('[[ -n "$action" ]]')]

    assert variable in unset_block


def test_the_temporary_directory_is_root_controlled() -> None:
    """TMPDIR decides where response bodies and staging files are written."""
    source = _read(GATEWAY)

    assert 'readonly MGO_ROOT_TMPDIR="/run/mgo-validate-tmp"' in source
    assert 'readonly MGO_ROOT_TMPDIR_PARENT="/run"' in source


def test_no_git_or_uv_call_bypasses_the_unprivileged_runner() -> None:
    """No direct ``git`` or ``uv`` invocation survives anywhere in the gateway.

    Read from the shipped text because the guarantee is about *absence*: a test
    that only exercised the paths it knows about could not prove it.
    """
    for line in _read(GATEWAY).splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or not stripped:
            continue
        for command in ("git ", "uv "):
            if stripped.startswith(command):
                pytest.fail(f"{command.strip()} is invoked directly: {stripped}")


def test_only_the_service_restart_runs_at_the_root_boundary() -> None:
    """``systemctl`` is the one thing root is kept for."""
    source = _read(GATEWAY)

    assert "systemctl restart" in source
    assert "runuser -u" in source
    # The runtime account is used only for a readability probe, never for work.
    for line in source.splitlines():
        stripped = line.strip()
        if "runtime_account" in stripped and "runuser" in stripped:
            assert "git" not in stripped
            assert "uv" not in stripped


# --------------------------------------------------------------------------
# repository preconditions
# --------------------------------------------------------------------------


def _git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )
    return result.stdout.strip()


def _init_repository(path: Path, *, name: str = "main") -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "--quiet", f"--initial-branch={name}")
    _git(path, "config", "user.email", "test@example.invalid")
    _git(path, "config", "user.name", "Test")
    _git(path, "config", "commit.gpgsign", "false")


def _commit(repository: Path, message: str, *, filename: str = "file.txt") -> str:
    (repository / filename).write_text(f"{message}\n", encoding="utf-8")
    _git(repository, "add", filename)
    _git(repository, "commit", "--quiet", "-m", message)
    return _git(repository, "rev-parse", "HEAD")


@pytest.fixture
def production(tmp_path: Path) -> dict[str, object]:
    """A temporary production checkout with a temporary upstream.

    The upstream is a real repository on disk, so ``ls-remote``, ``fetch`` and
    ``merge --ff-only`` all execute for real.
    """
    upstream = tmp_path / "upstream"
    _init_repository(upstream)
    first = _commit(upstream, "first")

    checkout = tmp_path / "production"
    subprocess.run(
        ["git", "clone", "--quiet", str(upstream), str(checkout)],
        capture_output=True,
        text=True,
        check=True,
    )
    _git(checkout, "config", "user.email", "test@example.invalid")
    _git(checkout, "config", "user.name", "Test")
    _git(checkout, "config", "commit.gpgsign", "false")

    return {"upstream": upstream, "checkout": checkout, "first": first}


PASSTHROUGH_RUNNER = 'run_as_admin() { shift; "$@"; }\n'


def _preconditions(
    checkout: Path,
    *,
    expected: str = "emjayelle01/garden-observatory",
    branch: str = "main",
    preamble: str = "",
) -> subprocess.CompletedProcess[str]:
    """Run the shipped precondition check against a temporary checkout.

    ``remote_matches_repository`` is exercised separately; here the remote check
    is satisfied by a double so that a temporary clone's local path does not
    have to impersonate GitHub.
    """
    doubles = (
        PASSTHROUGH_RUNNER
        + "remote_matches_repository() { return 0; }\n"
        + preamble
    )
    return call_gateway_function(
        "require_repository_preconditions "
        f'"claude" "{_posix(checkout)}" "{expected}" "{branch}" "origin"',
        preamble=doubles,
    )


def test_a_healthy_checkout_passes_its_preconditions(
    production: dict[str, object],
) -> None:
    """The positive case, so the negatives below mean something."""
    result = _preconditions(production["checkout"])  # type: ignore[arg-type]

    assert result.returncode == 0, result.stderr


def test_a_dirty_working_tree_is_rejected(production: dict[str, object]) -> None:
    """A fast-forward over local edits would destroy or refuse them."""
    checkout = production["checkout"]
    assert isinstance(checkout, Path)
    (checkout / "file.txt").write_text("edited\n", encoding="utf-8")

    result = _preconditions(checkout)

    assert result.returncode == EX_PRECONDITION
    assert "not clean" in result.stderr


def test_an_untracked_file_is_rejected(production: dict[str, object]) -> None:
    """Untracked is not harmless: it may shadow a path the target adds."""
    checkout = production["checkout"]
    assert isinstance(checkout, Path)
    (checkout / "stray.txt").write_text("stray\n", encoding="utf-8")

    result = _preconditions(checkout)

    assert result.returncode == EX_PRECONDITION
    assert "not clean" in result.stderr


def test_a_stash_is_rejected(production: dict[str, object]) -> None:
    """A stash is uncommitted work the deployment would strand."""
    checkout = production["checkout"]
    assert isinstance(checkout, Path)
    (checkout / "file.txt").write_text("edited\n", encoding="utf-8")
    _git(checkout, "stash", "push", "--quiet")

    result = _preconditions(checkout)

    assert result.returncode == EX_PRECONDITION
    assert "stash" in result.stderr


def test_the_wrong_branch_is_rejected(production: dict[str, object]) -> None:
    """Only ``main`` is deployable."""
    checkout = production["checkout"]
    assert isinstance(checkout, Path)
    _git(checkout, "switch", "--quiet", "-c", "feature")

    result = _preconditions(checkout)

    assert result.returncode == EX_PRECONDITION
    assert "not on main" in result.stderr


def test_a_detached_head_is_rejected(production: dict[str, object]) -> None:
    """Detached HEAD has no branch to fast-forward."""
    checkout = production["checkout"]
    assert isinstance(checkout, Path)
    _git(checkout, "switch", "--quiet", "--detach", "HEAD")

    result = _preconditions(checkout)

    assert result.returncode == EX_PRECONDITION
    assert "detached" in result.stderr


@pytest.mark.parametrize(
    "marker", ["MERGE_HEAD", "REBASE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD"]
)
def test_an_operation_in_progress_is_rejected(
    production: dict[str, object], marker: str
) -> None:
    """An interrupted operation is not a safe starting point."""
    checkout = production["checkout"]
    assert isinstance(checkout, Path)
    (checkout / ".git" / marker).write_text("x\n", encoding="utf-8")

    result = _preconditions(checkout)

    assert result.returncode == EX_PRECONDITION
    assert "in progress" in result.stderr


def test_an_unexpected_worktree_is_rejected(
    production: dict[str, object], tmp_path: Path
) -> None:
    """A second worktree means another HEAD shares these objects."""
    checkout = production["checkout"]
    assert isinstance(checkout, Path)
    _git(checkout, "worktree", "add", "--quiet", "--detach", str(tmp_path / "extra"))

    result = _preconditions(checkout)

    assert result.returncode == EX_PRECONDITION
    assert "worktree" in result.stderr


def test_a_missing_repository_is_rejected(tmp_path: Path) -> None:
    """No repository, no deployment."""
    result = _preconditions(tmp_path / "absent")

    assert result.returncode == EX_PRECONDITION
    assert "no repository" in result.stderr


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/emjayelle01/garden-observatory.git",
        "https://github.com/emjayelle01/garden-observatory",
        "git@github.com:emjayelle01/garden-observatory.git",
        "ssh://git@github.com/emjayelle01/garden-observatory.git",
    ],
)
def test_the_expected_remote_is_accepted(url: str) -> None:
    """HTTPS or SSH, with or without ``.git``, all name the same repository."""
    result = call_gateway_function(
        f'remote_matches_repository "{url}" "emjayelle01/garden-observatory"'
    )

    assert result.returncode == 0


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/someone-else/garden-observatory.git",
        "https://github.com/emjayelle01/garden-observatory-fork.git",
        "https://evil.example.com/emjayelle01/garden-observatory.git",
        "https://github.com/emjayelle01/garden-observatory.evil.git",
    ],
)
def test_an_unexpected_remote_is_rejected(url: str) -> None:
    """Matching the owner/name pair, so a look-alike cannot pass on a substring."""
    result = call_gateway_function(
        f'remote_matches_repository "{url}" "emjayelle01/garden-observatory"'
    )

    assert result.returncode != 0


# --------------------------------------------------------------------------
# fast-forward proofs
# --------------------------------------------------------------------------


def _fast_forward_check(
    checkout: Path, head: str, target: str
) -> subprocess.CompletedProcess[str]:
    return call_gateway_function(
        "require_fast_forward_target "
        f'"claude" "{_posix(checkout)}" "{head}" "{target}"',
        preamble=PASSTHROUGH_RUNNER,
    )


def test_a_strict_descendant_is_accepted(production: dict[str, object]) -> None:
    """The ordinary deployment: the target is ahead on the same history."""
    upstream = production["upstream"]
    checkout = production["checkout"]
    assert isinstance(upstream, Path)
    assert isinstance(checkout, Path)
    head = str(production["first"])
    target = _commit(upstream, "second")
    _git(checkout, "fetch", "--quiet", "origin", "main")

    assert _fast_forward_check(checkout, head, target).returncode == 0


def test_a_downgrade_is_rejected(production: dict[str, object]) -> None:
    """A target behind the deployed commit is a rollback wearing a deploy hat."""
    upstream = production["upstream"]
    checkout = production["checkout"]
    assert isinstance(upstream, Path)
    assert isinstance(checkout, Path)
    older = str(production["first"])
    newer = _commit(upstream, "second")
    _git(checkout, "fetch", "--quiet", "origin", "main")

    result = _fast_forward_check(checkout, newer, older)

    assert result.returncode == EX_PRECONDITION
    assert "behind" in result.stderr or "descendant" in result.stderr


def test_both_fast_forward_proofs_are_stated_explicitly() -> None:
    """The ancestry proof and the downgrade refusal are both present.

    They are logically equivalent — ``rev-list --count X..Y`` is zero exactly
    when Y is an ancestor of X — so no input can fail one without failing the
    other, and neither can be detected by removing it and watching a behaviour
    test. Both are kept because they fail with different messages, and this
    asserts that removing either one is a change to the shipped contract.
    """
    body = _function_body("require_fast_forward_target()", "# --- environment")

    assert "merge-base --is-ancestor" in body
    assert "is not a descendant of the deployed commit" in body
    assert 'rev-list --count "$target..$head"' in body
    assert "is behind the deployed commit" in body


def test_divergent_history_is_rejected(
    production: dict[str, object], tmp_path: Path
) -> None:
    """Unrelated histories can never fast-forward."""
    checkout = production["checkout"]
    assert isinstance(checkout, Path)
    other = tmp_path / "other"
    _init_repository(other)
    unrelated = _commit(other, "unrelated")
    _git(checkout, "remote", "add", "other", str(other))
    _git(checkout, "fetch", "--quiet", "other")

    result = _fast_forward_check(checkout, str(production["first"]), unrelated)

    assert result.returncode == EX_PRECONDITION


# --------------------------------------------------------------------------
# preview status parsing
# --------------------------------------------------------------------------


def _python() -> str:
    """The interpreter standing in for the Pi's ``/usr/bin/python3``."""
    return _posix(Path(sys.executable))


def _parse_state(
    tmp_path: Path, payload: bytes
) -> subprocess.CompletedProcess[str]:
    """Run the shipped JSON parser against a real response file."""
    body = tmp_path / "body.json"
    body.write_bytes(payload)
    return call_gateway_function(
        f'preview_state_from_file "{_posix(body)}" "{_python()}"'
    )


@pytest.mark.parametrize(
    ("label", "payload", "expected"),
    [
        ("compact", b'{"enabled":true,"state":"running","owner":"preview"}', "running"),
        (
            "formatted",
            b'{\n  "enabled": true,\n  "state": "stopped",\n  "owner": null\n}\n',
            "stopped",
        ),
        ("failed", b'{"state":"failed","last_error":null}', "failed"),
        ("spaced", b'{"state" : "starting"}', "starting"),
        (
            "nested plus one valid top-level",
            b'{"state":"running","camera":{"state":"available"}}',
            "running",
        ),
    ],
)
def test_a_real_json_document_yields_its_top_level_state(
    tmp_path: Path, label: str, payload: bytes, expected: str
) -> None:
    """Parsed as JSON, not pattern-matched.

    A nested ``state`` no longer competes with the real one, and formatting is
    irrelevant because the document is actually parsed.
    """
    result = _parse_state(tmp_path, payload)

    assert result.returncode == 0, f"{label}: {result.stderr}"
    assert result.stdout.strip() == expected, label


@pytest.mark.parametrize(
    ("label", "payload"),
    [
        ("missing state", b"{}"),
        ("empty body", b""),
        ("malformed with a matching fragment", b'{"state":"running"'),
        ("trailing garbage", b'{"state":"running"} rubbish'),
        ("leading garbage", b'rubbish {"state":"running"}'),
        ("duplicate top-level state", b'{"state":"running","state":"running"}'),
        ("conflicting duplicate", b'{"state":"running","state":"stopped"}'),
        ("nested only", b'{"camera":{"state":"running"}}'),
        ("not an object", b'["state","running"]'),
        ("bare string", b'"running"'),
        ("non-string state", b'{"state":3}'),
        ("null state", b'{"state":null}'),
        ("invalid utf-8", b'{"state":"run\xffning"}'),
        ("embedded nul", b'{"state":"run\x00ning"}'),
    ],
)
def test_an_unusable_status_document_is_refused_not_invented(
    tmp_path: Path, label: str, payload: bytes
) -> None:
    """There is no fallback value, and no fragment survives a broken document.

    A grep could find ``"state":"running"`` inside a truncated or
    garbage-padded response and hand it back as a deployment baseline. A parser
    refuses the document outright, which is the only honest answer.
    """
    result = _parse_state(tmp_path, payload)

    assert result.returncode != 0, label
    assert result.stdout.strip() == "", label


@pytest.mark.parametrize("state", ["paused", "unknown", "RUNNING", ""])
def test_an_unsupported_state_token_is_refused(tmp_path: Path, state: str) -> None:
    """The vocabulary is closed, and the check sits outside the parser."""
    body = tmp_path / "body.json"
    body.write_bytes(f'{{"state":"{state}"}}'.encode())
    result = call_gateway_function(
        'read_preview_state "url"',
        preamble=(
            "make_temporary_file() { command mktemp; }\n"
            "http_get_200() { return 0; }\n"
            "discard_temporary() { return 0; }\n"
            "preview_state_from_file() {\n"
            f'    "{_python()}" -c \'import sys; sys.stdout.write("{state}\\n")\'\n'
            "}\n"
        ),
    )

    assert result.returncode != 0


@pytest.mark.parametrize(
    ("label", "payload"),
    [
        ("NaN as the state", b'{"state":NaN}'),
        ("NaN elsewhere", b'{"state":"running","frames":NaN}'),
        ("Infinity", b'{"state":"running","uptime":Infinity}'),
        ("negative Infinity", b'{"state":"running","drift":-Infinity}'),
    ],
)
def test_a_non_standard_numeric_constant_is_refused(
    tmp_path: Path, label: str, payload: bytes
) -> None:
    """NaN, Infinity and -Infinity are Python extensions, not JSON.

    ``json.loads`` accepts all three by default, so a document containing one
    would parse and its ``state`` would be handed back as a deployment
    baseline. A document that is not JSON is not a status document, and this
    gateway does not get to decide what a non-standard token meant.
    """
    result = _parse_state(tmp_path, payload)

    assert result.returncode != 0, label
    assert result.stdout.strip() == "", label


def test_an_unusable_document_is_refused_cleanly(tmp_path: Path) -> None:
    """Refused, not crashed.

    A parser that falls out of a type check with a traceback is still refusing,
    but it refuses by accident: the next document that happens not to raise
    would be accepted. It also puts a Python stack trace in front of an
    operator who asked about a camera.
    """
    for payload in (
        b'["state","running"]',
        b'"running"',
        b'{"state":3}',
        b"{}",
        b"",
    ):
        result = _parse_state(tmp_path, payload)

        assert result.returncode == 1, payload
        assert "Traceback" not in result.stderr, payload
        assert result.stdout.strip() == "", payload


def test_the_parser_decodes_strictly(tmp_path: Path) -> None:
    """Invalid UTF-8 is refused, never repaired into something plausible.

    ``errors="replace"`` turns an undecodable byte into U+FFFD and hands back a
    document that parses — so a corrupted response would yield a state.
    """
    body = _function_body("preview_state_from_file()", "# Stable, transient")
    decode = [line for line in body.splitlines() if ".decode(" in line]

    assert decode == ['    text = raw.decode("utf-8")'], decode
    assert _parse_state(tmp_path, b'{"state":"run\xffning"}').returncode != 0


def test_the_parser_refuses_constants_explicitly() -> None:
    """Refused by contract, not by whatever the interpreter happens to do."""
    body = _function_body("preview_state_from_file()", "# Stable, transient")

    assert "parse_constant=refuse_constant" in body
    assert "raise ValueError" in body


def test_the_parser_runs_in_an_isolated_interpreter() -> None:
    """PYTHONINSPECT, PYTHONSTARTUP and PYTHONWARNINGS all change a run.

    ``-I`` implies ``-E``, so every PYTHON* variable is ignored, and the user
    site directory is left out — belt and braces alongside the constructed
    environment the whole process already runs in.
    """
    body = _function_body("preview_state_from_file()", "# Stable, transient")

    assert '"$MGO_ENV_COMMAND" -i "$parser" -I -c' in body


def test_the_state_parser_is_not_grep() -> None:
    """The correction is structural: pattern matching is gone."""
    body = _function_body("preview_state_from_file()", "# Stable, transient")

    assert "grep" not in body
    assert "json.loads" in body
    assert "object_pairs_hook" in body
    assert 'readonly MGO_JSON_PARSER="/usr/bin/python3"' in _read(GATEWAY)


def test_the_parser_reads_the_file_rather_than_shell_arguments() -> None:
    """An arbitrary remote response never passes through the shell."""
    body = _function_body("preview_state_from_file()", "# Stable, transient")

    assert '"$body_file"' in body
    assert "$(cat" not in body


def test_the_parser_does_not_import_the_application() -> None:
    """Parsing a response must not depend on the code being deployed."""
    body = _function_body("preview_state_from_file()", "# Stable, transient")

    assert "import mgo" not in body
    assert ".venv" not in body


# --------------------------------------------------------------------------
# recovery is never best effort
# --------------------------------------------------------------------------


def test_recovery_requires_both_active_service_and_healthy_endpoint() -> None:
    """A unit can be active while the application fails every request."""
    result = call_gateway_function(
        'await_recovery "mgo.service" "http://127.0.0.1:8080/health" 2',
        preamble=(
            "service_is_active() { return 0; }\n"
            "endpoint_is_ok() { return 1; }\n"
            "sleep() { :; }\n"
        ),
    )

    assert result.returncode != 0


def test_recovery_succeeds_once_both_conditions_hold() -> None:
    """The positive case returns the elapsed seconds it waited."""
    result = call_gateway_function(
        'await_recovery "mgo.service" "http://127.0.0.1:8080/health" 5',
        preamble=(
            "service_is_active() { return 0; }\n"
            "endpoint_is_ok() { return 0; }\n"
            "sleep() { :; }\n"
        ),
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "0"


def test_recovery_is_bounded_and_does_not_wait_for_ever(tmp_path: Path) -> None:
    """The bound is a real limit, not a comment: it probes exactly N times."""
    result = call_gateway_function(
        'await_recovery "mgo.service" "http://127.0.0.1:8080/health" 3',
        preamble=(
            "service_is_active() { return 0; }\n"
            'endpoint_is_ok() { printf "x" >> attempts; return 1; }\n'
            "sleep() { :; }\n"
        ),
        cwd=tmp_path,
    )

    assert result.returncode != 0
    assert (tmp_path / "attempts").read_text(encoding="utf-8") == "xxx"


def test_no_endpoint_probe_uses_a_proxy() -> None:
    """Loopback means this host, not whatever a proxy would answer for."""
    for line in _read(GATEWAY).splitlines():
        if "curl" in line and not line.strip().startswith("#"):
            assert "--noproxy" in line, line


# --------------------------------------------------------------------------
# exact HTTP status
# --------------------------------------------------------------------------


def _curl_double(status: str, *, body: str = "{}", fail: bool = False) -> str:
    """Stand in for ``curl``, honouring --output and --write-out.

    Real ``curl`` semantics matter here: it writes the body to the file named
    by ``--output`` and prints ``--write-out`` on stdout, which is exactly what
    the gateway relies on to separate the status from the body.
    """
    # The response body goes to a real temporary file, but not to the fixed
    # production directory, which does not exist on a development host. The
    # contract that the *shipped* helper names that directory explicitly is
    # asserted separately.
    temporary = "make_temporary_file() { command mktemp; }\n"
    if fail:
        return temporary + "curl() { return 7; }\n"
    return temporary + (
        "curl() {\n"
        "    local out=\"\"\n"
        "    while [[ $# -gt 0 ]]; do\n"
        '        if [[ "$1" == "--output" ]]; then out="$2"; shift; fi\n'
        "        shift\n"
        "    done\n"
        f'    [[ -n "$out" ]] && printf \'{body}\' > "$out"\n'
        f"    printf '{status}'\n"
        "}\n"
    )


@pytest.mark.parametrize("status", ["201", "204", "301", "302", "307", "404", "500"])
def test_only_an_exact_200_counts_as_healthy(status: str) -> None:
    """``curl -f`` accepts every 2xx and reports a redirect as success."""
    result = call_gateway_function(
        'endpoint_is_ok "http://127.0.0.1:8080/health"',
        preamble=_curl_double(status),
    )

    assert result.returncode != 0, status


def test_an_exact_200_is_healthy() -> None:
    """The positive case, so the refusals above mean something."""
    result = call_gateway_function(
        'endpoint_is_ok "http://127.0.0.1:8080/health"',
        preamble=_curl_double("200"),
    )

    assert result.returncode == 0, result.stderr


def test_a_transport_failure_is_not_healthy() -> None:
    """A connection that never happened is not a 200."""
    result = call_gateway_function(
        'endpoint_is_ok "http://127.0.0.1:8080/health"',
        preamble=_curl_double("000", fail=True),
    )

    assert result.returncode != 0


@pytest.mark.parametrize("status", ["204", "302", "404", "500"])
def test_a_non_200_body_is_never_interpreted(status: str) -> None:
    """An error page must not be read as a status document."""
    result = call_gateway_function(
        'read_preview_state "http://127.0.0.1:8080/camera/preview/status"',
        preamble=_curl_double(status, body='{"state":"running"}'),
    )

    assert result.returncode != 0
    assert "running" not in result.stdout


def test_a_200_body_is_used(tmp_path: Path) -> None:
    """Once the status is exactly 200, the document is parsed and used."""
    result = call_gateway_function(
        'read_preview_state "http://127.0.0.1:8080/camera/preview/status"',
        preamble=(
            _curl_double("200", body='{"state":"running"}')
            # The parser is exercised directly elsewhere; here the point is
            # that a 200 body reaches it at all.
            + "preview_state_from_file() { printf 'running\\n'; }\n"
        ),
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "running"


@pytest.mark.parametrize("status", ["201", "202", "204", "302", "404", "500"])
def test_a_preview_start_requires_an_exact_200(status: str) -> None:
    """A 202 "accepted" is not a preview that came back."""
    result = call_gateway_function(
        'start_preview "http://127.0.0.1:8080/camera/preview/start"',
        preamble=_curl_double(status),
    )

    assert result.returncode != 0, status


def test_a_preview_start_accepts_an_exact_200() -> None:
    """The positive case."""
    result = call_gateway_function(
        'start_preview "http://127.0.0.1:8080/camera/preview/start"',
        preamble=_curl_double("200"),
    )

    assert result.returncode == 0, result.stderr


def test_every_probe_ignores_curl_configuration() -> None:
    """``--disable`` first, so no ``.curlrc`` can rewrite the request.

    A configuration file could add ``--location``, change the proxy or alter
    the timeout, and the gateway would obey it silently — at root.
    """
    for name, following in (
        ("http_get_200()", "http_post_200()"),
        ("http_post_200()", "# Literal loopback"),
    ):
        body = _function_body(name, following)
        index = body.index("curl ")
        options = body[index + len("curl ") :].split()

        assert options[0] == "--disable", name
        assert "--no-location" in body, name
        assert "--max-redirs" in body, name
        assert "--noproxy" in body, name


@pytest.mark.parametrize(
    ("label", "status"),
    [("moved permanently", "301"), ("found", "302"), ("temporary", "307")],
)
def test_configuration_cannot_make_a_redirect_look_healthy(
    label: str, status: str
) -> None:
    """Even a curl told to follow redirects reports the redirect's own status.

    The double answers with the redirect status the way a configured curl
    would, and the exact-200 comparison still refuses it.
    """
    for call in (
        'endpoint_is_ok "http://127.0.0.1:8080/health"',
        'start_preview "http://127.0.0.1:8080/camera/preview/start"',
    ):
        result = call_gateway_function(call, preamble=_curl_double(status))
        assert result.returncode != 0, f"{label}: {call}"


def test_no_probe_follows_a_redirect() -> None:
    """``--location`` would turn a moved endpoint into a healthy one.

    Checked over the whole request-issuing function, not per line: every curl
    invocation here is a continuation, so a per-line scan would only ever see
    the first one.
    """
    for name, following in (
        ("http_get_200()", "http_post_200()"),
        ("http_post_200()", "# Literal loopback"),
    ):
        body = "\n".join(
            line
            for line in _function_body(name, following).splitlines()
            if not line.strip().startswith("#")
        )
        assert "--max-time" in body, name
        # Both halves: nothing that follows a redirect, and the two options
        # that refuse to. Asserting only the absence would be satisfied by a
        # probe that had simply lost its refusals.
        assert "--location" not in body.replace("--no-location", ""), name
        assert " -L " not in body, name
        assert "--no-location" in body, name
        assert "--max-redirs 0" in body, name


def test_every_probe_captures_the_status_explicitly() -> None:
    """The status is compared, not inferred from curl's exit code."""
    for name in ("http_get_200()", "http_post_200()"):
        body = _function_body(name, "}\n")
        assert "--write-out '%{http_code}'" in body
        assert '[[ "$status" == "200" ]]' in body


def test_response_bodies_go_to_temporary_files_that_are_removed() -> None:
    """Securely created, always cleaned up, never left in a shared directory."""
    for name, following in (
        ("endpoint_is_ok()", "# There is deliberately no"),
        ("read_preview_state()", "read_stable_preview_state()"),
        ("start_preview()", "# The physical producer contract"),
    ):
        body = _function_body(name, following)
        assert "make_temporary_file" in body, name
        assert "discard_temporary" in body, name


def test_a_failed_temporary_cleanup_is_not_reported_as_success() -> None:
    """A helper that leaks its own scratch file has not succeeded."""
    result = call_gateway_function(
        'endpoint_is_ok "http://127.0.0.1:8080/health"',
        preamble=_curl_double("200") + "discard_temporary() { return 1; }\n",
    )

    assert result.returncode != 0


def test_a_failed_cleanup_after_a_preview_start_is_not_success() -> None:
    """Same contract on the one write the gateway makes."""
    result = call_gateway_function(
        'start_preview "http://127.0.0.1:8080/camera/preview/start"',
        preamble=_curl_double("200") + "discard_temporary() { return 1; }\n",
    )

    assert result.returncode != 0


def test_cleanup_never_broadens_into_a_wildcard() -> None:
    """One tracked path, never a pattern and never a directory."""
    body = _function_body("discard_temporary()", "endpoint_is_ok()")

    assert 'rm -f -- "$path"' in body
    assert "*" not in body
    assert "-r" not in body


# --------------------------------------------------------------------------
# preview preservation
# --------------------------------------------------------------------------


def _producer_double(rpicam: int = 1, libcamera: int = 0) -> str:
    """Stand in for ``pgrep`` so producer counts can be driven exactly."""
    return (
        "count_processes() {\n"
        '    case "$1" in\n'
        f"        rpicam-vid) printf '{rpicam}\\n' ;;\n"
        f"        libcamera-vid) printf '{libcamera}\\n' ;;\n"
        "        *) printf '0\\n' ;;\n"
        "    esac\n"
        "}\n"
    )


PREVIEW_DOUBLES = (
    "start_preview() { printf 'START\\n' >> calls; }\n"
    "sleep() { :; }\n"
)


def _restore_preview(
    previous: str,
    *,
    current: str,
    rpicam: int | None = None,
    libcamera: int = 0,
    extra: str = "",
    cwd: Path,
) -> subprocess.CompletedProcess[str]:
    """Exercise the shipped restoration against a driven world.

    ``rpicam`` defaults to the count that *should* accompany ``current``, so a
    test only states it when the point is that it disagrees.
    """
    if rpicam is None:
        rpicam = 1 if current == "running" else 0
    preamble = (
        PREVIEW_DOUBLES
        + _producer_double(rpicam=rpicam, libcamera=libcamera)
        + f"read_preview_state() {{ printf '{current}\\n'; }}\n"
        + extra
    )
    return call_gateway_function(
        f'restore_preview_state "{previous}" "status" "start" 3',
        preamble=preamble,
        cwd=cwd,
    )


def test_an_already_running_preview_receives_no_duplicate_start(
    tmp_path: Path,
) -> None:
    """A second start would be a duplicate request against an owned camera."""
    result = _restore_preview("running", current="running", cwd=tmp_path)

    assert result.returncode == 0, result.stderr
    assert "already running" in result.stdout
    assert not (tmp_path / "calls").exists()


@pytest.mark.parametrize("previous", ["stopped", "failed", "unknown"])
def test_a_non_running_preview_that_stays_non_running_passes(
    tmp_path: Path, previous: str
) -> None:
    """stopped -> stopped, failed -> non-running, unknown -> non-running."""
    result = _restore_preview(previous, current="stopped", cwd=tmp_path)

    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "calls").exists()
    assert "non-running state before deployment" in result.stdout


@pytest.mark.parametrize("previous", ["stopped", "failed", "unknown"])
def test_a_non_running_preview_that_drifts_into_running_fails(
    tmp_path: Path, previous: str
) -> None:
    """Drift is surfaced and sent to rollback, not quietly accepted.

    Deliberately no stop request: a camera that started itself is a fault to
    report, and stopping it would be a second unrequested mutation.
    """
    result = _restore_preview(previous, current="running", cwd=tmp_path)

    assert result.returncode != 0
    assert not (tmp_path / "calls").exists()
    assert "but is now running" in result.stderr


@pytest.mark.parametrize("previous", ["stopped", "failed"])
def test_a_producer_running_behind_a_stopped_preview_fails(
    tmp_path: Path, previous: str
) -> None:
    """Status saying stopped while a producer lives is not preservation."""
    result = _restore_preview(previous, current="stopped", rpicam=1, cwd=tmp_path)

    assert result.returncode != 0


@pytest.mark.parametrize(
    ("previous", "current"), [("running", "running"), ("stopped", "stopped")]
)
def test_a_legacy_producer_fails_in_any_final_state(
    tmp_path: Path, previous: str, current: str
) -> None:
    """A libcamera-vid means something else is holding the camera."""
    result = _restore_preview(
        previous, current=current, libcamera=1, cwd=tmp_path
    )

    assert result.returncode != 0


def test_a_stopped_preview_that_was_running_is_started_once(
    tmp_path: Path,
) -> None:
    """Exactly one start request, then a poll until it reports running."""
    preamble = (
        PREVIEW_DOUBLES
        + _producer_double(rpicam=1)
        + "read_preview_state() {\n"
        '    if [[ -f started ]]; then printf "running\\n"; '
        'else printf "stopped\\n"; fi\n'
        "}\n"
        "start_preview() { printf 'START\\n' >> calls; : > started; }\n"
    )
    result = call_gateway_function(
        'restore_preview_state "running" "status" "start" 3',
        preamble=preamble,
        cwd=tmp_path,
    )

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "calls").read_text(encoding="utf-8") == "START\n"


def test_a_duplicate_producer_fails_the_restoration(tmp_path: Path) -> None:
    """Two producers means the old one was never reaped."""
    result = _restore_preview("running", current="running", rpicam=2, cwd=tmp_path)

    assert result.returncode != 0


def test_the_gateway_never_contacts_the_stream_or_capture_endpoints() -> None:
    """Deployment restores an operating state; it does not use the camera."""
    source = _read(GATEWAY)

    assert "preview/stream" not in source
    assert "camera/capture" not in source


# --------------------------------------------------------------------------
# environment contract
# --------------------------------------------------------------------------


def test_the_environment_sync_is_always_frozen() -> None:
    """A resolving sync would let production drift off the locked set."""
    source = _read(GATEWAY)

    assert "uv sync --frozen" in source
    for line in source.splitlines():
        if "uv sync" in line and not line.strip().startswith("#"):
            assert "--frozen" in line, line


def test_a_missing_uv_is_a_hard_failure() -> None:
    """Deploying code against a stale environment is not a degraded success."""
    result = call_gateway_function(
        'require_uv_available "claude"',
        preamble="run_as_admin() { return 127; }\n",
    )

    assert result.returncode == EX_PRECONDITION
    assert "uv is not available" in result.stderr


def test_uv_availability_is_proven_before_the_checkout_moves() -> None:
    """Discovering it afterwards would mean rolling back a known problem."""
    check, merge = _ordered_indices("require_uv_available", "merge --ff-only")

    assert check < merge


def test_a_failed_sync_is_reported_to_the_caller() -> None:
    """The forward deployment stops; it does not carry on to the restart."""
    result = call_gateway_function(
        'sync_environment "claude" "/tmp/repo"',
        preamble="run_as_admin() { return 1; }\n",
    )

    assert result.returncode != 0


def test_the_runtime_probe_imports_the_application(tmp_path: Path) -> None:
    """An executable bit proves almost nothing about a deployment.

    The restart would otherwise fail with 203/EXEC or an ImportError after the
    code has already moved, and the diagnosis would happen in the journal
    instead of here.
    """
    log = tmp_path / "runuser.log"
    result = call_gateway_function(
        'require_runtime_can_execute "mgo" "/opt/garden-observatory"',
        preamble=(
            f'RUNUSER_LOG="{_posix(log)}"\n'
            'account_home() { printf "/var/lib/garden-observatory\\n"; }\n'
            'runuser() { printf \'%s\\n\' "$*" >> "$RUNUSER_LOG"; return 0; }\n'
        ),
    )
    calls = log.read_text(encoding="utf-8")

    assert result.returncode == 0, result.stderr
    assert "import mgo.core.config, mgo.api.app" in calls
    assert "MGO_CONFIG_PATH=/etc/garden-observatory/mgo.toml" in calls
    assert ".venv/bin/python" in calls
    assert ".venv/bin/uvicorn" in calls


def test_the_runtime_probe_runs_as_the_runtime_account(tmp_path: Path) -> None:
    """It answers "can *mgo* load this", not "can root load this"."""
    log = tmp_path / "runuser.log"
    call_gateway_function(
        'require_runtime_can_execute "mgo" "/opt/garden-observatory"',
        preamble=(
            f'RUNUSER_LOG="{_posix(log)}"\n'
            'account_home() { printf "/var/lib/garden-observatory\\n"; }\n'
            'runuser() { printf \'%s\\n\' "$*" >> "$RUNUSER_LOG"; return 0; }\n'
        ),
    )

    for line in log.read_text(encoding="utf-8").splitlines():
        assert line.startswith("-u mgo --"), line


def test_a_failed_runtime_import_stops_the_deployment() -> None:
    """A probe that cannot import must not be a warning."""
    result = call_gateway_function(
        'require_runtime_can_execute "mgo" "/opt/garden-observatory"',
        preamble=(
            'account_home() { printf "/var/lib/garden-observatory\\n"; }\n'
            "runuser() {\n"
            '    case "$*" in *"import mgo"*) return 1 ;; esac\n'
            "    return 0\n"
            "}\n"
        ),
    )

    assert result.returncode != 0


def test_the_runtime_probe_starts_nothing() -> None:
    """Import only: no lifespan, no camera, no stream, no writes."""
    body = _function_body("require_runtime_can_execute()", "# --- restart and")

    assert "-c" in body
    assert "import" in body
    for forbidden in ("uvicorn mgo.api.app:app", "--host", "--port", "capture"):
        assert forbidden not in body, forbidden


def test_tracked_file_drift_after_a_sync_is_detected(
    production: dict[str, object],
) -> None:
    """A sync that rewrote the lockfile is a deployment defect, not a detail."""
    checkout = production["checkout"]
    assert isinstance(checkout, Path)
    (checkout / "file.txt").write_text("drifted\n", encoding="utf-8")

    result = call_gateway_function(
        f'require_clean_after_sync "claude" "{_posix(checkout)}"',
        preamble=PASSTHROUGH_RUNNER,
    )

    assert result.returncode != 0


# --------------------------------------------------------------------------
# ordering
# --------------------------------------------------------------------------


def _ordered_indices(*needles: str) -> list[int]:
    source = _read(GATEWAY)
    body = source[source.index("action_deploy_main()") :]
    return [body.index(needle) for needle in needles]


def test_the_approval_is_validated_before_any_remote_access() -> None:
    """Authority first: nothing is asked of the network before it is known."""
    approval, remote = _ordered_indices(
        "validate_approval_file", "remote_branch_sha"
    )

    assert approval < remote


def test_the_remote_sha_is_proven_before_the_fetch() -> None:
    """A fetch first would already have named the wrong commit locally."""
    remote, match, fetch = _ordered_indices(
        "remote_branch_sha", '"$remote_sha" == "$approved"', "fetch --no-tags"
    )

    assert remote < match < fetch


def test_the_tracking_ref_is_verified_after_the_fetch() -> None:
    """What the fetch actually landed is checked, not what it promised."""
    fetch, tracking = _ordered_indices(
        "fetch --no-tags", '"$tracking" == "$approved"'
    )

    assert fetch < tracking


def test_ancestry_is_proven_before_the_checkout_is_mutated() -> None:
    """No production state changes until the move is proven safe."""
    ancestry, merge = _ordered_indices(
        "require_fast_forward_target", "merge --ff-only"
    )

    assert ancestry < merge


def test_the_fast_forward_precedes_the_frozen_sync() -> None:
    """The environment is synchronised to the code that was just deployed."""
    merge, sync = _ordered_indices("merge --ff-only", "sync_environment")

    assert merge < sync


def test_the_frozen_sync_precedes_the_restart() -> None:
    """Restarting first would start the new code against the old environment."""
    sync, restart = _ordered_indices("sync_environment", "restart_service")

    assert sync < restart


def test_health_success_precedes_preview_restoration() -> None:
    """There is no point asking a dead API to start a camera."""
    recovery, preview = _ordered_indices("await_recovery", "restore_preview_state")

    assert recovery < preview


def test_the_final_report_follows_preview_restoration() -> None:
    """The deployment is announced after the last thing it had to do."""
    preview, report = _ordered_indices("restore_preview_state", 'log "deployed')

    assert preview < report


# --------------------------------------------------------------------------
# idempotence
# --------------------------------------------------------------------------


def test_an_already_current_target_stops_before_any_mutation() -> None:
    """Idempotent by construction: the early return precedes every change."""
    already, ancestry, merge, sync, restart = _ordered_indices(
        "already at the approved target",
        "require_fast_forward_target",
        "merge --ff-only",
        "sync_environment",
        "restart_service",
    )

    assert already < ancestry < merge < sync < restart


def test_the_already_current_branch_returns_success() -> None:
    """Nothing to do is a success, not a failure."""
    source = _read(GATEWAY)
    index = source.index("already at the approved target")
    following = source[index : index + 120]

    assert "return 0" in following


# --------------------------------------------------------------------------
# restart-api contract
# --------------------------------------------------------------------------


def test_restart_api_does_not_fetch_merge_or_sync() -> None:
    """It restarts what is already deployed; it is not a deployment."""
    source = _read(GATEWAY)
    body = source[
        source.index("action_restart_api()") : source.index("action_deploy_main()")
    ]

    assert "fetch" not in body
    assert "merge" not in body
    assert "sync_environment" not in body
    assert "restore_preview_state" not in body
    assert "start_preview" not in body


def test_restart_api_requires_the_deployed_commit_to_be_approved() -> None:
    """Restarting an unapproved checkout would launder it into service."""
    source = _read(GATEWAY)
    body = source[
        source.index("action_restart_api()") : source.index("action_deploy_main()")
    ]

    assert "validate_approval_file" in body
    assert "require_restart_preconditions" in body
    assert "acquire_transaction_lock" in body
    assert "await_recovery" in body


# --------------------------------------------------------------------------
# legacy paths
# --------------------------------------------------------------------------


def test_the_install_action_is_rejected() -> None:
    """The ambiguous verb is gone, and says where each meaning went.

    Executed, not only described: a refusal that exists in the text but is not
    reached — because the word slipped back into the accepted set — refuses
    nothing.
    """
    result = run_bash(f'"{_posix(GATEWAY)}" install')

    assert result.returncode == EX_REQUEST
    assert "has been removed" in result.stderr
    assert "deploy-main" in result.stderr
    assert "install-service-identity.sh" in result.stderr

    source = _read(GATEWAY)
    index = source.index("        install)")
    body = source[index : index + 600]

    assert "die" in body
    assert "deploy-main" in body
    assert "install-service-identity.sh" in body


def test_no_feature_branch_constant_survives() -> None:
    """Nothing in the gateway is pinned to one task's branch."""
    source = _read(GATEWAY)

    assert "task-010-operations" not in source
    assert "FEATURE_BRANCH" not in source


def test_the_gateway_exposes_exactly_three_public_actions() -> None:
    """A closed set: the action parser is the whole input surface."""
    source = _read(GATEWAY)
    case_body = source[source.index('case "$action" in') :]
    actions = {
        line.strip().rstrip(")")
        for line in case_body.splitlines()
        if line.startswith("        ") and line.strip().endswith(")")
    }

    assert {"show-approval", "deploy-main", "restart-api"} <= actions
    assert "install" in actions  # present only to be refused


@pytest.mark.parametrize(
    "action", ["", "deploy", "install-service-identity", "shell", "--help"]
)
def test_an_unsupported_action_is_rejected(action: str) -> None:
    """Anything outside the closed set is refused before privilege is used."""
    result = run_bash(
        f'"{_posix(GATEWAY)}" {action}',
    )

    assert result.returncode == EX_REQUEST


def test_extra_arguments_are_rejected() -> None:
    """One word in, so no path, ref or command can ride along."""
    result = run_bash(f'"{_posix(GATEWAY)}" show-approval /etc/passwd')

    assert result.returncode == EX_REQUEST
    assert "no arguments" in result.stderr


def test_the_gateway_accepts_no_caller_supplied_production_value() -> None:
    """Fixed constants, never environment, never arguments."""
    source = _read(GATEWAY)

    for constant in (
        'readonly MGO_REPOSITORY="/opt/garden-observatory"',
        'readonly MGO_APPROVAL_FILE="/etc/garden-observatory/claude-approved-sha"',
        'readonly MGO_SERVICE="mgo.service"',
        'readonly MGO_ADMIN_ACCOUNT="claude"',
        'readonly MGO_RUNTIME_ACCOUNT="mgo"',
    ):
        assert constant in source

    code = "\n".join(
        line for line in source.splitlines() if not line.strip().startswith("#")
    )
    assert "eval" not in code
    assert "${MGO_REPOSITORY:-" not in code
    assert "${MGO_APPROVAL_FILE:-" not in code


# --------------------------------------------------------------------------
# control-plane lock
# --------------------------------------------------------------------------


EX_BUSY = 75


def _flock_double(*, busy: bool) -> str:
    """Stand in for ``flock``, which Git Bash does not ship.

    Only the kernel primitive is doubled. The shipped logic around it — opening
    descriptor 9, the exit codes, the refusal message, the ordering — still
    executes. The exclusion itself is the kernel's and is exercised for real
    during Raspberry Pi validation.
    """
    return f"flock() {{ return {1 if busy else 0}; }}\n"


def _secure_lock_double() -> str:
    """Report the lock object as root-owned and 0600.

    Windows has no POSIX ownership, so the *contract* is exercised directly by
    the lock-object tests below and stood in for here, where the subject is the
    acquisition sequence rather than the file's metadata.
    """
    return "require_secure_lock_object() { return 0; }\n"


def test_a_busy_control_plane_refuses_rather_than_waits(tmp_path: Path) -> None:
    """Non-blocking by design: a second caller cannot know what the first will do."""
    lock = tmp_path / "mgo-deployment.lock"
    result = call_gateway_function(
        f'acquire_transaction_lock "{_posix(lock)}"',
        preamble=_flock_double(busy=True) + _secure_lock_double(),
    )

    assert result.returncode == EX_BUSY
    assert "already in progress" in result.stderr
    # The double answers "busy" however it is called, so the option that makes
    # the real flock non-blocking is asserted on the shipped text: without it
    # the second caller would wait instead of reporting.
    body = _function_body("acquire_transaction_lock()", "# --- approval file")
    assert "flock -n" in body


def test_an_uncontended_lock_is_acquired(tmp_path: Path) -> None:
    """The positive case."""
    lock = tmp_path / "mgo-deployment.lock"
    result = call_gateway_function(
        f'acquire_transaction_lock "{_posix(lock)}"',
        preamble=_flock_double(busy=False) + _secure_lock_double(),
    )

    assert result.returncode == 0, result.stderr
    assert lock.exists()


def test_the_lock_directory_is_created_when_absent(tmp_path: Path) -> None:
    """/run/lock may not survive a reboot on every host."""
    lock = tmp_path / "nested" / "mgo-deployment.lock"
    result = call_gateway_function(
        f'acquire_transaction_lock "{_posix(lock)}"',
        preamble=_flock_double(busy=False) + _secure_lock_double(),
    )

    assert result.returncode == 0, result.stderr
    assert lock.parent.is_dir()


@pytest.mark.parametrize(
    "action", ["action_deploy_main", "action_restart_api"]
)
def test_every_mutating_action_takes_the_lock_first(action: str) -> None:
    """deploy-main and restart-api contend for the same checkout and service."""
    body = _function_body(f"{action}()", "\n}\n")
    executable = [
        line.strip()
        for line in body.splitlines()
        if line.strip()
        and not line.strip().startswith("#")
        and not line.strip().startswith("local ")
        and "()" not in line
    ]

    assert any("acquire_transaction_lock" in line for line in executable), action
    assert "acquire_transaction_lock" in executable[0], action


def test_show_approval_is_not_blocked_by_the_lock() -> None:
    """A read-only action must not be excluded by a running deployment."""
    body = _function_body("action_show_approval()", "action_restart_api()")

    assert "acquire_transaction_lock" not in body


def test_both_actions_and_the_installer_share_one_lock_path() -> None:
    """Two locks would exclude nothing."""
    gateway = _read(GATEWAY)
    installer = _read(INSTALLER)

    assert 'readonly MGO_LOCK_FILE="/run/lock/mgo-deployment.lock"' in gateway
    assert 'readonly LOCK_FILE="/run/lock/mgo-deployment.lock"' in installer
    assert "readonly MGO_LOCK_FD=9" in gateway
    assert "readonly LOCK_FD=9" in installer


def test_the_lock_is_held_on_a_descriptor_nothing_closes() -> None:
    """A lock released between the restart and the final check is not a lock."""
    body = _function_body("acquire_transaction_lock()", "# --- approval file")

    assert "exec 9>>" in body
    assert 'flock -n "$MGO_LOCK_FD"' in body
    assert "flock -w" not in body
    assert "while" not in body


def test_the_lock_is_never_released_before_the_process_exits() -> None:
    """Rollback is the *most* dangerous moment to let a second caller in.

    Nothing in the gateway closes or unlocks descriptor 9. The kernel releases
    it when the process ends, which is the only moment the transaction is
    genuinely over — success, ordinary failure or rollback alike.
    """
    code = "\n".join(
        line
        for line in _read(GATEWAY).splitlines()
        if not line.strip().startswith("#")
    )

    assert "9>&-" not in code
    assert "9<&-" not in code
    assert "flock -u" not in code
    assert code.count("exec 9") == 1


def _lock_check(
    tmp_path: Path, *, ownership: str = "0:0", mode: str = "600", make: str = "file"
) -> subprocess.CompletedProcess[str]:
    """Drive the shipped lock-object contract against a real path."""
    lock = tmp_path / "lock" / "mgo-deployment.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    if make == "file":
        lock.write_bytes(b"")
    elif make == "directory":
        lock.mkdir()
    stat_double = (
        "stat() {\n"
        '    case "$2" in\n'
        f"        '%u:%g') printf '{ownership}\\n' ;;\n"
        f"        '%a') printf '{mode}\\n' ;;\n"
        "        *) printf '\\n' ;;\n"
        "    esac\n"
        "}\n"
    )
    return call_gateway_function(
        f'require_secure_lock_object "{_posix(lock)}"', preamble=stat_double
    )


def test_a_root_owned_private_lock_is_accepted(tmp_path: Path) -> None:
    """0600 root:root is the only shape that keeps the lock out of reach."""
    assert _lock_check(tmp_path).returncode == 0


@pytest.mark.parametrize(
    ("label", "kwargs"),
    [
        ("world-readable", {"mode": "644"}),
        ("group-readable", {"mode": "640"}),
        ("world-writable", {"mode": "666"}),
        ("non-root owner", {"ownership": "1001:1001"}),
        ("non-root group", {"ownership": "0:984"}),
        ("directory", {"make": "directory"}),
        ("absent", {"make": "none"}),
    ],
)
def test_an_insecure_lock_object_is_refused(
    tmp_path: Path, label: str, kwargs: dict[str, str]
) -> None:
    """A readable lock file is a denial-of-deployment primitive.

    Any unprivileged process that can open it read-only can hold an exclusive
    flock on it and block every deployment and restart indefinitely.
    """
    assert _lock_check(tmp_path, **kwargs).returncode != 0, label  # type: ignore[arg-type]


def test_the_lock_symlink_refusal_precedes_the_regular_file_check() -> None:
    """A symlink to a valid file passes ``-f``, so ``-L`` must come first.

    Asserted against the shipped text: this suite runs on hosts that cannot
    create symlinks, and a skip would delete the check from the record.
    """
    body = _function_body("require_secure_lock_object()", "# Create the lock file")
    symlink_index = body.index('[[ ! -L "$lock_path" ]]')
    regular_index = body.index('[[ -f "$lock_path" ]]')

    assert symlink_index < regular_index
    # The parent directory is proven real before the lock itself is examined.
    assert body.index('[[ ! -L "$directory" ]]') < symlink_index


def test_the_installer_lock_refuses_a_symlink_too() -> None:
    """The installer enforces the identical object contract."""
    body = _installer_function_body(
        "require_secure_lock_object()", "# The one repair"
    )

    assert body.index('[[ ! -L "$lock_path" ]]') < body.index('[[ -f "$lock_path" ]]')
    assert '[[ ! -L "$directory" ]]' in body


def test_the_installer_never_replaces_an_insecure_lock_inode() -> None:
    """A legitimate holder's lock lives on the inode, so only the mode is repaired.

    Replacing the file would silently drop whatever lock is held on the old
    inode, which is the opposite of what a lock is for.
    """
    repair = _installer_function_body(
        "repair_lock_mode()", "acquire_transaction_lock()"
    )
    acquire = _installer_function_body(
        "acquire_transaction_lock()", "# --- inspection"
    )

    assert "chmod 0600" in repair
    for destructive in ("rm ", "mv ", "> \"$lock_path\"", "install "):
        assert destructive not in repair, destructive

    # An existing object is repaired at most, then re-validated before flock.
    assert "repair_lock_mode" in acquire
    assert acquire.index("repair_lock_mode") < acquire.index("flock -n")
    assert acquire.count("require_secure_lock_object") >= 2


def test_an_unsafe_lock_object_is_never_replaced() -> None:
    """Replacing it would drop a legitimate holder's lock on the old inode."""
    body = _function_body("require_secure_lock_object()", "# Create the lock file")

    assert "rm " not in body
    assert "mv " not in body
    assert ">" not in body.replace("return 1", "")


def test_the_lock_is_created_privately_and_without_clobbering(
    tmp_path: Path,
) -> None:
    """noclobber gives O_EXCL, so a simultaneous creator never loses its inode."""
    body = _function_body("create_lock_object()", "acquire_transaction_lock()")

    assert "umask 0077" in body
    assert "set -C" in body

    lock = tmp_path / "fresh.lock"
    result = call_gateway_function(f'create_lock_object "{_posix(lock)}"')

    assert result.returncode == 0, result.stderr
    assert lock.exists()


def test_the_lock_object_is_validated_before_flock() -> None:
    """Checking after taking the lock would be checking too late."""
    body = _function_body("acquire_transaction_lock()", "# --- approval file")

    assert body.index("require_secure_lock_object") < body.index("flock -n")
    assert body.index("require_secure_lock_object") < body.index("exec 9>>")


def test_the_gateway_and_installer_secure_the_same_lock_object() -> None:
    """One path, one contract, one inode."""
    gateway = _read(GATEWAY)
    installer = _read(INSTALLER)

    assert 'readonly MGO_LOCK_FILE="/run/lock/mgo-deployment.lock"' in gateway
    assert 'readonly LOCK_FILE="/run/lock/mgo-deployment.lock"' in installer
    assert "require_secure_lock_object" in gateway
    assert "require_secure_lock_object" in installer


def test_a_busy_lock_changes_nothing(tmp_path: Path) -> None:
    """A refusal must not have touched the repository, service or camera."""
    lock = tmp_path / "mgo-deployment.lock"
    result = call_gateway_function(
        f'acquire_transaction_lock "{_posix(lock)}"\nprintf "unreachable\\n"',
        preamble=_flock_double(busy=True) + _secure_lock_double(),
    )

    assert result.returncode == EX_BUSY
    assert "unreachable" not in result.stdout


def test_the_installer_takes_the_lock_before_inspecting_targets() -> None:
    """Installing replaces the file a running deployment is executing from."""
    source = _read(INSTALLER)
    body = source[source.index("run_installation()") :]

    assert body.index("acquire_transaction_lock") < body.index("target_is_current")


def test_a_busy_installer_reports_the_shared_exit_code() -> None:
    """One vocabulary for a busy control plane, whichever entry point saw it."""
    source = _read(INSTALLER)

    assert "readonly EX_BUSY=75" in source
    assert 'return "$EX_BUSY"' in source


# --------------------------------------------------------------------------
# preview baseline
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ("running", "stable"),
        ("stopped", "stable"),
        ("failed", "stable"),
        ("starting", "transient"),
        ("stopping", "transient"),
        ("unknown", "unsupported"),
        ("paused", "unsupported"),
    ],
)
def test_preview_states_are_classified(state: str, expected: str) -> None:
    """A settled camera can be a baseline; a transitioning one cannot."""
    result = call_gateway_function(f'classify_preview_state "{state}"')

    assert result.stdout.strip() == expected


@pytest.mark.parametrize("state", ["starting", "stopping"])
def test_a_transient_state_is_not_a_deployment_baseline(state: str) -> None:
    """Deploying against a camera mid-transition measures against nothing."""
    result = call_gateway_function(
        'read_stable_preview_state "url"',
        preamble=f'read_preview_state() {{ printf "{state}\\n"; }}\n',
    )

    assert result.returncode != 0


@pytest.mark.parametrize(
    ("state", "rpicam", "libcamera", "accepted"),
    [
        ("running", 1, 0, True),
        ("running", 0, 0, False),
        ("running", 2, 0, False),
        ("stopped", 0, 0, True),
        ("stopped", 1, 0, False),
        ("failed", 0, 0, True),
        ("failed", 1, 0, False),
        ("running", 1, 1, False),
        ("stopped", 0, 1, False),
        ("failed", 0, 1, False),
    ],
)
def test_the_baseline_reconciles_status_with_processes(
    state: str, rpicam: int, libcamera: int, accepted: bool
) -> None:
    """Status and processes disagreeing means nobody knows what the camera is doing.

    An inconsistent baseline is refused, never repaired: a deployment that
    "restored" it would be restoring a fiction.
    """
    result = call_gateway_function(
        f'require_preview_baseline "{state}"',
        preamble=_producer_double(rpicam=rpicam, libcamera=libcamera),
    )

    assert (result.returncode == 0) is accepted


def test_the_baseline_is_captured_only_after_reconciliation() -> None:
    """Ordering: read a stable state, reconcile it, then record it."""
    stable, reconcile, pid = _ordered_indices(
        "read_stable_preview_state", "require_preview_baseline", "previous_pid="
    )

    assert stable < reconcile < pid


def test_the_baseline_read_precedes_every_mutation() -> None:
    """Refusing an unusable baseline must happen before anything moves."""
    reconcile, merge = _ordered_indices(
        "require_preview_baseline", "merge --ff-only"
    )

    assert reconcile < merge


def test_no_preview_transition_happens_during_preflight() -> None:
    """Preflight observes; it does not start or stop a camera."""
    source = _read(GATEWAY)
    body = source[source.index("action_deploy_main()") :]
    preflight = body[: body.index("merge --ff-only")]

    assert "start_preview" not in preflight
    assert "restore_preview_state" not in preflight


# --------------------------------------------------------------------------
# fast-forward failure
# --------------------------------------------------------------------------


def test_a_failed_fast_forward_enters_the_rollback(tmp_path: Path) -> None:
    """A non-zero Git exit is not proof that nothing moved.

    The command can fail part-way through a checkout, on a permission or I/O
    error, or after the ref has already been updated.
    """
    source = _read(GATEWAY)
    body = source[source.index("action_deploy_main()") :]
    index = body.index("the fast-forward failed")

    assert "fail_before_restart" in body[index - 200 : index + 200]
    assert 'die "$EX_PRECONDITION" "the fast-forward was refused"' not in source


def test_the_transaction_opens_before_the_merge_is_invoked() -> None:
    """Everything from the merge onward is inside the transaction."""
    source = _read(GATEWAY)
    body = source[source.index("action_deploy_main()") :]
    merge = body.index("merge --ff-only")
    after = body[merge:]

    # No bare exit between the merge and the success message.
    tail = after[: after.index('log "deployed')]
    assert 'die "$EX_PRECONDITION"' not in tail
    assert 'die "$EX_DEPLOY"' not in tail


def test_the_checkout_is_verified_immediately_after_the_merge() -> None:
    """What the merge left behind is checked before anything builds on it."""
    merge, verify, sync = _ordered_indices(
        "merge --ff-only", "verify_after_fast_forward", "sync_environment"
    )

    assert merge < verify < sync


@pytest.mark.parametrize(
    ("label", "kwargs"),
    [
        ("wrong HEAD", {"head": "f" * 40}),
        ("wrong local main", {"local_main": "f" * 40}),
        ("wrong origin/main", {"tracking": "f" * 40}),
        ("wrong branch", {"branch": "feature"}),
        ("dirty tree", {"porcelain": " M src/mgo/api/app.py"}),
        ("untracked file", {"porcelain": "?? stray.txt"}),
        ("operation marker", {"in_progress": True}),
    ],
)
def test_post_fast_forward_verification_refuses_a_wrong_checkout(
    label: str, kwargs: dict[str, object]
) -> None:
    """A merge that "succeeded" into the wrong state is still a failure."""
    defaults: dict[str, object] = {
        "branch": "main",
        "head": APPROVED_SHA,
        "local_main": APPROVED_SHA,
        "tracking": APPROVED_SHA,
        "porcelain": "",
        "in_progress": False,
    }
    defaults.update(kwargs)
    preamble = (
        "git_admin() {\n"
        "    shift 2\n"
        '    case "$*" in\n'
        f'        "branch --show-current") printf \'{defaults["branch"]}\\n\' ;;\n'
        f'        "rev-parse HEAD") printf \'{defaults["head"]}\\n\' ;;\n'
        f'        "rev-parse main") printf \'{defaults["local_main"]}\\n\' ;;\n'
        f'        "rev-parse origin/main") printf \'{defaults["tracking"]}\\n\' ;;\n'
        '        "status --porcelain --untracked-files=all")'
        f" printf '{defaults['porcelain']}' ;;\n"
        "        *) printf '' ;;\n"
        "    esac\n"
        "}\n"
        "operation_in_progress() { return "
        f"{0 if defaults['in_progress'] else 1}; }}\n"
    )
    result = call_gateway_function(
        f'verify_after_fast_forward "{APPROVED_SHA}"', preamble=preamble
    )

    assert result.returncode != 0, label


def test_post_fast_forward_verification_accepts_a_correct_checkout() -> None:
    """The positive case."""
    preamble = (
        "git_admin() {\n"
        "    shift 2\n"
        '    case "$*" in\n'
        "        \"branch --show-current\") printf 'main\\n' ;;\n"
        f"        \"rev-parse HEAD\") printf '{APPROVED_SHA}\\n' ;;\n"
        f"        \"rev-parse main\") printf '{APPROVED_SHA}\\n' ;;\n"
        f"        \"rev-parse origin/main\") printf '{APPROVED_SHA}\\n' ;;\n"
        "        *) printf '' ;;\n"
        "    esac\n"
        "}\n"
        "operation_in_progress() { return 1; }\n"
    )
    result = call_gateway_function(
        f'verify_after_fast_forward "{APPROVED_SHA}"', preamble=preamble
    )

    assert result.returncode == 0, result.stderr


def test_the_pre_restart_rollback_proves_the_service_still_serves() -> None:
    """A PID and a timestamp do not say the API is answering."""
    body = _function_body(
        "fail_before_restart()", "# Branch, HEAD, tree and in-progress"
    )

    assert "rollback_repository_state_is_intact" in body
    assert "stash list" in body
    assert "service_is_active" in body
    assert "endpoint_is_ok" in body
    assert "read_stable_preview_state" in body
    assert "require_preview_baseline" in body
    assert "restart_service" not in body


def test_the_pre_restart_rollback_never_restarts_the_service() -> None:
    """Nothing disturbed it, so restarting would cause the outage."""
    body = _function_body(
        "fail_before_restart()", "# Branch, HEAD, tree and in-progress"
    )

    assert "restart_service" not in body
    assert 'die "$EX_DEPLOY"' in body
    assert "the service was never restarted" in body


# --------------------------------------------------------------------------
# branch-aware restart-api
# --------------------------------------------------------------------------


def _restart_preconditions(
    *,
    branch: str = "main",
    head: str = APPROVED_SHA,
    local_branch: str | None = None,
    porcelain: str = "",
    stash: str = "",
    worktrees: int = 1,
    remote_ok: bool = True,
    in_progress: bool = False,
    upstream: str | None = "origin/main",
    upstream_sha: str = APPROVED_SHA,
) -> subprocess.CompletedProcess[str]:
    if local_branch is None:
        local_branch = head
    upstream_case = (
        f"        *\"--symbolic-full-name\"*) printf '{upstream}\\n' ;;\n"
        if upstream is not None
        else '        *"--symbolic-full-name"*) return 1 ;;\n'
    )
    preamble = (
        "git_admin() {\n"
        "    shift 2\n"
        '    case "$*" in\n'
        f'        "branch --show-current") printf \'{branch}\\n\' ;;\n'
        f'        "rev-parse HEAD") printf \'{head}\\n\' ;;\n'
        + upstream_case
        + f'        "rev-parse {upstream}") printf \'{upstream_sha}\\n\' ;;\n'
        f'        "rev-parse {branch}") printf \'{local_branch}\\n\' ;;\n'
        '        "status --porcelain --untracked-files=all")'
        f" printf '{porcelain}' ;;\n"
        f'        "stash list") printf \'{stash}\' ;;\n'
        f'        "worktree list") printf \'%s\\n\' {"x " * worktrees} ;;\n'
        "        \"remote get-url origin\")"
        " printf 'https://github.com/emjayelle01/garden-observatory.git\\n' ;;\n"
        "        *) printf '' ;;\n"
        "    esac\n"
        "}\n"
        f"remote_matches_repository() {{ return {0 if remote_ok else 1}; }}\n"
        "operation_in_progress() { return "
        f"{0 if in_progress else 1}; }}\n"
    )
    return call_gateway_function(
        "require_restart_preconditions "
        f'"claude" "{_posix(PROJECT_ROOT)}" '
        f'"emjayelle01/garden-observatory" "origin" "{APPROVED_SHA}"',
        preamble=preamble,
    )


def test_restart_api_accepts_an_approved_main() -> None:
    """The ordinary case is unchanged by becoming branch-aware."""
    result = _restart_preconditions()

    assert result.returncode == 0, result.stderr


def test_restart_api_refuses_a_checkout_behind_its_approved_upstream() -> None:
    """The deployed commit must be the approved one, not merely related to it.

    A branch tracking the right remote at the right SHA can still have a
    working copy sitting behind it. Restarting that would launder an
    unapproved commit into service while every ref check agreed.
    """
    result = _restart_preconditions(head="b" * 40, upstream_sha=APPROVED_SHA)

    assert result.returncode == EX_PRECONDITION
    assert "not the approved SHA" in result.stderr


def test_restart_api_accepts_an_approved_feature_branch() -> None:
    """Pi validation of an approved feature SHA no longer needs bespoke steps."""
    result = _restart_preconditions(
        branch="task-013-thing", upstream="origin/task-013-thing"
    )

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("label", "kwargs"),
    [
        ("detached HEAD", {"branch": ""}),
        ("HEAD not at branch tip", {"local_branch": "a" * 40}),
        ("dirty tree", {"porcelain": " M src/mgo/api/app.py"}),
        ("untracked file", {"porcelain": "?? stray.txt"}),
        ("stash", {"stash": "stash@{0}: WIP"}),
        ("operation in progress", {"in_progress": True}),
        ("additional worktree", {"worktrees": 2}),
        ("wrong origin", {"remote_ok": False}),
        ("unapproved HEAD", {"head": "b" * 40, "local_branch": "b" * 40}),
        ("no upstream", {"upstream": None}),
        (
            "wrong upstream branch",
            {"branch": "task-013-thing", "upstream": "origin/main"},
        ),
        ("upstream SHA mismatch", {"upstream_sha": "c" * 40}),
    ],
)
def test_restart_api_refuses_every_unsafe_checkout(
    label: str, kwargs: dict[str, object]
) -> None:
    """Read-only, but not permissive."""
    result = _restart_preconditions(**kwargs)  # type: ignore[arg-type]

    assert result.returncode == EX_PRECONDITION, label


def test_restart_api_proves_the_production_path_is_canonical() -> None:
    """Restarting is privileged too.

    A symlinked component would point every check below it at a different tree
    than the one the service is about to run.
    """
    body = _function_body(
        "require_restart_preconditions()", "# --- service and endpoint state"
    )

    assert "pwd -P" in body
    assert "pwd -L" in body
    assert "resolves through a symlink" in body
    assert body.index("pwd -P") < body.index("branch --show-current")


def test_both_privileged_actions_prove_the_canonical_path() -> None:
    """The same proof, in deployment and in restart alike."""
    deployment = _function_body(
        "require_repository_preconditions()", "# An interrupted merge"
    )
    restart = _function_body(
        "require_restart_preconditions()", "# --- service and endpoint state"
    )

    for body in (deployment, restart):
        assert "pwd -P" in body
        assert "pwd -L" in body


def test_restart_api_discovers_the_branch_rather_than_accepting_one() -> None:
    """The branch comes from the checkout, never from the caller."""
    body = _function_body(
        "require_restart_preconditions()", "# --- service and endpoint state"
    )

    assert "branch --show-current" in body
    assert '"$remote/$branch"' in body
    assert "fetch" not in body
    assert "merge" not in body
    assert "sync" not in body


def test_deploy_main_remains_main_only() -> None:
    """Branch-awareness is restart-api's, and only restart-api's."""
    body = _function_body("action_deploy_main()", "\n}\n")

    assert "require_repository_preconditions" in body
    assert "require_restart_preconditions" not in body
    assert '"$MGO_BRANCH"' in body


# --------------------------------------------------------------------------
# final verification
# --------------------------------------------------------------------------


def _final_verification(
    *,
    approval: str = APPROVED_SHA,
    branch: str = "main",
    head: str = APPROVED_SHA,
    local_main: str = APPROVED_SHA,
    tracking: str = APPROVED_SHA,
    porcelain: str = "",
    stash: str = "",
    in_progress: bool = False,
    active: bool = True,
    healthy: bool = True,
    preview: str = "running",
    expected_preview: str = "running",
    rpicam: int | None = None,
    libcamera: int = 0,
) -> subprocess.CompletedProcess[str]:
    """Drive the shipped final check against a fully described world."""
    if rpicam is None:
        rpicam = 1 if expected_preview == "running" else 0
    preamble = (
        f'validate_approval_file() {{ printf "{approval}\\n"; }}\n'
        "git_admin() {\n"
        "    shift 2\n"
        '    case "$*" in\n'
        f'        "branch --show-current") printf \'{branch}\\n\' ;;\n'
        f'        "rev-parse HEAD") printf \'{head}\\n\' ;;\n'
        f'        "rev-parse main") printf \'{local_main}\\n\' ;;\n'
        f'        "rev-parse origin/main") printf \'{tracking}\\n\' ;;\n'
        f'        "status --porcelain --untracked-files=all")'
        f" printf '{porcelain}' ;;\n"
        f'        "stash list") printf \'{stash}\' ;;\n'
        "        *) printf '' ;;\n"
        "    esac\n"
        "}\n"
        f"operation_in_progress() {{ return {0 if in_progress else 1}; }}\n"
        f"service_is_active() {{ return {0 if active else 1}; }}\n"
        f"endpoint_is_ok() {{ return {0 if healthy else 1}; }}\n"
        f'read_preview_state() {{ printf "{preview}\\n"; }}\n'
        + _producer_double(rpicam=rpicam, libcamera=libcamera)
    )
    return call_gateway_function(
        f'final_verification "{APPROVED_SHA}" "{expected_preview}"',
        preamble=preamble,
    )


def test_final_verification_passes_on_a_correct_deployment() -> None:
    """The positive case, so every refusal below means something."""
    result = _final_verification()

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("label", "kwargs"),
    [
        ("approval no longer names this commit", {"approval": "b" * 40}),
        ("wrong branch", {"branch": "feature"}),
        ("wrong HEAD", {"head": "c" * 40}),
        ("wrong local main", {"local_main": "d" * 40}),
        ("wrong origin/main", {"tracking": "e" * 40}),
        ("untracked file", {"porcelain": "?? stray.txt"}),
        ("dirty tracked file", {"porcelain": " M src/mgo/api/app.py"}),
        ("stash", {"stash": "stash@{0}: WIP"}),
        ("operation in progress", {"in_progress": True}),
        ("inactive service", {"active": False}),
        ("non-200 health", {"healthy": False}),
        ("preview did not come back", {"preview": "stopped"}),
        ("wrong producer count", {"rpicam": 2}),
        ("legacy producer", {"libcamera": 1}),
    ],
)
def test_final_verification_refuses_every_wrong_end_state(
    label: str, kwargs: dict[str, object]
) -> None:
    """Each earlier step checked its own outcome; this checks the result."""
    result = _final_verification(**kwargs)  # type: ignore[arg-type]

    assert result.returncode != 0, label
    assert "final check" in result.stderr, label


def test_final_verification_refuses_a_preview_that_should_not_be_running() -> None:
    """A previously stopped preview must not be running at the end.

    The producer count is deliberately consistent with the reported state, so
    that this exercises the state comparison alone. With a producer running too
    the orphan-producer check below would catch it first, and the comparison
    could be deleted unnoticed.
    """
    result = _final_verification(
        expected_preview="stopped", preview="running", rpicam=0
    )

    assert result.returncode != 0
    assert "final check" in result.stderr


def test_final_verification_accepts_a_preserved_non_running_preview() -> None:
    """failed -> stopped is preservation: it is matched on running-ness."""
    result = _final_verification(
        expected_preview="failed", preview="stopped", rpicam=0
    )

    assert result.returncode == 0, result.stderr


def test_final_verification_refuses_an_orphan_producer() -> None:
    """A producer with no preview behind it is not a preserved state."""
    result = _final_verification(
        expected_preview="stopped", preview="stopped", rpicam=1
    )

    assert result.returncode != 0


def test_final_verification_runs_after_preview_restoration() -> None:
    """Ordering: restore, then verify, then claim."""
    preview, final, success = _ordered_indices(
        "restore_preview_state", "final_verification", 'log "deployed'
    )

    assert preview < final < success


def test_no_success_is_printed_before_final_verification() -> None:
    """"deployed" is a conclusion, and only one stage may draw it."""
    source = _read(GATEWAY)
    body = source[source.index("action_deploy_main()") :]
    before = body[: body.index("final_verification")]

    assert 'log "deployed' not in before


def test_a_final_verification_failure_rolls_back() -> None:
    """It is a deployment failure like any other."""
    source = _read(GATEWAY)
    body = source[source.index("action_deploy_main()") :]
    index = body.index("final verification failed")

    assert "fail_after_restart" in body[index - 200 : index + 200]


def test_every_cleanliness_check_includes_untracked_files() -> None:
    """An untracked file is drift the deployment must not carry."""
    source = _read(GATEWAY)

    assert "--untracked-files=no" not in source
    # Every executable ``status --porcelain`` carries the flag, checked by
    # pairing the counts rather than by a lower bound. A lower bound is
    # satisfied by dropping one of them, and dropping one is exactly the defect
    # this asserts against: one place reading a different cleanliness from the
    # rest. Comment lines are stripped because they name the flag too.
    body = "\n".join(
        line for line in source.splitlines() if not line.strip().startswith("#")
    )
    assert body.count("status --porcelain") == 7
    assert body.count("--untracked-files=all") == 7


# --------------------------------------------------------------------------
# transaction rollback
# --------------------------------------------------------------------------


EX_ROLLBACK = 78


def _rollback_repository(
    checkout: Path, previous_sha: str, *, extra: str = ""
) -> subprocess.CompletedProcess[str]:
    preamble = PASSTHROUGH_RUNNER + "sync_environment() { return 0; }\n" + extra
    call = (
        f'rollback_repository "claude" "{_posix(checkout)}" '
        f'"{previous_sha}" "main" || true\n'
        'printf "stage=%s\\n" "$ROLLBACK_STAGE"\n'
    )
    return call_gateway_function(call, preamble=preamble)


def test_a_rollback_restores_the_exact_captured_commit(
    production: dict[str, object],
) -> None:
    """The checkout goes back to the commit recorded before the mutation."""
    upstream = production["upstream"]
    checkout = production["checkout"]
    assert isinstance(upstream, Path)
    assert isinstance(checkout, Path)
    previous = str(production["first"])
    _commit(upstream, "second")
    _git(checkout, "fetch", "--quiet", "origin", "main")
    _git(checkout, "merge", "--ff-only", "--quiet", "origin/main")
    assert _git(checkout, "rev-parse", "HEAD") != previous

    result = _rollback_repository(checkout, previous)

    assert result.returncode == 0, result.stderr
    assert "stage=" in result.stdout
    assert _git(checkout, "rev-parse", "HEAD") == previous
    assert _git(checkout, "branch", "--show-current") == "main"
    assert _git(checkout, "status", "--porcelain") == ""


def test_a_rollback_names_the_stage_that_failed(
    production: dict[str, object],
) -> None:
    """"It failed" is not an operator report; where it failed is."""
    checkout = production["checkout"]
    assert isinstance(checkout, Path)

    result = _rollback_repository(
        checkout,
        str(production["first"]),
        extra="sync_environment() { return 1; }\n",
    )

    assert "stage=environment" in result.stdout


def test_a_rollback_that_cannot_move_the_checkout_reports_that_stage(
    tmp_path: Path,
) -> None:
    """A failure to restore the commit is reported as the checkout stage."""
    result = _rollback_repository(tmp_path / "absent", "0" * 40)

    assert "stage=checkout" in result.stdout


def test_a_rollback_verifies_rather_than_assumes(
    production: dict[str, object],
) -> None:
    """Restoration is proven against HEAD, branch and cleanliness."""
    source = _read(GATEWAY)
    body = source[source.index("rollback_repository()") :]
    body = body[: body.index("fail_before_restart()")]

    assert 'ROLLBACK_STAGE="verification"' in body
    assert "rev-parse HEAD" in body
    assert "branch --show-current" in body
    assert "status --porcelain" in body
    # And the values are actually compared. Reading three facts and then not
    # looking at them is how "verifies" quietly becomes "assumes".
    assert '"$head" != "$previous_sha"' in body
    assert '"$current_branch" != "$branch"' in body
    assert '-n "$porcelain"' in body


def test_only_the_rollback_path_uses_reset() -> None:
    """The forward deployment is a fast-forward; reset exists for going back."""
    source = _read(GATEWAY)
    occurrences = [
        line
        for line in source.splitlines()
        if "reset --hard" in line and not line.strip().startswith("#")
    ]

    assert len(occurrences) == 1
    assert "reset --hard" in _function_body(
        "restore_checkout()", "rollback_repository()"
    )


def test_a_pre_restart_failure_does_not_restart_the_service() -> None:
    """Nothing disturbed the service, so recovering it would cause the outage."""
    source = _read(GATEWAY)
    body = source[
        source.index("fail_before_restart()") : source.index("fail_after_restart()")
    ]

    assert "restart_service" not in body
    assert "restore_preview_state" not in body
    assert "service_main_pid" in body
    assert "service_active_enter_timestamp" in body


def test_a_pre_restart_rollback_still_reports_failure() -> None:
    """A recovered deployment is still a failed deployment."""
    source = _read(GATEWAY)
    body = source[
        source.index("fail_before_restart()") : source.index("fail_after_restart()")
    ]

    assert 'die "$EX_DEPLOY"' in body
    assert "rollback succeeded" in body
    assert "return 0" not in body


def test_a_post_restart_rollback_restarts_once_and_restores_preview() -> None:
    """The service is already on the new code, so it has to come back."""
    source = _read(GATEWAY)
    body = source[source.index("fail_after_restart()") :]
    body = body[: body.index("# --- actions")]

    assert body.count("restart_service") == 1
    assert "await_recovery" in body
    assert "restore_preview_state" in body
    assert 'die "$EX_DEPLOY" "deployment failed; rollback succeeded"' in body


def test_a_failed_rollback_uses_a_distinct_high_severity_code() -> None:
    """Not restored is a different outcome from restored, and says so."""
    source = _read(GATEWAY)

    assert "readonly EX_ROLLBACK=78" in source
    for body_name in ("fail_before_restart()", "fail_after_restart()"):
        index = source.index(body_name)
        body = source[index : index + 2000]
        assert 'die "$EX_ROLLBACK"' in body
        assert "was NOT" in body


def test_a_failed_rollback_never_claims_production_was_restored() -> None:
    """Every rollback-failure message says the opposite, explicitly."""
    source = _read(GATEWAY)

    for line in source.splitlines():
        if "$EX_ROLLBACK" in line and "readonly" not in line:
            continue
    messages = [
        line
        for line in source.splitlines()
        if "production was NOT" in line or "production was NOT fully" in line
    ]

    assert len(messages) >= 5


def test_the_rollback_does_not_loop() -> None:
    """One attempt. A retrying rollback turns one failure into a flap."""
    for name, following in (
        ("fail_before_restart()", "# Branch, HEAD, tree and in-progress"),
        ("fail_after_restart()", "# --- actions"),
    ):
        # Executable lines only: the prose explaining *why* there is no retry
        # naturally contains the words a naive scan would trip on.
        body = "\n".join(
            line
            for line in _function_body(name, following).splitlines()
            if not line.strip().startswith("#")
        )
        assert "while" not in body
        assert "until" not in body
        # Neither handler calls the other, or itself, a second time.
        assert body.count("rollback_repository ") == 1


def test_the_rollback_handlers_take_their_target_only_from_their_caller() -> None:
    """No environment default may stand behind the captured SHA.

    Checked inside the handlers, not only at their call sites: a
    ``${SOMETHING:-$2}`` in either body would let an environment variable
    choose what production is restored to, and the call-site test above would
    not see it.
    """
    for name, following in (
        ("fail_before_restart()", "# Branch, HEAD, tree and in-progress"),
        ("fail_after_restart()", "# --- actions"),
    ):
        body = _function_body(name, following)
        assert 'local previous_sha="$2"' in body, name
        assert ":-$2" not in body, name
        assert "MGO_ROLLBACK" not in body, name

    for name, following in (
        ("restore_checkout()", "rollback_repository()"),
        ("rollback_repository()", "# §16.1"),
    ):
        body = _function_body(name, following)
        assert ":-" not in body, name


def test_no_production_value_falls_back_to_the_environment() -> None:
    """``${VAR:-default}`` is how a fixed constant quietly becomes tunable."""
    allowed = {
        "${SUDO_USER:-}",
        "${1:-}",
        "${BASH_SOURCE[0]}",
        # The environment boundary reads these three in order to *replace*
        # them, and tolerates their absence rather than tripping ``set -u``.
        "${PATH:-}",
        "${HOME:-}",
        "${LC_ALL:-}",
        "${TMPDIR:-}",
        "${MGO_ENVIRONMENT_CONSTRUCTED:-}",
        # The established parameter-for-testability pattern: the default is the
        # production constant, and no production call site passes an argument.
        "${1:-$MGO_ROOT_TMPDIR}",
    }
    for line in _read(GATEWAY).splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or ":-" not in stripped:
            continue
        assert any(token in stripped for token in allowed), stripped


def test_no_rollback_target_is_accepted_from_a_caller() -> None:
    """The only SHA a rollback moves to is the one this run recorded."""
    source = _read(GATEWAY)
    body = source[source.index("action_deploy_main()") :]

    for call in ("fail_before_restart ", "fail_after_restart "):
        for fragment in body.split(call)[1:]:
            arguments = fragment[: fragment.index("\n    fi")]
            assert '"$head"' in arguments, arguments
            assert "$1" not in arguments


@pytest.mark.parametrize(
    ("reason", "handler"),
    [
        ("the frozen dependency sync failed", "fail_before_restart"),
        ("the sync changed a tracked file", "fail_before_restart"),
        (
            "the runtime account cannot execute the deployed environment",
            "fail_before_restart",
        ),
        ("the service restart failed", "fail_after_restart"),
        ("the service did not recover within the bound", "fail_after_restart"),
        ("the preview state could not be restored", "fail_after_restart"),
    ],
)
def test_every_post_mutation_failure_is_routed_to_a_rollback(
    reason: str, handler: str
) -> None:
    """No failure after the fast-forward exits without restoring."""
    source = _read(GATEWAY)
    body = source[source.index("action_deploy_main()") :]
    index = body.index(reason)

    assert handler in body[max(0, index - 200) : index + 200]


def test_no_post_mutation_failure_exits_without_rolling_back() -> None:
    """The mutation boundary is where bare ``die`` stops being acceptable."""
    source = _read(GATEWAY)
    body = source[source.index("action_deploy_main()") :]
    after_mutation = body[body.index("merge --ff-only") :]
    after_mutation = after_mutation[: after_mutation.index('log "deployed')]

    assert 'die "$EX_DEPLOY"' not in after_mutation


# --------------------------------------------------------------------------
# shipped asset hygiene
# --------------------------------------------------------------------------


SHELL_ASSETS = (GATEWAY, INSTALLER, UPDATE_MAIN)


@pytest.mark.parametrize("asset", SHELL_ASSETS, ids=lambda path: path.name)
def test_every_shell_asset_parses(asset: Path) -> None:
    """``bash -n`` on what is actually shipped."""
    result = subprocess.run(
        [_bash(), "-n", str(asset)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "asset", (*SHELL_ASSETS, SUDOERS), ids=lambda path: path.name
)
def test_every_deployment_asset_is_lf_only(asset: Path) -> None:
    """A CR after the shebang would make the kernel hunt for ``bash\\r``."""
    assert b"\r\n" not in asset.read_bytes()


@pytest.mark.parametrize("asset", SHELL_ASSETS, ids=lambda path: path.name)
def test_every_shell_asset_uses_strict_mode(asset: Path) -> None:
    """Strict mode is the repository's standing convention for shell."""
    text = _read(asset)

    assert "set -Eeuo pipefail" in text or "set -euo pipefail" in text


@pytest.mark.parametrize("asset", SHELL_ASSETS, ids=lambda path: path.name)
def test_every_shell_asset_names_a_fixed_interpreter(asset: Path) -> None:
    """``/usr/bin/env bash`` picks the interpreter out of the environment.

    The kernel runs env, which searches an inherited PATH for something called
    bash — so the choice of interpreter would be made by the caller's
    environment before a single statement of the script could sanitise
    anything. Isolation has to begin at process entry.
    """
    shebang = _read(asset).splitlines()[0]

    assert shebang.startswith("#!/bin/bash"), shebang
    assert "/usr/bin/env" not in shebang


@pytest.mark.parametrize(
    "asset", (GATEWAY, INSTALLER), ids=lambda path: path.name
)
def test_every_privileged_asset_runs_in_privileged_bash(asset: Path) -> None:
    """``-p`` is the only thing that closes the window before line one.

    Bash reads ``$BASH_ENV`` and ``$ENV``, imports exported shell functions and
    honours ``SHELLOPTS``, ``BASHOPTS``, ``CDPATH`` and ``GLOBIGNORE`` while it
    is starting up. All of that has happened before the script's first
    statement, so re-executing afterwards cannot undo it. Privileged mode is
    what stops it happening at all.
    """
    text = _read(asset)

    assert text.splitlines()[0] == "#!/bin/bash -p"
    # The re-execution has to stay privileged too, or the boundary is only as
    # strong as the process that happened to be entered first.
    assert '/bin/bash -p "$0" "$@"' in text


def test_the_unprivileged_wrapper_does_no_privileged_work() -> None:
    """It may run as an ordinary shell because it does nothing as root."""
    text = _read(UPDATE_MAIN)
    body = "\n".join(
        line for line in text.splitlines() if not line.strip().startswith("#")
    )

    assert text.splitlines()[0] == "#!/bin/bash"
    assert "exec sudo" in body
    for forbidden in ("git ", "uv ", "systemctl", "chmod", "chown", "visudo"):
        assert forbidden not in body, forbidden
    # And what it tells an operator to run is the direct form. Naming the
    # interpreter would discard the installer's shebang, and with it the
    # privileged mode that closes the BASH_ENV window.
    assert "sudo ./scripts/deploy/install-mgo-validate.sh" in body
    assert "sudo bash" not in text


@pytest.mark.parametrize("asset", SHELL_ASSETS, ids=lambda path: path.name)
def test_every_shell_asset_is_executable_in_git(asset: Path) -> None:
    """The installer copies the mode Git records, so Git must record it."""
    relative = asset.relative_to(PROJECT_ROOT).as_posix()
    listing = subprocess.run(
        ["git", "ls-files", "--stage", relative],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    assert listing, f"{relative} is not tracked"
    assert listing.startswith("100755"), listing


def test_the_sudoers_policy_is_not_executable_in_git() -> None:
    """A policy file is data; it is installed 0440 and never run."""
    relative = SUDOERS.relative_to(PROJECT_ROOT).as_posix()
    listing = subprocess.run(
        ["git", "ls-files", "--stage", relative],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    assert listing.startswith("100644"), listing


# --------------------------------------------------------------------------
# sudoers policy
# --------------------------------------------------------------------------


def _sudoers_rules() -> list[str]:
    return [
        line.strip()
        for line in _read(SUDOERS).splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def test_the_sudoers_policy_grants_one_account_one_path() -> None:
    """The narrowest rule that still works.

    Exactly one command alias, naming exactly one absolute path, and exactly
    one grant against it. Everything else in the file is a ``Defaults!`` line
    that only ever *removes* environment.
    """
    rules = _sudoers_rules()
    aliases = [rule for rule in rules if rule.startswith("Cmnd_Alias ")]
    grants = [rule for rule in rules if "ALL=" in rule]
    defaults = [rule for rule in rules if rule.startswith("Defaults")]

    assert aliases == ["Cmnd_Alias MGO_VALIDATE = /usr/local/sbin/mgo-validate"]
    assert grants == ["claude ALL=(root) NOPASSWD: MGO_VALIDATE"]
    assert len(rules) == len(aliases) + len(grants) + len(defaults)
    for default in defaults:
        assert default.startswith("Defaults!MGO_VALIDATE "), default
        assert "env_reset" in default or "env_delete +=" in default, default


def test_the_sudoers_policy_resets_the_environment_for_this_command() -> None:
    """The earliest point the environment can be cut down, and the only one
    that also covers the loader.

    ``LD_PRELOAD`` and ``LD_LIBRARY_PATH`` take effect before the interpreter
    exists, so no shell script can defend against them — that part of the
    boundary belongs to sudo and to the operating system. The gateway's own
    ``bash -p`` and constructed environment come later and cannot speak for it.
    """
    rules = "\n".join(_sudoers_rules())

    assert "Defaults!MGO_VALIDATE env_reset" in rules
    for variable in (
        "BASH_ENV",
        "ENV",
        "SHELLOPTS",
        "BASHOPTS",
        "CDPATH",
        "GLOBIGNORE",
        "PYTHONPATH",
        "PYTHONHOME",
        "PYTHONINSPECT",
        "PYTHONSTARTUP",
        "LD_PRELOAD",
        "LD_LIBRARY_PATH",
        "GIT_DIR",
        "GIT_WORK_TREE",
        "UV_PROJECT",
        "CURL_HOME",
        "TMPDIR",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
    ):
        assert variable in rules, variable


def test_the_sudoers_policy_grants_no_setenv() -> None:
    """SETENV would hand back exactly what env_reset removes.

    With it, ``sudo BASH_ENV=/tmp/evil mgo-validate`` would be permitted and
    the whole boundary above would be a suggestion.
    """
    rules = "\n".join(_sudoers_rules())

    assert "SETENV" not in rules
    assert "setenv" not in rules


@pytest.mark.parametrize(
    "forbidden",
    [
        "/bin/bash",
        "/bin/sh",
        "/usr/bin/git",
        "/usr/local/bin/uv",
        "/usr/bin/systemctl",
        "install-service-identity",
        "ALL=(ALL)",
        "%",
        "*",
    ],
)
def test_the_sudoers_policy_grants_nothing_else(forbidden: str) -> None:
    """No shell, no arbitrary tool, no wildcard, no group."""
    rules = "\n".join(
        line
        for line in _read(SUDOERS).splitlines()
        if line.strip() and not line.strip().startswith("#")
    )

    assert forbidden not in rules


def test_the_sudoers_policy_names_neither_the_runtime_account_nor_a_group() -> None:
    """The service account must never be able to deploy its own code."""
    rules = "\n".join(
        line
        for line in _read(SUDOERS).splitlines()
        if line.strip() and not line.strip().startswith("#")
    )

    assert "mgo" not in rules.replace("/usr/local/sbin/mgo-validate", "")


# --------------------------------------------------------------------------
# installer
# --------------------------------------------------------------------------


EX_RESTORE_FAILED = 78

# Doubles that let the real transaction run on a filesystem without root.
#
# ``install`` is re-implemented rather than skipped so install_file's real
# control flow — mktemp, copy, rename, temp cleanup — still executes; only the
# ownership change, which no unprivileged process can perform, is emulated.
INSTALLER_DOUBLES = """
install() {
    local mode=""
    local directory=0
    local paths=()
    while [[ $# -gt 0 ]]; do
        case "$1" in
            -o|-g) shift 2 ;;
            -m) mode="$2"; shift 2 ;;
            -d) directory=1; shift ;;
            *) paths+=("$1"); shift ;;
        esac
    done
    printf 'install\\n' >> "$CALL_LOG"
    if [[ "$directory" -eq 1 ]]; then
        mkdir -p "${paths[0]}" || return 1
        return 0
    fi
    cp -f "${paths[0]}" "${paths[1]}" || return 1
    return 0
}
mv() { printf 'mv\\n' >> "$CALL_LOG"; command mv "$@"; }
mktemp() { printf 'mktemp\\n' >> "$CALL_LOG"; command mktemp "$@"; }
cp() { printf 'cp\\n' >> "$CALL_LOG"; command cp "$@"; }
rm() { printf 'rm\\n' >> "$CALL_LOG"; command rm "$@"; }
visudo() { return 0; }
file_metadata() {
    is_regular_file "$1" || return 1
    case "$1" in
        *sudoers*) printf '0:0:440\\n' ;;
        *) printf '0:0:755\\n' ;;
    esac
}
"""


def _installer_stat_double(parent_owner: str = "0:0", parent_mode: str = "700") -> str:
    """Stand in for ``stat`` across the whole installer transaction.

    Windows has no POSIX owner or mode, so the values the shipped checks
    compare against are supplied here and the refusal logic executes for real.
    The workspace and the files staged inside it have their own expected
    values — ``0700`` and ``0600`` — which is the point: a single blanket
    answer would let one of the two checks pass for the wrong reason.
    """
    return (
        "stat() {\n"
        '    local format="$2" path="$3"\n'
        '    case "$format" in\n'
        "        '%u:%g')\n"
        '            case "$path" in\n'
        "                */run.??????/*|*/run.??????) printf '0:0\\n' ;;\n"
        f"                *) printf '{parent_owner}\\n' ;;\n"
        "            esac\n"
        "            ;;\n"
        "        '%a')\n"
        '            case "$path" in\n'
        "                */run.??????/*) printf '600\\n' ;;\n"
        "                */run.??????) printf '700\\n' ;;\n"
        f"                *) printf '{parent_mode}\\n' ;;\n"
        "            esac\n"
        "            ;;\n"
        "        *) printf '\\n' ;;\n"
        "    esac\n"
        "}\n"
    )


def _install_pair(
    tmp_path: Path,
    *,
    extra: str = "",
    gateway_body: str = "#!/usr/bin/env bash\ntrue\n",
    sudoers_body: str = "claude ALL=(root) NOPASSWD: /usr/local/sbin/mgo-validate\n",
    existing_gateway: str | None = None,
    existing_sudoers: str | None = None,
) -> tuple[subprocess.CompletedProcess[str], Path, Path]:
    """Run the shipped installation transaction against temporary targets."""
    sources = tmp_path / "src"
    targets = tmp_path / "dst"
    sources.mkdir()
    targets.mkdir()
    gateway_source = sources / "mgo-validate"
    sudoers_source = sources / "mgo-validate.sudoers"
    gateway_source.write_bytes(gateway_body.encode("utf-8"))
    sudoers_source.write_bytes(sudoers_body.encode("utf-8"))

    gateway_target = targets / "mgo-validate"
    sudoers_target = targets / "mgo-validate.sudoers"
    if existing_gateway is not None:
        gateway_target.write_bytes(existing_gateway.encode("utf-8"))
    if existing_sudoers is not None:
        sudoers_target.write_bytes(existing_sudoers.encode("utf-8"))

    script = (
        "set +e\n"
        f'export CALL_LOG="{_posix(tmp_path / "calls.log")}"\n'
        ': > "$CALL_LOG"\n'
        f'source "{_posix(INSTALLER)}"\n'
        "set +e\n"
        f"{INSTALLER_DOUBLES}\n"
        f"{_installer_stat_double()}\n"
        # install(1) is doubled, so the transaction parent is made directly.
        # The per-run workspace inside it is created by the shipped function.
        'open_transaction_parent() { mkdir -p "$1"; }\n'
        f"{extra}\n"
        "install_pair "
        f'"{_posix(gateway_source)}" "{_posix(gateway_target)}" "0755" '
        f'"{_posix(sudoers_source)}" "{_posix(sudoers_target)}" "0440" '
        f'"{_posix(tmp_path / "transaction")}"\n'
        'printf "outcome=%s\\n" "$?"\n'
    )
    return run_bash(script), gateway_target, sudoers_target


def test_the_installer_installs_both_targets(tmp_path: Path) -> None:
    """The positive case, so every failure path below means something."""
    result, gateway, sudoers = _install_pair(tmp_path)

    assert "outcome=0" in result.stdout, result.stderr
    assert gateway.exists()
    assert sudoers.exists()


@pytest.mark.parametrize(
    ("label", "double"),
    [
        ("temporary-file creation", "mktemp() { return 1; }"),
        (
            "gateway content copy",
            'install() { case "$*" in *mgo-validate.*) return 1 ;; esac; return 0; }',
        ),
        # Scoped to the first rename: a blanket failure would also break the
        # restoration, which is a different outcome (78) tested separately.
        (
            "gateway rename",
            "MV_FAILS=1\nmv() {\n"
            '    if [[ "$MV_FAILS" -eq 1 ]]; then MV_FAILS=0; return 1; fi\n'
            '    command mv "$@"\n}',
        ),
        # Scoped to the installed targets: the sources must still checksum
        # normally, or "expected" and "actual" would agree on the wrong value.
        (
            "post-install checksum",
            "file_checksum() {\n"
            '    [[ -f "$1" ]] || return 1\n'
            '    case "$1" in\n'
            "        */dst/*) printf 'nomatch\\n' ;;\n"
            '        *) command sha256sum "$1" | awk \'{ print $1 }\' ;;\n'
            "    esac\n}",
        ),
        # Scoped to the gateway target alone, for the mirror-image reason: a
        # double that fails both would be caught by the sudoers branch, so the
        # gateway verification could be deleted unnoticed.
        (
            "gateway-only post-install checksum",
            "file_checksum() {\n"
            '    [[ -f "$1" ]] || return 1\n'
            '    case "$1" in\n'
            "        */dst/mgo-validate) printf 'nomatch\\n' ;;\n"
            '        *) command sha256sum "$1" | awk \'{ print $1 }\' ;;\n'
            "    esac\n}",
        ),
        # Scoped to the sudoers target alone. A double that fails both targets
        # trips the gateway branch first, so the sudoers verification branch
        # would never be reached and could be deleted unnoticed.
        (
            "sudoers-only post-install checksum",
            "file_checksum() {\n"
            '    [[ -f "$1" ]] || return 1\n'
            '    case "$1" in\n'
            "        */dst/*sudoers*) printf 'nomatch\\n' ;;\n"
            '        *) command sha256sum "$1" | awk \'{ print $1 }\' ;;\n'
            "    esac\n}",
        ),
        ("post-install metadata", "file_metadata() { printf 'pi:pi:777\\n'; }"),
        (
            "sudoers-only post-install metadata",
            "file_metadata() {\n"
            '    [[ -e "$1" ]] || return 1\n'
            '    case "$1" in\n'
            "        */dst/*sudoers*) printf 'pi:pi:777\\n' ;;\n"
            "        *sudoers*) printf 'root:root:440\\n' ;;\n"
            "        *) printf 'root:root:755\\n' ;;\n"
            "    esac\n}",
        ),
        (
            "installed policy validation",
            'policy_is_valid() { case "$1" in */dst/*) return 1 ;; esac; return 0; }',
        ),
    ],
)
def test_every_installer_failure_restores_the_previous_pair(
    tmp_path: Path, label: str, double: str
) -> None:
    """The exact original pair comes back — including "absent" as a state."""
    previous_gateway = "#!/usr/bin/env bash\n# the old gateway\n"
    previous_sudoers = "# the old policy\n"
    result, gateway, sudoers = _install_pair(
        tmp_path,
        extra=double,
        existing_gateway=previous_gateway,
        existing_sudoers=previous_sudoers,
    )

    assert "outcome=1" in result.stdout, f"{label}: {result.stdout}{result.stderr}"
    assert gateway.read_text(encoding="utf-8") == previous_gateway, label
    assert sudoers.read_text(encoding="utf-8") == previous_sudoers, label


@pytest.mark.parametrize(
    ("label", "double"),
    [
        (
            "gateway rename",
            "MV_FAILS=1\nmv() {\n"
            '    if [[ "$MV_FAILS" -eq 1 ]]; then MV_FAILS=0; return 1; fi\n'
            '    command mv "$@"\n}',
        ),
        (
            "installed policy validation",
            'policy_is_valid() { case "$1" in */dst/*) return 1 ;; esac; return 0; }',
        ),
    ],
)
def test_a_failure_with_no_previous_pair_removes_what_it_installed(
    tmp_path: Path, label: str, double: str
) -> None:
    """Absent is a recorded state, and restoring it means removal."""
    result, gateway, sudoers = _install_pair(tmp_path, extra=double)

    assert "outcome=1" in result.stdout, label
    assert not gateway.exists(), label
    assert not sudoers.exists(), label


def test_a_sudoers_failure_also_restores_the_gateway(tmp_path: Path) -> None:
    """A gateway with no policy is half a control plane, so both go back."""
    previous_gateway = "#!/usr/bin/env bash\n# the old gateway\n"
    result, gateway, sudoers = _install_pair(
        tmp_path,
        extra=(
            'install() { case "$*" in *sudoers*) return 1 ;; esac;'
            ' cp -f "${@: -2:1}" "${@: -1}"; }'
        ),
        existing_gateway=previous_gateway,
    )

    assert "outcome=1" in result.stdout, result.stdout + result.stderr
    assert gateway.read_text(encoding="utf-8") == previous_gateway
    assert not sudoers.exists()


def test_a_failed_restoration_is_reported_distinctly(tmp_path: Path) -> None:
    """Not restored is a different outcome from restored, and exits 78."""
    result, _gateway, _sudoers = _install_pair(
        tmp_path,
        extra=(
            'policy_is_valid() { case "$1" in */dst/*) return 1 ;; esac; return 0; }\n'
            "restore_one() { return 1; }"
        ),
        existing_gateway="#!/usr/bin/env bash\nold\n",
        existing_sudoers="# old\n",
    )

    assert f"outcome={EX_RESTORE_FAILED}" in result.stdout
    assert "was NOT restored" in result.stderr


def test_install_file_removes_its_temporary_file_on_failure(
    tmp_path: Path,
) -> None:
    """A failed install must not leave a half-written sibling behind."""
    target_dir = tmp_path / "dst"
    target_dir.mkdir()
    source = tmp_path / "source"
    source.write_text("content\n", encoding="utf-8")

    result = run_bash(
        "set +e\n"
        f'export CALL_LOG="{_posix(tmp_path / "calls.log")}"\n'
        ': > "$CALL_LOG"\n'
        f'source "{_posix(INSTALLER)}"\n'
        "set +e\n"
        f"{INSTALLER_DOUBLES}\n"
        "mv() { return 1; }\n"
        f'install_file "{_posix(source)}" "{_posix(target_dir / "target")}" "0755"\n'
        'printf "outcome=%s\\n" "$?"\n'
    )

    assert "outcome=1" in result.stdout
    assert list(target_dir.iterdir()) == []


def test_a_fully_current_installation_mutates_nothing(tmp_path: Path) -> None:
    """Correct files are verified and left alone — same inode, same mtime.

    The run does stage a snapshot of the sources into its own workspace, and
    must: "current" now means "matches the bytes this run validated", which
    cannot be decided before those bytes exist. What it must not do is touch a
    target — no ``install``, no rename, no new inode, no new timestamp.
    """
    targets = tmp_path / "dst"
    targets.mkdir()
    gateway_target = targets / "mgo-validate"
    sudoers_target = targets / "mgo-validate.sudoers"
    gateway_target.write_bytes(GATEWAY_SOURCE_BODY)
    sudoers_target.write_bytes(SUDOERS_SOURCE_BODY)
    before = {
        path: (path.stat().st_ino, path.stat().st_mtime_ns)
        for path in (gateway_target, sudoers_target)
    }

    result, _gateway, _sudoers, transaction = _run_installation(
        tmp_path, current=True
    )
    calls = (tmp_path / "calls.log").read_text(encoding="utf-8")

    assert "outcome=0" in result.stdout, result.stdout + result.stderr
    assert "nothing changed" in result.stdout
    # Two ``install`` calls, both of them staging into this run's workspace,
    # and no rename at all: ``install_file`` is the only thing that renames,
    # and it is what publishes a target.
    assert calls.count("install") == 2, f"a target was installed: {calls!r}"
    assert "mv" not in calls, f"a target was renamed: {calls!r}"
    for path, (inode, mtime) in before.items():
        assert path.stat().st_ino == inode, path.name
        assert path.stat().st_mtime_ns == mtime, path.name
    # The staging workspace is this run's, and it goes when the run does.
    assert list(transaction.iterdir()) == []


def test_matching_content_with_wrong_metadata_is_not_current(
    tmp_path: Path,
) -> None:
    """Right bytes, wrong owner or mode, is a defect to repair, not "current"."""
    target = tmp_path / "mgo-validate"
    target.write_bytes(b"content\n")

    result = run_bash(
        "set +e\n"
        f'source "{_posix(INSTALLER)}"\n'
        "set +e\n"
        "file_metadata() { printf 'pi:pi:777\\n'; }\n"
        "file_checksum() { printf 'abc\\n'; }\n"
        f'target_is_current "{_posix(target)}" "abc" "0755"\n'
        'printf "outcome=%s\\n" "$?"\n'
    )

    assert "outcome=1" in result.stdout


# --- source and target types -----------------------------------------------


def _type_double(unsafe: str) -> str:
    """Report one path as a non-regular type.

    Windows cannot create symlinks, FIFOs, sockets or devices, so the *type* is
    emulated while the shipped refusal logic executes for real. The directory
    case below is a genuine filesystem execution on every host, and all of them
    execute for real during Raspberry Pi validation.
    """
    return (
        "is_regular_file() {\n"
        # Anchored at the end: "/dst/mgo-validate" must not also match
        # "/dst/mgo-validate.sudoers".
        f'    case "$1" in *{unsafe}) return 1 ;; esac\n'
        '    [[ ! -L "$1" ]] || return 1\n'
        '    [[ -f "$1" ]]\n'
        "}\n"
    )


@pytest.mark.parametrize(
    ("label", "unsafe", "message"),
    [
        ("symlink gateway source", "src/mgo-validate", "gateway source"),
        ("symlink sudoers source", "src/mgo-validate.sudoers", "sudoers source"),
        ("symlink gateway target", "dst/mgo-validate", "gateway target"),
        ("symlink sudoers target", "dst/mgo-validate.sudoers", "sudoers target"),
    ],
)
def test_an_unsafe_file_type_is_refused_before_any_mutation(
    tmp_path: Path, label: str, unsafe: str, message: str
) -> None:
    """A link to matching content is not the file, and must not be replaced.

    ``-f`` follows symlinks, so such a target would pass every content check
    while installing over it replaced the link and restoring put back an
    ordinary file where a link used to be.
    """
    # The target cases need a target to have a type at all; an absent target is
    # a supported state and would never reach the check.
    existing = "old\n" if unsafe.startswith("dst/") else None
    result, gateway, sudoers = _install_pair(
        tmp_path,
        extra=_type_double(unsafe),
        existing_gateway=existing,
        existing_sudoers=existing,
    )
    calls = (tmp_path / "calls.log").read_text(encoding="utf-8")

    assert "outcome=1" in result.stdout, label
    assert message in result.stderr, label
    assert calls.strip() == "", f"{label}: mutating commands ran"
    if existing is not None:
        assert gateway.read_text(encoding="utf-8") == existing, label
        assert sudoers.read_text(encoding="utf-8") == existing, label


def test_a_directory_where_a_target_belongs_is_refused(tmp_path: Path) -> None:
    """A real filesystem execution, on every host: not emulated."""
    sources = tmp_path / "src"
    targets = tmp_path / "dst"
    sources.mkdir()
    targets.mkdir()
    (sources / "mgo-validate").write_bytes(b"#!/usr/bin/env bash\ntrue\n")
    (sources / "mgo-validate.sudoers").write_bytes(b"claude ALL=(root) x\n")
    (targets / "mgo-validate").mkdir()

    result = run_bash(
        f'export CALL_LOG="{_posix(tmp_path / "calls.log")}"\n'
        ': > "$CALL_LOG"\n'
        f'source "{_posix(INSTALLER)}"\n'
        "set +e\n"
        f"{INSTALLER_DOUBLES}\n"
        "install_pair "
        f'"{_posix(sources / "mgo-validate")}" '
        f'"{_posix(targets / "mgo-validate")}" "0755" '
        f'"{_posix(sources / "mgo-validate.sudoers")}" '
        f'"{_posix(targets / "mgo-validate.sudoers")}" "0440" '
        f'"{_posix(tmp_path / "transaction")}"\n'
        'printf "outcome=%s\\n" "$?"\n'
    )

    assert "outcome=1" in result.stdout, result.stderr
    assert "gateway target is not a regular file" in result.stderr
    assert (targets / "mgo-validate").is_dir()


def test_an_absent_pair_installs_cleanly(tmp_path: Path) -> None:
    """Absent is a supported starting state."""
    result, gateway, sudoers = _install_pair(tmp_path)

    assert "outcome=0" in result.stdout, result.stderr
    assert gateway.is_file()
    assert sudoers.is_file()


def test_a_regular_existing_pair_is_replaced(tmp_path: Path) -> None:
    """So is a regular file."""
    result, gateway, _sudoers = _install_pair(
        tmp_path, existing_gateway="old\n", existing_sudoers="# old\n"
    )

    assert "outcome=0" in result.stdout, result.stderr
    assert gateway.read_text(encoding="utf-8") != "old\n"


# --- transaction directory -------------------------------------------------


def test_backups_never_land_in_the_sudoers_include_directory(
    tmp_path: Path,
) -> None:
    """A backup under /etc/sudoers.d is a second policy in a directory sudo reads."""
    result, _gateway, sudoers = _install_pair(
        tmp_path,
        extra=(
            'policy_is_valid() { case "$1" in */dst/*)'
            ' return 1 ;; esac; return 0; }'
        ),
        existing_gateway="old\n",
        existing_sudoers="# old\n",
    )
    sudoers_directory = sudoers.parent

    assert "outcome=1" in result.stdout
    leftovers = [
        path.name
        for path in sudoers_directory.iterdir()
        if "previous" in path.name or path.name.startswith("previous")
    ]
    assert leftovers == [], leftovers


def test_the_transaction_directory_is_root_owned_and_private() -> None:
    """0700, root:root, and outside every included configuration directory."""
    source = _read(INSTALLER)

    assert 'readonly TRANSACTION_DIRECTORY="/run/mgo-validate-install"' in source
    assert "install -d -o root -g root -m 0700" in source
    assert "/etc/sudoers.d" not in _installer_function_body(
        "open_transaction_parent()", "# This run's own workspace"
    )


def test_a_transaction_directory_failure_stops_before_mutation(
    tmp_path: Path,
) -> None:
    """No scratch space, no transaction."""
    result, gateway, _sudoers = _install_pair(
        tmp_path, extra="open_transaction_parent() { return 1; }"
    )
    calls = (tmp_path / "calls.log").read_text(encoding="utf-8")

    assert "outcome=1" in result.stdout
    assert "transaction directory could not be created" in result.stderr
    assert "install" not in calls
    assert not gateway.exists()


def test_a_cleanup_failure_after_success_is_not_success(tmp_path: Path) -> None:
    """A run that left the previous policy on disk has not finished."""
    result, _gateway, _sudoers = _install_pair(
        tmp_path, extra="close_transaction_workspace() { return 1; }"
    )

    assert "outcome=1" in result.stdout
    assert "transaction directory could not be removed" in result.stderr


def test_a_cleanup_failure_after_rollback_is_reported(tmp_path: Path) -> None:
    """Restored but not tidied is still not a clean outcome."""
    result, _gateway, _sudoers = _install_pair(
        tmp_path,
        extra=(
            'policy_is_valid() { case "$1" in */dst/*) return 1 ;; esac; return 0; }\n'
            "close_transaction_workspace() { return 1; }"
        ),
        existing_gateway="old\n",
        existing_sudoers="# old\n",
    )

    assert f"outcome={EX_RESTORE_FAILED}" in result.stdout
    assert "cleanup failed" in result.stderr


def test_a_successful_installation_leaves_no_transaction_artefacts(
    tmp_path: Path,
) -> None:
    """Nothing of this run's transaction survives it.

    The parent remains — it is a fixed, root-owned 0700 directory, not an
    artefact — but it is empty, which is what the next run requires in order to
    conclude that no previous run was interrupted.
    """
    result, _gateway, _sudoers = _install_pair(
        tmp_path, existing_gateway="old\n", existing_sudoers="# old\n"
    )

    assert "outcome=0" in result.stdout, result.stderr
    assert list((tmp_path / "transaction").iterdir()) == []


def test_a_successful_rollback_leaves_no_transaction_artefacts(
    tmp_path: Path,
) -> None:
    """Nor does a failed installation that restored cleanly."""
    result, _gateway, _sudoers = _install_pair(
        tmp_path,
        extra=(
            'policy_is_valid() { case "$1" in */dst/*)'
            ' return 1 ;; esac; return 0; }'
        ),
        existing_gateway="old\n",
        existing_sudoers="# old\n",
    )

    assert "outcome=1" in result.stdout
    assert list((tmp_path / "transaction").iterdir()) == []


def _without_comments(text: str) -> str:
    """Executable lines only.

    The comments here explain the very flags being asserted, so a text check
    over the whole function passes on the prose after the code has lost them.
    """
    return "\n".join(
        line for line in text.splitlines() if not line.strip().startswith("#")
    )


def test_a_restoration_preserves_numeric_owner_group_and_mode() -> None:
    """Restoring an approximation of the file is not restoring the file."""
    body = _without_comments(
        _installer_function_body("record_previous()", "restore_one()")
    )
    restore = _without_comments(
        _installer_function_body("restore_one()", "# Both targets, always")
    )

    assert "--preserve=all" in body
    assert "--no-dereference" in body
    assert "--preserve=all" in restore
    assert "--no-dereference" in restore


def test_the_installer_requires_root_for_a_real_run(tmp_path: Path) -> None:
    """Writing to /usr/local/sbin and /etc/sudoers.d needs privilege."""
    result = run_bash(
        "set +e\n"
        f'source "{_posix(INSTALLER)}"\n'
        "set +e\n"
        'run_installation "/x" "/y" "0755" "/a" "/b" "0440" 0 1001\n'
        'printf "outcome=%s\\n" "$?"\n'
    )

    assert "outcome=1" in result.stdout
    assert "requires root" in result.stderr


def test_the_installer_rejects_an_unknown_argument() -> None:
    """The installer's input surface is as closed as the gateway's."""
    result = subprocess.run(
        [_bash(), str(INSTALLER), "--force"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0


def test_the_installer_installs_both_files_with_the_required_modes() -> None:
    """0755 for the executable, 0440 for the policy, root:root for both."""
    source = _read(INSTALLER)

    assert 'readonly GATEWAY_MODE="0755"' in source
    assert 'readonly SUDOERS_MODE="0440"' in source
    assert "-o root -g root" in source
    # Numeric, so the comparison does not depend on what passwd renders.
    assert '"0:0:${expected_mode#0}"' in source
    assert "stat -c '%u:%g:%a'" in source


def test_no_installer_command_relies_on_errexit_inside_a_condition() -> None:
    """Bash disables errexit for a function run as an ``if !`` condition."""
    body = _installer_function_body("install_file()", "# Record a target's")

    for command in ("mktemp", "install ", "mv -f"):
        assert command in body, command
    # Each mutating step has its own explicit failure branch.
    assert body.count("return 1") >= 4


def _installer_function_body(name: str, next_name: str) -> str:
    source = _read(INSTALLER)
    start = source.index(name)
    return source[start : source.index(next_name, start)]


# --- dry run ---------------------------------------------------------------


def _dry_run(tmp_path: Path, *, visudo: str) -> subprocess.CompletedProcess[str]:
    """Run a dry run with a deterministic ``visudo`` outcome.

    The installer no longer honours a caller-supplied PATH — that is the point
    of the process-entry boundary — so a fake executable dropped into a
    temporary directory can no longer be reached. The three outcomes are
    therefore driven at the two points the installer actually consults:
    ``command -v visudo`` for presence and ``visudo -cf`` for validity. That
    ``command -v`` is what decides presence is asserted separately, on the
    shipped text.
    """
    present = "0" if visudo != "absent" else "1"
    doubles = (
        'command() {\n'
        f'    if [[ "$1" == "-v" ]]; then return {present}; fi\n'
        '    builtin command "$@"\n'
        "}\n"
    )
    if visudo != "absent":
        doubles += f"visudo() {{ return {0 if visudo == 'valid' else 1}; }}\n"

    return run_bash(
        "set +e\n"
        f'source "{_posix(INSTALLER)}"\n'
        "set +e\n"
        f"{doubles}"
        f'run_installation "{_posix(GATEWAY)}" "{_posix(tmp_path / "gw")}" "0755" '
        f'"{_posix(SUDOERS)}" "{_posix(tmp_path / "policy")}" "0440" 1 1000 '
        f'"{_posix(tmp_path / "transaction")}" "{_posix(tmp_path / "lock")}"\n'
    )


def test_the_presence_of_visudo_is_decided_by_command_v() -> None:
    """Presence is asked of the shell, not assumed from a path."""
    body = _installer_function_body("run_installation()", "main()")

    assert "command -v visudo" in body
    assert body.index("command -v visudo") < body.index("visudo -cf")


def test_a_dry_run_with_a_valid_policy_reports_and_changes_nothing(
    tmp_path: Path,
) -> None:
    """A dry run is a report — but only once validation has actually passed."""
    result = _dry_run(tmp_path, visudo="valid")

    assert result.returncode == 0, result.stderr
    assert "nothing was changed" in result.stdout


def test_a_dry_run_fails_when_the_policy_is_invalid(tmp_path: Path) -> None:
    """An invalid policy is a finding, not a footnote in a successful report."""
    result = _dry_run(tmp_path, visudo="invalid")

    assert result.returncode != 0
    assert "failed validation" in result.stderr
    assert "dry run complete" not in result.stdout


def test_a_dry_run_fails_closed_without_visudo(tmp_path: Path) -> None:
    """The help says a dry run validates everything, so it must not skip it."""
    result = _dry_run(tmp_path, visudo="absent")

    assert result.returncode != 0
    assert "visudo is not available" in result.stderr
    assert "dry run complete" not in result.stdout


def test_a_real_run_fails_closed_without_visudo() -> None:
    """Never an unvalidated policy under /etc/sudoers.d."""
    result = run_bash(
        "set +e\n"
        f'source "{_posix(INSTALLER)}"\n'
        "set +e\n"
        "command() { return 1; }\n"
        f'run_installation "{_posix(GATEWAY)}" "/y" "0755" '
        f'"{_posix(SUDOERS)}" "/b" "0440" 0 0\n'
        'printf "outcome=%s\\n" "$?"\n'
    )

    assert "outcome=1" in result.stdout
    assert "visudo is not available" in result.stderr


def test_validation_precedes_every_mutation() -> None:
    """Both checks come before the first thing that writes a target.

    They now live inside the transaction, after the snapshot, because what has
    to be validated is the bytes that will be installed — not the bytes that
    happened to be in the checkout when the run started.
    """
    body = _installer_function_body("install_pair()", "abort_pair()")
    staged = body.index("staged_gateway=")
    syntax = body.index("gateway_syntax_is_valid")
    policy = body.index("policy_is_valid")
    install = body.index("install_file")

    assert staged < syntax < policy < install


def test_the_lock_is_taken_before_any_source_is_validated() -> None:
    """A concurrent deploy-main can move the checkout under the installer.

    Validating, checksumming and then installing from a live working tree is
    three reads of a moving target. The lock now comes before the first of
    them, so the whole sequence sees one state of the repository.
    """
    body = _installer_function_body("run_installation()", "dry_run_installation()")
    lock = body.index("acquire_transaction_lock")
    transaction = body.index("install_pair \\")

    assert lock < transaction
    # And nothing validates or checksums a source before that point.
    preamble = body[:lock]
    for premature in ("gateway_syntax_is_valid", "file_checksum", "visudo -cf"):
        assert premature not in preamble, premature


def test_the_installation_reads_only_the_staged_snapshot() -> None:
    """After staging, the live checkout is never read again.

    The whole point of the snapshot is that "the bytes installed are the bytes
    validated" stops depending on nothing having changed in between.
    """
    body = _installer_function_body("install_pair()", "abort_pair()")
    after = body[body.index("require_secure_staged_file") :]
    lines = [
        line
        for line in after.splitlines()
        if not line.strip().startswith("#")
        and ("gateway_source" in line or "sudoers_source" in line)
    ]

    assert lines == [], lines
    assert 'install_file "$staged_gateway"' in body
    assert 'install_file "$staged_sudoers"' in body


def test_the_installer_touches_no_approval_repository_or_service() -> None:
    """Provisioning the control plane is not deploying, and not restarting."""
    source = _read(INSTALLER)
    body = "\n".join(
        line for line in source.splitlines() if not line.strip().startswith("#")
    )

    assert "claude-approved-sha" not in body
    assert "systemctl" not in body
    assert "git " not in body
    assert "uv " not in body


def test_the_installer_is_separate_from_service_identity_provisioning() -> None:
    """Two operational concerns, two entry points, two names."""
    identity = _read(DEPLOY_DIRECTORY / "install-service-identity.sh")

    assert "mgo-validate" not in identity


# --------------------------------------------------------------------------
# the root temporary directory
# --------------------------------------------------------------------------


def _directory_stat_double(owner: str = "0:0", mode: str = "700") -> str:
    """Drive numeric ownership and mode for a directory on any filesystem.

    Windows has no POSIX owner or mode, and a developer's real values are
    irrelevant to the contract: what matters is that the shipped logic refuses
    the combinations it is shown. On the Raspberry Pi these execute against the
    kernel's own answers.
    """
    return (
        "stat() {\n"
        '    case "$2" in\n'
        f"        '%u:%g') printf '{owner}\\n' ;;\n"
        f"        '%a') printf '{mode}\\n' ;;\n"
        "        *) printf '\\n' ;;\n"
        "    esac\n"
        "}\n"
    )


NAME = "mgo-validate-tmp"


def _prepare_tmpdir(
    parent: Path,
    *,
    owner: str = "0:0",
    mode: str = "700",
) -> subprocess.CompletedProcess[str]:
    """Prepare a temporary directory under a parent, as the shipped code does.

    The parent is resolved to the path ``pwd -P`` reports, because that is what
    the shipped physical-path proof compares against — on this host a
    drive-lettered path and its POSIX form name the same directory by different
    strings, and the production constant is already the physical one.
    """
    return call_gateway_function(
        f'parent="$(cd "{_posix(parent)}" && pwd -P)" || parent="{_posix(parent)}"\n'
        f'prepare_root_tmpdir "$parent/{NAME}" "$parent"\n'
        'printf "outcome=%s TMPDIR=%s\\n" "$?" "${TMPDIR:-<unset>}"',
        preamble=_directory_stat_double(owner=owner, mode=mode),
    )


def test_an_absent_temporary_directory_is_created_privately(tmp_path: Path) -> None:
    """Absence is handled by creating it, privately, before it is trusted."""
    parent = tmp_path / "run"
    parent.mkdir()

    result = _prepare_tmpdir(parent)

    assert "outcome=0" in result.stdout, result.stderr
    assert (parent / NAME).is_dir()
    assert "TMPDIR=" in result.stdout
    assert result.stdout.strip().endswith(f"/{NAME}")


def test_the_temporary_directory_is_created_in_one_step(tmp_path: Path) -> None:
    """The umask applies at creation, so there is no window at a wider mode.

    A ``mkdir`` followed by a ``chmod`` publishes the directory first and
    secures it afterwards, and anything that entered in between is already in.
    """
    body = _function_body("create_root_tmpdir()", "# Establish the temporary")

    assert "umask 0077" in body
    assert "chmod" not in body
    assert body.index("umask 0077") < body.index("mkdir")

    # And it executes: the shipped function creates the directory for real.
    path = tmp_path / "mgo-validate-tmp"
    result = call_gateway_function(f'create_root_tmpdir "{_posix(path)}"')

    assert result.returncode == 0, result.stderr
    assert path.is_dir()


def test_a_root_owned_private_temporary_directory_is_accepted(
    tmp_path: Path,
) -> None:
    """The positive case, so the refusals below mean something."""
    parent = tmp_path / "run"
    parent.mkdir()
    (parent / NAME).mkdir()

    result = _prepare_tmpdir(parent)

    assert "outcome=0" in result.stdout, result.stderr


@pytest.mark.parametrize("owner", ["1000:1000", "0:1000", "1000:0"])
def test_a_temporary_directory_owned_by_another_account_is_refused(
    tmp_path: Path, owner: str
) -> None:
    """Anything an unprivileged account owns, it can also replace."""
    parent = tmp_path / "run"
    parent.mkdir()
    (parent / NAME).mkdir()

    result = _prepare_tmpdir(parent, owner=owner)

    assert result.returncode == EX_PRECONDITION, owner
    assert "root-owned 0700 directory" in result.stderr


@pytest.mark.parametrize("mode", ["755", "750", "770", "777", "701", "600"])
def test_a_temporary_directory_with_any_other_mode_is_refused(
    tmp_path: Path, mode: str
) -> None:
    """Exactly 0700, not "no wider than": a mode is a value, not a range."""
    parent = tmp_path / "run"
    parent.mkdir()
    (parent / NAME).mkdir()

    result = _prepare_tmpdir(parent, mode=mode)

    assert result.returncode == EX_PRECONDITION, mode


def test_a_regular_file_where_the_temporary_directory_belongs_is_refused(
    tmp_path: Path,
) -> None:
    """A real filesystem execution of the single check that refuses four types.

    ``-d`` is what separates a directory from a regular file, a FIFO, a socket
    and a device: one check, one refusal, and this executes it for the one of
    the four a Windows filesystem can actually produce. All four execute during
    Raspberry Pi validation.
    """
    parent = tmp_path / "run"
    parent.mkdir()
    path = parent / NAME
    path.write_bytes(b"not a directory\n")

    result = _prepare_tmpdir(parent)

    assert result.returncode == EX_PRECONDITION, result.stdout
    assert "root-owned 0700 directory" in result.stderr
    assert path.read_bytes() == b"not a directory\n"


def test_a_temporary_directory_symlink_is_refused_before_anything_follows_it() -> None:
    """``-d`` follows a symlink; ``-L`` is what refuses one.

    Asserted on the shipped text because this filesystem will not create a
    symlink. The ordering is the whole property: the old code proved the path
    was a directory with ``-d`` — which a link to one satisfies — and then
    applied ``chmod`` to whatever the link pointed at.
    """
    prepare = _function_body("prepare_root_tmpdir()", "# One tracked temporary file")
    secure = _function_body("require_secure_root_tmpdir()", "# Create it private")

    assert prepare.index('-L "$path"') < prepare.index("create_root_tmpdir")
    assert prepare.index('-L "$path"') < prepare.index("require_secure_root_tmpdir")
    assert secure.index('! -L "$path"') < secure.index('-d "$path"')


def test_the_temporary_directory_is_never_repaired() -> None:
    """An unsafe object is refused, not adopted.

    Repairing it would mean this gateway asserting ownership of a directory
    something else created, which is a decision for an operator.
    """
    for name, following in (
        ("require_secure_root_tmpdir()", "# Create it private"),
        ("create_root_tmpdir()", "# Establish the temporary"),
        ("prepare_root_tmpdir()", "# One tracked temporary file"),
    ):
        body = _function_body(name, following)
        assert "chmod" not in body, name
        assert "chown" not in body, name
        assert "rm " not in body, name


def test_the_temporary_directory_parent_must_be_its_physical_path(
    tmp_path: Path,
) -> None:
    """A symlinked ``/run`` would move every response body somewhere else.

    The failure is executed for real: an unresolvable parent stops the check
    before ownership is ever read.
    """
    parent = tmp_path / "absent"

    result = _prepare_tmpdir(parent)

    assert result.returncode == EX_PRECONDITION
    body = _function_body("require_secure_root_tmpdir()", "# Create it private")
    assert "pwd -P" in body
    assert '"$canonical" == "$parent"' in body


def test_a_caller_tmpdir_cannot_choose_where_a_temporary_file_goes(
    tmp_path: Path,
) -> None:
    """``--tmpdir=`` states the location at the call site."""
    fixed = tmp_path / "fixed"
    fixed.mkdir()
    hostile = tmp_path / "hostile"
    hostile.mkdir()

    result = call_gateway_function(
        f'make_temporary_file "{_posix(fixed)}"',
        preamble=f'export TMPDIR="{_posix(hostile)}"\n',
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().startswith(_posix(fixed))
    assert list(hostile.iterdir()) == []
    assert len(list(fixed.iterdir())) == 1


def test_every_temporary_file_is_named_relative_to_the_fixed_directory() -> None:
    """No bare ``mktemp`` survives: a bare one consults TMPDIR."""
    body = _function_body("make_temporary_file()", "# --- caller and privilege")
    assert '--tmpdir="$directory"' in body
    assert 'directory="${1:-$MGO_ROOT_TMPDIR}"' in body

    for line in _read(GATEWAY).splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or "mktemp" not in stripped:
            continue
        assert "--tmpdir=" in stripped, stripped


def test_show_approval_prepares_nothing_and_changes_nothing() -> None:
    """A read-only action must not create a directory, a lock or a file."""
    body = _function_body("main()", "# Program when executed")
    dispatch = body[body.index("# The temporary directory is prepared per action") :]
    show = dispatch[dispatch.index("show-approval)") : dispatch.index("deploy-main)")]

    assert "prepare_root_tmpdir" not in show
    assert "acquire_transaction_lock" not in show
    # Prepared per action, and only for the two that mutate anything.
    assert body.count("prepare_root_tmpdir") == 2

    action = _function_body("action_show_approval()", "action_restart_api()")
    for forbidden in ("mktemp", "mkdir", "acquire_transaction_lock", "chmod"):
        assert forbidden not in action, forbidden


def test_a_failed_temporary_directory_cleanup_is_not_success() -> None:
    """Cleanup failure remains non-zero, wherever it happens."""
    result = call_gateway_function(
        'endpoint_is_ok "http://127.0.0.1:8080/health"',
        preamble=_curl_double("200") + "discard_temporary() { return 1; }\n",
    )

    assert result.returncode != 0


# --------------------------------------------------------------------------
# environment isolation at process entry
# --------------------------------------------------------------------------


HOSTILE_PROCESS_ENVIRONMENT = {
    "BASH_ENV": "/tmp/evil-bashenv.sh",
    "ENV": "/tmp/evil-env.sh",
    "PYTHONINSPECT": "1",
    "PYTHONSTARTUP": "/tmp/evil-startup.py",
    "PYTHONWARNINGS": "error",
    "PYTHONPATH": "/tmp/evil",
    "PYTHONHOME": "/tmp/evil",
    "GIT_DIR": "/tmp/evil.git",
    "GIT_WORK_TREE": "/tmp/evil",
    "GIT_SSH_COMMAND": "ssh -i /tmp/evil",
    "UV_PROJECT": "/tmp/evil",
    "UV_CONFIG_FILE": "/tmp/evil.toml",
    "VIRTUAL_ENV": "/tmp/evil-venv",
    "CURL_HOME": "/tmp/evil",
    "TMPDIR": "/tmp/evil-tmp",
    "LD_LIBRARY_PATH": "/tmp/evil-lib",
    # LD_PRELOAD is deliberately absent from this map. It is honoured by the
    # loader before the interpreter starts, so a hostile value does not produce
    # a script that behaves wrongly — it produces no interpreter at all, which
    # no script can defend against from the inside. It is unset at the root
    # boundary and asserted in the register test instead.
    "SSH_AUTH_SOCK": "/tmp/evil.sock",
    "HTTP_PROXY": "http://evil",
    "HTTPS_PROXY": "http://evil",
    "ALL_PROXY": "http://evil",
    "MGO_EVIL": "1",
}

SAFE_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


def _entry_probe(tmp_path: Path, asset: Path, *, marker: str = "") -> Path:
    """A program that reports the environment its ``main`` actually runs in.

    It sources the shipped asset and replaces the first thing ``main`` calls
    after constructing the environment, so what is printed is the environment
    every later command would inherit. The re-execution names ``$0``, which is
    this probe, so the probe is what runs again — exercising the shipped
    re-execution rather than describing it.
    """
    probe = tmp_path / "probe.sh"
    probe.write_bytes(
        (
            "#!/bin/bash\n"
            "set +e\n"
            f'source "{_posix(asset)}"\n'
            "set +e\n"
            f"{marker}"
            "_report() {\n"
            "    compgen -e | sort | tr '\\n' ' '\n"
            '    printf "\\nPATH=%s\\nHOME=%s\\nLC_ALL=%s\\nTMPDIR=%s\\n" \\\n'
            '        "$PATH" "$HOME" "${LC_ALL:-<unset>}" "${TMPDIR:-<unset>}"\n'
            "    exit 0\n"
            "}\n"
            "require_root_caller() { _report; }\n"
            "run_installation() { _report; }\n"
            'main "$@"\n'
        ).encode()
    )
    return probe


def _run_probe(
    probe: Path, *, extra: dict[str, str] | None = None, argument: str = "show-approval"
) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ)
    environment.update(HOSTILE_PROCESS_ENVIRONMENT)
    environment["PATH"] = "/tmp/evil-bin:" + environment.get("PATH", "")
    if extra:
        environment.update(extra)
    return subprocess.run(
        [_bash(), _posix(probe), argument],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=environment,
        check=False,
    )


@pytest.mark.parametrize(
    ("asset", "argument"),
    [(GATEWAY, "show-approval"), (INSTALLER, "--dry-run")],
    ids=["gateway", "installer"],
)
def test_no_inherited_variable_survives_into_operational_execution(
    tmp_path: Path, asset: Path, argument: str
) -> None:
    """Twenty-two hostile variables, none of which reaches a command.

    Each of these steers something real: the interpreter, the repository, the
    index, the SSH key, the parser, the loader, the proxy, or where temporary
    files are written. Unsetting a named list would leave whatever the list
    forgot; starting from an empty environment leaves nothing.
    """
    probe = _entry_probe(tmp_path, asset)
    result = _run_probe(probe, argument=argument)
    exported = result.stdout.splitlines()[0].split()

    assert result.returncode == 0, result.stderr
    for variable in HOSTILE_PROCESS_ENVIRONMENT:
        assert variable not in exported, variable
    assert f"PATH={SAFE_PATH}" in result.stdout
    assert "HOME=/root" in result.stdout
    assert "LC_ALL=C" in result.stdout
    assert "TMPDIR=<unset>" in result.stdout


@pytest.mark.parametrize(
    ("asset", "argument"),
    [(GATEWAY, "show-approval"), (INSTALLER, "--dry-run")],
    ids=["gateway", "installer"],
)
def test_an_inherited_path_cannot_choose_which_executable_runs(
    tmp_path: Path, asset: Path, argument: str
) -> None:
    """PATH decides which git, which curl, which python3 and which stat."""
    probe = _entry_probe(tmp_path, asset)
    result = _run_probe(probe, argument=argument)

    assert result.returncode == 0, result.stderr
    assert f"PATH={SAFE_PATH}" in result.stdout
    assert "/tmp/evil-bin" not in result.stdout


@pytest.mark.parametrize(
    ("asset", "marker", "argument"),
    [
        (GATEWAY, 'export MGO_ENVIRONMENT_CONSTRUCTED="1"\n', "show-approval"),
        (
            INSTALLER,
            'export MGO_INSTALL_ENVIRONMENT_CONSTRUCTED="1"\n',
            "--dry-run",
        ),
    ],
    ids=["gateway", "installer"],
)
def test_presenting_the_marker_does_not_skip_construction(
    tmp_path: Path, asset: Path, marker: str, argument: str
) -> None:
    """The marker only prevents a second re-execution.

    If it were trusted as permission to skip sanitisation, a caller could set
    it alongside a hostile PATH and keep the whole environment. Instead the
    process asserts the three fixed values itself and removes everything else,
    so a forged marker buys nothing.
    """
    probe = _entry_probe(tmp_path, asset, marker=marker)
    result = _run_probe(probe, argument=argument)
    exported = result.stdout.splitlines()[0].split()

    assert result.returncode == 0, result.stderr
    for variable in HOSTILE_PROCESS_ENVIRONMENT:
        assert variable not in exported, variable
    assert f"PATH={SAFE_PATH}" in result.stdout
    assert "HOME=/root" in result.stdout


#: Reaching ``environment_is_constructed``'s allowlist from inside a Bash that
#: has already started.
#:
#: The entry-path test below hands PATH in through the *process* environment,
#: and Git Bash prepends its own directories to PATH before the child sees it.
#: The function's first guard therefore fails on that host, it returns before
#: the allowlist loop, and the entry-path test passes whether or not the
#: allowlist works. This preparation removes the platform's variables with the
#: gateway's own ``purge_environment`` and then assigns the three fixed values
#: in-process, where nothing is left to rewrite them.
#:
#: Sourcing the gateway turns on ``errexit``; a function returning non-zero is
#: the point of this test, so it goes back off first.
ALLOWLIST_PREPARATION = "\n".join(
    (
        "set +e",
        "purge_environment",
        "purge_status=$?",
        'export PATH="$MGO_SAFE_PATH"',
        'export HOME="$MGO_ROOT_HOME"',
        "export LC_ALL=C",
    )
)

#: Builtins only. PATH has just been replaced with a set of directories that do
#: not exist on a Windows workstation, so anything needing an external binary
#: would fail for a reason that has nothing to do with what is being tested.
ALLOWLIST_REPORT = "\n".join(
    (
        "environment_is_constructed",
        'printf "constructed=%s\\n" "$?"',
        'printf "purge=%s\\n" "$purge_status"',
        'if [[ "$PATH" == "$MGO_SAFE_PATH" ]]; then',
        '    printf "path=exact\\n"',
        "else",
        '    printf "path=rewritten\\n"',
        "fi",
    )
)


def _reported(result: subprocess.CompletedProcess[str], key: str) -> str:
    """One ``key=value`` line from the probe's output."""
    for line in result.stdout.splitlines():
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1]
    raise AssertionError(f"no {key}= line in:\n{result.stdout}\n{result.stderr}")


def test_environment_allowlist_rejects_an_unknown_variable_after_shell_startup() -> (
    None
):
    """The allowlist branch itself, reached on every platform.

    ``environment_is_constructed`` checks PATH, HOME and LC_ALL before it walks
    the allowlist. Any host that rewrites an inherited PATH makes the first
    guard fail, and a test entering through a fresh process then never reaches
    the loop — so removing the loop entirely changes nothing it can observe.
    That is not a hypothetical: it is why the ``env-allowlist-open`` mutation
    went undetected on Windows while the Raspberry Pi register caught it.

    The positive control is what makes the negative result mean something. If
    preparation or the purge failed, the environment would not be constructed
    for a reason unrelated to the allowlist, and a mutation would look detected
    when it was not.
    """
    baseline = call_gateway_function(ALLOWLIST_REPORT, preamble=ALLOWLIST_PREPARATION)

    assert baseline.returncode == 0, baseline.stderr
    assert _reported(baseline, "purge") == "0", baseline.stdout
    assert _reported(baseline, "path") == "exact", (
        "the fixed PATH did not survive into the shell, so the allowlist was "
        f"never reached:\n{baseline.stdout}"
    )
    assert _reported(baseline, "constructed") == "0", baseline.stdout

    witness = call_gateway_function(
        ALLOWLIST_REPORT,
        preamble=ALLOWLIST_PREPARATION + "\nexport MGO_EVIL=1",
    )

    assert witness.returncode == 0, witness.stderr
    assert _reported(witness, "purge") == "0", witness.stdout
    assert _reported(witness, "path") == "exact", witness.stdout
    assert _reported(witness, "constructed") != "0", (
        "an exported variable outside the permitted set was accepted as a "
        f"constructed environment:\n{witness.stdout}"
    )


@pytest.mark.parametrize(
    ("asset", "argument"),
    [(GATEWAY, "show-approval"), (INSTALLER, "--dry-run")],
    ids=["gateway", "installer"],
)
def test_an_environment_that_looks_constructed_is_still_verified(
    tmp_path: Path, asset: Path, argument: str
) -> None:
    """The three fixed values are necessary, not sufficient.

    A caller who sets PATH, HOME and LC_ALL to exactly the right values, and
    one hostile variable alongside them, would pass a check that only compared
    those three. What makes the answer "this is the constructed environment" is
    that *nothing else is exported* — which is why the check is an allowlist
    over the whole environment rather than three comparisons.

    The witness is deliberately a variable **no denylist names**. GIT_DIR would
    prove nothing here: it is in the explicit unset register, so it would go
    whether or not the allowlist worked. What an allowlist buys is the variable
    nobody thought of.

    This is the **entry path**, and it is not the cross-platform witness for
    the allowlist branch. On a host that rewrites an inherited PATH — Git Bash
    prepends its own directories — the first guard fails, both the real and a
    mutated function take the reconstruction path, and MGO_EVIL disappears
    either way. What this test proves everywhere is that the entry path ends
    in a clean environment. That the allowlist is what decides it is proved by
    ``test_environment_allowlist_rejects_an_unknown_variable_after_shell_startup``.
    """
    probe = _entry_probe(tmp_path, asset)
    result = _run_probe(
        probe,
        extra={"PATH": SAFE_PATH, "HOME": "/root", "LC_ALL": "C", "MGO_EVIL": "1"},
        argument=argument,
    )
    exported = result.stdout.splitlines()[0].split()

    assert result.returncode == 0, result.stderr
    assert "MGO_EVIL" not in exported
    assert "GIT_DIR" not in exported


@pytest.mark.parametrize(
    ("asset", "argument"),
    [(GATEWAY, "show-approval"), (INSTALLER, "--dry-run")],
    ids=["gateway", "installer"],
)
def test_a_hostile_bash_env_never_executes(
    tmp_path: Path, asset: Path, argument: str
) -> None:
    """Privileged mode stops it being read at all — not merely afterwards.

    Bash reads ``$BASH_ENV`` while it is starting up, before the script's first
    statement. Re-executing through ``env -i`` removes it from the *second*
    process, which is why the marker used to be written exactly once; ``-p``
    means it is never written.

    The script is executed directly so the shebang is what chooses the
    interpreter. Naming ``bash`` on the command line would discard it, which is
    exactly why the documented invocation no longer does.
    """
    marker = tmp_path / "bash-env-ran"
    startup = tmp_path / "startup.sh"
    startup.write_bytes(f'printf "x" >> "{_posix(marker)}"\n'.encode())

    # The variables are set for the exec'd process only. Exporting them to the
    # launching shell as well would have that shell write the marker, and the
    # test would be measuring itself.
    result = run_bash(
        f'BASH_ENV="{_posix(startup)}" ENV="{_posix(startup)}"'
        f' exec "{_posix(asset)}" {argument}'
    )

    assert not marker.exists(), (
        f"BASH_ENV was executed: {marker.read_text(encoding='utf-8')!r}"
        f"\n{result.stdout}{result.stderr}"
    )


@pytest.mark.parametrize(
    ("asset", "argument"),
    [(GATEWAY, "show-approval"), (INSTALLER, "--dry-run")],
    ids=["gateway", "installer"],
)
def test_an_exported_shell_function_does_not_reach_either_script(
    tmp_path: Path, asset: Path, argument: str
) -> None:
    """An exported function silently replaces a command for the whole script.

    ``stat``, ``curl``, ``git`` and ``install`` are all names a caller could
    export a function under, and a function takes precedence over the
    executable. Privileged mode refuses to import any of them.
    """
    marker = tmp_path / "function-ran"
    probe = tmp_path / "probe.sh"
    probe.write_bytes(
        (
            "#!/bin/bash\n"
            f"stat() {{ printf 'x' >> \"{_posix(marker)}\"; }}\n"
            "export -f stat\n"
            f'exec "{_posix(asset)}" "$1"\n'
        ).encode()
    )

    subprocess.run(
        [_bash(), _posix(probe), argument],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )

    assert not marker.exists(), "an exported shell function was imported"


def test_a_bash_env_file_does_not_reach_the_operational_process(
    tmp_path: Path,
) -> None:
    """BASH_ENV is read by the interpreter before a script's first statement.

    Nothing the script does could undo that, which is precisely why the
    operational work happens in a second process started from an empty
    environment. The evidence is that the file is sourced once, not twice: the
    re-executed interpreter never sees BASH_ENV at all.
    """
    marker = tmp_path / "sourced"
    startup = tmp_path / "startup.sh"
    startup.write_bytes(f'printf "x" >> "{_posix(marker)}"\n'.encode())
    probe = _entry_probe(tmp_path, GATEWAY)

    result = _run_probe(probe, extra={"BASH_ENV": _posix(startup)})

    assert result.returncode == 0, result.stderr
    assert marker.read_text(encoding="utf-8") == "x"
    assert "BASH_ENV" not in result.stdout.splitlines()[0].split()


@pytest.mark.parametrize("asset", [GATEWAY, INSTALLER], ids=lambda p: p.name)
def test_shellopts_does_not_survive_the_boundary(
    tmp_path: Path, asset: Path
) -> None:
    """An exported SHELLOPTS turns shell options on in every child shell."""
    probe = _entry_probe(tmp_path, asset)
    argument = "show-approval" if asset is GATEWAY else "--dry-run"

    result = _run_probe(probe, extra={"SHELLOPTS": "noclobber"}, argument=argument)
    exported = result.stdout.splitlines()[0].split()

    assert result.returncode == 0, result.stderr
    assert "SHELLOPTS" not in exported


@pytest.mark.parametrize("asset", [GATEWAY, INSTALLER], ids=lambda p: p.name)
def test_the_environment_boundary_is_an_allowlist(asset: Path) -> None:
    """"Nothing else is here" is not a statement a denylist can make."""
    source = _read(asset)

    assert "PERMITTED_ENVIRONMENT" in source
    assert "compgen -e" in source
    assert "env -i" in source
    # The interpreter and env(1) are both named absolutely: they are what
    # construct the environment, so neither may be found through one. The
    # re-executed interpreter stays privileged, or the second process would be
    # the weaker of the two.
    assert '/bin/bash -p "$0"' in source
    assert 'ENV_COMMAND="/usr/bin/env"' in source


@pytest.mark.parametrize("asset", [GATEWAY, INSTALLER], ids=lambda p: p.name)
def test_only_the_action_and_the_sudo_caller_cross_the_boundary(
    asset: Path,
) -> None:
    """Everything else is rebuilt; these two are carried deliberately."""
    source = _read(asset)
    start = source.index('exec "$')
    boundary = source[start : source.index('/bin/bash -p "$0"', start)]

    assert "SUDO_USER=${SUDO_USER:-}" in boundary
    assert "LC_ALL=C" in boundary
    assert "TMPDIR" not in boundary
    # Named as the fixed constants, not passed through. "PATH=" alone would be
    # satisfied by "PATH=$PATH", which is the whole defect.
    if asset is GATEWAY:
        assert "PATH=$MGO_SAFE_PATH" in boundary
        assert "HOME=$MGO_ROOT_HOME" in boundary
    else:
        assert "PATH=$SAFE_PATH" in boundary
        assert "HOME=$ROOT_HOME" in boundary


def test_the_installer_does_not_rely_on_sudo_configuration() -> None:
    """``env_reset`` and ``secure_path`` are optional; correctness is not."""
    source = _read(INSTALLER)

    assert f'readonly SAFE_PATH="{SAFE_PATH}"' in source
    assert 'readonly ROOT_HOME="/root"' in source
    assert "require_constructed_environment" in _installer_function_body(
        "main()", "# Program when executed"
    )


# --------------------------------------------------------------------------
# the installer transaction workspace
# --------------------------------------------------------------------------


EX_STALE = 65

GATEWAY_SOURCE_BODY = b"#!/bin/bash\ntrue\n"
SUDOERS_SOURCE_BODY = b"claude ALL=(root) NOPASSWD: /x\n"


def _make_current(target: Path, content: bytes) -> None:
    """Put `content` at `target`, tolerating a file a previous run installed.

    Rewriting a file the shipped installer has just created through ``mv`` is
    not always permitted on this filesystem, and the write is unnecessary when
    the bytes already match — which is exactly the case in the sequenced tests
    that install first and then run again.
    """
    if target.exists() and target.read_bytes() == content:
        return
    if target.exists():
        target.chmod(0o600)
        target.unlink()
    target.write_bytes(content)


def _run_installation(
    tmp_path: Path,
    *,
    extra: str = "",
    current: bool = True,
    parent_owner: str = "0:0",
    parent_mode: str = "700",
    dry_run: int = 0,
) -> tuple[subprocess.CompletedProcess[str], Path, Path, Path]:
    """Run the shipped ``run_installation`` against temporary paths.

    The lock is stubbed because it is exercised on its own elsewhere; the
    transaction parent, its state and the per-run workspace are all real.
    """
    sources = tmp_path / "src"
    targets = tmp_path / "dst"
    transaction = tmp_path / "transaction"
    sources.mkdir(exist_ok=True)
    targets.mkdir(exist_ok=True)
    gateway_source = sources / "mgo-validate"
    sudoers_source = sources / "mgo-validate.sudoers"
    gateway_source.write_bytes(GATEWAY_SOURCE_BODY)
    sudoers_source.write_bytes(SUDOERS_SOURCE_BODY)
    gateway_target = targets / "mgo-validate"
    sudoers_target = targets / "mgo-validate.sudoers"
    if current:
        _make_current(gateway_target, gateway_source.read_bytes())
        _make_current(sudoers_target, sudoers_source.read_bytes())

    script = (
        "set +e\n"
        f'export CALL_LOG="{_posix(tmp_path / "calls.log")}"\n'
        ': > "$CALL_LOG"\n'
        f'source "{_posix(INSTALLER)}"\n'
        "set +e\n"
        f"{INSTALLER_DOUBLES}\n"
        f"{_installer_stat_double(parent_owner, parent_mode)}\n"
        'open_transaction_parent() { mkdir -p "$1"; }\n'
        "visudo() { return 0; }\n"
        "command() {\n"
        '    if [[ "$1" == "-v" ]]; then return 0; fi\n'
        '    builtin command "$@"\n'
        "}\n"
        "acquire_transaction_lock() { return 0; }\n"
        f"{extra}\n"
        "run_installation "
        f'"{_posix(gateway_source)}" "{_posix(gateway_target)}" "0755" '
        f'"{_posix(sudoers_source)}" "{_posix(sudoers_target)}" "0440" '
        f"{dry_run} 0 "
        f'"{_posix(transaction)}" "{_posix(tmp_path / "lock")}"\n'
        'printf "outcome=%s\\n" "$?"\n'
    )
    return run_bash(script), gateway_target, sudoers_target, transaction


def test_a_clean_first_installation_succeeds(tmp_path: Path) -> None:
    """Nothing installed, no transaction parent: the ordinary starting state."""
    result, gateway, sudoers, transaction = _run_installation(
        tmp_path, current=False
    )

    assert "outcome=0" in result.stdout, result.stdout + result.stderr
    assert gateway.is_file()
    assert sudoers.is_file()
    assert list(transaction.iterdir()) == []


def test_a_clean_idempotent_run_changes_nothing(tmp_path: Path) -> None:
    """Both targets correct, policy valid, no stale state: verified."""
    result, _gateway, _sudoers, _transaction = _run_installation(tmp_path)

    assert "outcome=0" in result.stdout, result.stdout + result.stderr
    assert "nothing changed" in result.stdout


def test_stale_transaction_state_refuses_an_otherwise_idempotent_run(
    tmp_path: Path,
) -> None:
    """"Both targets are correct" is not an answer to "a run did not finish".

    This is the defect: a cleanup failure left the workspace behind, and the
    next run reached its idempotent-success return before looking, reporting
    "verified; nothing changed" over an unfinished transaction.
    """
    transaction = tmp_path / "transaction"
    stale = transaction / "run.ABC123"
    stale.mkdir(parents=True)
    (stale / "previous.XYZ").write_bytes(b"# the previous sudoers policy\n")

    result, _gateway, _sudoers, _transaction = _run_installation(tmp_path)

    assert f"outcome={EX_STALE}" in result.stdout, result.stdout + result.stderr
    assert "stale transaction state" in result.stderr
    assert "nothing changed" not in result.stdout


def test_a_stale_backup_file_alone_refuses_an_idempotent_run(
    tmp_path: Path,
) -> None:
    """Any entry at all, not only a directory: a loose backup is evidence too."""
    transaction = tmp_path / "transaction"
    transaction.mkdir()
    (transaction / "previous.ABC123").write_bytes(b"# the previous policy\n")

    result, _gateway, _sudoers, _transaction = _run_installation(tmp_path)

    assert f"outcome={EX_STALE}" in result.stdout, result.stdout + result.stderr
    assert "stale transaction state" in result.stderr


def test_stale_transaction_evidence_is_preserved_not_destroyed(
    tmp_path: Path,
) -> None:
    """Removing it would destroy what an operator needs to understand it."""
    transaction = tmp_path / "transaction"
    stale = transaction / "run.ABC123"
    stale.mkdir(parents=True)
    backup = stale / "previous.XYZ"
    backup.write_bytes(b"# the previous sudoers policy\n")

    result, _gateway, _sudoers, _transaction = _run_installation(tmp_path)

    assert f"outcome={EX_STALE}" in result.stdout
    assert backup.read_bytes() == b"# the previous sudoers policy\n"
    assert "preserved for inspection" in result.stderr


def test_a_second_run_keeps_refusing_until_the_stale_state_is_resolved(
    tmp_path: Path,
) -> None:
    """The refusal is not a one-off warning that clears itself."""
    transaction = tmp_path / "transaction"
    stale = transaction / "run.ABC123"
    stale.mkdir(parents=True)
    (stale / "previous.XYZ").write_bytes(b"# old\n")

    first, _g, _s, _t = _run_installation(tmp_path)
    second, _g2, _s2, _t2 = _run_installation(tmp_path)

    assert f"outcome={EX_STALE}" in first.stdout
    assert f"outcome={EX_STALE}" in second.stdout
    assert stale.is_dir()


def test_a_cleanup_failure_is_visible_to_the_next_run(tmp_path: Path) -> None:
    """The two halves of the defect, in sequence.

    A run whose cleanup fails exits non-zero and leaves its workspace behind;
    the next run finds it and refuses instead of reporting a clean host.
    """
    first, _g, _s, transaction = _run_installation(
        tmp_path,
        current=False,
        extra="close_transaction_workspace() { return 1; }",
    )

    # Not success, and the workspace survives — which holds whether the run
    # reached its cleanup after installing (1) or after rolling back (78).
    # Pinning the exact code here would make this test depend on whether a
    # rename happened to succeed, which is not what it is about.
    assert "outcome=0" not in first.stdout, first.stdout + first.stderr
    assert list(transaction.iterdir()) != []

    second, _g2, _s2, _t2 = _run_installation(tmp_path)

    assert f"outcome={EX_STALE}" in second.stdout, second.stdout + second.stderr


def test_each_run_gets_its_own_workspace(tmp_path: Path) -> None:
    """Unique names, so one run's artefacts are never another's."""
    transaction = tmp_path / "transaction"
    transaction.mkdir()

    result = run_bash(
        "set +e\n"
        f'source "{_posix(INSTALLER)}"\n'
        "set +e\n"
        f'open_transaction_workspace "{_posix(transaction)}"\n'
        f'open_transaction_workspace "{_posix(transaction)}"\n'
    )
    workspaces = [line for line in result.stdout.splitlines() if line.strip()]

    assert len(workspaces) == 2, result.stderr
    assert workspaces[0] != workspaces[1]
    assert len(list(transaction.iterdir())) == 2


def test_a_run_never_removes_another_runs_workspace(tmp_path: Path) -> None:
    """Cleanup is scoped to one tracked path — no pattern, no wildcard."""
    transaction = tmp_path / "transaction"
    foreign = transaction / "run.FOREIGN"
    foreign.mkdir(parents=True)
    (foreign / "previous.KEEP").write_bytes(b"# another run's evidence\n")

    result = run_bash(
        "set +e\n"
        f'source "{_posix(INSTALLER)}"\n'
        "set +e\n"
        f'workspace="$(open_transaction_workspace "{_posix(transaction)}")"\n'
        'close_transaction_workspace "$workspace"\n'
        'printf "outcome=%s gone=%s\\n" "$?" "$([[ -e "$workspace" ]] || echo yes)"\n'
    )

    assert "outcome=0 gone=yes" in result.stdout, result.stdout + result.stderr
    assert (foreign / "previous.KEEP").read_bytes() == b"# another run's evidence\n"

    body = _installer_function_body(
        "close_transaction_workspace()", "# --- the locked source snapshot"
    )
    assert 'rm -rf -- "$workspace"' in body
    assert "*" not in body


def test_the_transaction_parent_must_be_root_owned(tmp_path: Path) -> None:
    """It holds copies of the sudoers policy a rollback would restore."""
    transaction = tmp_path / "transaction"
    transaction.mkdir()

    result, _gateway, _sudoers, _transaction = _run_installation(
        tmp_path, parent_owner="1000:1000"
    )

    assert "outcome=1" in result.stdout, result.stdout + result.stderr
    assert "root-owned 0700 directory" in result.stderr


@pytest.mark.parametrize("mode", ["755", "770", "777", "750"])
def test_the_transaction_parent_must_be_private(tmp_path: Path, mode: str) -> None:
    """Anything readable exposes the previous policy; anything writable steers it."""
    transaction = tmp_path / "transaction"
    transaction.mkdir()

    result, _gateway, _sudoers, _transaction = _run_installation(
        tmp_path, parent_mode=mode
    )

    assert "outcome=1" in result.stdout, mode
    assert "root-owned 0700 directory" in result.stderr


def test_a_transaction_parent_symlink_is_refused_before_it_is_followed() -> None:
    """Asserted on the shipped text: this filesystem will not create one."""
    state = _installer_function_body(
        "transaction_parent_state()", "# A root-owned 0700 scratch parent"
    )
    secure = _installer_function_body(
        "require_secure_transaction_parent()", "# Absent or safe and empty"
    )
    opener = _installer_function_body(
        "open_transaction_parent()", "# This run's own workspace"
    )

    assert state.index('! -L "$parent"') < state.index('-e "$parent"')
    assert secure.index('! -L "$parent"') < secure.index('-d "$parent"')
    assert opener.index('! -L "$parent"') < opener.index("install -d")


def test_a_dry_run_reports_stale_state_and_exits_as_the_real_run_would(
    tmp_path: Path,
) -> None:
    """A validation command must not answer "fine" for a state that refuses.

    The exit status is the same 65 the real installation would use, so a
    wrapper reading the status learns the same thing from either mode. Nothing
    is removed: the evidence outlives the report.
    """
    transaction = tmp_path / "transaction"
    stale = transaction / "run.ABC123"
    stale.mkdir(parents=True)
    (stale / "previous.XYZ").write_bytes(b"# old\n")

    result, _gateway, _sudoers, _transaction = _run_installation(
        tmp_path, dry_run=1
    )

    assert f"outcome={EX_STALE}" in result.stdout, result.stdout + result.stderr
    assert "stale transaction state" in result.stderr
    assert "would refuse" in result.stderr
    assert stale.is_dir()
    assert (stale / "previous.XYZ").read_bytes() == b"# old\n"


@pytest.mark.parametrize("which", ["gateway", "sudoers"])
def test_changing_a_source_after_staging_does_not_change_installed_bytes(
    tmp_path: Path, which: str
) -> None:
    """The bytes installed are the bytes validated.

    A concurrent ``deploy-main`` can fast-forward the checkout at any moment.
    Between the syntax check and the checksum, or between the checksum and the
    rename, that used to mean a host running bytes nothing had looked at. The
    snapshot is what closes it: everything after staging reads this run's copy,
    so rewriting the live file mid-transaction changes nothing that lands.

    The rewrite happens inside ``stage_source``, immediately after the copy is
    made, which is the narrowest possible expression of "the source moved".
    """
    name = "mgo-validate" if which == "gateway" else "mgo-validate.sudoers"
    tampered = b"# TAMPERED AFTER STAGING\n"
    result, gateway_target, sudoers_target = _install_pair(
        tmp_path,
        extra=(
            "stage_source() {\n"
            '    local source="$1" workspace="$2" name="$3"\n'
            '    is_regular_file "$source" || return 1\n'
            '    install -o root -g root -m 0600 "$source" "${workspace}/${name}"'
            " || return 1\n"
            f'    if [[ "$name" == "{name}" ]]; then\n'
            f"        printf '%s' '{tampered.decode()}' > \"$source\"\n"
            "    fi\n"
            '    printf \'%s\\n\' "${workspace}/${name}"\n'
            "}\n"
        ),
    )
    installed = gateway_target if which == "gateway" else sudoers_target

    assert "outcome=0" in result.stdout, result.stdout + result.stderr
    assert installed.read_bytes() != tampered, "the live source was installed"
    assert (tmp_path / "src" / name).read_bytes() == tampered, "the test did not tamper"


@pytest.mark.parametrize(
    ("label", "double", "message"),
    [
        (
            "staged gateway syntax",
            'gateway_syntax_is_valid() { case "$1" in */run.*)'
            " return 1 ;; esac; return 0; }",
            "staged gateway failed",
        ),
        (
            "staged sudoers policy",
            'policy_is_valid() { case "$1" in */run.*) return 1 ;; esac; return 0; }',
            "staged sudoers policy failed",
        ),
    ],
)
def test_an_invalid_staged_asset_refuses_before_any_target_is_touched(
    tmp_path: Path, label: str, double: str, message: str
) -> None:
    """Validation happens on the snapshot, and nothing is written until it passes."""
    result, gateway, sudoers = _install_pair(
        tmp_path,
        extra=double,
        existing_gateway="# the old gateway\n",
        existing_sudoers="# the old policy\n",
    )
    calls = (tmp_path / "calls.log").read_text(encoding="utf-8")

    assert "outcome=1" in result.stdout, label
    assert message in result.stderr, label
    assert "mv" not in calls, f"{label}: a target was published"
    assert gateway.read_text(encoding="utf-8") == "# the old gateway\n", label
    assert sudoers.read_text(encoding="utf-8") == "# the old policy\n", label


def test_an_installed_gateway_that_does_not_parse_restores_both_targets(
    tmp_path: Path,
) -> None:
    """The checksum says the bytes match; this says they still work.

    A gateway published under /usr/local/sbin that Bash cannot parse is a
    control plane that cannot be invoked at all — and the sudoers rule pointing
    at it would be the only way in.
    """
    result, gateway, sudoers = _install_pair(
        tmp_path,
        extra=(
            'gateway_syntax_is_valid() { case "$1" in */dst/*)'
            " return 1 ;; esac; return 0; }"
        ),
        existing_gateway="# the old gateway\n",
        existing_sudoers="# the old policy\n",
    )

    assert "outcome=1" in result.stdout, result.stdout + result.stderr
    assert "installed gateway failed" in result.stderr
    assert gateway.read_text(encoding="utf-8") == "# the old gateway\n"
    assert sudoers.read_text(encoding="utf-8") == "# the old policy\n"


def test_no_staged_file_is_written_beside_the_targets(tmp_path: Path) -> None:
    """Staging happens in the workspace, never in /etc/sudoers.d."""
    result, _gateway, sudoers = _install_pair(
        tmp_path,
        extra='policy_is_valid() { case "$1" in */dst/*) return 1 ;; esac; return 0; }',
        existing_gateway="old\n",
        existing_sudoers="# old\n",
    )

    assert "outcome=1" in result.stdout
    assert sorted(path.name for path in sudoers.parent.iterdir()) == [
        "mgo-validate",
        "mgo-validate.sudoers",
    ]


@pytest.mark.parametrize(
    ("label", "owner", "mode"),
    [
        # Each corrupts exactly one property, so neither check can pass for the
        # other's reason. A double that broke both would let either be deleted.
        ("wrong owner", "1000:1000", "700"),
        ("wrong group", "0:1000", "700"),
        ("world-readable", "0:0", "755"),
        ("world-writable", "0:0", "777"),
    ],
)
def test_a_workspace_that_is_not_root_owned_and_private_is_refused(
    tmp_path: Path, label: str, owner: str, mode: str
) -> None:
    """mktemp says what it did; this says what is on disk.

    A copy of the sudoers policy is about to go inside, so the directory's own
    type, ownership, mode and location are all proven first.
    """
    result, gateway, _sudoers = _install_pair(
        tmp_path,
        extra=(
            "stat() {\n"
            '    local format="$2" path="$3"\n'
            '    case "$path" in\n'
            "        */run.??????)\n"
            '            case "$format" in\n'
            f"                '%u:%g') printf '{owner}\\n' ;;\n"
            f"                '%a') printf '{mode}\\n' ;;\n"
            "            esac\n"
            "            return 0\n"
            "            ;;\n"
            "    esac\n"
            '    case "$format" in\n'
            "        '%u:%g') printf '0:0\\n' ;;\n"
            "        '%a')\n"
            '            case "$path" in\n'
            "                */run.??????/*) printf '600\\n' ;;\n"
            "                *) printf '700\\n' ;;\n"
            "            esac\n"
            "            ;;\n"
            "        *) printf '\\n' ;;\n"
            "    esac\n"
            "}\n"
        ),
    )
    calls = (tmp_path / "calls.log").read_text(encoding="utf-8")

    assert "outcome=1" in result.stdout, f"{label}: {result.stdout}{result.stderr}"
    assert "not a root-owned 0700 directory inside" in result.stderr, label
    assert "mv" not in calls, label
    assert not gateway.exists(), label


def test_a_workspace_outside_the_transaction_parent_is_refused(
    tmp_path: Path,
) -> None:
    """A workspace somewhere else is not this installer's workspace."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    result = run_bash(
        "set +e\n"
        f'source "{_posix(INSTALLER)}"\n'
        "set +e\n"
        f"{_installer_stat_double()}\n"
        f'require_secure_workspace "{_posix(elsewhere)}"'
        f' "{_posix(tmp_path / "transaction")}"\n'
        'printf "outcome=%s\\n" "$?"\n'
    )

    assert "outcome=1" in result.stdout


def test_cleanup_does_not_report_success_while_the_path_survives(
    tmp_path: Path,
) -> None:
    """"Not a directory" is not success.

    If the tracked path is still there as a regular file, this run left
    something behind and the next one will find it — attributed to nobody
    unless this run says so now.
    """
    leftover = tmp_path / "transaction" / "run.LEFT"
    leftover.parent.mkdir()
    leftover.write_bytes(b"not a directory\n")

    result = run_bash(
        "set +e\n"
        f'source "{_posix(INSTALLER)}"\n'
        "set +e\n"
        f'close_transaction_workspace "{_posix(leftover)}"\n'
        'printf "outcome=%s\\n" "$?"\n'
    )

    assert "outcome=1" in result.stdout
    assert leftover.exists()


def test_a_transaction_parent_that_cannot_be_inspected_fails_closed(
    tmp_path: Path,
) -> None:
    """A failed inspection is not an empty directory.

    ``find`` on an unreadable directory exits non-zero with no output, and
    treating that as "nothing is here" would turn the one condition this check
    exists to detect into a clean answer.
    """
    transaction = tmp_path / "transaction"
    transaction.mkdir()

    result = run_bash(
        "set +e\n"
        f'source "{_posix(INSTALLER)}"\n'
        "set +e\n"
        f"{_installer_stat_double()}\n"
        "find() { return 1; }\n"
        f'transaction_parent_state "{_posix(transaction)}"\n'
        'printf "outcome=%s\\n" "$?"\n'
    )

    assert "outcome=1" in result.stdout, result.stdout + result.stderr


def test_a_dry_run_with_an_uninspectable_parent_exits_non_zero(
    tmp_path: Path,
) -> None:
    """And the dry run reports it rather than passing."""
    (tmp_path / "transaction").mkdir()
    result, _gateway, _sudoers, _transaction = _run_installation(
        tmp_path, dry_run=1, extra="find() { return 1; }"
    )

    assert "outcome=0" not in result.stdout, result.stdout + result.stderr


def test_a_dry_run_with_an_unsafe_parent_exits_non_zero(tmp_path: Path) -> None:
    """A state the real installation refuses is not a passing validation."""
    (tmp_path / "transaction").mkdir()
    result, _gateway, _sudoers, _transaction = _run_installation(
        tmp_path, dry_run=1, parent_mode="777"
    )

    assert "outcome=1" in result.stdout, result.stdout + result.stderr
    assert "root-owned 0700 directory" in result.stderr


def test_a_dry_run_creates_no_workspace_and_removes_nothing(
    tmp_path: Path,
) -> None:
    """Read-only in both directions."""
    transaction = tmp_path / "transaction"
    transaction.mkdir()
    result, gateway_target, sudoers_target, _t = _run_installation(
        tmp_path, dry_run=1
    )
    before = {
        path: (path.stat().st_ino, path.stat().st_mtime_ns)
        for path in (gateway_target, sudoers_target)
    }
    calls = (tmp_path / "calls.log").read_text(encoding="utf-8")

    assert "outcome=0" in result.stdout, result.stdout + result.stderr
    assert list(transaction.iterdir()) == []
    assert calls.strip() == "", f"a mutating command ran: {calls!r}"
    for path, (inode, mtime) in before.items():
        assert path.stat().st_ino == inode
        assert path.stat().st_mtime_ns == mtime


def test_a_dry_run_says_it_holds_no_lock() -> None:
    """It validates a point-in-time view and must not imply more."""
    body = _installer_function_body("dry_run_installation()", "main()")

    assert "no lock is held" in body
    assert "acquire_transaction_lock" not in body
    assert "install_file" not in body
    assert "open_transaction_workspace" not in body


def test_the_stale_check_precedes_the_idempotent_return() -> None:
    """Ordering is the whole correction.

    The stale refusal lives in ``run_installation``, before it calls
    ``install_pair``; the idempotent return lives inside ``install_pair``,
    which is only reached afterwards. The currency decision moved there because
    it now has to compare against the staged checksums.
    """
    entry = _installer_function_body("run_installation()", "dry_run_installation()")
    transaction = _installer_function_body("install_pair()", "abort_pair()")

    assert entry.index("stale transaction state is present") < entry.index(
        "install_pair \\"
    )
    assert "verified; nothing changed" in transaction
    assert "verified; nothing changed" not in entry


# --------------------------------------------------------------------------
# the update-main wrapper
# --------------------------------------------------------------------------


def _wrapper_body() -> str:
    """The wrapper's executable lines, without its explanatory comments."""
    return "\n".join(
        line
        for line in _read(UPDATE_MAIN).splitlines()
        if line.strip() and not line.strip().startswith("#")
    )


def test_the_wrapper_delegates_to_the_gateway() -> None:
    """One job: hand off to the gateway and get out of the way."""
    body = _wrapper_body()

    assert 'readonly GATEWAY="/usr/local/sbin/mgo-validate"' in body
    assert 'exec sudo -n "$GATEWAY" deploy-main' in body


@pytest.mark.parametrize(
    "forbidden",
    ["git ", "uv ", "chgrp", "chmod", "find ", "systemctl", "curl", "pull"],
)
def test_the_wrapper_retains_no_deployment_logic(forbidden: str) -> None:
    """A second, weaker deployment path is the defect this replaced."""
    assert forbidden not in _wrapper_body()


def test_the_wrapper_has_no_fallback_around_the_gateway() -> None:
    """If the gateway is missing, install it — do not deploy around it."""
    body = _wrapper_body()

    assert "is not installed at" in _read(UPDATE_MAIN)
    assert "install-mgo-validate.sh" in _read(UPDATE_MAIN)
    assert body.count("exec sudo") == 1


def test_the_wrapper_refuses_to_run_as_root() -> None:
    """The gateway raises its own privilege; the caller should not."""
    body = _wrapper_body()

    assert '[[ "${EUID}" -eq 0 ]]' in body
    assert "exit 1" in body


# --------------------------------------------------------------------------
# the isolated wrapper harness
# --------------------------------------------------------------------------
#
# The wrapper is the one shipped entry point whose whole job is to hand control
# to something the host owns. Executing the tracked file to test that makes the
# result depend on whether the host has a gateway installed — which is how a
# Raspberry Pi staging run ended up invoking the real control plane through
# sudo. Everything below executes a *copy* whose gateway constant points inside
# a temporary directory, with a fake ``sudo`` in front of the real one.


PRODUCTION_GATEWAY_CONSTANT = 'readonly GATEWAY="/usr/local/sbin/mgo-validate"'

# The tripwire's exit code. Distinctive on purpose: 0 and 1 are both codes the
# wrapper can produce by itself, so neither would prove the code came through.
FAKE_SUDO_EXIT = 42

# One line per invocation, written before the arguments, so "exactly once" is a
# count rather than an inference from how many arguments were logged.
FAKE_SUDO_MARKER = "--"


class IsolatedWrapper(NamedTuple):
    """A disposable wrapper and the boundary that contains it."""

    wrapper: Path
    gateway: Path
    binaries: Path
    sudo_log: Path


def _make_executable(path: Path) -> None:
    """Best effort: Windows has no execute bit, and Git Bash infers one."""
    os.chmod(path, 0o755)


def _disposable_wrapper(root: Path, *, gateway_present: bool) -> IsolatedWrapper:
    """Build a copy of the tracked wrapper that cannot reach the host.

    This function reads ``UPDATE_MAIN`` and executes nothing — the split is
    deliberate, because it is what lets the suite's own audit state a rule with
    no exceptions: no function that executes anything may so much as name the
    tracked wrapper.
    """
    source = _read(UPDATE_MAIN)
    assert source.count(PRODUCTION_GATEWAY_CONSTANT) == 1, (
        "the wrapper's production gateway constant is not where this harness "
        "expects it; re-point the harness before it can isolate anything"
    )

    binaries = root / "bin"
    binaries.mkdir()
    gateway = root / "sbin" / "mgo-validate"
    gateway.parent.mkdir()
    sudo_log = root / "sudo.log"

    replacement = f'readonly GATEWAY="{_posix(gateway)}"'
    text = source.replace(PRODUCTION_GATEWAY_CONSTANT, replacement)
    executable_lines = "\n".join(
        line for line in text.splitlines() if not line.strip().startswith("#")
    )
    assert "/usr/local/sbin" not in executable_lines, (
        "the disposable copy can still name the installed gateway"
    )

    # Bytes, not ``write_text``: the wrapper is LF-only and a CRLF copy would
    # make the kernel — and Git Bash — hunt for an interpreter called ``bash\r``.
    wrapper = root / "update-main.sh"
    wrapper.write_bytes(text.encode("utf-8"))
    _make_executable(wrapper)

    fake_sudo = binaries / "sudo"
    fake_sudo.write_bytes(
        (
            "#!/bin/sh\n"
            "# The tripwire. It records what it was asked to do and never does\n"
            "# it: nothing here executes $1, and the real sudo is not on PATH\n"
            "# ahead of this file.\n"
            f"printf '%s\\n' '{FAKE_SUDO_MARKER}' >> \"{_posix(sudo_log)}\"\n"
            'for argument in "$@"; do\n'
            f"    printf '%s\\n' \"$argument\" >> \"{_posix(sudo_log)}\"\n"
            "done\n"
            f"exit {FAKE_SUDO_EXIT}\n"
        ).encode()
    )
    _make_executable(fake_sudo)

    if gateway_present:
        # Harmless and never run: the tripwire exits before it could be.
        gateway.write_bytes(b"#!/bin/sh\nprintf 'the gateway ran\\n'\nexit 0\n")
        _make_executable(gateway)

    return IsolatedWrapper(
        wrapper=wrapper, gateway=gateway, binaries=binaries, sudo_log=sudo_log
    )


def run_isolated_wrapper(
    harness: IsolatedWrapper,
) -> subprocess.CompletedProcess[str]:
    """Execute the disposable wrapper with the fake ``sudo`` in front.

    Never names the tracked wrapper, never names an installed path, and refuses
    to start the child at all unless the isolation it depends on is in place.
    """
    environment = dict(os.environ)
    environment["PATH"] = (
        _posix(harness.binaries) + os.pathsep + environment.get("PATH", "")
    )

    # Guards, not decoration. Each one is a way this harness could stop being a
    # boundary without a single assertion below noticing.
    assert environment["PATH"].split(os.pathsep)[0] == _posix(harness.binaries), (
        "the fake sudo is not first on the child's PATH"
    )
    assert (harness.binaries / "sudo").is_file(), "the sudo tripwire is missing"
    executable_lines = "\n".join(
        line
        for line in _read(harness.wrapper).splitlines()
        if not line.strip().startswith("#")
    )
    assert "/usr/local/sbin" not in executable_lines, (
        "refusing to execute a wrapper that names the installed gateway"
    )

    return subprocess.run(
        [_bash(), _posix(harness.wrapper)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=environment,
        cwd=str(harness.wrapper.parent),
        check=False,
    )


def test_the_wrapper_reports_a_missing_gateway_and_stops(tmp_path: Path) -> None:
    """Executed, not described — and against a copy, not against this host.

    This test used to run ``scripts/deploy/update-main.sh`` itself. On a
    workstation with nothing installed it reached the missing-gateway branch and
    looked correct; on the Raspberry Pi ``/usr/local/sbin/mgo-validate`` exists,
    so the same test executed ``sudo -n /usr/local/sbin/mgo-validate
    deploy-main`` against the live control plane. What it asserted therefore
    depended on the host rather than on the wrapper. It no longer can.
    """
    harness = _disposable_wrapper(tmp_path, gateway_present=False)

    result = run_isolated_wrapper(harness)

    assert not harness.gateway.exists(), "the gateway was meant to be absent"
    assert result.returncode == 1, result.stdout + result.stderr
    assert "not installed" in result.stderr
    assert "install-mgo-validate.sh" in result.stderr
    assert "sudo ./scripts/deploy/install-mgo-validate.sh" in result.stderr
    # The message names the temporary gateway, which is the proof that the copy
    # under test was the redirected one.
    assert _posix(harness.gateway) in result.stderr
    assert "/usr/local/sbin" not in result.stderr
    # The tripwire. If the wrapper reached for sudo with no gateway installed,
    # this is what says so — and on a host that has one, it is the difference
    # between a test and an incident.
    assert not harness.sudo_log.exists() or not _read(harness.sudo_log).strip(), (
        f"the wrapper invoked sudo with no gateway present: "
        f"{_read(harness.sudo_log) if harness.sudo_log.exists() else ''!r}"
    )


def test_the_wrapper_hands_the_gateway_its_exit_code_and_nothing_else(
    tmp_path: Path,
) -> None:
    """The delegation contract, executed rather than asserted about.

    ``exec sudo -n "$GATEWAY" deploy-main`` makes three promises at once: the
    exact command, exactly once, and the gateway's own exit code. A static
    ``"exec sudo" in body`` check proves none of them.
    """
    harness = _disposable_wrapper(tmp_path, gateway_present=True)

    result = run_isolated_wrapper(harness)

    assert result.returncode == FAKE_SUDO_EXIT, result.stdout + result.stderr
    assert harness.sudo_log.is_file(), "the wrapper never delegated"

    recorded = _read(harness.sudo_log).splitlines()
    assert recorded.count(FAKE_SUDO_MARKER) == 1, recorded
    assert recorded[1:] == ["-n", _posix(harness.gateway), "deploy-main"], recorded

    # The gateway copy prints when it runs; the tripwire exits before that, so
    # an empty stdout is what proves nothing behind sudo was executed.
    assert result.stdout == "", result.stdout
    assert "/usr/local/sbin" not in _read(harness.sudo_log)


# --------------------------------------------------------------------------
# the suite's own boundary
# --------------------------------------------------------------------------
#
# A test module that executes shipped entry points is a program that runs on
# the host it is testing. The docstring at the top of this file claimed for
# months that nothing here touches a real sudo or a production path, and the
# claim was wrong the whole time, because nothing checked it. These registers
# and the audit below are the check. They are ordinary data and ordinary AST
# inspection deliberately: a comment cannot fail, and a substring search whose
# own assertion contains the substring passes against anything.


#: Every callable in this module that starts a process. A shape not named here
#: is not audited, which is why removing an entry is itself a registered
#: mutation.
EXECUTING_CALLABLES = frozenset(
    {
        "subprocess.run",
        "subprocess.Popen",
        "subprocess.call",
        "subprocess.check_call",
        "subprocess.check_output",
        "os.system",
        "os.popen",
        "os.execv",
        "os.execvp",
        "run_bash",
        "call_gateway_function",
        "run_isolated_wrapper",
    }
)

#: Assets this module may read but must never execute. The wrapper is here
#: because it is the only shipped script that resolves a host path and hands
#: control to it; the gateway and the installer both refuse an unprivileged
#: caller before they touch anything, so executing the repository copies of
#: those is a bounded thing to do.
UNEXECUTABLE_NAMES = frozenset({"UPDATE_MAIN"})

#: Installed paths. Naming one in a string is fine — half this suite asserts
#: that the shipped text contains them. Putting one in command position inside
#: something this module then runs is not.
HOST_CONTROL_PLANE_PATHS = (
    "/usr/local/sbin/mgo-validate",
    "/etc/sudoers.d",
    "/etc/garden-observatory",
    "/opt/garden-observatory",
    "/var/lib/garden-observatory",
    "/run/lock/mgo-deployment.lock",
    "/run/mgo-validate-tmp",
    "/run/mgo-validate-install",
)

#: Programs that change the host rather than report on it. Every one of these
#: appears in this suite only as a shell-function double, never as a command.
REAL_PRIVILEGE_COMMANDS = frozenset(
    {"sudo", "systemctl", "runuser", "flock", "visudo", "curl"}
)

_COMMAND_SEPARATORS = (";", "&", "|", "(", ")", "`", "$(", "{", "}", "\n")
_COMMAND_INTRODUCERS = (
    "exec",
    "sudo",
    "command",
    "eval",
    "source",
    "then",
    "else",
    "do",
)


def _dotted_name(node: ast.expr) -> str:
    """``subprocess.run`` from the call's ``func``, or "" for anything else."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_dotted_name(node.value)}.{node.attr}"
    return ""


def _referenced_names(node: ast.AST) -> set[str]:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def _string_constants(node: ast.AST) -> list[str]:
    return [
        n.value
        for n in ast.walk(node)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
    ]


def _in_command_position(text: str, index: int) -> bool:
    """Would a shell read ``text[index:]`` as the start of a command?

    A path or program name that appears as an *argument* — inside quotes, after
    another word — is data. The same token at the start of a line, after a
    separator, or after ``exec``/``sudo`` is something that runs.
    """
    prefix = text[:index]
    if prefix.endswith(('"', "'")):
        prefix = prefix[:-1]
    prefix = prefix.rstrip(" \t")
    if not prefix:
        return True
    if any(prefix.endswith(separator) for separator in _COMMAND_SEPARATORS):
        return True
    for introducer in _COMMAND_INTRODUCERS:
        if prefix == introducer:
            return True
        if prefix.endswith(introducer) and prefix[-len(introducer) - 1] in " \t\n;|&(":
            return True
    return False


def _local_assignments(function: ast.AST) -> dict[str, ast.expr]:
    """``name -> value`` for the plain assignments inside one function."""
    found: dict[str, ast.expr] = {}
    for node in ast.walk(function):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    found[target.id] = node.value
    return found


def _executed_program(argv: ast.expr) -> ast.expr | None:
    """The element of an argv list that names what will actually run.

    When the interpreter is ``_bash()`` the program is the first element after
    it that is not an option, so ``[_bash(), "-n", str(asset)]`` and
    ``[_bash(), _posix(script)]`` both resolve to the thing being run.
    """
    if not isinstance(argv, ast.List) or not argv.elts:
        return None
    head = argv.elts[0]
    if isinstance(head, ast.Call) and _dotted_name(head.func) == "_bash":
        for element in argv.elts[1:]:
            constants = _string_constants(element)
            if len(constants) == 1 and constants[0].startswith("-"):
                continue
            return element
        return None
    return head


def _parametrised_aliases(function: ast.FunctionDef, unsafe: set[str]) -> set[str]:
    """Parameters bound by ``parametrize`` to a collection holding an asset.

    ``SHELL_ASSETS`` contains the wrapper, so a test parametrised over it holds
    the wrapper in a variable that is never spelled ``UPDATE_MAIN``.
    """
    aliases: set[str] = set()
    for decorator in function.decorator_list:
        if not isinstance(decorator, ast.Call):
            continue
        if not _dotted_name(decorator.func).endswith("parametrize"):
            continue
        if len(decorator.args) < 2:
            continue
        argnames, argvalues = decorator.args[0], decorator.args[1]
        if not (isinstance(argnames, ast.Constant) and isinstance(argnames.value, str)):
            continue
        if _referenced_names(argvalues) & unsafe:
            aliases.update(part.strip() for part in argnames.value.split(","))
    return aliases


def _is_syntax_check(call: ast.Call) -> bool:
    """``bash -n`` parses a script; it never runs a statement of it."""
    return "-n" in _string_constants(call)


def test_no_test_can_reach_the_host_control_plane() -> None:
    """The suite's no-real-sudo claim, enforced against the suite's own AST.

    Four rules, each corresponding to a way the boundary has broken or could:

    1. a function that executes anything may not name the tracked wrapper —
       which is exactly what the Raspberry Pi staging failure was;
    2. what an executing call actually runs may not resolve to the wrapper,
       including through a ``parametrize`` alias or a local variable;
    3. nothing this module runs may put an installed path in command position;
    4. nothing this module runs may invoke a real privileged command.

    Deliberately structural. An earlier version of this promise was a sentence
    in the module docstring, and a substring search for ``sudo`` would have been
    satisfied by its own assertion.
    """
    source = _read(Path(__file__))
    tree = ast.parse(source)

    module_level = {
        target.id: node.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    # A collection is unsafe to execute if it holds something unsafe to execute.
    unsafe_collections = {
        name
        for name, value in module_level.items()
        if _referenced_names(value) & UNEXECUTABLE_NAMES
    }

    failures: list[str] = []
    for function in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
        calls = [
            node
            for node in ast.walk(function)
            if isinstance(node, ast.Call)
            and _dotted_name(node.func) in EXECUTING_CALLABLES
        ]
        operational = [call for call in calls if not _is_syntax_check(call)]
        where = f"{function.name} (line {function.lineno})"

        # 1 — naming and executing, in one function.
        named = _referenced_names(function) & UNEXECUTABLE_NAMES
        if operational and named:
            failures.append(f"{where} executes and also names {sorted(named)}")

        # 2 — what is actually run.
        assignments = _local_assignments(function)
        aliases = _parametrised_aliases(
            function, unsafe_collections | UNEXECUTABLE_NAMES
        )
        for call in operational:
            if not call.args:
                continue
            argv = call.args[0]
            if isinstance(argv, ast.Name) and argv.id in assignments:
                argv = assignments[argv.id]
            program = _executed_program(argv)
            if program is None:
                continue
            reached = _referenced_names(program) & (UNEXECUTABLE_NAMES | aliases)
            if reached:
                failures.append(
                    f"{where} executes {sorted(reached)} at line {call.lineno}"
                )

        # 3 and 4 — what the executed text says. Arguments passed by name are
        # resolved one level, so a command built into a variable is still read.
        for call in calls:
            reachable = list(_string_constants(call))
            for node in ast.walk(call):
                if isinstance(node, ast.Name):
                    bound = assignments.get(node.id) or module_level.get(node.id)
                    if bound is not None:
                        reachable.extend(_string_constants(bound))

            for text in reachable:
                for path in HOST_CONTROL_PLANE_PATHS:
                    index = text.find(path)
                    while index != -1:
                        if _in_command_position(text, index):
                            failures.append(
                                f"{where} runs the installed path {path} "
                                f"(line {call.lineno})"
                            )
                        index = text.find(path, index + 1)
                for program_name in sorted(REAL_PRIVILEGE_COMMANDS):
                    if text.strip() == program_name:
                        failures.append(
                            f"{where} passes {program_name} as a program "
                            f"(line {call.lineno})"
                        )
                    index = text.find(program_name)
                    while index != -1:
                        end = index + len(program_name)
                        after = text[end : end + 1]
                        if after in (" ", "\t") and _in_command_position(text, index):
                            failures.append(
                                f"{where} invokes the real {program_name} "
                                f"(line {call.lineno})"
                            )
                        index = text.find(program_name, index + 1)

    assert not failures, "the suite can reach the host:\n  " + "\n  ".join(failures)


def test_the_host_escape_audit_is_looking_at_something() -> None:
    """The audit passes vacuously if its registers are emptied.

    Every rule above is a lookup in a frozenset or a tuple, so the cheapest way
    to delete this boundary is not to touch the audit at all — it is to remove
    the entry it depends on. These are the entries that were the difference
    between a safe suite and a Raspberry Pi incident.
    """
    assert "subprocess.run" in EXECUTING_CALLABLES
    assert "run_bash" in EXECUTING_CALLABLES
    assert "call_gateway_function" in EXECUTING_CALLABLES
    assert "run_isolated_wrapper" in EXECUTING_CALLABLES

    assert "UPDATE_MAIN" in UNEXECUTABLE_NAMES

    assert "/usr/local/sbin/mgo-validate" in HOST_CONTROL_PLANE_PATHS
    assert "/etc/sudoers.d" in HOST_CONTROL_PLANE_PATHS
    assert "/opt/garden-observatory" in HOST_CONTROL_PLANE_PATHS

    assert "sudo" in REAL_PRIVILEGE_COMMANDS
    assert "systemctl" in REAL_PRIVILEGE_COMMANDS
    assert "runuser" in REAL_PRIVILEGE_COMMANDS

    # And the audit itself has to still be here to use them.
    module = ast.parse(_read(Path(__file__)))
    defined = {
        node.name for node in ast.walk(module) if isinstance(node, ast.FunctionDef)
    }
    assert "test_no_test_can_reach_the_host_control_plane" in defined
    assert "_disposable_wrapper" in defined
    assert "run_isolated_wrapper" in defined


def test_the_audit_rejects_the_shapes_it_exists_to_reject() -> None:
    """A boundary nobody has watched fail is a boundary nobody has tested.

    The rules are checked against synthetic modules rather than by breaking
    this one, so the evidence that they bite does not depend on a mutation run.
    """

    def audit(snippet: str) -> list[str]:
        tree = ast.parse(snippet)
        problems: list[str] = []
        for function in [
            n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
        ]:
            calls = [
                node
                for node in ast.walk(function)
                if isinstance(node, ast.Call)
                and _dotted_name(node.func) in EXECUTING_CALLABLES
            ]
            operational = [call for call in calls if not _is_syntax_check(call)]
            if operational and (_referenced_names(function) & UNEXECUTABLE_NAMES):
                problems.append("names the wrapper")
            for call in calls:
                for text in _string_constants(call):
                    for path in HOST_CONTROL_PLANE_PATHS:
                        index = text.find(path)
                        if index != -1 and _in_command_position(text, index):
                            problems.append("runs an installed path")
                    for name in REAL_PRIVILEGE_COMMANDS:
                        index = text.find(name)
                        if (
                            index != -1
                            and text[index + len(name) : index + len(name) + 1] == " "
                            and _in_command_position(text, index)
                        ):
                            problems.append("runs a real privileged command")
        return problems

    assert audit("def t():\n    subprocess.run([_bash(), str(UPDATE_MAIN)])\n")
    assert audit('def t():\n    run_bash("exec /usr/local/sbin/mgo-validate x")\n')
    assert audit('def t():\n    run_bash("sudo -n /usr/local/sbin/mgo-validate")\n')
    # And the shapes it must not reject, or the suite could not test anything:
    # a disposable copy, a production path passed as an argument to a doubled
    # function, and a shell-function double named after a real command.
    assert not audit("def t():\n    subprocess.run([_bash(), _posix(copy)])\n")
    assert not audit(
        'def t():\n    call_gateway_function(\'f "mgo" "/opt/garden-observatory"\')\n'
    )
    assert not audit('def t():\n    run_bash("visudo() { return 0; }\\n")\n')


# --------------------------------------------------------------------------
# documentation
# --------------------------------------------------------------------------


#: The two files whose contents are matched byte-exactly by the mutation
#: register. Several ``old`` anchors span lines, so a CRLF checkout silently
#: turns every one of them stale.
MUTATION_CRITICAL_ASSETS = (
    "tests/test_deployment_gateway.py",
    "tests/mutation_register.py",
)


def test_mutation_critical_python_assets_are_forced_to_lf() -> None:
    """A checkout artefact must not be able to fake a stale mutation.

    ``core.autocrlf=true`` is the Windows default and this repository has no
    repository-wide Python rule, so before these lines existed a fresh clone
    checked both files out as CRLF and the register's multi-line anchors
    stopped matching code that had not changed.

    Asserting the current bytes are LF is not enough: the working copy is LF
    right now precisely *because* someone noticed. What has to hold is that
    the next checkout is instructed to keep it that way, which is why the
    attribute Git actually resolves is asserted rather than the file it was
    read from.
    """
    attributes = _read(PROJECT_ROOT / ".gitattributes")
    for asset in MUTATION_CRITICAL_ASSETS:
        assert f"{asset} text eol=lf" in attributes, asset

    for asset in MUTATION_CRITICAL_ASSETS:
        assert b"\r\n" not in (PROJECT_ROOT / asset).read_bytes(), asset

    resolved = subprocess.run(
        ["git", "check-attr", "text", "eol", "--", *MUTATION_CRITICAL_ASSETS],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )

    assert resolved.returncode == 0, resolved.stderr
    for asset in MUTATION_CRITICAL_ASSETS:
        assert f"{asset}: text: set" in resolved.stdout, resolved.stdout
        assert f"{asset}: eol: lf" in resolved.stdout, resolved.stdout


DEPLOYMENT_DOC = PROJECT_ROOT / "docs" / "Deployment-Gateway.md"


def test_the_deployment_document_exists_and_is_linked() -> None:
    """The gateway is not discoverable unless the docs point at it."""
    assert DEPLOYMENT_DOC.exists()

    for source in (
        PROJECT_ROOT / "README.md",
        PROJECT_ROOT / "scripts" / "README.md",
        PROJECT_ROOT / "docs" / "Remote-Access.md",
    ):
        assert "Deployment-Gateway.md" in _read(source), source.name


@pytest.mark.parametrize(
    "topic",
    [
        "approval file",
        "show-approval",
        "deploy-main",
        "restart-api",
        "fast-forward",
        "--frozen",
        "rollback",
        "sudoers",
        "install-service-identity.sh",
        "capture",
    ],
)
def test_the_deployment_document_covers_the_contract(topic: str) -> None:
    """Each promise the gateway makes is written down where operators look."""
    assert topic in _read(DEPLOYMENT_DOC)


@pytest.mark.parametrize(
    "statement",
    [
        "### 13a. Production installation record",
        "| Installed | 2026-08-05 |",
        "71d3755cd3807acffa09173fe743c9ef109faa9f",
        "3e26a7cea23d15944f8f0b8c9949e3bb79ce555c95c64a4b45a174e038f0bf3e",
        "a34191d54a7139f107834861c53e6fa4db96501bfbea67ba457adc09055cbebc",
        "**The legacy wildcard policy was retired in the same authorised sequence.**",
        "/usr/local/sbin/mgo-validate *",
        "it must not be restored",
        "no wildcard, no `SETENV`",
        "/root/mgo-gateway-install-71d3755",
        "/root/mgo-gateway-preinstall-backup-71d3755",
        "/root/mgo-legacy-sudoers-retirement-71d3755",
        "### Production validation",
        "**proven on their production forward paths**",
    ],
)
def test_the_deployment_document_records_the_production_installation(
    statement: str,
) -> None:
    """An operator reading the gateway doc must learn what is actually on the host.

    Kept to the operational facts — what is installed, what the policy now
    permits, what evidence is retained, and what has not been run through it.
    The narrative belongs in the task record, and duplicating it here would
    give the project two accounts to keep in step.
    """
    assert statement in _read(DEPLOYMENT_DOC)


def test_the_deployment_document_records_the_incident() -> None:
    """The reason the gateway exists is part of the gateway's documentation."""
    text = _read(DEPLOYMENT_DOC)

    assert "task-010-operations" in text
    assert "128" in text
    assert "Production was untouched" in text


def _shell_blocks(markdown: str) -> list[str]:
    """Extract fenced ``bash`` blocks — the runnable part of a document.

    Prose that *names* an obsolete command in order to explain why it is gone
    is not a bypass; a copy-pasteable block still teaching it would be.
    """
    blocks: list[str] = []
    current: list[str] | None = None
    for line in markdown.splitlines():
        if line.strip() == "```bash":
            current = []
        elif line.strip() == "```" and current is not None:
            blocks.append("\n".join(current))
            current = None
        elif current is not None:
            current.append(line)
    return blocks


def test_the_remote_access_document_no_longer_teaches_the_old_path() -> None:
    """A documented bypass is a bypass — checked against runnable blocks."""
    text = _read(PROJECT_ROOT / "docs" / "Remote-Access.md")
    deployment = text[text.index("## 8. Deployment") : text.index("## 9.")]
    commands = "\n".join(_shell_blocks(deployment))

    assert "git pull" not in commands
    assert "chgrp" not in commands
    assert "chmod" not in commands
    assert "systemctl restart" not in commands
    assert "uv sync" not in commands
    assert "mgo-validate deploy-main" in commands
    assert "mgo-validate show-approval" in commands


@pytest.mark.parametrize(
    "document",
    ["docs/Deployment-Gateway.md", "docs/Remote-Access.md"],
)
def test_no_document_teaches_the_interpreter_discarding_invocation(
    document: str,
) -> None:
    """``sudo bash …`` discards the shebang, and with it privileged mode.

    Not a style point: the mode is what stops Bash reading ``BASH_ENV`` and
    importing exported shell functions before the script's first statement, and
    it can only travel on the shebang. An operator following a documented
    command that names the interpreter loses it silently.

    Checked against the **runnable blocks**, as the old-path test is. Prose is
    allowed to name the wrong form in order to warn against it; a fenced
    command block is what someone copies.
    """
    commands = "\n".join(_shell_blocks(_read(PROJECT_ROOT / document)))

    assert "sudo bash" not in commands
    assert "bash scripts/deploy/install-mgo-validate.sh" not in commands
    assert "bash /opt/garden-observatory/scripts/deploy/install-mgo" not in commands
    assert "install-mgo-validate.sh" in commands


@pytest.mark.parametrize(
    "asset",
    ["scripts/deploy/install-mgo-validate.sh", "scripts/deploy/update-main.sh"],
)
def test_no_shipped_script_prints_the_interpreter_discarding_invocation(
    asset: str,
) -> None:
    """What an operator is told to run must be the direct form.

    The installer's own ``--help`` and the wrapper's "it is not installed"
    message are both places someone reads a command and then types it.
    """
    text = _read(PROJECT_ROOT / asset)
    printed = "\n".join(
        line for line in text.splitlines() if not line.strip().startswith("#")
    )

    assert "sudo bash" not in printed
    assert "sudo ./" in printed or "sudo /opt" in printed


ACCEPTANCE_RECORD_PATH = (
    PROJECT_ROOT / "docs" / "tasks" / "Task-012-Physical-Camera-Acceptance.md"
)


def _remediation_status_section() -> str:
    """The acceptance record's remediation-status prose, up to its evidence table.

    Sliced by its own end marker rather than a character count: the section has
    grown twice now, and a fixed-width window silently stops covering the text
    it was written to police.
    """
    text = _read(ACCEPTANCE_RECORD_PATH)
    index = text.index("Remediation status")
    return text[index : text.index("### Evidence", index)]


def test_the_task_record_states_the_installation_without_overclaiming() -> None:
    """Installed is not exercised, and the record must not blur them.

    This test used to assert the opposite — that nothing had been installed.
    That was true when it was written and is not true now. What has to survive
    the change is the distinction it existed for: a control plane that is in
    place is not a control plane that has deployed anything.
    """
    section = _remediation_status_section()

    # Installed, with the identity of what was installed.
    assert "passed installation review" in section
    assert "**installed** on the Raspberry" in section
    assert "3e26a7ce" in section
    assert "a34191d5" in section
    assert "retired and archived" in section
    # The boundary installation did not cross, dated to when that was true,
    # followed by the live actions that have since crossed it.
    assert "**As at installation, installed was not yet exercised.**" in section
    assert "**Both actions have since passed in production.**" in section
    assert "`MainPID` 1494 → 16531" in section
    assert "`MainPID` 16531 → 17702" in section
    assert "the repository provably untouched" in section
    assert "the approval file is empty" in section
    assert "Rollback has **not** been deliberately triggered in production" in section
    # The first staging attempt keeps its classification.
    assert "first Raspberry Pi staging attempt (2026-08-04) was incomplete" in section
    assert "does not count as a pass" in section
    assert "Production was untouched" in section
    assert "Physical camera acceptance remains pending" in section
    # The escaped request was refused as an unsupported action, and this record
    # must not borrow the 2026-08-01 install failure's explanation for it.
    assert "does not implement `deploy-main` at all" in section
    assert "unsupported-action refusal" in section
    assert "the approval file was also empty" in section
    assert "the branch pin is not" in section


def test_the_remediation_record_states_the_installation_truthfully() -> None:
    """Reviewed, staged, installed, and now exercised for real.

    This test previously asserted the opposite of its last three lines — that
    neither action had run. That was true when written. What has to survive the
    change is the shape of the claim: each stage is stated at the strength the
    evidence supports, and the one stage with no production evidence at all
    still says so.
    """
    text = _read(REMEDIATION_RECORD)

    assert "**Deployment-gateway remediation is complete.**" in text
    assert "staging validation passed" in text
    assert "legacy wildcard sudoers policy is retired" in text
    assert "installed-policy boundary validation passed" in text
    assert "Raspberry Pi staging validation (first attempt) | **Incomplete**" in text
    assert "Raspberry Pi staging validation (second run) | **Passed**" in text
    assert "Legacy wildcard sudoers policy | **Retired**" in text
    assert "Installation on the Raspberry Pi | **Passed**" in text
    assert "Installation review | **Passed**" in text
    assert "Installed-policy boundary validation | **Passed**" in text
    # The two live actions, now passed.
    assert "Live `deploy-main` validation | **Passed**" in text
    assert "Live `restart-api` validation | **Passed**" in text
    assert "Deployment-gateway remediation | **Complete**" in text
    assert "`deploy-main` passed on 2026-08-05" in text
    assert "`restart-api` passed on 2026-08-05" in text
    # And the stages that still have no production evidence.
    assert "Production rollback | **Not deliberately triggered**" in text

    # The camera rows moved once Matthew accepted the prototype. They are five
    # rows rather than one because "physical camera acceptance" stopped being a
    # single verdict: one part of it passed and the rest was deferred, and a
    # single row cannot say both without saying neither.
    assert (
        "Functional prototype camera acceptance | **Passed** — Matthew, 2026-08-06"
        in text
    )
    assert "Production hardware hardening | **Deferred**" in text
    assert "24-hour observation | **Deferred** — not passed" in text
    assert "48-hour stability observation | **Deferred** — not passed" in text
    assert "Production camera stability | **Not claimed**" in text


def test_the_gateway_document_no_longer_says_the_actions_are_unexercised() -> None:
    """The operator-facing document must not describe a proven action as pending.

    Scoped to the production installation and validation record — the section
    an operator reads to learn what the host can do — rather than banned across
    the whole file, so a dated historical statement elsewhere stays legal.
    """
    text = _read(DEPLOYMENT_DOC)
    start = text.index("### 13a. Production installation record")
    section = text[start : text.index("## 14. Sudoers boundary", start)]

    for stale in (
        "have not yet been exercised",
        "not yet been exercised",
        "no live deployment has run",
    ):
        assert stale not in section, stale

    assert "### Production validation" in section
    assert "**proven on their production forward paths**" in section
    assert "**Passed 2026-08-05**" in section
    assert "recovered in **1 second**" in section
    assert "repository untouched" in section
    assert "Matthew withdrew the\napproval after each action" in section
    # And the limitation that survives two green forward paths.
    assert "**Rollback has not been deliberately triggered in production" in section


REMEDIATION_RECORD = (
    PROJECT_ROOT / "docs" / "tasks" / "Task-012-Deployment-Gateway-Remediation.md"
)


def _event_b_section() -> str:
    """The 2026-08-04 escape's own subsection, and nothing around it.

    Sliced rather than searched whole-file, because the point of these
    assertions is that Event A's mechanism does not appear *here*. It appears
    correctly several times elsewhere in the same document.
    """
    text = _read(REMEDIATION_RECORD)
    start = text.index("### Event B")
    return text[start : text.index("### Production non-interference", start)]


@pytest.mark.parametrize(
    "statement",
    [
        # The classification, stated as a classification.
        "TASK 12 GATEWAY PI STAGING VALIDATION INCOMPLETE",
        "Raspberry Pi staging validation (first attempt) | **Incomplete**",
        "It is not a pass, and it",
        # Production non-interference.
        "1aec2245010a1bd971d028be235c1864af6b46b3",
        "70709",
        "71087",
        "8346e732c2545ff369f6c4f0e3fc2e415d10993d8fe6b4b1b2c67600555183da",
        "Empty and unchanged",
        # What the attempt did and did not establish.
        "/usr/sbin/visudo -cf",
        "/bin/bash -p -n",
        "596 of 597 focused tests passed",
        "the mutation register;",
        "the unprivileged installer dry run.",
        # The correction.
        "The isolated wrapper harness",
        "disposable copy",
        "test_no_test_can_reach_the_host_control_plane",
        "The module docstring was rewritten only after that enforcement existed.",
        # And what that attempt left behind. Scoped to the attempt in the past
        # tense: a gateway *was* installed later, by a separately authorised
        # act, and this section is not about that one.
        "No gateway was installed by that attempt, and the production gateway was\n"
        "unchanged by it.",
    ],
)
def test_the_incomplete_pi_staging_attempt_is_recorded(statement: str) -> None:
    """An incident recorded as an outcome cannot be reviewed.

    Every fact here was established on the Raspberry Pi and cannot be recovered
    from this repository: what ran, what refused it, and what the host looked
    like before and after.
    """
    assert statement in _read(REMEDIATION_RECORD)


@pytest.mark.parametrize(
    "statement",
    [
        "2026-08-01",
        "`mgo-validate install`",
        "task-010-operations",
        "origin/task-010-operations",
        "128",
        "install-service-identity.sh",
        "service-identity\nand systemd provisioner",
        "Production remained untouched",
        "Event A — the 2026-08-01 install-action failure",
    ],
)
def test_the_2026_08_01_install_failure_is_recorded(statement: str) -> None:
    """Event A: the authorised deployment attempt that exited 128.

    The `install` action was pinned to a branch, required a ref that no longer
    existed, and provisioned service identity rather than deploying code. That
    is the correct explanation of *that* failure and it stays.
    """
    assert statement in _read(REMEDIATION_RECORD)


@pytest.mark.parametrize(
    "statement",
    [
        "2026-08-04",
        "sudo -n /usr/local/sbin/mgo-validate deploy-main",
        "Event B — the 2026-08-04 staging test escape",
        # The refusal mechanism, stated as the mechanism.
        "does not implement\n`deploy-main` at all",
        "unsupported-action refusal",
        "`show-approval`, `install` and `restart-api`",
        "The approval file was\nalso empty",
        # What did not happen.
        "Nothing was fetched, nothing was merged, no `uv sync` ran, no service was"
        "\nrestarted and no preview transition occurred",
        "no installed file changed",
        # And the honest reading of the refusal.
        "A gateway that accepted\n`deploy-main` could have changed production",
        "not evidence that the boundary\nheld",
    ],
)
def test_the_2026_08_04_staging_escape_is_recorded(statement: str) -> None:
    """Event B: the escaped test invocation and why it changed nothing.

    The installed gateway had no ``deploy-main``, so the request met the
    unsupported-action refusal. That is a fact about which gateway was
    installed, not about the boundary this branch exists to build.
    """
    assert statement in _read(REMEDIATION_RECORD)


def test_the_record_carries_the_staging_mechanism_correction() -> None:
    """The first version of this record gave the wrong reason, and says so."""
    text = _read(REMEDIATION_RECORD)

    assert "Correction to the initial staging record" in text
    assert (
        "did\nnot fail a main-versus-`task-010-operations` branch precondition" in text
    )
    assert "refused the action as\nunsupported" in text
    assert "applied to the separate 2026-08-01 `install`-action failure" in text
    # Superseding is stated, not implied — the commit message stays as written.
    assert "6b27891" in text


def test_the_two_gateway_events_are_not_conflated() -> None:
    """Event A's mechanism must not appear inside Event B's subsection.

    Both events involve the same installed legacy gateway and both ended with
    production untouched, which is exactly why the wrong explanation was
    plausible enough to be written down. The branch pin, the missing ref and
    the checkout's branch belong to the 2026-08-01 `install` failure alone.
    """
    section = _event_b_section()

    for borrowed in (
        "task-010-operations",
        "checkout is on",
        "checkout was on",
        "precondition",
        "pinned",
        "FEATURE_BRANCH",
        "128",
    ):
        assert borrowed not in section, borrowed

    # And the document as a whole still records Event A's mechanism correctly,
    # so this is a scoping rule rather than a deletion.
    assert "task-010-operations" in _read(REMEDIATION_RECORD)


def test_the_camera_record_does_not_conflate_the_two_events_either() -> None:
    """The summary in the acceptance record carries the same distinction."""
    text = _read(
        PROJECT_ROOT / "docs" / "tasks" / "Task-012-Physical-Camera-Acceptance.md"
    )
    start = text.index("**The first Raspberry Pi staging attempt (2026-08-04) was")
    section = text[start : text.index("### Evidence", start)]

    assert "does not implement `deploy-main` at all" in section
    assert "unsupported-action refusal" in section
    assert "the approval file was also empty" in section
    assert "the branch pin is not\nwhat stopped the escaped request" in section


def _first_staging_attempt_section() -> str:
    """The incomplete first attempt's own section, and nothing after it."""
    text = _read(REMEDIATION_RECORD)
    start = text.index("## Raspberry Pi staging validation — incomplete")
    return text[start : text.index("## The unsafe test, and the correction", start)]


def test_the_first_staging_attempt_is_never_described_as_passed() -> None:
    """The one summary that would make the record useless.

    Scoped to the first attempt's own section rather than the whole document,
    because a *second* staging run did later pass and the record now says so.
    An unscoped ban would force the record to omit a true later result in order
    to keep an earlier one honest, which is how documents start lying by
    subtraction.
    """
    section = _first_staging_attempt_section()

    for claim in (
        "staging validation passed",
        "staging validation complete",
        "Raspberry Pi validation passed",
        "PI STAGING VALIDATION PASSED",
    ):
        assert claim not in section, claim

    # The classification itself still has to be in there.
    assert "TASK 12 GATEWAY PI STAGING VALIDATION INCOMPLETE" in section


@pytest.mark.parametrize(
    "statement",
    [
        "## Raspberry Pi staging validation — the successful second run",
        "a separate, later event from the incomplete",
        "| Ruff | Passed |",
        "| mypy | Passed — 50 source files |",
        "| Gateway-focused suite | **654 passed** |",
        "| Acceptance-document suite | **28 passed** |",
        "| Full Pi suite | **2333 passed, 1 skipped** |",
        "single-branch disposable clone has no local `main` ref",
        "| Mutation register | **All 166 mutations detected** |",
        "| Stale mutations | **Zero** |",
        "| Outer command tripwire | Clean |",
        "| Exec tracing | Clean |",
        "| Real `sudo` executed | **None** |",
        "| Installed gateway invoked | **Never** |",
        "| Unprivileged installer dry run | Exited zero |",
        "| Production | Unchanged |",
        "| Disposable clone | Removed |",
    ],
)
def test_the_successful_second_staging_run_is_recorded(statement: str) -> None:
    """The run the installation was authorised from.

    Every number here was established on the Raspberry Pi and cannot be
    recovered from this repository, which is the whole reason it is written
    down rather than re-derived.
    """
    assert statement in _read(REMEDIATION_RECORD)


def test_the_power_failure_not_the_installation_explains_the_baseline_change() -> None:
    """A reboot moved the PID and stopped preview. The installation did not.

    Both stories end with a different ``MainPID`` and a stopped preview, and
    only one of them is true. The comfortable version — that the gateway work
    is what changed the running service — is the one this test exists to keep
    out, because it would turn an untouched production host into an apparently
    modified one.
    """
    text = _read(REMEDIATION_RECORD)
    start = text.index("## Power-failure baseline transition")
    section = text[start : text.index("## Legacy sudoers retirement", start)]

    assert "power failure rebooted the Pi at **2026-08-04 14:34:59 SAST**" in section
    assert "started successfully at\n**2026-08-04 14:35:10 SAST**" in section
    assert "| `MainPID` | `1494` |" in section
    assert (
        "**The power failure, not the gateway installation, is what changed the\n"
        "baseline.**" in section
    )
    # The superseded figures are named, so a reader meeting them elsewhere in
    # the repository knows which service they belong to.
    assert "`70709`" in section
    assert "`71087`" in section
    assert "both belong to the pre-power-failure service" in section
    assert "`preview.auto_start` remains `false`" in section
    assert "the preview correctly did not come back" in section
    # And the installation is recorded as having preserved that baseline,
    # rather than as having produced it.
    assert "The installation preserved the post-power baseline" in section

    # The acceptance record carries the same attribution.
    acceptance = _remediation_status_section()
    assert (
        "**Preview is currently stopped because of a power failure, not because of "
        "the\ninstallation.**" in acceptance
    )


@pytest.mark.parametrize(
    "statement",
    [
        "dd3f71c2f69c1c7dfbfd602aafd3ad9d9bf02d7686f9b656078ba99d9186333e",
        "Defaults:claude !requiretty",
        "claude ALL=(root) NOPASSWD: /usr/local/sbin/mgo-validate *",
        "The third is a **wildcard grant**",
        "byte-for-byte identical and passes `visudo` validation",
        "/root/mgo-legacy-sudoers-retirement-71d3755",
        "the wildcard grant was **not** restored",
        # The stopped first attempt is a stopped attempt, not a failed one.
        "**The first retirement attempt stopped before any mutation.**",
        "the file held three",
        "attempt, not a failed retirement transaction",
    ],
)
def test_the_legacy_policy_retirement_is_recorded(statement: str) -> None:
    """Three directives, archived, removed, and never put back."""
    assert statement in _read(REMEDIATION_RECORD)


@pytest.mark.parametrize(
    "statement",
    [
        "3e26a7cea23d15944f8f0b8c9949e3bb79ce555c95c64a4b45a174e038f0bf3e",
        "a34191d54a7139f107834861c53e6fa4db96501bfbea67ba457adc09055cbebc",
        "`root:root`, mode `0755`",
        "`root:root`, mode `0440`",
        "The complete active configuration passes `visudo -c`.",
        "one `NOPASSWD` grant, for the exact gateway path",
        "**no legacy wildcard rule**",
        "**no `SETENV`**",
        "eleven command-scoped `env_delete` groups",
        # Root lock and transaction state, and an idempotent dry run.
        "`/run/lock/mgo-deployment.lock` is a root-owned `0600` regular file",
        "`/run/mgo-validate-install` is a root-owned `0700` directory",
        "the\n  transaction parent is empty",
        "reported both installed targets current",
        "inode, mtime, size, ownership and mode were unchanged across it",
        "the approval file remained empty and unchanged",
    ],
)
def test_the_installation_facts_are_recorded(statement: str) -> None:
    """What was installed, what it is allowed to do, and what it left behind."""
    assert statement in _read(REMEDIATION_RECORD)


def test_the_probe_count_is_stated_exactly() -> None:
    """Five probes plus one listing, written out rather than rounded.

    An earlier completion report called these "four authorised sudo probes".
    A probe count is exactly the kind of number that gets approximated in a
    summary and then cited back as evidence of what was exercised.
    """
    text = _read(REMEDIATION_RECORD)

    assert "**Five gateway or sudo probes were executed in total**" in text
    assert "additional `sudo -n -l` policy-listing command" in text
    assert "Six invocations, not four" in text
    assert "four authorised sudo probes" not in text
    # And why the extra listing was needed at all.
    assert "`/etc/sudoers.d` is mode `0750`" in text
    assert "`EACCES`, not `ENOENT`" in text


@pytest.mark.parametrize(
    "statement",
    [
        "| Production SHA | `1aec2245010a1bd971d028be235c1864af6b46b3`, unchanged |",
        "| `MainPID` | `1494`, unchanged |",
        "| `NRestarts` | `0`, unchanged |",
        "| Capture catalogue records | 8 |",
        "| Physical capture files | 5 |",
        "| Approval file | Empty and unchanged |",
        "No `deploy-main`, no `restart-api`, no application-code deployment",
    ],
)
def test_the_installation_changed_nothing_in_production(statement: str) -> None:
    """The installed control plane did not move the application."""
    text = _read(REMEDIATION_RECORD)
    start = text.index("## Production non-interference — the installation")
    section = text[start : text.index("## Retained evidence", start)]

    assert statement in section


@pytest.mark.parametrize(
    "statement",
    [
        "/root/mgo-gateway-install-71d3755",
        "/root/mgo-gateway-preinstall-backup-71d3755",
        "/root/mgo-legacy-sudoers-retirement-71d3755",
        "pending a separately reviewed closeout",
        "**None of this is cleaned by the record that documents it**",
        # The limitation list is now dated to the moment it described, because
        # the first two entries have since been discharged by the live actions.
        # It is kept, not deleted: it is what the installation review accepted.
        "**What was not established as at 2026-08-05 08:32",
        "`deploy-main` had **not** been invoked through the newly installed gateway",
        "`restart-api` had **not** been invoked through the newly installed gateway",
        "no live application deployment had yet proved the installed transaction",
        "physical camera acceptance remained pending",
        "the 24-hour and 48-hour physical camera gates had **not** started",
        "were not subsequently read",
        "An installed control plane is not a proven one.",
        "It has since been run.",
    ],
)
def test_the_retained_evidence_and_limitations_are_recorded(statement: str) -> None:
    """What is kept, and what is still unproven."""
    assert statement in _read(REMEDIATION_RECORD)


def _live_section(title: str, following: str) -> str:
    """One live-validation section of the remediation record."""
    text = _read(REMEDIATION_RECORD)
    start = text.index(title)
    return text[start : text.index(following, start)]


@pytest.mark.parametrize(
    "statement",
    [
        "**Passed on 2026-08-05.**",
        # Where production came from and went to, and that the move was legal.
        "`1aec2245010a1bd971d028be235c1864af6b46b3`",
        "`938134d4f4963256cd74b5bbf59123abe49e1d5d`",
        "**21 commits ahead, zero\nbehind**",
        "`git merge-base --is-ancestor` exit 0",
        "`Updating 1aec224..938134d` / `Fast-forward`",
        # What the gateway did.
        "`uv sync --frozen`",
        "**recovered in 1 second**",
        "sudo -n /usr/local/sbin/mgo-validate deploy-main",
        "invoked exactly once** and exited **0**",
        # The service transition.
        "**MainPID 1494 → 16531**",
        "**NRestarts still 0**",
        "`Wed 2026-08-05 15:10:08 SAST`",
        # Preservation, before and after.
        "**preview stopped**",
        "**preview still stopped**",
        "**producer count still zero**",
        "**still the same eight\nrecords**",
        # The readiness probe is not a failure.
        "**It is a probe,\nnot a deployment failure.**",
        # And the window was closed afterwards.
        "cleared the approval atomically",
        "exit **64** with empty stdout",
        # The setsid deviation is recorded as a safeguard, not a defect.
        "accepted operational\nsafeguard, not a gateway defect**",
    ],
)
def test_live_deploy_main_validation_is_recorded(statement: str) -> None:
    """The first real production deployment through the gateway.

    None of this can be re-derived from the repository: it happened on the
    Raspberry Pi, once, and the record is the only place it survives.
    """
    assert statement in _live_section(
        "## First live deploy-main validation",
        "## First live restart-api validation",
    )


@pytest.mark.parametrize(
    "statement",
    [
        "**Passed on 2026-08-05.**",
        "sudo -n /usr/local/sbin/mgo-validate restart-api",
        "invoked exactly once** and exited **0**",
        "**Recovery time: 1 second.**",
        "**MainPID 16531 → 17702**",
        "**NRestarts still 0**",
        # Production did not move.
        "production SHA **still**\n`938134d4f4963256cd74b5bbf59123abe49e1d5d`",
        "**No fetch was run during preflight.**",
        # And the proof that it did not, which an unchanged SHA alone is not.
        "**`restart-api` provably updated nothing.**",
        "a fetch that found nothing new looks identical",
        "the Git reflog received **no new entry**",
        "`FETCH_HEAD` mtime remained `2026-08-05 15:10:07`",
        "`.venv` mtime remained `2026-07-27 09:32:45`",
        "**no fetch, no merge, no dependency sync and no checkout movement**",
        # Preservation and closure.
        "**preview still stopped**",
        "**zero camera\nproducers**",
        "**still the same eight records**",
        "cleared the approval atomically",
        "exit **64** with empty stdout",
    ],
)
def test_live_restart_api_validation_is_recorded(statement: str) -> None:
    """The first real production restart through the gateway.

    The load-bearing part is not that the SHA was unchanged but that nothing
    was fetched, merged or synced — which is what separates this action from
    ``deploy-main``.
    """
    assert statement in _live_section(
        "## First live restart-api validation",
        "## Capture-catalogue baseline correction",
    )


def test_capture_catalogue_is_the_stable_eight_record_baseline() -> None:
    """Eight is a catalogue count, not a telemetry count.

    One operator instruction read the stable eight as an ``observations``
    count. Asserting that would have aborted a valid deployment; asserting it
    unchanged afterwards would have reported a false failure, because a restart
    necessarily spans a health-collection interval.
    """
    section = _live_section(
        "## Capture-catalogue baseline correction", "## Boundaries"
    )

    assert "**The stable eight-record baseline is the capture catalogue.**" in section
    assert "four `rpicam-still` and four\n`mock`" in section
    assert "not merely the same count" in section
    assert "physical JPEG count is **five**" in section

    # And the telemetry table is named as what it is.
    assert (
        "**`observations` is continuously growing operational telemetry.**" in section
    )
    assert "`health_snapshot` and `camera_status`" in section
    assert "**18,828 rows**" in section
    assert "**must not** be asserted equal before and after a service\n  action" in (
        section
    )
    assert "**must not** be described as the stable eight-record baseline" in section
    assert "is **not** a permanent invariant" in section

    # The operational document carries the same distinction.
    gateway_document = _read(DEPLOYMENT_DOC)
    assert "is the **capture catalogue**" in gateway_document
    assert "is not an invariant count" in gateway_document


def test_current_status_does_not_overclaim_rollback_or_physical_acceptance() -> None:
    """What two green forward paths do not entitle the record to say.

    Both live actions succeeded, which makes it tempting to describe the
    transaction as proven. It is not: the rollback branch has never run in
    production, and the hardware hardening gates were deferred rather than met.

    Task 12 now closes at functional prototype scope, which is the easiest claim
    in this repository to inflate. "Accepted" reads as "finished" unless the
    scope travels with it.
    """
    remediation = _read(REMEDIATION_RECORD)
    acceptance = _read(ACCEPTANCE_RECORD_PATH)
    gateway_document = _read(DEPLOYMENT_DOC)
    remediation_status = remediation[: remediation.index("## Why this task exists")]
    acceptance_status = acceptance[: acceptance.index("## Purpose")]

    for banned in (
        "rollback passed",
        "rollback validated",
        "rollback proven",
        "Production rollback | **Passed**",
        "Physical camera acceptance | **Complete**",
        "Physical camera acceptance | **Passed**",
        "Physical camera acceptance run | **Passed**",
        "Physical camera acceptance run | **Complete**",
        "24-hour observation | **Passed**",
        "48-hour stability | **Passed**",
        "Production camera stability | **Passed**",
        "Production camera stability | **Claimed**",
        "Production hardware hardening | **Complete**",
        "Production hardware hardening | **Passed**",
        "PRODUCTION CAMERA INSTALLATION HARDENED",
    ):
        assert banned not in remediation_status, banned
        assert banned not in acceptance_status, banned

    # The two time-gate phrases are not banned outright -- the status block now
    # denies them by name, and a ban that fires on the denial of a claim is
    # worse than no ban. Each occurrence must sit in a sentence that withholds
    # it instead.
    for line in acceptance_status.splitlines():
        if not any(
            phrase in line
            for phrase in ("CAMERA BRING-UP PASSED", "CAMERA PIPELINE STABLE")
        ):
            continue
        assert "not claimed" in line or "not passed" in line, line

    # Rollback is stated as untriggered, in both records that mention it.
    assert "Production rollback | **Not deliberately triggered**" in remediation_status
    assert "rollback has never been deliberately\ntriggered in production" in (
        remediation_status
    )
    assert "be induced solely to demonstrate it" in gateway_document

    # Task 12 closed at prototype scope; the hardening gates did not close at
    # all. Each one names itself so the deferral cannot be read as a sweep.
    # Both records now say so -- the remediation record said "Pending" until
    # 2026-08-06, which was true when written and became a contradiction of the
    # acceptance record the moment Matthew signed.
    assert (
        "Functional prototype camera acceptance | **Passed** — Matthew, 2026-08-06"
        in remediation_status
    )
    assert "Production hardware hardening | **Deferred**" in remediation_status
    assert "24-hour observation | **Deferred** — not passed" in remediation_status
    assert "48-hour stability observation | **Deferred** — not passed" in (
        remediation_status
    )
    assert "Production camera stability | **Not claimed**" in remediation_status

    # Scoped to the remediation record's own current status and gate table: the
    # three phrasings that were true until 2026-08-06 and are now contradictions
    # of the acceptance record. Dated historical sections elsewhere in that file
    # legitimately say acceptance *remained* pending at the time they describe,
    # and banning that globally would delete true history.
    for stale in (
        "Physical camera acceptance remains pending",
        "is now the remaining Task 12",
        "Physical camera acceptance remains pending in full",
        "Physical camera acceptance | **Pending**",
    ):
        assert stale not in remediation_status, stale

    assert "Functional prototype acceptance | **Passed**" in acceptance_status
    assert "Task 12 functional scope | **Complete**" in acceptance_status
    assert "Production hardware hardening | **Deferred**" in acceptance_status
    assert "Mechanical stability | **Deferred** — not performed" in acceptance_status
    assert "Camera disconnect | **Deferred** — not performed" in acceptance_status
    assert "24-hour observation | **Deferred** — not passed" in acceptance_status
    assert "48-hour stability | **Deferred** — not passed" in acceptance_status
    assert "Production camera stability | **Not claimed**" in acceptance_status

    # Root-only evidence was not inspected during the live actions, and the
    # record must not imply otherwise.
    assert "did **not** directly inspect the retained root evidence" in remediation
    assert "cannot distinguish absent from unreadable" in remediation

    # The gateway prerequisites are complete while acceptance is not.
    assert "Deployment-gateway prerequisites | **Complete**" in acceptance_status


def test_no_current_status_claims_physical_acceptance_completed() -> None:
    """Live gateway validation must not be mistaken for hardware acceptance.

    Both public mutating gateway actions have passed in production, and Task 12
    has since closed at functional prototype scope. This test guards the one
    distinction that closure blurs: a camera accepted as a working prototype is
    not a camera whose installation has been hardened.

    A status table is read faster than prose, so the rows that say so are
    asserted positively as well as the phrasings that would say the opposite.
    """
    remediation = _read(REMEDIATION_RECORD)
    acceptance = _read(ACCEPTANCE_RECORD_PATH)
    remediation_status = remediation[: remediation.index("## Why this task exists")]
    acceptance_status = acceptance[: acceptance.index("## Purpose")]

    # The deploy-main and restart-api bans this list once carried have been
    # removed: both actions have since passed in production, so banning a
    # "passed" row for them would now forbid the truth. The same has happened to
    # the acceptance run itself -- it ran, and Matthew accepted its result. What
    # stays banned is the scope that acceptance did not cover.
    for banned in (
        "Production hardware hardening | **Complete**",
        "Production hardware hardening | **Passed**",
        "Permanent mounting hardening | **Passed**",
        "Mechanical stability | **Passed**",
        "Camera disconnect | **Passed**",
        "24-hour observation | **Passed**",
        "48-hour stability | **Passed**",
        "Production camera stability | **Passed**",
        "Production camera stability | **Claimed**",
        "PRODUCTION CAMERA INSTALLATION HARDENED",
        # Deliberately not a bare "camera has been accepted": that phrase also
        # occurs inside the record's own denial of it, and a ban that fires on
        # the negation of the claim it polices is worse than no ban.
        "physical acceptance complete",
        "physical camera acceptance is complete",
    ):
        assert banned not in remediation_status, banned
        assert banned not in acceptance_status, banned

    # And the rows that state the true position are present.
    assert "Live `deploy-main` validation | **Passed**" in remediation_status
    assert "Live `restart-api` validation | **Passed**" in remediation_status
    assert (
        "Functional prototype camera acceptance | **Passed** — Matthew, 2026-08-06"
        in remediation_status
    )
    assert "Production camera stability | **Not claimed**" in remediation_status
    assert "Live `deploy-main` through the installed gateway | **Passed**" in (
        acceptance_status
    )
    assert "Live `restart-api` through the installed gateway | **Passed**" in (
        acceptance_status
    )

    # Task 12 closed, at a named scope, on the strength of gates that really ran.
    assert "Functional prototype acceptance | **Passed**" in acceptance_status
    assert "Task 12 functional scope | **Complete**" in acceptance_status
    assert "Application restart gate | **Passed**" in acceptance_status
    assert "Reboot recovery gate | **Passed**" in acceptance_status
    assert "Zero-hour checkpoint | **Recorded**" in acceptance_status

    # Everything the acceptance did not cover names itself as deferred, so the
    # closure cannot be read as a sweep across the whole checklist.
    assert "Production hardware hardening | **Deferred**" in acceptance_status
    assert "Permanent mounting hardening | **Deferred**" in acceptance_status
    assert "Mechanical stability | **Deferred** — not performed" in acceptance_status
    assert "Camera disconnect | **Deferred** — not performed" in acceptance_status
    assert "24-hour observation | **Deferred** — not passed" in acceptance_status
    assert "48-hour stability | **Deferred** — not passed" in acceptance_status
    assert "Production camera stability | **Not claimed**" in acceptance_status

    # The scope decision is attributed and dated, not left as an unexplained
    # softening of a contract that used to block.
    assert "## Scope decision — 2026-08-06" in acceptance
    assert "deliberate owner and project-priority decision" in acceptance
    assert "It is **not** an inferred test result" in acceptance

    # The next phase is unblocked, and unblocking is not the same as starting.
    assert (
        "The next application phase may proceed after repository review and merge."
        in acceptance_status
    )

    # The history sentence beneath the table must draw the same line the table
    # does. It previously said the deployment rows had *never* changed, which
    # stopped being true the moment the gateway was installed and then used —
    # and a false claim of immutability is worse than none, because it invites
    # a reader to trust the rows without asking what moved them. The same trap
    # reopened when the acceptance run began: a sentence saying those rows
    # "remain unchanged" went false the moment the run recorded its first gate.
    assert "The software and deployment-gateway rows" in acceptance_status
    assert "above were updated only after their separately authorised" in (
        acceptance_status
    )
    assert "Pi installation and live-validation runs." in acceptance_status
    assert "The physical-camera acceptance rows" in acceptance_status
    assert "were updated by the separately authorised hardware acceptance run" in (
        acceptance_status
    )
    assert (
        "Every `Deferred` row is deferred work, not a passed check, and\nmay be "
        "marked passed only by a later hardening run that actually performs it."
        in acceptance_status
    )
    assert (
        "**The camera is accepted as a working prototype, not as a\nhardened "
        "production installation**" in acceptance_status
    )

    # Scoped to the current status block: an earlier dated section elsewhere in
    # the file may legitimately describe a state in which nothing had yet been
    # deployed, and banning that text globally would delete true history.
    for stale in (
        "sign-off and gate rows have never changed",
        "Nothing has been deployed through the installed gateway",
        # Went false when the acceptance run started recording gates.
        "Physical camera acceptance has not been performed",
        "acceptance run, Matthew's visual sign-off, 24-hour gate and 48-hour "
        "gate remain unchanged",
        # Went false when Matthew accepted the prototype on 2026-08-06.
        "Physical camera acceptance is now **in progress**",
        "Task 12 is not complete",
    ):
        assert stale not in acceptance_status, stale


@pytest.mark.parametrize(
    "finding",
    [
        "Finding 1 — no exclusive deployment transaction",
        "Finding 2 — the pre-deployment preview state was not proven",
        "Finding 3 — a failed fast-forward bypassed the rollback",
        "Finding 4 — the installer followed unsafe target types",
        "Finding 5 — restart-api lost branch-aware validation",
        "Finding 1 — the lock object was not secured",
        "Finding 2 — the status document was pattern-matched, not parsed",
        "Finding 3 — curl obeyed configuration",
        "Finding 4 — the caller's environment was inherited",
        "Finding 1 — the implementation had not passed the complete mutation set",
        "Finding 2 — the root temporary directory was not secured",
        "Finding 3 — environment isolation did not begin at process entry",
        "Finding 4 — stale installer transaction state was hidden by idempotence",
        "Finding 1 — environment isolation started too late",
        "Finding 2 — a dry run returned success for a refused installation",
        "Finding 3 — source validation happened outside the lock",
    ],
)
def test_every_re_review_finding_is_recorded(finding: str) -> None:
    """Every round-2, final-review and final-confirmation finding is written down."""
    record = (
        PROJECT_ROOT / "docs" / "tasks" / "Task-012-Deployment-Gateway-Remediation.md"
    )

    assert finding in _read(record)


@pytest.mark.parametrize(
    "topic",
    [
        "/run/lock/mgo-deployment.lock",
        "First installation is the exception",
        "no fallback state",
        "starting",
        "transaction opens before the merge is invoked",
        "branch-aware",
        "root-owned `0600`",
        "umask 0077",
        "parsed as JSON",
        "--disable",
        "env -i",
        "/run/mgo-validate-tmp",
        # Final-confirmation corrections.
        "#!/bin/bash",
        "MGO_ENVIRONMENT_CONSTRUCTED=1",
        "allowlist over the whole environment",
        "every other exported variable",
        "env_reset",
        "mktemp --tmpdir=",
        "Stale transaction state is a refusal",
        "uniquely named workspace",
        "preserved",
        "`NaN`, `Infinity` and `-Infinity` are refused",
        "isolated mode",
        # Entry-boundary corrections.
        "#!/bin/bash -p",
        "privileged mode",
        "exported shell functions",
        "sudo ./scripts/deploy/install-mgo-validate.sh",
        "What a shell script cannot do",
        "the interpreter exists*",
        "Defaults!MGO_VALIDATE env_reset",
        "no `SETENV`",
        "The locked source snapshot",
        "No byte used for installation is read outside that snapshot",
        "What a dry run promises",
        "point-in-time",
    ],
)
def test_the_deployment_document_covers_the_round_two_corrections(
    topic: str,
) -> None:
    """The operator-facing model keeps pace with the code."""
    assert topic in _read(DEPLOYMENT_DOC)


@pytest.mark.parametrize(
    "topic",
    [
        "Testing this gateway without deploying through it",
        "disposable copy",
        "tripwire",
        "test_no_test_can_reach_the_host_control_plane",
        "The tracked wrapper is unchanged by any of this",
        "enforced rather than promised",
        # The two dry runs, told apart.
        "The two dry runs are not the same command",
        "./scripts/deploy/install-mgo-validate.sh --dry-run        # staging",
        "sudo ./scripts/deploy/install-mgo-validate.sh --dry-run   # authoritative",
        "It is *not* a substitute for the root form",
    ],
)
def test_the_deployment_document_covers_the_test_isolation_correction(
    topic: str,
) -> None:
    """The operator-facing model keeps pace with the suite, too."""
    assert topic in _read(DEPLOYMENT_DOC)


def test_the_script_index_distinguishes_the_two_dry_runs() -> None:
    """One of them is evidence about this host; the other is not."""
    text = _read(PROJECT_ROOT / "scripts" / "README.md")

    assert "./scripts/deploy/install-mgo-validate.sh --dry-run" in text
    assert "sudo ./scripts/deploy/install-mgo-validate.sh --dry-run" in text
    assert "authoritative pre-installation report" in text
    assert "not** a substitute" in text


def test_the_installer_header_distinguishes_the_two_dry_runs() -> None:
    """The header is where an operator reads the command before typing it."""
    header = "\n".join(
        line for line in _read(INSTALLER).splitlines() if line.startswith("#")
    )

    assert "Unprivileged staging/source validation" in header
    assert "Authoritative root pre-installation validation" in header
    assert "./scripts/deploy/install-mgo-validate.sh --dry-run" in header
    assert "sudo ./scripts/deploy/install-mgo-validate.sh --dry-run" in header
    assert "sudo ./scripts/deploy/install-mgo-validate.sh\n" in header + "\n"
    assert "is not a substitute for (2)" in header
    assert "cannot fully inspect them" in header
    # And the transaction summary says which bytes are validated against which.
    assert (
        "both staged source snapshots are validated before either installed" in header
    )


def test_the_mutation_register_is_part_of_the_repository() -> None:
    """A mutation set that exists only as an action taken cannot be re-run.

    That is the defect this register closes: earlier rounds recorded outcomes
    rather than mutations, so re-running them against a later tip meant
    reconstructing them from memory, and four had already gone stale unnoticed.
    """
    register = PROJECT_ROOT / "tests" / "mutation_register.py"
    runner = PROJECT_ROOT / "scripts" / "dev" / "run-mutations.py"

    assert register.is_file()
    assert runner.is_file()

    # Loaded by path: ``tests`` is a plain directory, not an importable
    # package, and making it one to satisfy a test would change collection.
    specification = importlib.util.spec_from_file_location(
        "mgo_mutation_register", register
    )
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    mutations = module.MUTATIONS

    identifiers = [mutation.identifier for mutation in mutations]
    assert len(identifiers) == len(set(identifiers)), "duplicate mutation id"
    assert len(mutations) >= 150

    # Every entry still applies to the current tip, exactly once. An entry that
    # no longer matches is a stale mutation, which is precisely the silent
    # failure this file exists to make loud.
    #
    # Read the way the runner reads — bytes decoded, not ``read_text``. The two
    # disagree: ``read_text`` translates CRLF to LF, so a multi-line ``old``
    # against a document that is CRLF in a Windows working tree passes here and
    # then goes stale in the runner. A staleness check that only holds on one
    # host is the thing this test exists to prevent.
    for mutation in mutations:
        asset = (PROJECT_ROOT / mutation.asset).read_bytes().decode("utf-8")
        assert asset.count(mutation.old) == 1, mutation.identifier
        assert mutation.tests, mutation.identifier
        assert mutation.note.endswith("."), mutation.identifier


@pytest.mark.parametrize(
    "finding",
    [
        "Finding 1 — approval parsing was not byte-exact",
        "Finding 2 — the installer relied on errexit inside a conditional",
        "Finding 3 — the installer had no real transaction",
        "Finding 4 — an identical installation still rewrote files",
        "Finding 5 — HTTP 200 was not enforced",
        "Finding 6 — a non-running preview was not preserved, only skipped",
        "Finding 7 — there was no final verification",
    ],
)
def test_every_review_finding_is_recorded(finding: str) -> None:
    """All seven findings and their corrections are written down."""
    record = (
        PROJECT_ROOT / "docs" / "tasks" / "Task-012-Deployment-Gateway-Remediation.md"
    )

    assert finding in _read(record)


# --------------------------------------------------------------------------
# environment safety
# --------------------------------------------------------------------------


def test_the_gateway_sets_a_fixed_safe_path_when_it_runs() -> None:
    """A caller-supplied PATH must not decide which binaries root executes."""
    source = _read(GATEWAY)

    assert 'PATH="$MGO_SAFE_PATH"' in source
    main_body = source[source.index("main() {") :]
    # The fixed path is now established by the constructed environment, which
    # main() requires before it looks at anything else.
    assert main_body.index("require_constructed_environment") < main_body.index(
        'case "$action"'
    )
    boundary = _function_body(
        "require_constructed_environment()", "# --- the root temporary directory"
    )
    assert 'PATH="$MGO_SAFE_PATH"' in boundary


def test_the_gateway_is_a_library_when_sourced_and_a_program_when_executed() -> None:
    """The guard is what makes the shipped logic testable without privilege."""
    source = _read(GATEWAY)

    assert 'if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then' in source
    assert source.rstrip().endswith("fi")


def test_sourcing_the_gateway_runs_no_action() -> None:
    """Sourcing must define functions and do nothing else."""
    result = run_bash(f'source "{_posix(GATEWAY)}"; printf "sourced\\n"')

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "sourced"
