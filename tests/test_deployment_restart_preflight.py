"""``restart-api`` asks the runtime account before it touches the service.

Task 14.5E. The Task 14.5B incident left a checkout that Git called clean and
the ``mgo`` service account could not read: Git tracks the executable bit and
nothing else, so a tree whose every source is ``0600`` passes every repository
precondition ``restart-api`` has. PR #17 taught ``deploy-main`` to ask the
runtime account before anything moves, but ``restart-api`` still went straight
from "the checkout is the approved commit" to ``systemctl restart``. Against
the 14.5B checkout that would have stopped a service that was serving and
started one that could not import, and the diagnosis would have happened in the
journal, after the outage.

This module drives the real ``action_restart_api`` -- the shipped shell,
sourced -- against the disposable production of the umask-safety suite: a
real Git checkout, a real interpreter behind a launcher, and doubles only at
the seams through which the gateway's fixed production constants leave the
process. The one new seam is ``systemctl``, recorded and never reached, so the
proof that a refused preflight issues **zero** service actions is a proof about
the shipped restart path rather than about a double of it.

What is proved:

* a readable runtime restarts exactly as it did before, health wait included;
* the probe runs after every read-only precondition and before the first
  service action, as ``mgo``, with ``-B`` and ``PYTHONDONTWRITEBYTECODE=1``,
  and writes no bytecode;
* an unreadable source, an untraversable package directory, an interpreter
  that cannot start, an absent interpreter and a configuration that cannot be
  imported are each refused with exit 65 before any stop, restart, reload,
  signal or health wait -- while the tree is clean and at the approved SHA;
* the approval is read once and never cleared; the repository is untouched;
* the caller's umask changes nothing about any of it.
"""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path

import pytest

from test_deployment_umask_safety import (
    EX_PRECONDITION,
    GATEWAY,
    INCIDENT_UMASK,
    PUBLICATION_UMASK,
    UMASK_ENFORCED,
    Deployment,
    _application_files,
    _assert_no_bytecode,
    _assert_no_production_path_reached_a_command,
    _assert_probes_disable_bytecode,
    _git,
    _posix,
    _read,
    _write_files,
    deployment,
    run_bash,
)

#: The fixture is imported for collection; naming it here keeps that deliberate.
__all__ = ["deployment"]

#: The unit the gateway restarts, by its fixed name.
SERVICE = "mgo.service"

#: The message the preflight refuses with. Asserted verbatim in part, because
#: an operator reading the journal must be told the service was left alone.
REFUSAL = "the runtime account cannot read or execute the deployed environment"

#: Every recorded event that would mean the service was acted on. ``await`` is
#: the health-wait loop; it must not run after a refusal either.
SERVICE_ACTION_PREFIXES = (
    "systemctl restart",
    "systemctl stop",
    "systemctl reload",
    "systemctl kill",
    "systemctl start",
    "await",
    "preview-restore",
)

RESTART_HARNESS = r"""
# The service seam. The umask-safety harness doubles restart_service itself;
# here the shipped body is put back, so the only route to a restart is the
# real one -- systemctl -- and systemctl is what is recorded. It never
# reaches the host.
MGO_TEST_UNIT_STATUS=0
systemctl() {
    record "systemctl $*"
    case "${1:-}" in
        cat) return "$MGO_TEST_UNIT_STATUS" ;;
        restart) return "$MGO_TEST_RESTART_STATUS" ;;
    esac
    record "unexpected systemctl $*"
    return 1
}
restart_service() { shipped_restart_service "$@"; }

# The restart preconditions read the production path on the root side, as the
# deployment preconditions do. The shipped body is kept and handed the mapped
# path.
eval "original_$(declare -f require_restart_preconditions)"
require_restart_preconditions() {
    original_require_restart_preconditions "$1" "$(map_production_path "$2")" \
        "$3" "$4" "$5"
}
"""


# --------------------------------------------------------------------------
# driving the shipped action
# --------------------------------------------------------------------------


def _run_restart(
    deployment: Deployment,
    *,
    approved: str = "",
    caller_umask: str = PUBLICATION_UMASK,
    settings: str = "",
) -> subprocess.CompletedProcess[str]:
    """Run the shipped ``restart-api`` action as a caller with ``caller_umask``.

    ``approved`` defaults to the checkout's HEAD, which is the ordinary
    restart: the approved commit is the deployed one.
    """
    approved = approved or deployment.head()
    script = (
        f"umask {caller_umask}\n"
        f'source "{_posix(GATEWAY)}"\n'
        'eval "shipped_$(declare -f restart_service)"\n'
        f'source "{_posix(deployment.harness)}"\n'
        f"{RESTART_HARNESS}\n"
        f"MGO_TEST_APPROVED='{approved}'\n"
        f"{settings}\n"
        "action_restart_api\n"
        "printf 'shell-umask=%s\\n' \"$(umask)\"\n"
    )
    return run_bash(script)


def _service_actions(deployment: Deployment) -> list[str]:
    return [
        line
        for line in deployment.events
        if line.startswith(SERVICE_ACTION_PREFIXES)
    ]


def _probes(deployment: Deployment) -> list[str]:
    return deployment.events_containing("runtime account=")


def _reset_events(deployment: Deployment) -> None:
    """Forget the recorded events; the modelled modes stay, as the files do."""
    path = deployment.ledger / "events.log"
    if path.exists():
        path.unlink()


def _make_untraversable(deployment: Deployment, relative: str) -> None:
    """A tracked package directory the runtime account cannot enter.

    Real on an honest kernel, modelled everywhere, exactly as the umask-safety
    suite models an unreadable file.
    """
    path = deployment.checkout / relative
    assert path.is_dir()
    if UMASK_ENFORCED:
        path.chmod(0o700)
    # LF explicitly: the ledger is read by Bash, and a CR would ride into the
    # mode field on Windows.
    with (deployment.ledger / "modes").open(
        "a", encoding="utf-8", newline="\n"
    ) as ledger:
        ledger.write(f"{_posix(path)} 700\n")


def _publish_current(deployment: Deployment, files: dict[str, str]) -> str:
    """Make ``files`` the deployed, approved, upstream-matching commit.

    The restart preconditions require HEAD, the tracking branch and the
    approval to agree, so a broken *current* build has to arrive the way a
    real one would: committed upstream and fast-forwarded into the checkout.
    """
    _write_files(deployment.upstream, files)
    _git(deployment.upstream, "add", "--all")
    _git(deployment.upstream, "commit", "--quiet", "-m", "current build")
    _git(deployment.checkout, "pull", "--quiet", "--ff-only")
    head = deployment.head()
    assert head == _git(deployment.upstream, "rev-parse", "HEAD")
    assert _git(deployment.checkout, "status", "--porcelain") == ""
    return head


def _assert_refused_untouched(
    deployment: Deployment,
    result: subprocess.CompletedProcess[str],
    *,
    head: str = "",
) -> None:
    """Exit 65, the runtime named as the reason, and nothing else happened."""
    head = head or deployment.previous
    assert result.returncode == EX_PRECONDITION, result.stderr
    assert REFUSAL in result.stderr
    assert "was not restarted" in result.stderr
    assert "readable and executable by the runtime account" in result.stderr

    # No service control of any kind, and no health wait after it.
    assert _service_actions(deployment) == [], deployment.events
    assert deployment.events_containing("await") == []
    assert "restarting" not in result.stdout
    assert "recovered" not in result.stdout
    assert "MainPID" not in result.stdout
    assert "activated" not in result.stdout

    # The refusal is the only thing reported, and it is reported once.
    reports = [line for line in result.stderr.splitlines() if line.strip()]
    assert len(reports) == 1, result.stderr

    # The lock was taken first; the approval was read once and never cleared.
    assert deployment.events[0].startswith("lock "), deployment.events
    assert deployment.events_containing("approval read") == ["approval read"]
    assert deployment.events_containing("approval cleared") == []

    # Repository, environment, configuration: untouched.
    assert deployment.head() == head
    assert _git(deployment.checkout, "status", "--porcelain") == ""
    assert _git(deployment.checkout, "stash", "list") == ""
    for marker in ("MERGE_HEAD", "REBASE_HEAD", "CHERRY_PICK_HEAD"):
        assert not (deployment.checkout / ".git" / marker).exists()
    for forbidden in (" fetch ", " merge --ff-only ", " reset --hard ", "uv sync"):
        assert deployment.events_containing(forbidden) == [], forbidden

    # The runtime account was asked, and every question went to it.
    probes = _probes(deployment)
    assert probes, deployment.events
    for line in probes:
        assert line.startswith("runtime account=mgo "), line
    _assert_no_production_path_reached_a_command(deployment)


# --------------------------------------------------------------------------
# A. a readable runtime restarts exactly as before
# --------------------------------------------------------------------------


@pytest.mark.parametrize("caller_umask", [PUBLICATION_UMASK, INCIDENT_UMASK])
def test_a_readable_runtime_is_restarted_with_the_existing_verification(
    deployment: Deployment, caller_umask: str
) -> None:
    """The ordinary restart is unchanged: one restart, then the health wait."""
    result = _run_restart(deployment, caller_umask=caller_umask)

    assert result.returncode == 0, result.stderr
    assert f"restarting {SERVICE} at {deployment.previous}" in result.stdout
    assert "recovered in 1s" in result.stdout
    assert "MainPID 4242" in result.stdout
    assert "activated Mon 2026-09-07" in result.stdout
    assert deployment.events_containing("systemctl restart") == [
        f"systemctl restart {SERVICE}"
    ]
    assert deployment.events_containing("await") == ["await"]
    assert deployment.event_index("systemctl restart") < deployment.event_index(
        "await"
    )
    assert deployment.events_containing("approval cleared") == []
    assert deployment.head() == deployment.previous
    assert _git(deployment.checkout, "status", "--porcelain") == ""
    assert f"shell-umask={caller_umask}" in result.stdout
    _assert_no_production_path_reached_a_command(deployment)


def test_the_probe_runs_after_every_precondition_and_before_the_restart(
    deployment: Deployment,
) -> None:
    """Lock, approval, repository checks, unit check, probe, restart: in that
    order, with the probe immediately before the first service action."""
    result = _run_restart(deployment)

    assert result.returncode == 0, result.stderr
    events = deployment.events
    probe_indices = [
        index
        for index, line in enumerate(events)
        if line.startswith("runtime account=")
    ]
    assert probe_indices, events
    lock = deployment.event_index("lock ")
    approval = deployment.event_index("approval read")
    unit = deployment.event_index("systemctl cat")
    restart = deployment.event_index("systemctl restart")
    assert lock < approval < unit < probe_indices[0]
    assert probe_indices[-1] < restart
    # Nothing but the probe stands between the unit check and the restart.
    between = events[unit + 1 : restart]
    assert between, events
    assert all(line.startswith("runtime account=") for line in between), between


@pytest.mark.parametrize("caller_umask", [PUBLICATION_UMASK, INCIDENT_UMASK])
def test_the_probe_runs_as_the_runtime_account_and_writes_no_bytecode(
    deployment: Deployment, caller_umask: str
) -> None:
    result = _run_restart(deployment, caller_umask=caller_umask)

    assert result.returncode == 0, result.stderr
    probes = _probes(deployment)
    assert probes, deployment.events
    for line in probes:
        assert line.startswith("runtime account=mgo "), line
    imports = [line for line in probes if "import mgo.core.config, mgo.api.app" in line]
    assert len(imports) == 1, probes
    launcher = _posix(deployment.checkout / ".venv" / "bin" / "python")
    assert launcher in imports[0]
    assert "env -i" in imports[0]
    assert "MGO_CONFIG_PATH=/etc/garden-observatory/mgo.toml" in imports[0]
    assert "PYTHONDONTWRITEBYTECODE=1" in imports[0]
    assert f" {launcher} -B -c " in imports[0]
    _assert_probes_disable_bytecode(deployment)
    _assert_no_bytecode(deployment)
    # The probe imports; it does not start anything.
    assert "uvicorn" not in imports[0]
    assert deployment.events_containing("preview-restore") == []


# --------------------------------------------------------------------------
# B. an unrunnable runtime is refused with the service untouched
# --------------------------------------------------------------------------


@pytest.mark.parametrize("caller_umask", [PUBLICATION_UMASK, INCIDENT_UMASK])
def test_an_unreadable_source_is_refused_before_service_control(
    deployment: Deployment, caller_umask: str
) -> None:
    """The Task 14.5B shape: Git says clean, ``mgo`` cannot read a source."""
    deployment.make_unreadable("src/mgo/core/config.py")
    assert _git(deployment.checkout, "status", "--porcelain") == ""

    result = _run_restart(deployment, caller_umask=caller_umask)

    _assert_refused_untouched(deployment, result)
    assert deployment.events_containing("runtime denied") != []


def test_an_untraversable_package_directory_is_refused_before_service_control(
    deployment: Deployment,
) -> None:
    _make_untraversable(deployment, "src/mgo/core")
    assert _git(deployment.checkout, "status", "--porcelain") == ""

    result = _run_restart(deployment)

    _assert_refused_untouched(deployment, result)
    assert deployment.events_containing("runtime denied") != []


def test_an_interpreter_that_cannot_start_is_refused_before_service_control(
    deployment: Deployment,
) -> None:
    """Executable, present, and useless: the launcher exits before Python."""
    launcher = deployment.checkout / ".venv" / "bin" / "python"
    launcher.write_text("#!/bin/bash\nexit 127\n", encoding="utf-8")
    launcher.chmod(0o755)

    result = _run_restart(deployment)

    _assert_refused_untouched(deployment, result)


def test_an_absent_interpreter_is_refused_before_service_control(
    deployment: Deployment,
) -> None:
    (deployment.checkout / ".venv" / "bin" / "python").unlink()

    result = _run_restart(deployment)

    _assert_refused_untouched(deployment, result)


def test_a_configuration_that_cannot_be_imported_is_refused_before_service_control(
    deployment: Deployment,
) -> None:
    """A clean, approved, upstream-matching build whose configuration module
    raises on import. Every repository check passes; the runtime says no."""
    files = _application_files(1)
    files["src/mgo/core/config.py"] = (
        'raise ImportError("the production configuration cannot be loaded")\n'
    )
    head = _publish_current(deployment, files)

    result = _run_restart(deployment, approved=head)

    _assert_refused_untouched(deployment, result, head=head)
    assert deployment.events_containing("runtime denied") == []


# --------------------------------------------------------------------------
# C. the existing preconditions still come first
# --------------------------------------------------------------------------


def test_an_unapproved_checkout_is_refused_before_the_probe(
    deployment: Deployment,
) -> None:
    """The expected-SHA check precedes the probe: an unapproved build is never
    even asked whether it could run."""
    result = _run_restart(deployment, approved="a" * 40)

    assert result.returncode == EX_PRECONDITION, result.stderr
    assert "not the approved SHA" in result.stderr
    assert REFUSAL not in result.stderr
    assert _probes(deployment) == []
    assert _service_actions(deployment) == []


def test_a_missing_unit_is_refused_before_the_probe(deployment: Deployment) -> None:
    """The unit check is the last read-only precondition; the probe follows it."""
    result = _run_restart(deployment, settings="MGO_TEST_UNIT_STATUS=1")

    assert result.returncode == EX_PRECONDITION, result.stderr
    assert "the service unit is not installed" in result.stderr
    assert _probes(deployment) == []
    assert _service_actions(deployment) == []


def test_a_dirty_tree_is_still_refused_without_a_probe(deployment: Deployment) -> None:
    (deployment.checkout / "README.md").write_text("edited\n", encoding="utf-8")

    result = _run_restart(deployment)

    assert result.returncode == EX_PRECONDITION, result.stderr
    assert "not clean" in result.stderr
    assert _probes(deployment) == []
    assert _service_actions(deployment) == []


# --------------------------------------------------------------------------
# D. the caller's umask is not an input
# --------------------------------------------------------------------------


def _without_umask_tokens(events: list[str]) -> list[str]:
    return [
        " ".join(token for token in line.split(" ") if not token.startswith("umask="))
        for line in events
    ]


def test_the_caller_umask_does_not_change_the_outcome(deployment: Deployment) -> None:
    """Same events, same exit, under 0022 and under the incident's 0077 --
    for the restart that proceeds and for the one that is refused."""
    first = _run_restart(deployment, caller_umask=PUBLICATION_UMASK)
    first_events = _without_umask_tokens(deployment.events)
    _reset_events(deployment)
    second = _run_restart(deployment, caller_umask=INCIDENT_UMASK)
    second_events = _without_umask_tokens(deployment.events)
    assert first.returncode == second.returncode == 0
    assert first_events == second_events
    _reset_events(deployment)

    deployment.make_unreadable("src/mgo/api/app.py")
    refused = _run_restart(deployment, caller_umask=PUBLICATION_UMASK)
    refused_events = _without_umask_tokens(deployment.events)
    _reset_events(deployment)
    with (deployment.ledger / "modes").open("a", encoding="utf-8") as ledger:
        ledger.write(f"{_posix(deployment.checkout / 'src/mgo/api/app.py')} 600\n")
    refused_again = _run_restart(deployment, caller_umask=INCIDENT_UMASK)
    refused_again_events = _without_umask_tokens(deployment.events)
    assert refused.returncode == refused_again.returncode == EX_PRECONDITION
    assert refused_events == refused_again_events
    assert _service_actions(deployment) == []


# --------------------------------------------------------------------------
# E. the shipped text says what the scenarios prove
# --------------------------------------------------------------------------


def _restart_body() -> str:
    source = _read(GATEWAY)
    return source[
        source.index("action_restart_api()") : source.index("action_deploy_main()")
    ]


def test_the_preflight_sits_between_the_unit_check_and_the_restart() -> None:
    body = _restart_body()
    probe = body.index(
        'require_runtime_can_execute "$MGO_RUNTIME_ACCOUNT" "$MGO_REPOSITORY"'
    )

    assert body.index("systemctl cat") < probe
    assert probe < body.index('log "restarting')
    assert probe < body.index("restart_service")
    assert body.count("restart_service") == 1
    refusal = body[probe : body.index("restart_service")]
    assert 'die "$EX_PRECONDITION"' in refusal
    assert REFUSAL in refusal
    assert "clear_approval_file" not in body


def test_the_preflight_reuses_the_deployment_probe() -> None:
    """One probe, defined once, asked by both privileged actions as ``mgo``."""
    source = _read(GATEWAY)

    assert source.count("require_runtime_can_execute() {") == 1
    deploy = source[
        source.index("action_deploy_main()") : source.index("action_clear_approval()")
    ]
    assert 'require_runtime_can_execute "$MGO_RUNTIME_ACCOUNT"' in deploy
    assert 'require_runtime_can_execute "$MGO_RUNTIME_ACCOUNT"' in _restart_body()
    assert 'require_runtime_can_execute "$MGO_ADMIN_ACCOUNT"' not in source
    assert 'require_runtime_can_execute "root"' not in source


def test_the_public_action_set_is_unchanged() -> None:
    source = _read(GATEWAY)

    assert (
        "        show-approval | clear-approval | deploy-main | restart-api) ;;"
        in source
    )
    assert (
        "unsupported action; expected show-approval, clear-approval, deploy-main "
        "or restart-api" in source
    )


def test_this_module_reaches_no_host_control_plane() -> None:
    """Every process this module starts is Bash running the sourced gateway
    with doubles, or Git against a temporary repository -- through the
    umask-safety suite's own runners -- and no string constant here names
    the privilege tool, an installed gateway path or a real unit directory."""
    tree = ast.parse(_read(Path(__file__)))

    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert not (
                isinstance(node.func.value, ast.Name)
                and node.func.value.id == "subprocess"
            ), "start processes only through run_bash and _git"

    strings = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]
    # Spelled in halves so this list is not itself a hit.
    for forbidden in (
        "su" + "do",
        "/usr/local/" + "sbin",
        "/etc/" + "systemd",
        "/run/" + "mgo-validate",
    ):
        for text in strings:
            assert forbidden not in text, forbidden
