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
of the ``(st_dev, st_ino)`` identity **across all three**. Windows populates both
fields from ``GetFileInformationByHandle``, and they agree between ``fstat`` and
``lstat`` for the same object, which is what makes the comparison meaningful.
A substitution is therefore *detected* on Windows rather than *prevented*, and
the difference is recorded here rather than papered over.

The pre-open comparison is not decoration. An earlier version took that
``lstat``, rejected a symlink, and then **discarded the observation**. That left
a regular file replaceable by another regular file between the ``lstat`` and the
``os.open``: from the open onwards every observation would describe the
replacement, and they would all agree with one another, so nothing later could
notice. Only the discarded observation disagreed.

Descriptor lifetime is part of the guarantee too. Every path comparison happens
**while the descriptor is still open**, because holding it is what stops an
unlinked inode being recycled underneath the comparison -- the same reasoning
that makes :class:`SourceAnchor` work.

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


@dataclass(frozen=True)
class OpenedSource:
    """A descriptor, plus what was observed about the path before opening it.

    ``pre_open_identity`` is ``None`` on a platform where ``O_NOFOLLOW`` did the
    work: there the kernel is authoritative and no earlier observation is needed
    or meaningful. On the fallback platform it carries the ``lstat`` taken
    immediately before the open, so the caller can prove that the object it
    opened is the object it inspected.

    Retaining that observation is the whole point. An earlier version rejected a
    pre-open symlink and then *discarded* the ``lstat``, which left a regular
    file replaceable by another regular file between the check and the open: the
    descriptor and every later observation would agree with each other, because
    both would describe the replacement.
    """

    descriptor: int
    pre_open_identity: SourceIdentity | None


def _open_flags() -> int:
    """Return the most protective read-only open flags this platform offers."""
    flags = os.O_RDONLY
    # Linux: refuse a symlink in the kernel, and never leak the descriptor
    # across an exec. Gated on the module constant rather than on ``os``
    # directly, so a test can force the fallback path deterministically on a
    # platform that does support the flag.
    if SUPPORTS_NO_FOLLOW:
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


def open_no_follow(path: Path, *, subject: str, code: ErrorCode) -> OpenedSource:
    """Open ``path`` read-only, refusing to traverse a symbolic link.

    Returns the descriptor together with the pre-open observation (where one was
    taken). The caller must close the descriptor and must compare the
    observation against the descriptor's own ``fstat`` — see
    :func:`require_opened_identity`.

    On a platform without ``O_NOFOLLOW`` the link is rejected by an ``lstat``
    immediately before the open. That check alone *can* be raced, which is
    precisely why the observation is returned rather than thrown away.
    """
    pre_open: SourceIdentity | None = None

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
        pre_open = SourceIdentity.from_stat(pre)

    try:
        descriptor = os.open(path, _open_flags())
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

    return OpenedSource(descriptor=descriptor, pre_open_identity=pre_open)


def require_opened_identity(
    opened: OpenedSource,
    observed: SourceIdentity,
    *,
    subject: str,
    code: ErrorCode,
) -> None:
    """Confirm the descriptor refers to the object observed before opening.

    A no-op where ``O_NOFOLLOW`` was used, because the kernel already guaranteed
    it. On the fallback platform this is the check that catches a **regular file
    replaced by another regular file** between the ``lstat`` and the ``os.open``
    — a substitution that no later comparison can see, because from the open
    onwards every observation describes the replacement consistently.
    """
    if opened.pre_open_identity is None:
        return

    if not observed.names_the_same_file_as(opened.pre_open_identity):
        raise _fail(
            code,
            f"{subject} was replaced between being inspected and being opened; "
            "the file that was opened is not the file that was validated.",
        )


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

    The sequence is what makes the result trustworthy, and the **ordering** is
    part of it:

    1. open without following a symlink, keeping any pre-open observation;
    2. ``fstat`` the **descriptor** -- so every later check describes the object
       actually opened, not whatever the path names by then;
    3. require a regular file and enforce the size bound *before* reading;
    4. confirm the descriptor is the object observed before the open (fallback
       platforms only);
    5. read at most ``max_bytes + 1``, the extra byte being how a file that grew
       is detected rather than silently truncated;
    6. ``fstat`` again, and ``lstat`` the path again -- **while the descriptor is
       still open**;
    7. require that the path still names this same object, that it is not now a
       symlink, and that size and modification time are unchanged;
    8. only then close the descriptor.

    Step 6 happening before step 8 is load-bearing. An earlier version closed
    the descriptor first, which meant an unlinked inode could in principle be
    recycled before the comparison ran -- and this module explains elsewhere
    that holding the descriptor open is exactly what prevents that.

    Any disagreement is a failure, and the descriptor is closed on every path.
    The caller receives no partial read: a snapshot that is half the old file
    and half the new one is worse than none.
    """
    opened = open_no_follow(path, subject=subject, code=code)
    descriptor = opened.descriptor
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

        identity = SourceIdentity.from_stat(before)
        require_opened_identity(opened, identity, subject=subject, code=code)

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

        if len(data) > max_bytes:
            raise _fail(code, f"{subject} exceeds the {max_bytes} byte limit.")

        # Still holding the descriptor: the inode cannot be recycled underneath
        # this comparison.
        current = _identity_of_path(path, subject=subject, code=code)

        if not current.names_the_same_file_as(identity):
            raise _fail(
                code,
                f"{subject} was replaced while it was being read; the path no "
                "longer names the file that was opened.",
            )

        finished = SourceIdentity.from_stat(after)
        if not finished.is_unmodified_since(identity) or len(data) != identity.size:
            raise _fail(
                code,
                f"{subject} changed while it was being read; the snapshot "
                "would be neither the old file nor the new one.",
            )
    finally:
        with suppress(OSError):
            os.close(descriptor)

    return data, identity


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
    path: Path,
    *,
    subject: str,
    code: ErrorCode,
    identity_code: ErrorCode | None = None,
) -> Iterator[SourceAnchor]:
    """Hold an identity anchor on a regular file for the duration of the block.

    The file is opened without following a symlink, required to be regular, and
    -- on a fallback platform -- proven to be the object observed immediately
    before the open, all *before* the block runs. The anchor can therefore only
    ever pin something that was safe to read at the moment it was pinned.

    ``identity_code`` is the error raised when an identity comparison fails,
    letting a caller distinguish "the source is unusable" from "the source was
    substituted". It defaults to ``code``.
    """
    failure_code = identity_code if identity_code is not None else code

    opened = open_no_follow(path, subject=subject, code=code)
    descriptor = opened.descriptor
    try:
        try:
            observed = os.fstat(descriptor)
        except OSError as exc:
            raise _fail(
                code, f"{subject} could not be inspected: {exc.strerror or exc}."
            ) from exc

        if not stat.S_ISREG(observed.st_mode):
            raise _fail(code, f"{subject} is not a regular file.")

        identity = SourceIdentity.from_stat(observed)
        require_opened_identity(
            opened, identity, subject=subject, code=failure_code
        )

        anchor = SourceAnchor(path, identity, descriptor)
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
    "OpenedSource",
    "SourceAnchor",
    "SourceIdentity",
    "anchored_source",
    "open_no_follow",
    "read_regular_file",
    "require_opened_identity",
]
