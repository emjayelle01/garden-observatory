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
    assert "malformed" in result.stderr


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
        preamble='runuser() { printf "runuser %s\\n" "$*"; }\n',
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "runuser -u claude -- git status"


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


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ('{"enabled":true,"state":"running","owner":"preview"}', "running"),
        ('{"enabled":true,"state":"stopped","owner":null}', "stopped"),
        ('{"state":"failed","last_error":null}', "failed"),
        ('{"state": "starting"}', "starting"),
        ("{}", "unknown"),
        ("", "unknown"),
    ],
)
def test_the_preview_state_is_read_from_the_status_document(
    body: str, expected: str
) -> None:
    """One bounded field read; the gateway needs no JSON parser."""
    result = call_gateway_function(f"preview_state_from_status '{body}'")

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == expected


def test_the_preview_state_read_prefers_the_first_state_field() -> None:
    """A nested document must not let a later field win."""
    body = '{"state":"running","camera":{"state":"available"}}'

    result = call_gateway_function(f"preview_state_from_status '{body}'")

    assert result.stdout.strip() == "running"


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
# preview preservation
# --------------------------------------------------------------------------


PREVIEW_DOUBLES = (
    "start_preview() { printf 'START\\n' >> calls; }\n"
    "count_processes() {\n"
    '    case "$1" in\n'
    "        rpicam-vid) printf '1\\n' ;;\n"
    "        *) printf '0\\n' ;;\n"
    "    esac\n"
    "}\n"
    "sleep() { :; }\n"
)


def _restore_preview(
    previous: str, *, current: str, extra: str = "", cwd: Path
) -> subprocess.CompletedProcess[str]:
    preamble = (
        PREVIEW_DOUBLES
        + f"read_preview_state() {{ printf '{current}\\n'; }}\n"
        + extra
    )
    return call_gateway_function(
        f'restore_preview_state "{previous}" "status" "start" 3',
        preamble=preamble,
        cwd=cwd,
    )


def test_a_previously_running_preview_is_restored(tmp_path: Path) -> None:
    """The deployment borrowed the camera and gives it back."""
    result = _restore_preview("running", current="running", cwd=tmp_path)
    # The restart left it stopped, then the start request brought it back.
    calls = tmp_path / "calls"

    assert result.returncode == 0, result.stderr
    assert not calls.exists() or calls.read_text(encoding="utf-8") == ""


def test_an_already_running_preview_receives_no_duplicate_start(
    tmp_path: Path,
) -> None:
    """A second start would be a duplicate request against an owned camera."""
    result = _restore_preview("running", current="running", cwd=tmp_path)

    assert result.returncode == 0, result.stderr
    assert "already running" in result.stdout
    assert not (tmp_path / "calls").exists()


def test_a_stopped_preview_that_was_running_is_started_once(
    tmp_path: Path,
) -> None:
    """Exactly one start request, then a poll until it reports running."""
    preamble = (
        PREVIEW_DOUBLES
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


@pytest.mark.parametrize("previous", ["stopped", "failed", "unknown"])
def test_a_preview_that_was_not_running_is_left_alone(
    tmp_path: Path, previous: str
) -> None:
    """Completing a deployment is not a reason to start a camera."""
    result = _restore_preview(previous, current="stopped", cwd=tmp_path)

    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "calls").exists()
    assert previous in result.stdout


def test_a_duplicate_producer_fails_the_restoration(tmp_path: Path) -> None:
    """Two producers means the old one was never reaped."""
    extra = (
        "count_processes() {\n"
        '    case "$1" in\n'
        "        rpicam-vid) printf '2\\n' ;;\n"
        "        *) printf '0\\n' ;;\n"
        "    esac\n"
        "}\n"
    )
    result = _restore_preview("running", current="running", extra=extra, cwd=tmp_path)

    assert result.returncode != 0


def test_a_legacy_producer_fails_the_restoration(tmp_path: Path) -> None:
    """A libcamera-vid producer is not the backend this deployment expects."""
    extra = (
        "count_processes() {\n"
        '    case "$1" in\n'
        "        rpicam-vid) printf '1\\n' ;;\n"
        "        libcamera-vid) printf '1\\n' ;;\n"
        "        *) printf '0\\n' ;;\n"
        "    esac\n"
        "}\n"
    )
    result = _restore_preview("running", current="running", extra=extra, cwd=tmp_path)

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
    assert '"$head" == "$approved"' in body
    assert '"$tracking" == "$approved"' in body
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

    assert "eval" not in source
    assert "${MGO_REPOSITORY:-" not in source
    assert "${MGO_APPROVAL_FILE:-" not in source


# --------------------------------------------------------------------------
# transaction rollback
# --------------------------------------------------------------------------


EX_ROLLBACK = 78


def _function_body(name: str, next_name: str) -> str:
    """Slice one shipped function out of the gateway, exactly."""
    source = _read(GATEWAY)
    start = source.index(name)
    return source[start : source.index(next_name, start)]


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
        ("fail_before_restart()", "fail_after_restart()"),
        ("fail_after_restart()", "# --- actions"),
    ):
        body = _function_body(name, following)
        assert "while" not in body
        assert "until" not in body
        # Neither handler calls the other, or itself, a second time.
        assert body.count("rollback_repository ") == 1


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


def test_the_installer_validates_sudoers_before_installing_anything() -> None:
    """An invalid policy in /etc/sudoers.d can lock every account out of sudo."""
    source = _read(INSTALLER)
    visudo = source.index("visudo -cf")
    install = source.index('log "installing $GATEWAY_TARGET"')

    assert visudo < install


def test_the_installer_checks_shell_syntax_before_installing() -> None:
    """A truncated gateway would still be executed by sudo."""
    source = _read(INSTALLER)

    assert source.index("bash -n") < source.index('log "installing $GATEWAY_TARGET"')


def test_the_installer_refuses_to_install_without_visudo() -> None:
    """No validator, no installation — never an unvalidated policy.

    The refusal is scoped to a real install: a dry run on a host without
    ``visudo`` reports the gap instead, because it installs nothing.
    """
    source = _read(INSTALLER)
    index = source.index(
        "visudo is not available; refusing to install an unvalidated policy"
    )

    assert "die" in source[index - 60 : index]
    assert 'elif [[ "$dry_run" -eq 1 ]]; then' in source
    assert "the policy was NOT validated" in source


def test_the_installer_requires_root_for_a_real_run() -> None:
    """Writing to /usr/local/sbin and /etc/sudoers.d needs privilege."""
    result = run_bash(f'EUID=1001; source "{_posix(INSTALLER)}"')

    assert result.returncode != 0


def test_the_installer_dry_run_needs_no_privilege_and_changes_nothing() -> None:
    """A dry run is a report, so it must be runnable by anyone, anywhere."""
    result = subprocess.run(
        [_bash(), str(INSTALLER), "--dry-run"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "nothing was changed" in result.stdout
    assert "would install" in result.stdout or "is current" in result.stdout


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


def test_the_installer_rolls_back_a_partial_installation() -> None:
    """A gateway with no policy, or a policy with no gateway, is not shipped."""
    source = _read(INSTALLER)
    index = source.index("the sudoers policy could not be installed")

    assert "restore_previous" in source[index - 400 : index]


def test_the_installer_verifies_what_reached_the_disk() -> None:
    """Trusting that a write succeeded is how a partial install goes unnoticed."""
    source = _read(INSTALLER)

    assert "verify_installed" in source
    assert "does not match its source" in source
    assert "wrong mode" in source
    assert "wrong owner" in source


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
    section = text[index : index + 900]

    assert "not** been reviewed" in section
    assert "not** been installed" in section
    assert "still the\nTask 10 one" in section or "still the" in section


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
