"""Tests for the approved production deployment gateway.

These execute the *shipped* shell logic rather than describing it. The gateway
is written to be a library when sourced and a program when executed, so each
test sources ``scripts/deploy/mgo-validate`` in a real Bash process and calls
the real function against temporary directories, temporary Git repositories and
recorded command doubles.

Nothing here touches a production path, a real ``sudo``, a real ``systemd``, the
network or the Raspberry Pi. Where a boundary genuinely cannot be executed
safely — the root and systemd calls, and the symlink refusal on a filesystem
that will not create symlinks — the shipped text is asserted instead, and the
test says so.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

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
    source = _read(GATEWAY)
    symlink_index = source.index('[[ ! -L "$path" ]]')
    regular_index = source.index('[[ -f "$path" ]]')

    assert symlink_index < regular_index
    following = source[symlink_index : symlink_index + 200]
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


def test_the_root_environment_is_fixed_before_any_action_runs() -> None:
    """curl, stat, systemctl, flock and mktemp all inherit it."""
    body = _function_body("main()", "# Program when executed")
    # The first case validates the request; the dispatch case follows the
    # environment setup, so an unsupported action is refused before the host is
    # touched at all.
    validation = body.index('case "$action"')
    dispatch = body.index("require_root_caller")

    assert body.index('PATH="$MGO_SAFE_PATH"') < validation
    assert body.index('HOME="/root"') < validation
    assert body.index("unset GIT_DIR") < validation
    assert body.index('TMPDIR="$MGO_ROOT_TMPDIR"') < dispatch


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
    ],
)
def test_every_behaviour_altering_variable_is_unset_at_the_root_boundary(
    variable: str,
) -> None:
    """Named explicitly, so adding one to the list is a deliberate act."""
    body = _function_body("main()", "# Program when executed")
    unset_block = body[body.index("unset GIT_DIR") : body.index("if [[ ! -d")]

    assert variable in unset_block


def test_the_temporary_directory_is_root_controlled() -> None:
    """TMPDIR decides where response bodies and staging files are written."""
    source = _read(GATEWAY)

    assert 'readonly MGO_ROOT_TMPDIR="/run/mgo-validate-tmp"' in source
    body = _function_body("main()", "# Program when executed")
    assert "chmod 0700" in body


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
            "mktemp() { printf '%s\\n' \"$1\"; }\n"
            "http_get_200() { return 0; }\n"
            "discard_temporary() { return 0; }\n"
            "preview_state_from_file() {\n"
            f'    "{_python()}" -c \'import sys; sys.stdout.write("{state}\\n")\'\n'
            "}\n"
        ),
    )

    assert result.returncode != 0


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
    if fail:
        return "curl() { return 7; }\n"
    return (
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
        assert "--location" not in body, name
        assert " -L " not in body, name
        assert "--max-time" in body, name


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
        assert "mktemp" in body, name
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
    """The ambiguous verb is gone, and says where each meaning went."""
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
    """A previously stopped preview must not be running at the end."""
    result = _final_verification(
        expected_preview="stopped", preview="running", rpicam=1
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
    assert source.count("--untracked-files=all") >= 4


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
    allowed = {"${SUDO_USER:-}", "${1:-}", "${BASH_SOURCE[0]}"}
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

    assert text.startswith("#!/usr/bin/env bash")
    assert "set -Eeuo pipefail" in text or "set -euo pipefail" in text


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


def test_the_sudoers_policy_grants_one_account_one_path() -> None:
    """The narrowest rule that still works."""
    rules = [
        line.strip()
        for line in _read(SUDOERS).splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]

    assert rules == ["claude ALL=(root) NOPASSWD: /usr/local/sbin/mgo-validate"]


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
        # install(1) is doubled, so the transaction directory is made directly.
        "open_transaction_directory() { mkdir -p \"$1\"; }\n"
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
        ("installed policy validation", "policy_is_valid() { return 1; }"),
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
        ("installed policy validation", "policy_is_valid() { return 1; }"),
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
        extra="policy_is_valid() { return 1; }\nrestore_one() { return 1; }",
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
    """Correct files are verified and left alone — same inode, same mtime."""
    sources = tmp_path / "src"
    targets = tmp_path / "dst"
    sources.mkdir()
    targets.mkdir()
    gateway_source = sources / "mgo-validate"
    sudoers_source = sources / "mgo-validate.sudoers"
    gateway_source.write_bytes(b"#!/usr/bin/env bash\ntrue\n")
    sudoers_source.write_bytes(b"claude ALL=(root) NOPASSWD: /x\n")
    gateway_target = targets / "mgo-validate"
    sudoers_target = targets / "mgo-validate.sudoers"
    gateway_target.write_bytes(gateway_source.read_bytes())
    sudoers_target.write_bytes(sudoers_source.read_bytes())

    before = {
        path: (path.stat().st_ino, path.stat().st_mtime_ns)
        for path in (gateway_target, sudoers_target)
    }

    result = run_bash(
        "set +e\n"
        f'export CALL_LOG="{_posix(tmp_path / "calls.log")}"\n'
        ': > "$CALL_LOG"\n'
        f'source "{_posix(INSTALLER)}"\n'
        "set +e\n"
        f"{INSTALLER_DOUBLES}\n"
        "visudo() { return 0; }\n"
        'command() {\n'
        '    if [[ "$1" == "-v" ]]; then return 0; fi\n'
        '    builtin command "$@"\n'
        "}\n"
        "acquire_transaction_lock() { return 0; }\n"
        "run_installation "
        f'"{_posix(gateway_source)}" "{_posix(gateway_target)}" "0755" '
        f'"{_posix(sudoers_source)}" "{_posix(sudoers_target)}" "0440" 0 0 '
        f'"{_posix(tmp_path / "transaction")}" '
        f'"{_posix(tmp_path / "lock")}"\n'
        'printf "outcome=%s\\n" "$?"\n'
    )
    calls = (tmp_path / "calls.log").read_text(encoding="utf-8")

    assert "outcome=0" in result.stdout, result.stderr
    assert "nothing changed" in result.stdout
    assert calls.strip() == "", f"mutating commands ran: {calls!r}"
    for path, (inode, mtime) in before.items():
        assert path.stat().st_ino == inode, path.name
        assert path.stat().st_mtime_ns == mtime, path.name


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
        extra="policy_is_valid() { return 1; }",
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
        "open_transaction_directory()", "# Record a target's"
    )


def test_a_transaction_directory_failure_stops_before_mutation(
    tmp_path: Path,
) -> None:
    """No scratch space, no transaction."""
    result, gateway, _sudoers = _install_pair(
        tmp_path, extra="open_transaction_directory() { return 1; }"
    )
    calls = (tmp_path / "calls.log").read_text(encoding="utf-8")

    assert "outcome=1" in result.stdout
    assert "transaction directory could not be created" in result.stderr
    assert "install" not in calls
    assert not gateway.exists()


def test_a_cleanup_failure_after_success_is_not_success(tmp_path: Path) -> None:
    """A run that left the previous policy on disk has not finished."""
    result, _gateway, _sudoers = _install_pair(
        tmp_path, extra="close_transaction_directory() { return 1; }"
    )

    assert "outcome=1" in result.stdout
    assert "transaction directory could not be removed" in result.stderr


def test_a_cleanup_failure_after_rollback_is_reported(tmp_path: Path) -> None:
    """Restored but not tidied is still not a clean outcome."""
    result, _gateway, _sudoers = _install_pair(
        tmp_path,
        extra=(
            "policy_is_valid() { return 1; }\n"
            "close_transaction_directory() { return 1; }"
        ),
        existing_gateway="old\n",
        existing_sudoers="# old\n",
    )

    assert f"outcome={EX_RESTORE_FAILED}" in result.stdout
    assert "cleanup failed" in result.stderr


def test_a_successful_installation_leaves_no_transaction_artefacts(
    tmp_path: Path,
) -> None:
    """Nothing of the transaction survives it."""
    result, _gateway, _sudoers = _install_pair(
        tmp_path, existing_gateway="old\n", existing_sudoers="# old\n"
    )

    assert "outcome=0" in result.stdout, result.stderr
    assert not (tmp_path / "transaction").exists()


def test_a_successful_rollback_leaves_no_transaction_artefacts(
    tmp_path: Path,
) -> None:
    """Nor does a failed installation that restored cleanly."""
    result, _gateway, _sudoers = _install_pair(
        tmp_path,
        extra="policy_is_valid() { return 1; }",
        existing_gateway="old\n",
        existing_sudoers="# old\n",
    )

    assert "outcome=1" in result.stdout
    assert not (tmp_path / "transaction").exists()


def test_a_restoration_preserves_numeric_owner_group_and_mode() -> None:
    """Restoring an approximation of the file is not restoring the file."""
    body = _installer_function_body("record_previous()", "restore_one()")
    restore = _installer_function_body("restore_one()", "# Both targets, always")

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
    """Run a dry run with a deterministic fake ``visudo`` on PATH.

    A fake executable rather than a shell function, because the installer asks
    ``command -v`` whether the tool exists at all — which is the property under
    test.
    """
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    if visudo != "absent":
        exit_code = 0 if visudo == "valid" else 1
        path = fake_bin / "visudo"
        path.write_bytes(f"#!/usr/bin/env bash\nexit {exit_code}\n".encode())
        # chmod through bash, not Python: on Windows os.chmod only toggles the
        # read-only attribute, and Git Bash would not treat the file as
        # executable, so ``command -v`` would report it missing.
        run_bash(f'chmod +x "{_posix(path)}"')

    # A drive-lettered path cannot go on PATH as-is: the colon after the drive
    # letter is the PATH separator, so "C:/x" is searched as "C" and "/x".
    return run_bash(
        f"fake_bin='{_posix(fake_bin)}'\n"
        "if command -v cygpath >/dev/null 2>&1; then\n"
        '    fake_bin="$(cygpath -u "$fake_bin")"\n'
        "fi\n"
        'export PATH="$fake_bin:$PATH"\n'
        f'"{_posix(INSTALLER)}" --dry-run\n'
    )


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
    """Both checks come before the first thing that writes."""
    source = _read(INSTALLER)
    body = source[source.index("run_installation()") :]
    syntax = body.index("bash -n")
    validation = body.index("visudo -cf")
    install = body.index("install_pair \\")

    assert syntax < validation < install


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


def test_the_wrapper_reports_a_missing_gateway_and_stops(tmp_path: Path) -> None:
    """Executed, not described: the real script on a host with no gateway."""
    result = subprocess.run(
        [_bash(), str(UPDATE_MAIN)],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        check=False,
    )

    assert result.returncode == 1
    assert "not installed" in result.stderr
    assert "install-mgo-validate.sh" in result.stderr


def test_the_wrapper_preserves_the_gateway_exit_code_by_execing() -> None:
    """A wrapper that summarised the result could report a false success."""
    assert "exec sudo" in _wrapper_body()


# --------------------------------------------------------------------------
# documentation
# --------------------------------------------------------------------------


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


def test_the_task_record_does_not_claim_the_production_gateway_changed() -> None:
    """Implemented is not installed, and the record must not blur them."""
    record = (
        PROJECT_ROOT / "docs" / "tasks" / "Task-012-Physical-Camera-Acceptance.md"
    )
    text = _read(record)
    index = text.index("Remediation status")
    section = text[index : index + 1600]

    assert "not** been final-reviewed" in section
    assert "not** been installed" in section
    assert "not** been validated" in section
    assert "still the" in section
    assert "nothing about the production host changed" in section


def test_the_remediation_record_states_the_review_corrections_truthfully() -> None:
    """Corrected is not re-reviewed, and neither is installed or validated."""
    record = (
        PROJECT_ROOT / "docs" / "tasks" / "Task-012-Deployment-Gateway-Remediation.md"
    )
    text = _read(record)

    assert "Re-review round 2 findings corrected" in text
    assert "not yet final-reviewed" in text
    assert "Final review | **Not performed**" in text
    assert "Installation on the Raspberry Pi | **Not performed**" in text
    assert "Raspberry Pi validation of the gateway | **Not performed**" in text
    assert "production\ngateway is unchanged" in text
    assert "Physical camera acceptance remains pending" in text


@pytest.mark.parametrize(
    "finding",
    [
        "Finding 1 — no exclusive deployment transaction",
        "Finding 2 — the pre-deployment preview state was not proven",
        "Finding 3 — a failed fast-forward bypassed the rollback",
        "Finding 4 — the installer followed unsafe target types",
        "Finding 5 — restart-api lost branch-aware validation",
    ],
)
def test_every_re_review_finding_is_recorded(finding: str) -> None:
    """All five round-2 findings and their corrections are written down."""
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
    ],
)
def test_the_deployment_document_covers_the_round_two_corrections(
    topic: str,
) -> None:
    """The operator-facing model keeps pace with the code."""
    assert topic in _read(DEPLOYMENT_DOC)


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
    assert main_body.index('PATH="$MGO_SAFE_PATH"') < main_body.index('case "$action"')


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
