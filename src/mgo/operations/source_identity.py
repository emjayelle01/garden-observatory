"""Race-resistant identification of the files a backup reads.

A backup runs unattended, as a service account, against paths an administrator
may edit at any moment. The obvious safety check --

.. code-block:: python

    if path.is_symlink():
        raise ...
    handle = path.open("rb")

-- is correct for a path that does not change, and only for that. Between the
check and the open, the path can be replaced: what was validated as a regular
file can be opened as a symlink into somewhere else entirely. This is the
classic check-to-open (TOCTOU) gap, and ``fstat`` on the resulting descriptor
does not close it: ``fstat`` proves the *opened object* is a regular file, but
says nothing about whether a symlink was followed to reach it.

This module closes the gap in the only place it can be closed -- the open
itself -- and then proves that the object opened is still the object the path
names.

**On Linux** (the deployment target) ``O_NOFOLLOW`` makes the kernel refuse to
open a symlink at all, so the race has no window. There is deliberately **no
fallback to following the link** after that refusal: retrying without
``O_NOFOLLOW`` would reinstate exactly the hole the flag exists to close.

**On Windows** (the development machine) ``O_NOFOLLOW`` does not exist. Rather
than weaken the Linux guarantee to make the tests convenient, the fallback uses
the strongest identity checks the platform does offer -- ``lstat`` before the
open, ``fstat`` on the descriptor, ``lstat`` again afterwards, and a comparison
of the ``(st_dev, st_ino)`` identity across all three. Windows populates both
fields from ``GetFileInformationByHandle``, and they agree between ``fstat`` and
``lstat`` for the same object, which is what makes the comparison meaningful.
A substitution is therefore *detected* on Windows rather than *prevented*, and
the difference is recorded here rather than papered over.

Nothing in this module reads, logs or returns file *contents* on a failure
path: the whole point is that these files may hold credentials.
"""

from __future__ import annotations

import errno
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path

from mgo.operations.errors import ErrorCode, OperationError

#: Whether the kernel can refuse to open a symbolic link. True on Linux (the
#: production target); False on Windows, where the fallback below applies.
SUPPORTS_NO_FOLLOW = hasattr(os, "O_NOFOLLOW")

#: ``errno`` values a kernel uses to say "that was a symlink and you said
#: ``O_NOFOLLOW``". Linux reports ``ELOOP``; some BSD-derived systems report
#: ``EMLINK``. Both are treated as a refusal, never as a reason to retry.
_SYMLINK_REFUSED = frozenset(
    value
    for value in (
        getattr(errno, "ELOOP", None),
        getattr(errno, "EMLINK", None),
    )
    if value is not None
)


@dataclass(frozen=True)
class SourceIdentity:
    """The stable identity of a file, as observed through one ``stat``.

    ``(device, inode)`` is the pair that answers "is this the same file?" --
    a path is not, because a path can be repointed, and a size and timestamp
    are not, because they can coincide.
    """

    device: int
    inode: int
    size: int
    mtime_ns: int

    @classmethod
    def from_stat(cls, result: os.stat_result) -> SourceIdentity:
        """Build an identity from a ``stat`` result."""
        return cls(
            device=result.st_dev,
            inode=result.st_ino,
            size=result.st_size,
            mtime_ns=result.st_mtime_ns,
        )

    @property
    def key(self) -> tuple[int, int]:
        """The part of the identity that must never change: which file it is."""
        return (self.device, self.inode)

    def names_the_same_file_as(self, other: SourceIdentity) -> bool:
        """Whether both observations refer to the same filesystem object.

        Where inode reporting is unavailable (``st_ino == 0``, which some
        exotic filesystems still do), the comparison would be vacuously true
        for every file, so it is treated as *unproven* rather than as a match.
        """
        if self.inode == 0 or other.inode == 0:
            return False
        return self.key == other.key

    def is_unmodified_since(self, other: SourceIdentity) -> bool:
        """Whether size and modification time are unchanged as well."""
        return (self.size, self.mtime_ns) == (other.size, other.mtime_ns)


def _open_flags() -> int:
    """Return the most protective read-only open flags this platform offers."""
    flags = os.O_RDONLY
    # Linux: refuse a symlink in the kernel, and never leak the descriptor
    # across an exec.
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    # Windows: no newline translation, and do not let child processes inherit
    # a handle to a file that may hold credentials.
    flags |= getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOINHERIT", 0)
    return flags


def _fail(code: ErrorCode, message: str) -> OperationError:
    """Build the failure raised for an unsafe or unidentifiable source."""
    return OperationError(code, message)


def open_no_follow(path: Path, *, subject: str, code: ErrorCode) -> int:
    """Open ``path`` read-only, refusing to traverse a symbolic link.

    Returns a file descriptor the caller must close.

    On a platform without ``O_NOFOLLOW`` the link is rejected by an ``lstat``
    immediately before the open. That check *can* be raced; the identity
    comparisons performed by the callers below are what detect such a race
    afterwards.
    """
    if not SUPPORTS_NO_FOLLOW:
        try:
            pre = os.lstat(path)
        except FileNotFoundError as exc:
            raise _fail(code, f"{subject} does not exist.") from exc
        except OSError as exc:
            raise _fail(
                code, f"{subject} could not be inspected: {exc.strerror or exc}."
            ) from exc
        if stat.S_ISLNK(pre.st_mode):
            raise _fail(
                code,
                f"{subject} is a symbolic link. Reading through a link is "
                "refused: the target could be changed between runs.",
            )

    try:
        return os.open(path, _open_flags())
    except FileNotFoundError as exc:
        raise _fail(code, f"{subject} does not exist.") from exc
    except IsADirectoryError as exc:
        raise _fail(code, f"{subject} is a directory, not a file.") from exc
    except PermissionError as exc:
        raise _fail(
            code, f"{subject} could not be read: {exc.strerror or exc}."
        ) from exc
    except OSError as exc:
        if exc.errno in _SYMLINK_REFUSED:
            # The kernel refused because it *is* a link. Never retry without
            # O_NOFOLLOW -- that would reopen the hole this flag closes.
            raise _fail(
                code,
                f"{subject} is a symbolic link. Reading through a link is "
                "refused: the target could be changed between runs.",
            ) from exc
        raise _fail(
            code, f"{subject} could not be opened: {exc.strerror or exc}."
        ) from exc


def _identity_of_path(path: Path, *, subject: str, code: ErrorCode) -> SourceIdentity:
    """Return the identity the *path* currently names, without following links."""
    try:
        result = os.lstat(path)
    except FileNotFoundError as exc:
        raise _fail(
            code, f"{subject} disappeared while it was being read."
        ) from exc
    except OSError as exc:
        raise _fail(
            code, f"{subject} could not be re-inspected: {exc.strerror or exc}."
        ) from exc

    if stat.S_ISLNK(result.st_mode):
        raise _fail(
            code,
            f"{subject} became a symbolic link while it was being used; the "
            "path was replaced mid-operation.",
        )
    return SourceIdentity.from_stat(result)


def read_regular_file(
    path: Path,
    *,
    max_bytes: int,
    subject: str,
    code: ErrorCode,
) -> tuple[bytes, SourceIdentity]:
    """Read a bounded regular file once, proving what was read.

    Returns the bytes and the identity of the object they came from.

    The sequence is what makes the result trustworthy:

    1. open without following a symlink;
    2. ``fstat`` the **descriptor** -- so every later check describes the object
       actually opened, not whatever the path names by then;
    3. require a regular file and enforce the size bound *before* reading;
    4. read at most ``max_bytes + 1``, the extra byte being how a file that grew
       is detected rather than silently truncated;
    5. ``fstat`` again, and ``lstat`` the path again;
    6. require that the path still names this same object, that it is not now a
       symlink, and that size and modification time are unchanged.

    Any disagreement is a failure. The caller receives no partial read: a
    snapshot that is half the old file and half the new one is worse than none.
    """
    descriptor = open_no_follow(path, subject=subject, code=code)
    try:
        try:
            before = os.fstat(descriptor)
        except OSError as exc:
            raise _fail(
                code, f"{subject} could not be inspected: {exc.strerror or exc}."
            ) from exc

        if not stat.S_ISREG(before.st_mode):
            raise _fail(code, f"{subject} is not a regular file.")

        if before.st_size > max_bytes:
            raise _fail(
                code,
                f"{subject} is {before.st_size} bytes, above the {max_bytes} "
                "byte limit.",
            )

        try:
            data = os.read(descriptor, max_bytes + 1)
            # os.read may return short; keep going until the bound or EOF.
            while len(data) <= max_bytes:
                chunk = os.read(descriptor, max_bytes + 1 - len(data))
                if not chunk:
                    break
                data += chunk
            after = os.fstat(descriptor)
        except OSError as exc:
            raise _fail(
                code, f"{subject} could not be read: {exc.strerror or exc}."
            ) from exc
    finally:
        with suppress(OSError):
            os.close(descriptor)

    if len(data) > max_bytes:
        raise _fail(
            code, f"{subject} exceeds the {max_bytes} byte limit."
        )

    opened = SourceIdentity.from_stat(before)
    finished = SourceIdentity.from_stat(after)
    current = _identity_of_path(path, subject=subject, code=code)

    if not current.names_the_same_file_as(opened):
        raise _fail(
            code,
            f"{subject} was replaced while it was being read; the path no "
            "longer names the file that was opened.",
        )

    if not finished.is_unmodified_since(opened) or len(data) != opened.size:
        raise _fail(
            code,
            f"{subject} changed while it was being read; the snapshot would be "
            "neither the old file nor the new one.",
        )

    return data, opened


class SourceAnchor:
    """An open descriptor pinning the identity of a file being backed up.

    Holding the descriptor is the load-bearing part. While it is open the
    kernel cannot reuse that inode, so an identity comparison against it stays
    meaningful: if the path is repointed mid-operation, the comparison fails
    rather than quietly matching a recycled inode number.

    This is how the database is protected across SQLite's own open. The
    tooling cannot hand SQLite a descriptor -- it opens the path itself, by
    name -- so instead the anchor is opened first, held while SQLite connects,
    and the path re-checked afterwards. A substitution in that window is
    detected before anything is published.
    """

    def __init__(self, path: Path, identity: SourceIdentity, descriptor: int) -> None:
        self._path = path
        self._identity = identity
        self._descriptor = descriptor

    @property
    def identity(self) -> SourceIdentity:
        """The identity captured when the anchor was opened."""
        return self._identity

    def verify(self, *, subject: str, code: ErrorCode) -> None:
        """Confirm the path still names the anchored file.

        Called *after* the consumer (SQLite) has opened the path by name, which
        is the only moment at which a substitution during that open becomes
        detectable.
        """
        current = _identity_of_path(self._path, subject=subject, code=code)
        if not current.names_the_same_file_as(self._identity):
            raise _fail(
                code,
                f"{subject} was replaced while the backup was opening it; the "
                "path no longer names the file that was validated. Nothing was "
                "published.",
            )

    def close(self) -> None:
        """Release the descriptor. Never raises."""
        with suppress(OSError):
            os.close(self._descriptor)


@contextmanager
def anchored_source(
    path: Path, *, subject: str, code: ErrorCode
) -> Iterator[SourceAnchor]:
    """Hold an identity anchor on a regular file for the duration of the block.

    The file is opened without following a symlink and required to be regular
    before the block runs, so the anchor can only ever pin something safe to
    read.
    """
    descriptor = open_no_follow(path, subject=subject, code=code)
    try:
        try:
            observed = os.fstat(descriptor)
        except OSError as exc:
            raise _fail(
                code, f"{subject} could not be inspected: {exc.strerror or exc}."
            ) from exc

        if not stat.S_ISREG(observed.st_mode):
            raise _fail(code, f"{subject} is not a regular file.")

        anchor = SourceAnchor(path, SourceIdentity.from_stat(observed), descriptor)
    except BaseException:
        with suppress(OSError):
            os.close(descriptor)
        raise

    try:
        yield anchor
    finally:
        anchor.close()


__all__ = [
    "SUPPORTS_NO_FOLLOW",
    "SourceAnchor",
    "SourceIdentity",
    "anchored_source",
    "open_no_follow",
    "read_regular_file",
]
