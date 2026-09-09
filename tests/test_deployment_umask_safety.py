"""The deployment gateway owns its publication umask (Task 14.5C).

The incident (Task 14.5B, 2026-09-07). ``deploy-main`` was invoked once, from a
wrapper that had set ``umask 077`` to protect its own log stage. ``sudo``
preserves the caller's umask, and the gateway set none of its own around the
commands that write the working tree, so the fast-forward created every file
the target touched as ``0600 claude:mgo``. The runtime probe -- correctly --
found that ``mgo`` could not import the application, the gateway restored the
previous commit and exited 70. The rollback rewrote the same files under the
same umask, so the *restored* checkout was unreadable by ``mgo`` too, and the
gateway reported ``rollback succeeded`` because Git said the tree was clean:
Git tracks the executable bit and nothing else, so a checkout whose every
source is ``0600`` is clean and unrunnable at once. The service survived only
because it was never restarted. Task 14.5B-R repaired 46 files by hand.

Two defects, then: the caller's umask reached the publication commands, and a
rollback was called successful without anyone asking the runtime account. This
module drives the real ``action_deploy_main`` -- the shipped shell, sourced,
against a disposable upstream, a disposable checkout and a disposable
interpreter launcher -- from a caller whose umask is ``0077``, and proves both
are closed.

Two hosts, one contract. On a kernel that honours umask (Linux, the Raspberry
Pi) the modes the deployment leaves behind are read from the filesystem and the
simulated runtime account is refused by them for real. Git for Windows' runtime
ignores umask and ``chmod`` for mode bits, which is why the incident could not
be reproduced there by inspection alone; so the harness also keeps a *kernel
model*: every publication command records the umask it ran under and the paths
it wrote, and the simulated runtime account is refused by the mode that umask
would have produced. Where the kernel is honest the model is checked against it
file by file, so a run on Linux proves the model that the Windows run relies
on. Neither host ever names a production path in command position: every seam
through which the gateway's fixed production constants leave the process is a
double that maps them onto the disposable checkout.
"""

from __future__ import annotations

import ast
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from mgo.core.config import PROJECT_ROOT

GATEWAY = PROJECT_ROOT / "scripts" / "deploy" / "mgo-validate"
SUDOERS = PROJECT_ROOT / "scripts" / "deploy" / "mgo-validate.sudoers"

#: The gateway's fixed production checkout. Never executed against: the harness
#: maps it onto the disposable checkout at every seam it can leave through.
PRODUCTION_CHECKOUT = "/opt/garden-observatory"

EX_PRECONDITION = 65
EX_DEPLOY = 70
EX_ROLLBACK = 78
EX_MANUAL_RECOVERY = 79

#: The umask the gateway must publish runtime files under, whatever it inherited.
PUBLICATION_UMASK = "0022"
#: The umask the Task 14.5B wrapper arrived with.
INCIDENT_UMASK = "0077"


# --------------------------------------------------------------------------
# bash
# --------------------------------------------------------------------------


def _bash() -> str:
    """Locate Bash on any supported development host (see the gateway suite)."""
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


def _posix(path: Path) -> str:
    return str(path).replace("\\", "/")


def _search_path_entry(path: Path) -> str:
    """Render a directory for Bash's ``PATH``.

    ``PATH`` is colon-separated, so a ``C:/...`` spelling would split at the
    drive letter; Git for Windows' runtime wants the ``/c/...`` form there.
    """
    rendered = _posix(path)
    if len(rendered) > 1 and rendered[1] == ":":
        return f"/{rendered[0].lower()}{rendered[2:]}"
    return rendered


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def run_bash(script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [_bash(), "-c", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def _probe_umask_enforcement() -> bool:
    """Does this kernel apply the umask to created files?

    Decided by doing it, not by ``sys.platform``: what matters is whether a
    ``stat`` after ``umask 0077`` reports ``600``.
    """
    with tempfile.TemporaryDirectory() as directory:
        result = run_bash(
            f'cd "{_posix(Path(directory))}" && (umask 0077 && : > probe) '
            "&& stat -c '%a' probe"
        )
    return result.returncode == 0 and result.stdout.strip() == "600"


UMASK_ENFORCED = _probe_umask_enforcement()


# --------------------------------------------------------------------------
# the disposable production
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


def _configure(repository: Path) -> None:
    _git(repository, "config", "user.email", "test@example.invalid")
    _git(repository, "config", "user.name", "Test")
    _git(repository, "config", "commit.gpgsign", "false")
    _git(repository, "config", "core.autocrlf", "false")


CONFIG_MODULE = '''\
"""A stand-in for mgo.core.config: the database lives beside the checkout."""

from pathlib import Path


class _Storage:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path


class _Config:
    def __init__(self, database_path: Path) -> None:
        self.storage = _Storage(database_path)


def load_config() -> _Config:
    return _Config(Path(__file__).resolve().parents[4] / "state" / "mgo.db")
'''


def _application_files(version: int, *, broken: bool = False) -> dict[str, str]:
    """The tracked files of the disposable application at one version.

    Version 2 is the incident shape: existing runtime sources change, a new
    runtime package directory appears, a non-runtime tracked file changes, and
    the build supports a higher schema.
    """
    imports = "import mgo.core.config\nimport mgo_dependency\n"
    if version == 2:
        imports += "from mgo.telemetry import bounds\n"
    if broken:
        imports += "import mgo.module_that_does_not_exist\n"
    files = {
        ".gitignore": "__pycache__/\n.venv/\n",
        "pyproject.toml": '[project]\nname = "mgo-disposable"\nversion = "0"\n',
        "uv.lock": "# frozen by the test fixture\n",
        "README.md": f"# disposable application, version {version}\n",
        "src/mgo/__init__.py": "",
        "src/mgo/core/__init__.py": "",
        "src/mgo/core/config.py": CONFIG_MODULE,
        "src/mgo/core/database.py": (
            f"CURRENT_SCHEMA_VERSION = {1 + version}\n"
        ),
        "src/mgo/api/__init__.py": "",
        "src/mgo/api/app.py": imports + f'APP = "version {version}"\n',
    }
    if version == 2:
        files["src/mgo/telemetry/__init__.py"] = ""
        files["src/mgo/telemetry/bounds.py"] = "LIMIT_SECONDS = 60.0\n"
    return files


def _write_files(root: Path, files: dict[str, str]) -> None:
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def _record_schema(database: Path, version: int) -> None:
    database.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database)
    try:
        connection.execute("DROP TABLE IF EXISTS schema_migrations")
        connection.execute("CREATE TABLE schema_migrations (version INTEGER)")
        connection.executemany(
            "INSERT INTO schema_migrations (version) VALUES (?)",
            [(value,) for value in range(1, version + 1)],
        )
        connection.commit()
    finally:
        connection.close()


LAUNCHER = """\
#!/bin/bash
# Test launcher standing in for the virtual environment's interpreter. A real
# venv interpreter finds the checkout through an editable-install .pth; this
# one states the same search path and hands everything else -- flags,
# environment, the -c program -- to the interpreter untouched.
export PYTHONPATH='{search_path}'
export SYSTEMROOT='{systemroot}'
exec '{interpreter}' "$@"
"""

FAKE_UV = """\
#!/bin/bash
# Test double for uv. It is the dependency-synchronisation seam: it creates a
# new runtime package directory and rewrites an existing runtime artefact, both
# under whatever umask it inherited, and records that umask and the model mode
# of everything it wrote. It never resolves, downloads or installs anything.
set -eu
ledger="$MGO_TEST_LEDGER"
case "${1:-}" in
    --version) printf 'uv 0.0.0-test-double\\n'; exit 0 ;;
    sync) ;;
    *) printf 'unsupported uv invocation\\n' >&2; exit 64 ;;
esac
[[ "${2:-}" == "--frozen" ]] || { printf 'sync was not frozen\\n' >&2; exit 65; }

count=0
[[ -f "$ledger/uv.count" ]] && count="$(<"$ledger/uv.count")"
count=$((count + 1))
printf '%s' "$count" > "$ledger/uv.count"

mask="$(umask)"
file_mode="$(printf '%o' $(( 0666 & ~8#$mask )))"
directory_mode="$(printf '%o' $(( 0777 & ~8#$mask )))"
# The sync acts on the directory it was started in, which must be the
# disposable checkout; paths are recorded in the checkout's own spelling.
[[ "$PWD" -ef "$MGO_TEST_CHECKOUT" ]] \\
    || { printf 'sync started outside the checkout\\n' >&2; exit 66; }
site="$MGO_TEST_CHECKOUT/.venv/lib/site-packages"

fresh="$site/mgo_synced_$count"
mkdir "$fresh"
printf 'SYNC = %s\\n' "$count" > "$fresh/__init__.py"
printf '%s %s\\n' "$fresh" "$directory_mode" >> "$ledger/modes"
printf '%s %s\\n' "$fresh/__init__.py" "$file_mode" >> "$ledger/modes"

dependency="$site/mgo_dependency/__init__.py"
unlink "$dependency"
if [[ "${MGO_TEST_UV_BREAK_ON:-0}" == "$count" ]]; then
    printf 'raise ImportError("the restored dependency is unusable")\\n' \\
        > "$dependency"
else
    printf 'VALUE = %s\\n' "$count" > "$dependency"
fi
printf '%s %s\\n' "$dependency" "$file_mode" >> "$ledger/modes"
printf 'uv sync %s umask=%s\\n' "$count" "$mask" >> "$ledger/events.log"
"""

HARNESS = r"""
# Doubles for every seam through which the gateway's fixed production
# constants leave the process, and for the host services a deployment talks
# to. Everything else -- Git, the interpreter launcher, the schema probes, the
# transaction itself -- is the shipped code, executed.

MGO_TEST_CHECKOUT='{checkout}'
MGO_TEST_LEDGER='{ledger}'
MGO_TEST_APPROVED='{approved}'
MGO_TEST_RUNTIME_ACCOUNT='mgo'
MGO_TEST_UMASK_ENFORCED='{enforced}'
MGO_TEST_RESTART_STATUS=0
MGO_TEST_FINAL_STATUS=0
export MGO_TEST_LEDGER MGO_TEST_CHECKOUT
PATH='{binaries}':"$PATH"

record() {{
    printf '%s\n' "$*" >> "$MGO_TEST_LEDGER/events.log"
}}

map_production_path() {{
    printf '%s' "${{1//\/opt\/garden-observatory/$MGO_TEST_CHECKOUT}}"
}}

model_mode() {{
    local base=0666
    [[ "$1" != "dir" ]] || base=0777
    printf '%o' $(( base & ~8#$2 ))
}}

record_model() {{
    printf '%s %s\n' "$1" "$2" >> "$MGO_TEST_LEDGER/modes"
}}

# The administrative runner. Records the umask every command ran under and, for
# the two commands that rewrite the working tree, the model mode of every file
# and directory the transition wrote.
run_as_admin() {{
    shift
    local -a command=()
    local argument
    for argument in "$@"; do
        command+=("$(map_production_path "$argument")")
    done
    local mask
    mask="$(umask)"
    record "admin umask=$mask ${{command[*]}}"

    case " ${{command[*]}} " in
        *" merge --ff-only "*|*" reset --hard "*)
            local before after status path
            before="$(git -C "$MGO_TEST_CHECKOUT" rev-parse HEAD)"
            local directories_before
            directories_before="$(builtin cd "$MGO_TEST_CHECKOUT" \
                && find src -type d | sort)"
            status=0
            "${{command[@]}}" || status=$?
            after="$(git -C "$MGO_TEST_CHECKOUT" rev-parse HEAD)"
            while IFS= read -r path; do
                [[ -n "$path" ]] || continue
                record_model "$MGO_TEST_CHECKOUT/$path" "$(model_mode file "$mask")"
            done < <(git -C "$MGO_TEST_CHECKOUT" diff --name-only "$before" "$after")
            while IFS= read -r path; do
                [[ -n "$path" ]] || continue
                record_model "$MGO_TEST_CHECKOUT/$path" "$(model_mode dir "$mask")"
            done < <(comm -13 <(printf '%s\n' "$directories_before") \
                <(builtin cd "$MGO_TEST_CHECKOUT" && find src -type d | sort))
            return "$status"
            ;;
        *)
            "${{command[@]}}"
            ;;
    esac
}}

# The runtime account is a different user in the checkout's group: it can read
# a file only with group-read set and enter a directory only with
# group-execute set. Where the kernel honours modes the real ones decide and
# the model is checked against them; elsewhere the model decides.
simulate_runtime_account_access() {{
    local -A modelled=()
    local path mode actual
    if [[ -f "$MGO_TEST_LEDGER/modes" ]]; then
        while read -r path mode; do
            modelled["$path"]="$mode"
        done < "$MGO_TEST_LEDGER/modes"
    fi
    local denied=""
    for path in "${{!modelled[@]}}"; do
        [[ -e "$path" ]] || continue
        mode="${{modelled[$path]}}"
        if [[ "$MGO_TEST_UMASK_ENFORCED" == 1 ]]; then
            actual="$(stat -c '%a' "$path")"
            if [[ "$actual" != "$mode" ]]; then
                record "model mismatch $path modelled=$mode actual=$actual"
                return 1
            fi
        fi
        if [[ -d "$path" ]]; then
            (( 8#$mode & 8#010 )) || denied="$path"
        else
            (( 8#$mode & 8#040 )) || denied="$path"
        fi
    done
    if [[ "$MGO_TEST_UMASK_ENFORCED" == 1 ]]; then
        local offending
        offending="$(find "$MGO_TEST_CHECKOUT/src" "$MGO_TEST_CHECKOUT/.venv" \
            \( -type f ! -perm -g=r \) -o \( -type d ! -perm -g=x \) \
            | head -n 1)"
        [[ -z "$offending" ]] || denied="$offending"
    fi
    if [[ -n "$denied" ]]; then
        record "runtime denied $denied"
        return 1
    fi
    return 0
}}

runuser() {{
    if [[ "$1" != "-u" || "$3" != "--" ]]; then
        record "runuser malformed $*"
        return 64
    fi
    local account="$2"
    shift 3
    local -a command=()
    local argument
    for argument in "$@"; do
        command+=("$(map_production_path "$argument")")
    done
    record "runtime account=$account umask=$(umask) ${{command[*]}}"
    if [[ "$account" == "$MGO_TEST_RUNTIME_ACCOUNT" ]]; then
        simulate_runtime_account_access || return 1
    fi
    "${{command[@]}}"
}}

# Three shipped functions read the production path directly on the root side
# rather than through the runners: the repository precondition check, the
# in-progress marker check and the sync's change of directory. Each keeps its
# real body and is handed the mapped path.
eval "original_$(declare -f require_repository_preconditions)"
require_repository_preconditions() {{
    original_require_repository_preconditions "$1" "$(map_production_path "$2")" \
        "$3" "$4" "$5"
}}
eval "original_$(declare -f operation_in_progress)"
operation_in_progress() {{
    original_operation_in_progress "$(map_production_path "$1")"
}}
eval "original_$(declare -f sync_environment)"
sync_environment() {{
    original_sync_environment "$1" "$(map_production_path "$2")"
}}

account_home() {{ printf '/home/%s\n' "$1"; }}
acquire_transaction_lock() {{ record "lock $1"; }}
validate_approval_file() {{ record "approval read"; printf '%s' "$MGO_TEST_APPROVED"; }}
clear_approval_file() {{ record "approval cleared"; return 1; }}
remote_matches_repository() {{ return 0; }}
service_is_active() {{ return 0; }}
endpoint_is_ok() {{ return 0; }}
read_stable_preview_state() {{ printf 'stopped'; }}
require_preview_baseline() {{ return 0; }}
require_producer_count() {{ return 0; }}
service_main_pid() {{ printf '4242'; }}
service_active_enter_timestamp() {{ printf 'Mon 2026-09-07 08:09:39 SAST'; }}
restart_service() {{ record "restart $1"; return "$MGO_TEST_RESTART_STATUS"; }}
await_recovery() {{ record "await"; printf '1'; return 0; }}
restore_preview_state() {{ record "preview-restore"; return 0; }}
final_verification() {{ record "final-verification"; return "$MGO_TEST_FINAL_STATUS"; }}
"""


@dataclass
class Deployment:
    root: Path
    upstream: Path
    checkout: Path
    ledger: Path
    binaries: Path
    database: Path
    harness: Path
    previous: str
    target: str = ""
    extra: dict[str, str] = field(default_factory=dict)

    @property
    def events(self) -> list[str]:
        path = self.ledger / "events.log"
        if not path.exists():
            return []
        return _read(path).splitlines()

    def event_index(self, needle: str) -> int:
        for index, line in enumerate(self.events):
            if needle in line:
                return index
        raise AssertionError(f"no event contains {needle!r}: {self.events}")

    def events_containing(self, needle: str) -> list[str]:
        return [line for line in self.events if needle in line]

    def modelled_modes(self) -> dict[Path, str]:
        path = self.ledger / "modes"
        modes: dict[Path, str] = {}
        if path.exists():
            for line in _read(path).splitlines():
                recorded, _, mode = line.rpartition(" ")
                modes[Path(recorded)] = mode
        return modes

    def head(self) -> str:
        return _git(self.checkout, "rev-parse", "HEAD")

    def tracking(self) -> str:
        return _git(self.checkout, "rev-parse", "origin/main")

    def publish_target(self, *, broken: bool = False) -> None:
        """Advance the upstream to version 2 and approve it."""
        _write_files(self.upstream, _application_files(2, broken=broken))
        _git(self.upstream, "add", "--all")
        _git(self.upstream, "commit", "--quiet", "-m", "version 2")
        self.target = _git(self.upstream, "rev-parse", "HEAD")
        text = _read(self.harness).replace(
            "MGO_TEST_APPROVED=''", f"MGO_TEST_APPROVED='{self.target}'"
        )
        self.harness.write_text(text, encoding="utf-8")

    def make_unreadable(self, relative: str) -> None:
        """A tracked runtime source the runtime account cannot read.

        Real on an honest kernel, modelled everywhere; the two agree, which the
        simulated account checks before it answers.
        """
        path = self.checkout / relative
        if UMASK_ENFORCED:
            path.chmod(0o600)
        # LF explicitly: the ledger is read by Bash, and on Windows a text-mode
        # newline would ride a CR into the mode field (Task 14.5E).
        with (self.ledger / "modes").open(
            "a", encoding="utf-8", newline="\n"
        ) as ledger:
            ledger.write(f"{_posix(path)} 600\n")

    def run(
        self, action: str, *, caller_umask: str, settings: str = ""
    ) -> subprocess.CompletedProcess[str]:
        """Run one gateway action as a caller who arrived with ``caller_umask``.

        The umask is set before the gateway is sourced, exactly as a wrapper's
        umask precedes the gateway when sudo starts it. ``settings`` are harness
        variables the scenario overrides, in Bash syntax.
        """
        script = (
            f"umask {caller_umask}\n"
            f'source "{_posix(GATEWAY)}"\n'
            f'source "{_posix(self.harness)}"\n'
            f"{settings}\n"
            f"{action}\n"
            "printf 'shell-umask=%s\\n' \"$(umask)\"\n"
        )
        return run_bash(script)


@pytest.fixture
def deployment(tmp_path: Path) -> Deployment:
    """A disposable production at version 1, with its upstream still at 1."""
    previous_umask = os.umask(0o022)
    try:
        root = tmp_path / "disposable"
        upstream = root / "upstream"
        checkout = root / "production"
        ledger = root / "ledger"
        binaries = root / "bin"
        database = root / "state" / "mgo.db"
        for directory in (upstream, ledger, binaries):
            directory.mkdir(parents=True)

        _git(upstream.parent, "init", "--quiet", "--initial-branch=main", str(upstream))
        _configure(upstream)
        _write_files(upstream, _application_files(1))
        _git(upstream, "add", "--all")
        _git(upstream, "commit", "--quiet", "-m", "version 1")
        previous = _git(upstream, "rev-parse", "HEAD")

        subprocess.run(
            ["git", "clone", "--quiet", str(upstream), str(checkout)],
            capture_output=True,
            text=True,
            check=True,
        )
        _configure(checkout)

        venv = checkout / ".venv"
        (venv / "bin").mkdir(parents=True)
        (venv / "lib" / "site-packages" / "mgo_dependency").mkdir(parents=True)
        (venv / "lib" / "site-packages" / "mgo_dependency" / "__init__.py").write_text(
            "VALUE = 0\n", encoding="utf-8"
        )
        search_path = os.pathsep.join(
            [_posix(checkout / "src"), _posix(venv / "lib" / "site-packages")]
        )
        launcher = venv / "bin" / "python"
        launcher.write_text(
            LAUNCHER.format(
                search_path=search_path,
                systemroot=os.environ.get("SYSTEMROOT", ""),
                interpreter=_posix(Path(sys.executable)),
            ),
            encoding="utf-8",
        )
        launcher.chmod(0o755)
        uvicorn = venv / "bin" / "uvicorn"
        uvicorn.write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
        uvicorn.chmod(0o755)

        fake_uv = binaries / "uv"
        fake_uv.write_text(FAKE_UV, encoding="utf-8")
        fake_uv.chmod(0o755)

        _record_schema(database, 2)

        harness = root / "harness.sh"
        harness.write_text(
            HARNESS.format(
                checkout=_posix(checkout),
                ledger=_posix(ledger),
                approved="",
                enforced="1" if UMASK_ENFORCED else "0",
                binaries=_search_path_entry(binaries),
            ),
            encoding="utf-8",
        )
    finally:
        os.umask(previous_umask)

    return Deployment(
        root=root,
        upstream=upstream,
        checkout=checkout,
        ledger=ledger,
        binaries=binaries,
        database=database,
        harness=harness,
        previous=previous,
    )


# --------------------------------------------------------------------------
# assertions shared by the scenarios
# --------------------------------------------------------------------------


def _runtime_tree(deployment: Deployment) -> list[Path]:
    paths: list[Path] = []
    for top in (deployment.checkout / "src", deployment.checkout / ".venv"):
        paths.extend(path for path in top.rglob("*"))
    return paths


def _assert_runtime_readable(deployment: Deployment) -> None:
    """Every runtime path the deployment wrote is readable by the group.

    Modelled modes are what the recorded umask would have produced; on an
    honest kernel the real modes are read as well and must agree.
    """
    modelled = deployment.modelled_modes()
    assert modelled, "no publication was modelled; nothing was written?"
    for path, mode in modelled.items():
        if not path.exists():
            continue
        bits = int(mode, 8)
        if path.is_dir():
            assert bits & 0o010, f"{path} is not group-traversable: {mode}"
        else:
            assert bits & 0o040, f"{path} is not group-readable: {mode}"
        if UMASK_ENFORCED:
            actual = oct(path.stat().st_mode & 0o777)[2:]
            assert actual == mode, f"{path}: modelled {mode}, real {actual}"
    if UMASK_ENFORCED:
        for path in _runtime_tree(deployment):
            bits = path.stat().st_mode
            if path.is_dir():
                assert bits & 0o010, f"{path} is not group-traversable"
            else:
                assert bits & 0o040, f"{path} is not group-readable"
    assert not deployment.events_containing("model mismatch")


def _assert_publications_used_the_gateway_umask(deployment: Deployment) -> None:
    publications = [
        line
        for line in deployment.events
        if " merge --ff-only " in line
        or " reset --hard " in line
        or line.startswith("uv sync ")
    ]
    assert publications, deployment.events
    for line in publications:
        assert f"umask={PUBLICATION_UMASK}" in line, line


def _assert_every_probe_ran_as_the_runtime_account(deployment: Deployment) -> None:
    probes = deployment.events_containing("runtime account=")
    assert probes, deployment.events
    for line in probes:
        assert line.startswith("runtime account=mgo "), line


def _assert_no_bytecode(deployment: Deployment) -> None:
    caches = [path for path in _runtime_tree(deployment) if path.name == "__pycache__"]
    assert caches == [], caches
    stray = [path for path in _runtime_tree(deployment) if path.suffix == ".pyc"]
    assert stray == [], stray


def _assert_probes_disable_bytecode(deployment: Deployment) -> None:
    imports = [
        line
        for line in deployment.events_containing("runtime account=")
        if " -c " in line
    ]
    assert imports, deployment.events
    for line in imports:
        assert "PYTHONDONTWRITEBYTECODE=1" in line, line
        assert " -B -c " in line, line


def _assert_no_production_path_reached_a_command(deployment: Deployment) -> None:
    for line in deployment.events:
        assert PRODUCTION_CHECKOUT not in line, line


# --------------------------------------------------------------------------
# A. a successful deployment under the incident's umask
# --------------------------------------------------------------------------


def test_a_deployment_from_a_restrictive_caller_publishes_a_readable_runtime(
    deployment: Deployment,
) -> None:
    """The Task 14.5B deployment, re-run: changed sources, a new package
    directory, a resynchronised dependency -- and a runtime account that can
    read all of it, because the gateway set its own umask."""
    deployment.publish_target()

    result = deployment.run("action_deploy_main", caller_umask=INCIDENT_UMASK)

    assert result.returncode == 0, result.stderr
    assert deployment.head() == deployment.target
    assert (deployment.checkout / "src" / "mgo" / "telemetry" / "bounds.py").exists()
    _assert_publications_used_the_gateway_umask(deployment)
    _assert_runtime_readable(deployment)
    _assert_every_probe_ran_as_the_runtime_account(deployment)
    _assert_no_bytecode(deployment)
    _assert_no_production_path_reached_a_command(deployment)
    assert len(deployment.events_containing("restart mgo.service")) == 1
    # The publication umask was scoped to the publication: the gateway's own
    # shell still has the umask it was started with afterwards.
    assert f"shell-umask={INCIDENT_UMASK}" in result.stdout


def test_the_changed_and_new_runtime_files_are_the_ones_that_were_modelled(
    deployment: Deployment,
) -> None:
    """The model covers exactly the incident's classes of path."""
    deployment.publish_target()
    deployment.run("action_deploy_main", caller_umask=INCIDENT_UMASK)

    modelled = {
        _posix(path.relative_to(deployment.checkout))
        for path in deployment.modelled_modes()
    }
    assert "src/mgo/api/app.py" in modelled
    assert "src/mgo/core/database.py" in modelled
    assert "src/mgo/telemetry" in modelled
    assert "src/mgo/telemetry/bounds.py" in modelled
    assert "README.md" in modelled
    assert ".venv/lib/site-packages/mgo_dependency/__init__.py" in modelled
    assert ".venv/lib/site-packages/mgo_synced_1" in modelled


# --------------------------------------------------------------------------
# B. a checkout that is already unreadable
# --------------------------------------------------------------------------


def test_an_unreadable_current_checkout_is_refused_before_any_repository_mutation(
    deployment: Deployment,
) -> None:
    """Git reports clean; the runtime account cannot read a source; nothing
    is fetched, moved, synchronised or restarted, and the message says why."""
    deployment.publish_target()
    deployment.make_unreadable("src/mgo/core/config.py")
    assert _git(deployment.checkout, "status", "--porcelain") == ""

    result = deployment.run("action_deploy_main", caller_umask=PUBLICATION_UMASK)

    assert result.returncode == EX_PRECONDITION, result.stderr
    assert (
        "runtime account cannot execute the environment already deployed"
        in result.stderr
    )
    assert "schema" not in result.stderr
    assert "drift" not in result.stderr
    assert deployment.head() == deployment.previous
    assert deployment.tracking() == deployment.previous
    assert deployment.events_containing(" fetch ") == []
    assert deployment.events_containing(" merge --ff-only ") == []
    assert deployment.events_containing(" reset --hard ") == []
    assert deployment.events_containing("uv sync") == []
    assert deployment.events_containing("restart ") == []
    assert deployment.events_containing("approval cleared") == []
    assert deployment.events_containing("runtime denied") != []


def test_the_current_runtime_is_asked_before_the_previous_schema_is(
    deployment: Deployment,
) -> None:
    """An unreadable build would otherwise fail the schema probe first and be
    reported as a build that cannot say what schema it supports."""
    deployment.publish_target()

    result = deployment.run("action_deploy_main", caller_umask=PUBLICATION_UMASK)

    assert result.returncode == 0, result.stderr
    first_probe = deployment.event_index("runtime account=mgo")
    first_schema = deployment.event_index("CURRENT_SCHEMA_VERSION")
    fetch = deployment.event_index(" fetch ")
    assert first_probe < first_schema < fetch


# --------------------------------------------------------------------------
# C. a pre-restart failure, rolled back under the incident's umask
# --------------------------------------------------------------------------


def test_a_pre_restart_rollback_restores_a_readable_runtime_and_proves_it(
    deployment: Deployment,
) -> None:
    """The incident's own path: the target cannot import, the previous commit
    comes back -- readable, resynchronised, and validated as mgo before the
    rollback is called successful. The service is never restarted."""
    deployment.publish_target(broken=True)

    result = deployment.run("action_deploy_main", caller_umask=INCIDENT_UMASK)

    assert result.returncode == EX_DEPLOY, result.stderr
    assert "rollback succeeded; the service was never restarted" in result.stderr
    assert deployment.head() == deployment.previous
    assert (
        _git(deployment.checkout, "status", "--porcelain", "--untracked-files=all")
        == ""
    )
    _assert_publications_used_the_gateway_umask(deployment)
    _assert_runtime_readable(deployment)
    _assert_every_probe_ran_as_the_runtime_account(deployment)
    assert deployment.events_containing("restart ") == []
    reset = deployment.event_index(" reset --hard ")
    restored_sync = [
        index
        for index, line in enumerate(deployment.events)
        if line.startswith("uv sync 2 ")
    ]
    assert restored_sync and restored_sync[0] > reset
    probes_after_reset = [
        index
        for index, line in enumerate(deployment.events)
        if line.startswith("runtime account=mgo") and " -c " in line and index > reset
    ]
    assert probes_after_reset, deployment.events
    assert probes_after_reset[0] > restored_sync[0]
    assert deployment.events_containing("runtime denied") == [], deployment.events


def test_content_rollback_alone_would_have_left_the_incident_modes(
    deployment: Deployment,
) -> None:
    """What the kernel model says a rollback under the caller's umask does.

    The regression above passes only because the gateway publishes under its
    own umask. This test states the counterfactual the incident proved: the
    same reset run under ``0077`` leaves every rewritten file ``0600``.
    """
    deployment.publish_target()
    _git(deployment.checkout, "fetch", "--quiet", "origin", "main")
    _git(deployment.checkout, "merge", "--ff-only", "--quiet", "origin/main")

    script = (
        f"umask {INCIDENT_UMASK}\n"
        f'source "{_posix(GATEWAY)}"\n'
        f'source "{_posix(deployment.harness)}"\n'
        f'run_as_admin claude git -C "{_posix(deployment.checkout)}" reset --hard '
        f'"{deployment.previous}" >/dev/null\n'
        "simulate_runtime_account_access && printf 'admitted\\n' "
        "|| printf 'denied\\n'\n"
    )
    result = run_bash(script)

    assert result.returncode == 0, result.stderr
    assert "denied" in result.stdout
    modelled = deployment.modelled_modes()
    rewritten = deployment.checkout / "src" / "mgo" / "api" / "app.py"
    assert modelled[rewritten] == "600"
    if UMASK_ENFORCED:
        assert oct(rewritten.stat().st_mode & 0o777) == "0o600"
    assert deployment.head() == deployment.previous
    assert _git(deployment.checkout, "status", "--porcelain") == ""


# --------------------------------------------------------------------------
# D. a restored runtime that cannot execute
# --------------------------------------------------------------------------


def test_a_restored_runtime_that_cannot_execute_is_not_a_successful_rollback(
    deployment: Deployment,
) -> None:
    """Content came back; the runtime did not. The gateway says so, exits 78,
    does not restart, and leaves the evidence."""
    deployment.publish_target(broken=True)

    result = deployment.run(
        "action_deploy_main",
        caller_umask=INCIDENT_UMASK,
        settings="export MGO_TEST_UV_BREAK_ON=2",
    )

    assert result.returncode == EX_ROLLBACK, result.stderr
    assert "rollback is INCOMPLETE" in result.stderr
    assert "previous commit and environment were restored" in result.stderr
    assert "cannot execute the restored environment" in result.stderr
    assert "production was NOT restored" in result.stderr
    assert "rollback succeeded" not in result.stderr
    assert deployment.head() == deployment.previous
    assert deployment.events_containing("restart ") == []
    assert (deployment.ledger / "events.log").exists()
    assert (deployment.ledger / "modes").exists()


def test_a_post_restart_rollback_whose_runtime_cannot_execute_is_not_restarted(
    deployment: Deployment,
) -> None:
    """After the restart, the same proof stands between restoration and the
    rollback restart: an unrunnable restored build is not started."""
    deployment.publish_target()

    result = deployment.run(
        "action_deploy_main",
        caller_umask=INCIDENT_UMASK,
        settings="MGO_TEST_FINAL_STATUS=1\nexport MGO_TEST_UV_BREAK_ON=2",
    )

    assert result.returncode == EX_ROLLBACK, result.stderr
    assert "rollback is INCOMPLETE" in result.stderr
    assert "rollback succeeded" not in result.stderr
    restarts = deployment.events_containing("restart mgo.service")
    assert len(restarts) == 1
    reset = deployment.event_index(" reset --hard ")
    assert deployment.event_index("restart mgo.service") < reset
    assert deployment.head() == deployment.previous


# --------------------------------------------------------------------------
# E. a post-restart failure with an equal schema
# --------------------------------------------------------------------------


def test_a_post_restart_rollback_validates_the_restored_runtime_before_restarting(
    deployment: Deployment,
) -> None:
    deployment.publish_target()

    result = deployment.run(
        "action_deploy_main",
        caller_umask=INCIDENT_UMASK,
        settings="MGO_TEST_FINAL_STATUS=1",
    )

    assert result.returncode == EX_DEPLOY, result.stderr
    assert result.stderr.rstrip().endswith("deployment failed; rollback succeeded")
    assert deployment.head() == deployment.previous
    _assert_publications_used_the_gateway_umask(deployment)
    _assert_runtime_readable(deployment)
    events = deployment.events
    reset = deployment.event_index(" reset --hard ")
    restarts = [i for i, line in enumerate(events) if "restart mgo.service" in line]
    assert len(restarts) == 2
    rollback_restart = restarts[1]
    restored_sync = next(
        i for i, line in enumerate(events) if line.startswith("uv sync 2 ")
    )
    restored_probe = next(
        i
        for i, line in enumerate(events)
        if i > restored_sync
        and line.startswith("runtime account=mgo")
        and " -c " in line
    )
    assert reset < restored_sync < restored_probe < rollback_restart
    assert events.index("await", rollback_restart) > rollback_restart
    assert "preview-restore" in events[rollback_restart:]


# --------------------------------------------------------------------------
# F. schema advancement
# --------------------------------------------------------------------------


def test_a_schema_advancement_refuses_rollback_and_leaves_a_readable_target(
    deployment: Deployment,
) -> None:
    """Exit 79 exactly as before, and the build left in place -- the one that
    matches the database -- is readable by the runtime account despite the
    caller's umask."""
    deployment.publish_target()
    _record_schema(deployment.database, 3)

    result = deployment.run(
        "action_deploy_main",
        caller_umask=INCIDENT_UMASK,
        settings="MGO_TEST_FINAL_STATUS=1",
    )

    assert result.returncode == EX_MANUAL_RECOVERY, result.stderr
    assert "REFUSED" in result.stderr
    assert "rollback succeeded" not in result.stderr
    assert deployment.head() == deployment.target
    assert deployment.events_containing(" reset --hard ") == []
    assert [line for line in deployment.events if line.startswith("uv sync ")] == [
        f"uv sync 1 umask={PUBLICATION_UMASK}"
    ]
    assert len(deployment.events_containing("restart mgo.service")) == 1
    assert deployment.events_containing("approval cleared") == []
    _assert_publications_used_the_gateway_umask(deployment)
    _assert_runtime_readable(deployment)
    assert deployment.events_containing("runtime denied") == []


# --------------------------------------------------------------------------
# G. bytecode containment
# --------------------------------------------------------------------------


@pytest.mark.parametrize("caller_umask", [PUBLICATION_UMASK, INCIDENT_UMASK])
def test_runtime_validation_writes_no_bytecode(
    deployment: Deployment, caller_umask: str
) -> None:
    """Three probes import the application as mgo; none may leave a .pyc or
    a __pycache__ behind, under either umask."""
    deployment.publish_target()

    result = deployment.run("action_deploy_main", caller_umask=caller_umask)

    assert result.returncode == 0, result.stderr
    _assert_no_bytecode(deployment)
    _assert_probes_disable_bytecode(deployment)
    assert len(deployment.events_containing("import mgo.core.config, mgo.api.app")) == 2
    assert len(deployment.events_containing("from mgo.core.database import")) == 2


# --------------------------------------------------------------------------
# H. sensitive control-plane objects
# --------------------------------------------------------------------------


def test_the_publication_umask_is_scoped_to_the_publication(tmp_path: Path) -> None:
    """Inside the publishing runner the umask is 0022; outside it the
    gateway keeps what it inherited, and its private objects are created
    under their own explicit umask as before."""
    lock = tmp_path / "control.lock"
    private = tmp_path / "private-tmp"
    script = (
        f"umask {INCIDENT_UMASK}\n"
        f'source "{_posix(GATEWAY)}"\n'
        "account_home() { printf '/home/%s\\n' \"$1\"; }\n"
        'runuser() { shift 3; "$@"; }\n'
        "publish_as_admin claude bash -c 'printf \"inside=%s\\n\" \"$(umask)\"'\n"
        "printf 'after=%s\\n' \"$(umask)\"\n"
        f'create_lock_object "{_posix(lock)}"\n'
        f'create_root_tmpdir "{_posix(private)}"\n'
        f'temporary="$(make_temporary_file "{_posix(tmp_path)}")"\n'
        "printf 'temporary=%s\\n' \"$temporary\"\n"
    )
    result = run_bash(script)

    assert result.returncode == 0, result.stderr
    assert f"inside={PUBLICATION_UMASK}" in result.stdout
    assert f"after={INCIDENT_UMASK}" in result.stdout
    assert lock.exists()
    assert private.is_dir()
    temporary = Path(result.stdout.split("temporary=")[1].strip())
    assert temporary.exists()
    if UMASK_ENFORCED:
        assert oct(lock.stat().st_mode & 0o777) == "0o600"
        assert oct(private.stat().st_mode & 0o777) == "0o700"
        assert oct(temporary.stat().st_mode & 0o777) == "0o600"


def test_sensitive_objects_do_not_inherit_the_publication_umask() -> None:
    """The private objects keep their own explicit umask; the publication
    umask lives in one subshell and nowhere else."""
    source = _read(GATEWAY)

    lock = _between(source, "create_lock_object()", "acquire_transaction_lock()")
    assert "umask 0077" in lock
    tmpdir = _between(source, "create_root_tmpdir()", "prepare_root_tmpdir()")
    assert "umask 0077" in tmpdir
    approval = _between(
        source, "clear_approval_file()", "# --- repository preconditions"
    )
    assert "chmod --reference" in approval
    assert "umask" not in approval

    publish = _between(source, "publish_as_admin()", "git_admin_publish()")
    assert 'umask "$MGO_PUBLICATION_UMASK"' in publish
    assert publish.count("(") >= 2 and publish.index("(") < publish.index("umask")
    assert source.count('umask "$MGO_PUBLICATION_UMASK"') == 1
    assert "umask 0022" not in source


# --------------------------------------------------------------------------
# the contract, read from the shipped text
# --------------------------------------------------------------------------


def _between(source: str, start: str, end: str) -> str:
    head = source[source.index(start) :]
    return head[: head.index(end)]


def test_the_publication_umask_is_the_gateways_own_constant() -> None:
    source = _read(GATEWAY)

    assert 'readonly MGO_PUBLICATION_UMASK="0022"' in source
    assert "MGO_PUBLICATION_UMASK" not in _between(
        source, "MGO_PERMITTED_ENVIRONMENT", "MGO_RECOVERY_TIMEOUT_SECONDS"
    )


def test_every_working_tree_publication_uses_the_publishing_runner() -> None:
    """The fast-forward, the reset and the frozen sync -- nothing else writes
    runtime files, and none of the three runs under the inherited umask."""
    source = _read(GATEWAY)

    merge = [line for line in source.splitlines() if "merge --ff-only" in line]
    assert merge == ['        merge --ff-only "$MGO_REMOTE/$MGO_BRANCH"; then']
    merge_call = _between(source, "log \"fast-forwarding", "merge --ff-only")
    assert "git_admin_publish" in merge_call
    assert 'if ! git_admin "' not in merge_call

    restore = _between(source, "restore_checkout()", "rollback_repository()")
    assert "git_admin_publish" in restore
    assert "reset --hard" in restore

    sync = _between(source, "sync_environment()", "require_uv_available()")
    assert 'publish_as_admin "$admin_account" uv sync --frozen' in sync
    assert "run_as_admin" not in sync


def test_the_current_runtime_is_validated_before_the_first_mutation() -> None:
    source = _read(GATEWAY)
    body = source[source.index("action_deploy_main()") :]

    preflight = body.index("cannot execute the environment already deployed")
    assert "require_runtime_can_execute" in body[preflight - 400 : preflight]
    assert 'die "$EX_PRECONDITION"' in body[preflight - 200 : preflight]
    assert preflight < body.index("build_supported_schema")
    assert preflight < body.index("fetch --no-tags")
    assert preflight < body.index("merge --ff-only")
    assert "$MGO_RUNTIME_ACCOUNT" in body[preflight - 400 : preflight]


def test_the_rollback_validates_the_restored_runtime_after_its_content() -> None:
    source = _read(GATEWAY)
    body = _between(source, "rollback_repository()", "# §16.1")

    assert 'local runtime_account="$5"' in body
    assert 'ROLLBACK_STAGE="runtime"' in body
    assert body.index('ROLLBACK_STAGE="verification"') < body.index(
        'require_runtime_can_execute "$runtime_account" "$repository"'
    )
    assert body.index('ROLLBACK_STAGE="runtime"') < body.rindex("return 0")
    for handler in ("fail_before_restart()", "fail_after_restart()"):
        handler_body = source[source.index(handler) : source.index(handler) + 1200]
        assert '"$MGO_BRANCH" "$MGO_RUNTIME_ACCOUNT"' in handler_body, handler
        assert "report_rollback_failure" in handler_body, handler


def test_the_rollback_report_distinguishes_content_from_runtime() -> None:
    source = _read(GATEWAY)
    report = _between(source, "report_rollback_failure()", "# §16.1")

    assert '[[ "$ROLLBACK_STAGE" == "runtime" ]]' in report
    assert "rollback is INCOMPLETE" in report
    assert "production was NOT restored" in report
    assert "rollback succeeded" not in report
    assert report.count('die "$EX_ROLLBACK"') == 2


def test_every_runtime_account_probe_disables_bytecode_writing() -> None:
    source = _read(GATEWAY)

    for name, end in (
        ("require_runtime_can_execute()", "# --- schema compatibility"),
        ("build_supported_schema()", "database_schema_version()"),
        ("database_schema_version()", "repository_rollback_is_safe()"),
    ):
        body = _between(source, name, end)
        assert '"PYTHONDONTWRITEBYTECODE=1"' in body, name
        assert '.venv/bin/python" -B -c' in body, name


def test_the_schema_aware_recovery_contract_is_unchanged() -> None:
    source = _read(GATEWAY)

    assert "readonly EX_MANUAL_RECOVERY=79" in source
    refusal = _between(
        source, "refuse_rollback_if_schema_advanced()", "# --- restart and recovery"
    )
    assert 'die "$EX_MANUAL_RECOVERY"' in refusal
    assert "was REFUSED" in refusal
    assert "umask" not in refusal
    safe = _between(
        source, "repository_rollback_is_safe()", "refuse_rollback_if_schema_advanced()"
    )
    assert '[[ "$actual_schema" == "$baseline_schema" ]]' in safe
    after = _between(source, "fail_after_restart()", "# --- actions")
    assert after.index("refuse_rollback_if_schema_advanced") < after.index(
        "rollback_repository "
    )


def test_the_public_action_set_is_unchanged() -> None:
    source = _read(GATEWAY)

    assert (
        "        show-approval | clear-approval | deploy-main | restart-api) ;;"
        in source
    )
    sudoers = _read(SUDOERS)
    assert "mgo-validate" in sudoers
    assert "umask" not in sudoers


def test_no_permission_repair_was_introduced() -> None:
    """The fix is the umask the files are created under, never a chmod
    afterwards -- and certainly never a recursive one."""
    for line in _read(GATEWAY).splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        assert "chmod -R" not in stripped
        assert "chown -R" not in stripped
        assert "g+r" not in stripped
        if "chmod" in stripped:
            assert "--reference" in stripped, stripped
        if stripped.startswith("find "):
            pytest.fail(f"find is used to select targets: {stripped}")


def test_this_module_reaches_no_host_control_plane() -> None:
    """Every process this module starts is Bash running the sourced gateway
    with doubles, or Git against a temporary repository."""
    tree = ast.parse(_read(Path(__file__)))

    executions = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "subprocess"
    ]
    assert executions, "the module is expected to start processes"
    for call in executions:
        assert call.func.attr == "run"
        argv = call.args[0]
        assert isinstance(argv, ast.List), ast.dump(argv)
        program = argv.elts[0]
        if isinstance(program, ast.Call):
            assert isinstance(program.func, ast.Name) and program.func.id == "_bash"
        else:
            assert isinstance(program, ast.Constant) and program.value == "git"

    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if (
            isinstance(body, list)
            and body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            del body[0]
    code = ast.unparse(tree).replace("SUDOERS", "").replace("sudoers", "")
    # Spelled in halves so this list is not itself a hit.
    for forbidden in (
        "su" + "do",
        "/usr/local/" + "sbin",
        "system" + "ctl",
        "/etc/garden-" + "observatory",
    ):
        assert forbidden not in code, forbidden
