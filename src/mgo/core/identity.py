"""Application release and build identity for Matt's Garden Observatory.

This module is the **single authority** for "what build is this?". Before it
existed the release version was a hard-coded string literal repeated in five
places in :mod:`mgo.api.app` while ``pyproject.toml`` separately declared the
same value for the distribution -- two authorities that agreed only by
coincidence. Everything that reports a version now resolves it from here, so
they cannot drift apart.

The release version comes from the installed distribution's **package
metadata**, which makes ``pyproject.toml`` ``[project].version`` the one place a
release is declared. Note that the import name (``mgo``) is not the
distribution name (``garden-observatory``): under an editable install
``importlib.metadata.packages_distributions()`` does not map the former back to
the latter, so :data:`DISTRIBUTION_NAME` is named explicitly.

Resolution is deliberately **cached and total**:

* cached, because scanning ``sys.path`` for distribution metadata is real work
  and an identity endpoint must not do it on every request. The values cannot
  change within a running process, so caching them costs no truthfulness;
* total, because this module is imported during application import. A missing
  or broken metadata entry must never crash startup -- it degrades to the
  truthful :data:`UNKNOWN_VERSION` instead of inventing a number.

Nothing here runs a subprocess, invokes Git, reads ``.git``, opens the
database, touches the camera or performs any I/O beyond the package-metadata
lookup. It is therefore safe on Windows, in CI, and on the Raspberry Pi under
the service's ``ProtectSystem=strict`` / ``NoNewPrivileges=yes`` sandbox.
"""

from __future__ import annotations

import os
import platform
import re
from dataclasses import dataclass
from functools import lru_cache
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as distribution_version

#: Distribution name declared by ``pyproject.toml`` ``[project].name``. It is
#: not the import name (``mgo``), so it must be stated explicitly.
DISTRIBUTION_NAME = "garden-observatory"

#: Reported when the release version cannot be resolved. A truthful marker is
#: better than a fabricated number: an operator can tell "I do not know" from
#: "0.1.0", and a wrong version is worse than an absent one.
UNKNOWN_VERSION = "unknown"

#: Optional environment variable carrying the commit SHA of the deployed build.
#:
#: It exists because the release version alone cannot identify a deployment:
#: ``0.1.0`` does not change between commits, so it cannot answer "is the Pi
#: running the build I just pushed?". Every Git-derived alternative was
#: rejected -- a subprocess needs ``git`` installed for a sandboxed service
#: account, and parsing ``.git`` means handling loose refs, ``packed-refs`` and
#: a detached HEAD for a value that is only ever advisory.
#:
#: It is entirely optional. Unset is the normal case and is reported as
#: ``None``, never as an error.
BUILD_COMMIT_ENV = "MGO_BUILD_COMMIT"

#: A commit is reported only if it *looks* like one: 7 to 40 hexadecimal
#: characters (an abbreviated SHA through a full SHA-1). This is a safety
#: boundary as much as a sanity check -- it guarantees that arbitrary
#: environment text, a path, a branch name or a credential-bearing remote URL
#: can never be echoed to a client through this field.
_COMMIT_PATTERN = re.compile(r"\A[0-9a-fA-F]{7,40}\Z")


@lru_cache(maxsize=1)
def get_application_version() -> str:
    """Return the release version, resolved once per process.

    Reads the installed distribution's package metadata. Any failure --
    the distribution not being installed, or metadata that cannot be read --
    yields :data:`UNKNOWN_VERSION` rather than propagating, so importing this
    module can never prevent the application from starting.

    Tests that manipulate the underlying metadata must call
    ``get_application_version.cache_clear()``.
    """
    try:
        return distribution_version(DISTRIBUTION_NAME)
    except PackageNotFoundError:
        return UNKNOWN_VERSION
    except Exception:
        # Deliberately broad: a corrupt or unreadable metadata directory raises
        # something other than PackageNotFoundError, and no metadata problem is
        # worth an unstartable application.
        return UNKNOWN_VERSION


@lru_cache(maxsize=1)
def get_build_commit() -> str | None:
    """Return the deployed commit SHA, or ``None`` when none was supplied.

    The value comes solely from :data:`BUILD_COMMIT_ENV`. Absent, blank and
    malformed values are all reported as ``None``: a missing optional build
    identifier is not an application error, and an unrecognised value is
    discarded rather than echoed. A recognised value is normalised to
    lowercase so the reported form is stable regardless of how it was set.

    Tests that manipulate the environment must call
    ``get_build_commit.cache_clear()``.
    """
    raw = os.environ.get(BUILD_COMMIT_ENV)
    if raw is None:
        return None

    candidate = raw.strip()
    if not _COMMIT_PATTERN.match(candidate):
        return None
    return candidate.lower()


@dataclass(frozen=True)
class ApplicationIdentity:
    """An immutable, fully resolved answer to "what build is this?".

    Every field is either configured, package metadata, or a pure ``platform``
    lookup. Nothing here is a filesystem path, a configuration location, a
    hostname, a secret or any part of the environment beyond the single
    validated commit value -- so serialising it in full is safe.
    """

    application: str
    version: str
    commit: str | None
    python_version: str
    architecture: str

    def as_dict(self) -> dict[str, str | None]:
        """Return the serialisable form used by ``GET /version``."""
        return {
            "application": self.application,
            "version": self.version,
            "commit": self.commit,
            "python_version": self.python_version,
            "architecture": self.architecture,
        }


def build_identity(application_name: str) -> ApplicationIdentity:
    """Assemble the application identity for the running process.

    The application *name* is passed in rather than read from configuration
    here, so this module stays independent of configuration loading and the
    caller keeps a single name source (``config.application.name``, already
    shared by ``GET /`` and ``GET /health``).

    The two resolved values are cached; the two ``platform`` lookups are pure
    in-process calls with no I/O. Calling this per request is therefore free of
    side effects and cheap.
    """
    return ApplicationIdentity(
        application=application_name,
        version=get_application_version(),
        commit=get_build_commit(),
        python_version=platform.python_version(),
        architecture=platform.machine(),
    )


__all__ = [
    "BUILD_COMMIT_ENV",
    "DISTRIBUTION_NAME",
    "UNKNOWN_VERSION",
    "ApplicationIdentity",
    "build_identity",
    "get_application_version",
    "get_build_commit",
]
