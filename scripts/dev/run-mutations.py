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
"""

from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
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


def _run_tests(
    selector: str, suite: str
) -> subprocess.CompletedProcess[str]:
    """Run one mutation's targeted tests in its own suite.

    ``suite`` is whitespace-separated so an entry can name more than one module;
    it comes from the register rather than from here, because the register is
    where a mutation says what is supposed to catch it.
    """
    return subprocess.run(
        [
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
        ],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def _apply(mutation: Mutation) -> tuple[bool, str]:
    """Apply, test, restore. Returns (detected, detail)."""
    path = PROJECT_ROOT / mutation.asset
    original = path.read_bytes()
    digest = hashlib.sha256(original).hexdigest()

    text = original.decode("utf-8")
    occurrences = text.count(mutation.old)
    if occurrences != 1:
        return False, f"STALE: `old` occurs {occurrences} times, expected exactly 1"

    try:
        _write_bytes(
            path, text.replace(mutation.old, mutation.new).encode("utf-8")
        )
        result = _run_tests(mutation.tests, mutation.suite)
    finally:
        _write_bytes(path, original)

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
    for index, mutation in enumerate(selected, start=1):
        detected, detail = _apply(mutation)
        status = "ok  " if detected else "FAIL"
        print(f"[{index:3}/{len(selected)}] {status} {mutation.identifier}")
        if not detected:
            print(f"          {detail}")
            failures.append((mutation, detail))

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
