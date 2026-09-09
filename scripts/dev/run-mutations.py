"""Run the deployment gateway's mutation register.

For each mutation: apply it to the shipped asset, run the tests the register
says must fail, restore the asset byte-for-byte, and confirm the restoration by
digest. A mutation counts as detected only when the named tests actually fail.

    uv run python scripts/dev/run-mutations.py
    uv run python scripts/dev/run-mutations.py --only lock

The asset is always restored, including when this script is interrupted or a
test run raises: the original bytes are held in memory and written back in a
``finally``. The digest comparison afterwards is what makes that a fact rather
than an intention.

Every write goes through :func:`_write_bytes`, which retries a transient
``PermissionError``. That is not defensiveness for its own sake: on Windows an
antivirus scanner or the search indexer can hold a handle to a just-written file
for a fraction of a second, and a bare ``write_bytes`` in the ``finally`` would
then raise *while restoring* -- ending the run with a shipped deployment asset
left mutated in the working tree. A run that dies is acceptable; a run that dies
holding a mutated gateway is exactly the silent failure this register exists to
prevent.

Every candidate runs with its own bytecode state (Task 14.5E). A ``.pyc`` is
validated against its source by *whole-second* modification time and byte
length, so two mutations of one Python source that leave the file the same
length and land inside the same second are indistinguishable to the import
system: the second candidate would execute the first candidate's bytecode --
or the committed source's -- and the register would report a survivor for code
it never ran. That is not a race that a faster host makes rarer; a faster host
makes it *likelier*. So every candidate's interpreter is started with ``-B`` and
``PYTHONDONTWRITEBYTECODE=1`` (it writes nothing) and with a fresh, empty,
uniquely named ``PYTHONPYCACHEPREFIX`` (it reads nothing left by anyone else:
with a cache prefix set, the import system never consults the ``__pycache__``
beside a source). The prefix directory is created by this process, used by one
candidate, and removed by its resolved literal path afterwards. Nothing under
the repository, and nothing that this process did not create, is ever removed.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.mutation_register import MUTATIONS, Mutation

PROJECT_ROOT = Path(__file__).resolve().parents[2]

#: Bounded retry for a write that loses a race with a virus scanner or indexer.
#: Bounded, and with a linear backoff, so a genuine permission problem still
#: fails loudly and quickly rather than being retried forever.
_WRITE_ATTEMPTS = 12
_WRITE_BACKOFF_SECONDS = 0.25

#: The name every bytecode-cache root this process creates begins with. The
#: cleanup refuses to remove a directory that was not created under it.
CACHE_ROOT_PREFIX = "mgo-mutation-bytecode-"


def _write_bytes(path: Path, payload: bytes) -> None:
    """Write ``payload`` to ``path``, retrying a transient lock.

    Used for the mutation write *and* the restore, so a lock that appears at the
    wrong moment cannot leave a shipped asset mutated.
    """
    last: OSError | None = None
    for attempt in range(1, _WRITE_ATTEMPTS + 1):
        try:
            path.write_bytes(payload)
            return
        except PermissionError as exc:
            last = exc
            time.sleep(_WRITE_BACKOFF_SECONDS * attempt)
    raise RuntimeError(
        f"could not write {path} after {_WRITE_ATTEMPTS} attempts"
    ) from last


# --- per-candidate bytecode isolation --------------------------------------


def create_cache_root() -> Path:
    """A private, empty directory to hold one run's per-candidate caches.

    Created under the process's temporary directory, never under the
    repository: a cache beside the sources is exactly what the isolation
    exists to keep the candidates away from.
    """
    return Path(tempfile.mkdtemp(prefix=CACHE_ROOT_PREFIX)).resolve()


def create_candidate_cache(cache_root: Path, ordinal: int) -> Path:
    """A fresh, empty, uniquely named bytecode cache for one candidate.

    ``mkdir`` without ``exist_ok``: a cache that already exists is a cache
    another candidate may have written to, which is the condition being ruled
    out, so it is an error rather than something to reuse.
    """
    cache = cache_root / f"candidate-{ordinal:04d}"
    cache.mkdir()
    return cache.resolve()


def candidate_environment(cache_prefix: Path) -> dict[str, str]:
    """The environment one candidate's interpreter starts with.

    The inherited environment, plus the two variables that make the candidate
    write no bytecode and read none but its own. ``PYTHONPYCACHEPREFIX`` is
    what closes the reuse: with it set, ``cache_from_source`` resolves under
    the prefix and the ``__pycache__`` beside the source is never opened.
    """
    environment = dict(os.environ)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONPYCACHEPREFIX"] = str(cache_prefix)
    return environment


def candidate_command(selector: str, suite: str) -> list[str]:
    """The pytest invocation for one candidate.

    ``suite`` is whitespace-separated so an entry can name more than one module;
    it comes from the register rather than from here, because the register is
    where a mutation says what is supposed to catch it. ``-B`` is the
    interpreter-side half of the bytecode contract; the environment carries the
    other half, so a caller that drops either is still covered by the other.
    """
    return [
        sys.executable,
        "-B",
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


def remove_candidate_cache(cache_root: Path, cache: Path) -> None:
    """Remove exactly the candidate cache this process created, and only it.

    Both paths are resolved literals held from creation. The removal refuses a
    path that is not a direct child of the cache root, is not a directory, or
    is a symbolic link -- there is no legitimate way for any of those to be
    the directory :func:`create_candidate_cache` returned.
    """
    if cache.parent != cache_root or cache.is_symlink() or not cache.is_dir():
        raise RuntimeError(f"refusing to remove {cache}: not a candidate cache")
    shutil.rmtree(cache)


def remove_cache_root(cache_root: Path) -> None:
    """Remove the run's cache root once every candidate cache is gone.

    An interrupted run can leave a candidate cache behind; those are removed
    here by the same rule, one level down and nothing further. The root itself
    must carry the prefix this process created it with.
    """
    if not cache_root.name.startswith(CACHE_ROOT_PREFIX) or cache_root.is_symlink():
        raise RuntimeError(f"refusing to remove {cache_root}: not a cache root")
    if not cache_root.is_dir():
        return
    for child in cache_root.iterdir():
        remove_candidate_cache(cache_root, child.resolve())
    cache_root.rmdir()


def _run_tests(
    selector: str, suite: str, cache_prefix: Path
) -> subprocess.CompletedProcess[str]:
    """Run one mutation's targeted tests in its own suite, in its own cache."""
    return subprocess.run(
        candidate_command(selector, suite),
        cwd=str(PROJECT_ROOT),
        env=candidate_environment(cache_prefix),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def _apply(mutation: Mutation, cache_root: Path, ordinal: int) -> tuple[bool, str]:
    """Apply, test, restore. Returns (detected, detail)."""
    path = PROJECT_ROOT / mutation.asset
    original = path.read_bytes()
    digest = hashlib.sha256(original).hexdigest()

    text = original.decode("utf-8")
    occurrences = text.count(mutation.old)
    if occurrences != 1:
        return False, f"STALE: `old` occurs {occurrences} times, expected exactly 1"

    cache = create_candidate_cache(cache_root, ordinal)
    try:
        _write_bytes(
            path, text.replace(mutation.old, mutation.new).encode("utf-8")
        )
        result = _run_tests(mutation.tests, mutation.suite, cache)
    finally:
        # The restore comes first: a failure to remove a cache must never be
        # able to stand between the working tree and its committed bytes.
        try:
            _write_bytes(path, original)
        finally:
            remove_candidate_cache(cache_root, cache)

    restored = hashlib.sha256(path.read_bytes()).hexdigest()
    if restored != digest:
        return False, "RESTORATION FAILED: the asset does not match its original bytes"

    if "no tests ran" in result.stdout or "no tests ran" in result.stderr:
        return False, f"NO TARGETED TESTS matched `{mutation.tests}`"

    if result.returncode == 0:
        return False, "NOT DETECTED: every targeted test still passed"

    return True, "detected"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", default="", help="substring filter on mutation id")
    arguments = parser.parse_args()

    selected = [m for m in MUTATIONS if arguments.only in m.identifier]
    print(f"{len(selected)} mutations to run against the current tip\n")

    started = time.monotonic()
    failures: list[tuple[Mutation, str]] = []
    cache_root = create_cache_root()
    try:
        for index, mutation in enumerate(selected, start=1):
            detected, detail = _apply(mutation, cache_root, index)
            status = "ok  " if detected else "FAIL"
            print(f"[{index:3}/{len(selected)}] {status} {mutation.identifier}")
            if not detected:
                print(f"          {detail}")
                failures.append((mutation, detail))
    finally:
        remove_cache_root(cache_root)

    elapsed = time.monotonic() - started
    print(
        f"\n{len(selected) - len(failures)}/{len(selected)} detected "
        f"in {elapsed:.0f}s"
    )
    for mutation, detail in failures:
        print(f"  - {mutation.identifier}: {detail}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
