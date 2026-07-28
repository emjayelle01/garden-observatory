"""A bounded, atomic, non-blocking lock for MGO operations jobs.

Two backups running at once would compete for the same destination directory,
the same retention decision and the same temporary-file namespace. The timer
makes an overlap plausible rather than theoretical: a daily run that is slow
because the SD card is busy can still be running when an operator takes a manual
backup.

The lock is a file created with ``O_CREAT | O_EXCL``. That single system call is
the whole mutual-exclusion mechanism, and it is atomic on both POSIX and
Windows, which is what lets the same code be exercised by the test suite on the
development machine and relied upon on the Pi.

Three decisions are deliberate:

* **No waiting.** Acquisition is a single attempt. A backup that queues behind
  another backup is not more useful than one that reports "already running" and
  exits; the timer will run again tomorrow, and a bounded job that never blocks
  cannot pile up under a stuck predecessor.
* **Stale reclamation is age-based only, never PID-based.** Checking whether a
  recorded PID still exists is the usual approach and it is wrong here: PID
  semantics differ across the platforms this code runs on, a PID can be reused,
  and "I cannot see that process" is not proof that it is dead. Age is a
  conservative, portable signal, and the threshold is set far beyond any
  plausible backup duration.
* **Release only removes a lock we still own.** The file records a random token
  written at acquisition; release re-reads it and leaves the file alone if it
  changed. Without that check, a process whose lock had already been reclaimed
  as stale would delete the *new* owner's lock on its way out.
"""

from __future__ import annotations

import json
import os
import secrets
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from mgo.operations.errors import ErrorCode, OperationError

#: Age beyond which a lock file is treated as abandoned. Six hours is far longer
#: than any backup of a Raspberry Pi observation database could plausibly take,
#: so reclaiming one is never a race with a healthy job -- it only ever recovers
#: from a power cut or a SIGKILL.
DEFAULT_STALE_AFTER_SECONDS = 6 * 60 * 60

#: Permissions for the lock file: owner read/write, group read. It carries no
#: secret, but nothing this tooling creates is world-readable.
LOCK_FILE_MODE = 0o640


@dataclass(frozen=True)
class LockInfo:
    """What a held lock records about its owner.

    ``pid`` is written for a human reading the file during an incident. It is
    deliberately **not** used to decide whether the lock is stale.
    """

    token: str
    pid: int
    operation: str
    acquired_at: str

    def as_dict(self) -> dict[str, object]:
        """Return the serialisable form written into the lock file."""
        return {
            "token": self.token,
            "pid": self.pid,
            "operation": self.operation,
            "acquired_at": self.acquired_at,
        }


def _read_token(path: Path) -> str | None:
    """Return the token recorded in a lock file, or ``None`` if unreadable.

    A lock file that is missing, truncated, not JSON or missing its token is
    reported as ``None`` rather than raising: the caller only ever uses the
    answer to decide "is this still mine?", and an unreadable lock is certainly
    not ours.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        payload = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    token = payload.get("token")
    return token if isinstance(token, str) else None


def _age_seconds(path: Path) -> float | None:
    """Return the lock file's age in seconds, or ``None`` if it cannot be read."""
    try:
        modified = path.stat().st_mtime
    except OSError:
        return None
    return max(0.0, datetime.now(UTC).timestamp() - modified)


class OperationLock:
    """An exclusive, non-blocking lock over one operations job.

    Prefer :func:`operation_lock`, which guarantees release. This class is
    public because a test needs to hold a lock across a second acquisition
    attempt without a nested ``with`` block obscuring what is being proved.
    """

    def __init__(
        self,
        path: Path,
        *,
        operation: str,
        stale_after_seconds: float = DEFAULT_STALE_AFTER_SECONDS,
    ) -> None:
        if stale_after_seconds <= 0:
            raise OperationError(
                ErrorCode.INVALID_ARGUMENT,
                "The stale-lock threshold must be a positive number of seconds.",
            )
        self._path = path
        self._operation = operation
        self._stale_after_seconds = stale_after_seconds
        self._token: str | None = None
        self.reclaimed_stale_lock = False

    @property
    def path(self) -> Path:
        """The lock file's location."""
        return self._path

    @property
    def held(self) -> bool:
        """Whether this instance currently owns the lock."""
        return self._token is not None

    def acquire(self) -> LockInfo:
        """Take the lock, or raise :class:`OperationError` if it is held.

        One attempt, no waiting. A lock older than the stale threshold is
        reclaimed once; if the reclaimed lock is immediately retaken by another
        process, this call fails rather than looping, because a second
        contention within microseconds means a live competitor rather than an
        abandoned file.
        """
        try:
            return self._create()
        except FileExistsError:
            pass

        age = _age_seconds(self._path)
        if age is None or age < self._stale_after_seconds:
            held_for = "unknown" if age is None else f"{age:.0f}s"
            raise OperationError(
                ErrorCode.BACKUP_LOCKED,
                f"Another {self._operation} operation is already running "
                f"(lock held for {held_for} at {self._path.name}). "
                "No second run was started.",
            )

        # Abandoned: older than any real job could be. Remove it and try once.
        with suppress(OSError):
            self._path.unlink()
        self.reclaimed_stale_lock = True

        try:
            return self._create()
        except FileExistsError as exc:
            raise OperationError(
                ErrorCode.BACKUP_LOCKED,
                f"A stale {self._operation} lock was reclaimed but immediately "
                "retaken by another process. No second run was started.",
            ) from exc

    def _create(self) -> LockInfo:
        """Create the lock file exclusively, or raise ``FileExistsError``."""
        token = secrets.token_hex(16)
        info = LockInfo(
            token=token,
            pid=os.getpid(),
            operation=self._operation,
            acquired_at=datetime.now(UTC).isoformat(),
        )

        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(
                self._path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                LOCK_FILE_MODE,
            )
        except FileExistsError:
            raise
        except OSError as exc:
            raise OperationError(
                ErrorCode.BACKUP_DESTINATION_UNWRITABLE,
                f"The {self._operation} lock at {self._path.name} could not be "
                f"created: {exc.strerror or exc}.",
            ) from exc

        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(info.as_dict(), handle)
        except OSError as exc:
            # A lock we cannot describe is a lock nobody can diagnose; do not
            # leave it behind.
            with suppress(OSError):
                self._path.unlink()
            raise OperationError(
                ErrorCode.BACKUP_DESTINATION_UNWRITABLE,
                f"The {self._operation} lock could not be written: "
                f"{exc.strerror or exc}.",
            ) from exc

        self._token = token
        return info

    def release(self) -> None:
        """Release the lock, leaving another owner's lock untouched.

        Never raises. Release runs in a ``finally`` block, often while an
        exception is already propagating, and a failure to tidy up must not
        replace the real error with a filesystem one.
        """
        if self._token is None:
            return
        if _read_token(self._path) == self._token:
            with suppress(OSError):
                self._path.unlink()
        self._token = None


@contextmanager
def operation_lock(
    path: Path,
    *,
    operation: str,
    stale_after_seconds: float = DEFAULT_STALE_AFTER_SECONDS,
) -> Iterator[OperationLock]:
    """Hold an exclusive operations lock for the duration of the block."""
    lock = OperationLock(
        path, operation=operation, stale_after_seconds=stale_after_seconds
    )
    lock.acquire()
    try:
        yield lock
    finally:
        lock.release()


__all__ = [
    "DEFAULT_STALE_AFTER_SECONDS",
    "LOCK_FILE_MODE",
    "LockInfo",
    "OperationLock",
    "operation_lock",
]
