"""The mutation runner cannot execute stale bytecode (Task 14.5E).

The observed case. On the Pi, the first complete run of the register after PR
#17 reported two survivors that a rerun with ``PYTHONDONTWRITEBYTECODE=1`` did
not. Neither mutation had survived: a ``.pyc`` is validated against its source
by *whole-second* modification time and byte length, so when consecutive
candidates rewrote one Python source to the same length inside the same second
the second candidate's interpreter loaded bytecode compiled from the first --
or from the committed source -- and the targeted tests passed against code that
was never run. A faster host makes that likelier, not rarer.

This module states the condition as a fact rather than a race: the source's
modification time is *pinned* to a whole-second instant before the bytecode is
written and to a later fraction of the same second after the source changes.
No sleep, no clock, no hope. With that fixture in place it proves three things:

* the condition is real on this interpreter (the stale bytecode is what runs
  when nothing isolates it), so the fixture is not vacuous;
* neither ``-B`` nor ``PYTHONDONTWRITEBYTECODE`` alone closes it -- they stop
  writing, not reading -- which is why the runner's previous invocation was
  exposed;
* the runner's per-candidate contract closes it: each candidate's interpreter
  reads its current source, writes no bytecode anywhere in the tree, and gets a
  fresh, uniquely named, task-owned cache prefix that is removed afterwards by
  its resolved literal path.

The end-to-end case then drives the runner's own ``_apply`` over a disposable
two-file project with the timestamp seam held inside one second, first with
the previous invocation (the survivor is reproduced) and then with the shipped
one (the mutation is detected, in either order).
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from types import ModuleType

import pytest

from mgo.core.config import PROJECT_ROOT

RUNNER = PROJECT_ROOT / "scripts" / "dev" / "run-mutations.py"

#: A whole-second instant, pinned so that "same second" is a fact these tests
#: state rather than a race they hope to win.
PINNED_SECOND = 1_700_000_000

#: The runner's pytest invocation before Task 14.5E: no ``-B``, and the
#: environment as inherited.
def _previous_command(selector: str, suite: str) -> list[str]:
    return [
        sys.executable,
        "-m",
        "pytest",
        *suite.split(),
        "-q",
        "-x",
        "--no-header",
        "-p",
        "no:cacheprovider",
        "-k",
        selector,
    ]


def _previous_environment(_cache_prefix: Path | None = None) -> dict[str, str]:
    """The environment the runner inherited: neither bytecode variable set."""
    environment = dict(os.environ)
    environment.pop("PYTHONDONTWRITEBYTECODE", None)
    environment.pop("PYTHONPYCACHEPREFIX", None)
    return environment


def _load_runner() -> ModuleType:
    """Load the runner by path: its name has a hyphen and it is a script."""
    specification = importlib.util.spec_from_file_location("mgo_run_mutations", RUNNER)
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


@pytest.fixture
def runner() -> ModuleType:
    return _load_runner()


def _pin(path: Path, fraction: float) -> None:
    stamp = PINNED_SECOND + fraction
    os.utime(path, (stamp, stamp))


def _import_value(
    package: Path, *, environment: dict[str, str], flags: tuple[str, ...] = ()
) -> str:
    result = subprocess.run(
        [sys.executable, *flags, "-c", "import stale_probe; print(stale_probe.VALUE)"],
        cwd=str(package),
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return result.stdout.strip()


def _cached_bytecode(package: Path) -> list[Path]:
    cache = package / "__pycache__"
    if not cache.exists():
        return []
    return sorted(cache.glob("stale_probe.*.pyc"))


@pytest.fixture
def stale_package(tmp_path: Path) -> Path:
    """A module whose ``__pycache__`` describes a source that has since changed.

    ``VALUE = 1`` is imported once with bytecode writing enabled, which writes
    a ``.pyc`` recording the source's whole-second mtime and byte length. The
    source is then rewritten to ``VALUE = 2`` -- the same length -- and its
    mtime placed inside the same second, half a second later. To the import
    system the cached bytecode is valid.
    """
    package = tmp_path / "package"
    package.mkdir()
    source = package / "stale_probe.py"
    source.write_bytes(b"VALUE = 1\n")
    _pin(source, 0.25)
    assert _import_value(package, environment=_previous_environment()) == "1"
    assert len(_cached_bytecode(package)) == 1, "the interpreter wrote no bytecode"
    source.write_bytes(b"VALUE = 2\n")
    _pin(source, 0.75)
    assert source.stat().st_size == 10
    return package


# --------------------------------------------------------------------------
# the condition, and why the previous invocation was exposed to it
# --------------------------------------------------------------------------


def test_same_second_same_size_bytecode_is_reused_by_the_import_system(
    stale_package: Path,
) -> None:
    """The fixture is real: without isolation the old source's bytecode runs."""
    assert _import_value(stale_package, environment=_previous_environment()) == "1"


def test_suppressing_bytecode_writing_does_not_stop_it_being_read(
    stale_package: Path,
) -> None:
    """``-B`` and ``PYTHONDONTWRITEBYTECODE=1`` together still read the stale
    cache. Writing and reading are different halves; the previous runner had
    neither, and adding only these would not have been enough."""
    environment = _previous_environment()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"

    assert _import_value(stale_package, environment=environment, flags=("-B",)) == "1"


# --------------------------------------------------------------------------
# the runner's contract
# --------------------------------------------------------------------------


def test_a_candidate_reads_its_current_source(
    stale_package: Path, runner: ModuleType, tmp_path: Path
) -> None:
    cache_root = tmp_path / "caches"
    cache_root.mkdir()
    cache = runner.create_candidate_cache(cache_root, 1)
    before = _cached_bytecode(stale_package)

    value = _import_value(
        stale_package, environment=runner.candidate_environment(cache), flags=("-B",)
    )

    assert value == "2"
    assert _cached_bytecode(stale_package) == before, "the stale cache was rewritten"
    assert list(cache.iterdir()) == [], "the candidate wrote bytecode"


def test_the_cache_prefix_is_what_redirects_the_read(
    stale_package: Path, runner: ModuleType, tmp_path: Path
) -> None:
    """With writing re-enabled the prefix still keeps the candidate away from
    the source tree's cache in both directions: it reads the current source and
    writes under the prefix, never beside the source. Asserted separately so a
    change that keeps ``-B`` but drops the prefix is visible."""
    cache_root = tmp_path / "caches"
    cache_root.mkdir()
    cache = runner.create_candidate_cache(cache_root, 1)
    environment = runner.candidate_environment(cache)
    del environment["PYTHONDONTWRITEBYTECODE"]
    before = _cached_bytecode(stale_package)

    value = _import_value(stale_package, environment=environment)

    assert value == "2"
    assert _cached_bytecode(stale_package) == before
    assert list(cache.rglob("*.pyc")) != []


def test_the_candidate_command_and_environment_carry_both_halves(
    runner: ModuleType, tmp_path: Path
) -> None:
    command = runner.candidate_command("a_selector", "tests/one.py tests/two.py")
    environment = runner.candidate_environment(tmp_path / "cache")

    assert command[0] == sys.executable
    assert command[1] == "-B"
    assert command[2:4] == ["-m", "pytest"]
    assert "tests/one.py" in command and "tests/two.py" in command
    assert command[-2:] == ["-k", "a_selector"]
    assert "no:cacheprovider" in command
    assert environment["PYTHONDONTWRITEBYTECODE"] == "1"
    assert environment["PYTHONPYCACHEPREFIX"] == str(tmp_path / "cache")


def test_candidate_caches_are_unique_task_owned_and_removed_by_literal_path(
    runner: ModuleType, tmp_path: Path
) -> None:
    root = runner.create_cache_root()
    try:
        assert root.is_dir()
        assert root.name.startswith(runner.CACHE_ROOT_PREFIX)
        assert not root.is_relative_to(PROJECT_ROOT)
        first = runner.create_candidate_cache(root, 1)
        second = runner.create_candidate_cache(root, 2)
        assert first != second
        assert first.parent == root and second.parent == root
        assert list(first.iterdir()) == [] and list(second.iterdir()) == []
        with pytest.raises(FileExistsError):
            runner.create_candidate_cache(root, 1)

        # Only a direct child of the root is ever removed.
        foreign = tmp_path / "not-a-candidate"
        foreign.mkdir()
        with pytest.raises(RuntimeError):
            runner.remove_candidate_cache(root, foreign)
        with pytest.raises(RuntimeError):
            runner.remove_candidate_cache(root, root)
        with pytest.raises(RuntimeError):
            runner.remove_candidate_cache(root, first / "deeper")
        assert foreign.is_dir()

        runner.remove_candidate_cache(root, first)
        assert not first.exists()
        assert second.is_dir()
        with pytest.raises(RuntimeError):
            runner.remove_cache_root(tmp_path)
    finally:
        runner.remove_cache_root(root)
    assert not root.exists()
    assert tmp_path.is_dir()


# --------------------------------------------------------------------------
# end to end, through the runner's own apply-test-restore
# --------------------------------------------------------------------------


def _mini_project(root: Path) -> None:
    (root / "tests").mkdir(parents=True)
    (root / "probe.py").write_bytes(b"VALUE = 1\n")
    (root / "other.py").write_bytes(b"OTHER = 1\n")
    (root / "tests" / "test_probe.py").write_text(
        textwrap.dedent(
            """\
            import sys
            from pathlib import Path

            sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

            import other
            import probe


            def test_value():
                assert probe.VALUE == 1


            def test_other():
                assert other.OTHER == 1
            """
        ),
        encoding="utf-8",
    )
    for name in ("probe.py", "other.py"):
        _pin(root / name, 0.0)


def _mutations(runner: ModuleType) -> dict[str, object]:
    return {
        "other": runner.Mutation(
            "other-changed",
            "other.py",
            "OTHER = 1",
            "OTHER = 2",
            "test_other",
            "The other module changed.",
            "tests/test_probe.py",
        ),
        "probe": runner.Mutation(
            "probe-changed",
            "probe.py",
            "VALUE = 1",
            "VALUE = 2",
            "test_value",
            "The probed module changed, to the same length.",
            "tests/test_probe.py",
        ),
    }


def _hold_writes_inside_one_second(
    monkeypatch: pytest.MonkeyPatch, runner: ModuleType
) -> None:
    """Every write the runner makes lands inside the pinned second.

    This is the seam that makes the observed race a stated fact: the mutation
    write and the restore both keep the source's whole-second mtime while
    advancing its fraction, exactly as a fast host does by itself.
    """
    shipped = runner._write_bytes
    fraction = [0.0]

    def pinned(path: Path, payload: bytes) -> None:
        shipped(path, payload)
        fraction[0] += 0.1
        assert fraction[0] < 1.0
        _pin(path, fraction[0])

    monkeypatch.setattr(runner, "_write_bytes", pinned)


def _run_register(
    runner: ModuleType, root: Path, order: list[str], monkeypatch: pytest.MonkeyPatch
) -> dict[str, tuple[bool, str]]:
    monkeypatch.setattr(runner, "PROJECT_ROOT", root)
    mutations = _mutations(runner)
    cache_root = runner.create_cache_root()
    try:
        return {
            key: runner._apply(mutations[key], cache_root, ordinal)
            for ordinal, key in enumerate(order, start=1)
        }
    finally:
        runner.remove_cache_root(cache_root)


def test_the_previous_invocation_reports_a_survivor_for_code_it_never_ran(
    runner: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The observed case, reproduced: the first candidate's tests import the
    committed ``probe`` and its bytecode is written; the second candidate
    changes ``probe`` to the same length inside the same second and its tests
    run the committed bytecode. ``NOT DETECTED``, for a mutation that was
    never executed."""
    root = tmp_path / "project"
    _mini_project(root)
    _hold_writes_inside_one_second(monkeypatch, runner)
    monkeypatch.setattr(runner, "candidate_command", _previous_command)
    monkeypatch.setattr(runner, "candidate_environment", _previous_environment)

    results = _run_register(runner, root, ["other", "probe"], monkeypatch)

    assert results["other"] == (True, "detected")
    detected, detail = results["probe"]
    assert not detected
    assert detail.startswith("NOT DETECTED")
    assert (root / "probe.py").read_bytes() == b"VALUE = 1\n"
    assert (root / "other.py").read_bytes() == b"OTHER = 1\n"


@pytest.mark.parametrize("order", [["other", "probe"], ["probe", "other"]])
def test_the_shipped_invocation_detects_it_in_either_order(
    runner: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    order: list[str],
) -> None:
    root = tmp_path / "project"
    _mini_project(root)
    _hold_writes_inside_one_second(monkeypatch, runner)

    results = _run_register(runner, root, order, monkeypatch)

    assert results == {key: (True, "detected") for key in order}
    assert (root / "probe.py").read_bytes() == b"VALUE = 1\n"
    assert (root / "other.py").read_bytes() == b"OTHER = 1\n"
    # No bytecode anywhere in the controlled tree: not beside a source, not
    # beside a test.
    assert list(root.rglob("*.pyc")) == []
    assert list(root.rglob("__pycache__")) == []


def test_an_interrupted_candidate_restores_the_asset_and_removes_its_cache(
    runner: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A signal or an exception during the tests leaves the committed bytes in
    place and no cache behind. The interruption itself still propagates."""
    root = tmp_path / "project"
    _mini_project(root)
    monkeypatch.setattr(runner, "PROJECT_ROOT", root)
    seen: list[Path] = []

    def interrupted(selector: str, suite: str, cache_prefix: Path) -> None:
        seen.append(cache_prefix)
        assert (root / "probe.py").read_bytes() == b"VALUE = 2\n"
        raise KeyboardInterrupt

    monkeypatch.setattr(runner, "_run_tests", interrupted)
    cache_root = runner.create_cache_root()
    try:
        with pytest.raises(KeyboardInterrupt):
            runner._apply(_mutations(runner)["probe"], cache_root, 1)
        assert (root / "probe.py").read_bytes() == b"VALUE = 1\n"
        assert seen and not seen[0].exists()
        assert list(cache_root.iterdir()) == []
    finally:
        runner.remove_cache_root(cache_root)
    assert not cache_root.exists()


def test_a_stale_anchor_creates_no_cache_and_touches_nothing(
    runner: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "project"
    _mini_project(root)
    monkeypatch.setattr(runner, "PROJECT_ROOT", root)
    stale = runner.Mutation(
        "stale", "probe.py", "VALUE = 9", "VALUE = 8", "test_value", "Stale.",
        "tests/test_probe.py",
    )
    cache_root = runner.create_cache_root()
    try:
        detected, detail = runner._apply(stale, cache_root, 1)
        assert not detected
        assert detail.startswith("STALE")
        assert list(cache_root.iterdir()) == []
        assert (root / "probe.py").read_bytes() == b"VALUE = 1\n"
    finally:
        runner.remove_cache_root(cache_root)


def test_the_runner_documents_the_isolation_it_performs() -> None:
    source = RUNNER.read_text(encoding="utf-8")

    assert "PYTHONPYCACHEPREFIX" in source
    assert "PYTHONDONTWRITEBYTECODE" in source
    assert '"-B"' in source
    assert "sleep(" not in source.split("def create_cache_root")[1]
    # The one recursive removal is of a candidate cache held by literal path;
    # nothing under the repository is ever named for removal.
    assert source.count("rmtree(") == 1
    assert "rmtree(cache)" in source
    assert "PROJECT_ROOT /" in source
    assert 'PROJECT_ROOT / "__pycache__"' not in source
    assert '"__pycache__"' not in source
