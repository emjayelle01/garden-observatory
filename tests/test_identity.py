"""Tests for the central application identity/version resolution.

These exercise :mod:`mgo.core.identity` directly: version resolution from
package metadata, its fallback when metadata is unavailable, build-commit
validation, caching, and the assembled identity object.

Both resolvers are cached for the life of the process, so every test that
manipulates metadata or the environment clears the caches on the way in *and*
on the way out -- otherwise a fake would leak into the rest of the session
through the shared cache.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import FrozenInstanceError
from importlib.metadata import PackageNotFoundError
from typing import Any

import pytest

from mgo.core.identity import (
    BUILD_COMMIT_ENV,
    DISTRIBUTION_NAME,
    UNKNOWN_VERSION,
    ApplicationIdentity,
    build_identity,
    get_application_version,
    get_build_commit,
)

#: A full-length SHA-1, used only as test data. Nothing here reads the real
#: checkout's HEAD: identity must be provable without a Git repository.
_FULL_SHA = "d381b6d00aa1deff2303e1890f2fcfea22ab48cd"


@pytest.fixture(autouse=True)
def _clear_identity_caches() -> Iterator[None]:
    """Resolve from scratch in every test, and leave no fake behind."""
    get_application_version.cache_clear()
    get_build_commit.cache_clear()
    yield
    get_application_version.cache_clear()
    get_build_commit.cache_clear()


# --- release version --------------------------------------------------------


def test_version_comes_from_package_metadata() -> None:
    """The release version is read from the installed distribution."""
    calls: list[str] = []

    def _fake_version(name: str) -> str:
        calls.append(name)
        return "1.2.3"

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("mgo.core.identity.distribution_version", _fake_version)
        assert get_application_version() == "1.2.3"

    # The distribution name, not the import name: they differ in this project.
    assert calls == [DISTRIBUTION_NAME]
    assert DISTRIBUTION_NAME == "garden-observatory"


def test_version_matches_the_real_installed_distribution() -> None:
    """Against the real environment, resolution returns a usable version.

    Deliberately does not assert a specific number -- that would couple the
    test to the current release -- only that a real, non-fallback version is
    resolved, which is what proves package metadata is genuinely reachable
    from an editable ``uv`` install.
    """
    resolved = get_application_version()

    assert resolved != UNKNOWN_VERSION
    assert resolved
    assert resolved[0].isdigit()


def test_missing_package_metadata_falls_back_to_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An uninstalled distribution yields ``unknown``, never a crash."""

    def _not_found(name: str) -> str:
        raise PackageNotFoundError(name)

    monkeypatch.setattr("mgo.core.identity.distribution_version", _not_found)

    assert get_application_version() == UNKNOWN_VERSION


def test_broken_package_metadata_falls_back_to_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Any metadata failure degrades truthfully rather than propagating.

    A corrupt or unreadable ``dist-info`` directory raises something other than
    ``PackageNotFoundError``; identity resolution must still not be able to
    prevent the application from importing or starting.
    """

    def _explode(name: str) -> str:
        raise OSError("dist-info is unreadable")

    monkeypatch.setattr("mgo.core.identity.distribution_version", _explode)

    assert get_application_version() == UNKNOWN_VERSION


def test_version_is_resolved_once_and_then_cached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repeated calls do not re-discover package metadata.

    This is the guarantee that lets ``GET /version`` be served without doing
    filesystem work per request.
    """
    calls: list[str] = []

    def _counting_version(name: str) -> str:
        calls.append(name)
        return "9.9.9"

    monkeypatch.setattr("mgo.core.identity.distribution_version", _counting_version)

    results = [get_application_version() for _ in range(5)]

    assert results == ["9.9.9"] * 5
    assert len(calls) == 1


def test_version_is_deterministic_across_repeated_resolution() -> None:
    """The resolved version never varies within a running process."""
    assert len({get_application_version() for _ in range(10)}) == 1


# --- build commit -----------------------------------------------------------


def test_absent_build_commit_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unset variable is absent, not an error."""
    monkeypatch.delenv(BUILD_COMMIT_ENV, raising=False)

    assert get_build_commit() is None


@pytest.mark.parametrize(
    "supplied",
    [
        "",
        "   ",
        "abc",  # too short to be an abbreviated SHA
        "z" * 40,  # not hexadecimal
        "0" * 41,  # longer than a SHA-1
        "d381b6d /etc/garden-observatory/mgo.toml",
        "refs/heads/main",
        "git@github.com:someone/private.git",
        "token=s3cret",
    ],
)
def test_malformed_build_commit_is_discarded(
    monkeypatch: pytest.MonkeyPatch,
    supplied: str,
) -> None:
    """Anything that is not a plausible SHA is dropped, never echoed.

    This is the boundary that stops arbitrary environment text -- a path, a
    branch name, a remote URL, a secret -- reaching a client through the
    ``commit`` field.
    """
    monkeypatch.setenv(BUILD_COMMIT_ENV, supplied)

    assert get_build_commit() is None


@pytest.mark.parametrize(
    ("supplied", "expected"),
    [
        ("d381b6d", "d381b6d"),
        ("  d381b6d  ", "d381b6d"),
        ("D381B6D", "d381b6d"),
        (_FULL_SHA, _FULL_SHA),
        (_FULL_SHA.upper(), _FULL_SHA),
    ],
)
def test_valid_build_commit_is_normalised(
    monkeypatch: pytest.MonkeyPatch,
    supplied: str,
    expected: str,
) -> None:
    """A plausible SHA is accepted, trimmed and lowercased."""
    monkeypatch.setenv(BUILD_COMMIT_ENV, supplied)

    assert get_build_commit() == expected


def test_build_commit_is_resolved_once_and_then_cached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The environment is read once; later mutation cannot change the answer.

    Identity must be stable for the life of a process, so a value that changed
    mid-run would be a bug, not a feature.
    """
    monkeypatch.setenv(BUILD_COMMIT_ENV, "d381b6d")
    first = get_build_commit()

    monkeypatch.setenv(BUILD_COMMIT_ENV, "0000000")
    second = get_build_commit()

    assert first == "d381b6d"
    assert second == "d381b6d"


# --- the assembled identity -------------------------------------------------


def test_identity_carries_every_field(monkeypatch: pytest.MonkeyPatch) -> None:
    """The identity combines the configured name, metadata and platform."""
    monkeypatch.setattr(
        "mgo.core.identity.distribution_version", lambda name: "4.5.6"
    )
    monkeypatch.setenv(BUILD_COMMIT_ENV, "abcdef1")

    identity = build_identity("Matt's Garden Observatory")

    assert identity == ApplicationIdentity(
        application="Matt's Garden Observatory",
        version="4.5.6",
        commit="abcdef1",
        python_version=identity.python_version,
        architecture=identity.architecture,
    )
    assert identity.python_version
    assert identity.architecture


def test_identity_is_truthful_when_nothing_is_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no metadata and no build commit the identity still resolves."""

    def _not_found(name: str) -> str:
        raise PackageNotFoundError(name)

    monkeypatch.setattr("mgo.core.identity.distribution_version", _not_found)
    monkeypatch.delenv(BUILD_COMMIT_ENV, raising=False)

    identity = build_identity("MGO")

    assert identity.version == UNKNOWN_VERSION
    assert identity.commit is None
    # The platform facts are always knowable, so they are never "unknown".
    assert identity.python_version
    assert identity.architecture


def test_identity_is_immutable() -> None:
    """The identity is a frozen value object, not mutable global state."""
    identity = build_identity("MGO")

    with pytest.raises(FrozenInstanceError):
        identity.version = "tampered"  # type: ignore[misc]


def test_identity_serialises_deterministically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``as_dict`` has a fixed key set and stable values."""
    monkeypatch.setattr(
        "mgo.core.identity.distribution_version", lambda name: "1.0.0"
    )
    monkeypatch.delenv(BUILD_COMMIT_ENV, raising=False)

    first = build_identity("MGO").as_dict()
    second = build_identity("MGO").as_dict()

    assert first == second
    assert set(first) == {
        "application",
        "version",
        "commit",
        "python_version",
        "architecture",
    }
    assert first["commit"] is None


# --- no side effects --------------------------------------------------------


def test_identity_resolution_runs_no_subprocess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing in identity resolution shells out -- to Git or anything else.

    ``git`` is not guaranteed to exist for the sandboxed production service
    account, so a subprocess-based design would be untruthful in exactly the
    deployment that matters.
    """

    def _forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("identity resolution must not run a subprocess")

    monkeypatch.setattr("subprocess.run", _forbidden)
    monkeypatch.setattr("subprocess.Popen", _forbidden)
    monkeypatch.setattr("subprocess.check_output", _forbidden)

    identity = build_identity("MGO")

    assert identity.version
    assert identity.python_version


def test_identity_module_imports_without_touching_git_or_the_database() -> None:
    """The module exposes no Git, repository-path or database machinery."""
    import mgo.core.identity as identity_module

    source = identity_module.__file__
    assert source is not None

    exported = set(identity_module.__all__)
    assert exported == {
        "BUILD_COMMIT_ENV",
        "DISTRIBUTION_NAME",
        "UNKNOWN_VERSION",
        "ApplicationIdentity",
        "build_identity",
        "get_application_version",
        "get_build_commit",
    }
    # No repository or database import ever became part of the module.
    assert not hasattr(identity_module, "subprocess")
    assert not hasattr(identity_module, "sqlite3")
