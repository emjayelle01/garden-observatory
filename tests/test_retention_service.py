"""Tests for the destructive retention service and its safety boundary.

Every test here operates on a temporary capture root created by the test itself.
Nothing touches the repository's ``data/`` directory, the Task 13.2 validation
captures, or any path outside ``tmp_path`` -- and several tests assert that
explicitly rather than trusting it, because "no file outside the test root was
touched" is the single property this module exists to establish.

Where the host cannot create the condition under test -- a symlink on a Windows
account without the privilege, a file whose size changes between validation and
unlink -- the filesystem seam is replaced rather than the test skipped. A refusal
that is only tested on Linux is a refusal that is not tested on the machine the
code is written on.
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

import mgo.retention.service as service_module
from mgo.core.config import RetentionConfig
from mgo.core.database import apply_migrations, database_connection
from mgo.core.observations import list_observations
from mgo.operations.backup import LOCK_FILENAME as BACKUP_LOCK_FILENAME
from mgo.retention.models import (
    RetentionErrorCategory,
    RetentionReason,
    RetentionRuntimeState,
    RetentionState,
)
from mgo.retention.repository import RetentionRepository, RetentionRepositoryError
from mgo.retention.service import (
    OBSERVATION_KIND,
    OBSERVATION_SOURCE,
    SUCCESS_STATUS,
    RetentionService,
)

NOW = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)
PAYLOAD = b"jpeg-bytes-stand-in"


class _Harness:
    """A temporary database, capture root and service, wired together."""

    def __init__(self, tmp_path: Path, config: RetentionConfig) -> None:
        self.root = tmp_path / "captures"
        self.root.mkdir()
        self.database_path = tmp_path / "mgo.db"
        apply_migrations(self.database_path)
        self.repository = RetentionRepository(
            self.database_path, clock=lambda: NOW
        )
        self.state = RetentionRuntimeState(enabled=config.enabled)
        # Task 14.5A: a run holds the backup's lock, so every executing
        # service needs a backup directory to coordinate with.
        self.backup_directory = tmp_path / "backups"
        self.backup_directory.mkdir()
        self.backup_lock_path = self.backup_directory / BACKUP_LOCK_FILENAME
        self.service = RetentionService(
            config,
            self.repository,
            self.state,
            self.root,
            clock=lambda: NOW,
            database_path=self.database_path,
            backup_lock_path=self.backup_lock_path,
        )

    def add(
        self,
        identifier: str,
        *,
        days_old: float = 900.0,
        origin: str | None = "motion",
        payload: bytes = PAYLOAD,
        filename: str | None = None,
        absolute_path: str | None = None,
        catalogue_size: int | None = None,
        write_file: bool = True,
    ) -> Path:
        """Catalogue one capture and (by default) write its media."""
        name = filename if filename is not None else f"{identifier}.jpg"
        media = self.root / name
        if write_file:
            media.write_bytes(payload)

        timestamp = (NOW - timedelta(days=days_old)).isoformat()
        metadata = {} if origin is None else {"origin": origin}
        with database_connection(self.database_path) as connection:
            connection.execute(
                """
                INSERT INTO captures (
                    id, filename, absolute_path, captured_at_utc, width, height,
                    filesize_bytes, camera_backend, created_at_utc, extra_metadata
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    identifier,
                    name,
                    absolute_path if absolute_path is not None else str(media),
                    timestamp,
                    4608,
                    2592,
                    (
                        catalogue_size
                        if catalogue_size is not None
                        else len(payload)
                    ),
                    "simulator",
                    timestamp,
                    json.dumps(metadata),
                ),
            )
        return media

    def lifecycle(self) -> dict[str, str]:
        """Return ``{capture_id: state}`` for every lifecycle row."""
        with database_connection(self.database_path) as connection:
            return {
                str(row[0]): str(row[1])
                for row in connection.execute(
                    "SELECT capture_id, state FROM capture_media_lifecycle"
                )
            }

    def capture_ids(self) -> set[str]:
        """Return every catalogued capture id."""
        with database_connection(self.database_path) as connection:
            return {
                str(row[0])
                for row in connection.execute("SELECT id FROM captures")
            }

    def observations(self) -> list[Any]:
        """Return the retention observations, newest first."""
        return list_observations(self.database_path, kind=OBSERVATION_KIND)


def _config(
    *,
    enabled: bool = True,
    max_age_days: int | None = 7,
    max_managed_bytes: int | None = None,
    minimum_keep_count: int = 1,
    max_deletions_per_run: int = 25,
) -> RetentionConfig:
    """Build a retention configuration for a service test."""
    return RetentionConfig(
        enabled=enabled,
        max_age_days=max_age_days,
        max_managed_bytes=max_managed_bytes,
        minimum_keep_count=minimum_keep_count,
        max_deletions_per_run=max_deletions_per_run,
    )


#: Every harness seeds one very recent managed capture. ``minimum_keep_count``
#: is validated ``>= 1``, so without it the newest managed capture in a test
#: would always be the protected one and a single-capture test could never
#: select anything. This filler occupies that slot and is expected to survive
#: every run in this module.
PROTECTED_FILLER = "keep-newest"


def _harness(tmp_path: Path, *, seed_protected: bool = True, **kwargs: Any) -> _Harness:
    """Build a harness with the given retention configuration."""
    harness = _Harness(tmp_path, _config(**kwargs))
    if seed_protected:
        harness.add(PROTECTED_FILLER, days_old=0.0)
    return harness


# --- the happy path ---------------------------------------------------------


def test_an_eligible_managed_capture_is_deleted_and_recorded(
    tmp_path: Path,
) -> None:
    """One eligible capture: file gone, lifecycle deleted, one observation."""
    harness = _harness(tmp_path)
    media = harness.add("cap-old")
    harness.add("cap-new", days_old=1)

    result = harness.service.run_once()

    assert result.executed is True
    assert result.error_category is None
    assert (result.deleted_count, result.bytes_reclaimed) == (1, len(PAYLOAD))
    assert not media.exists()
    assert harness.lifecycle() == {"cap-old": "deleted"}
    [observation] = harness.observations()
    assert observation.status == SUCCESS_STATUS
    assert observation.source == OBSERVATION_SOURCE
    assert observation.correlation_id == "cap-old"
    assert observation.payload["recovered_pending"] is False
    assert observation.payload["policy_reason"] == RetentionReason.AGE.value


def test_the_capture_row_survives_the_deletion_of_its_media(
    tmp_path: Path,
) -> None:
    """Retention reclaims media; it never erases the history of the capture."""
    harness = _harness(tmp_path)
    harness.add("cap-old")

    harness.service.run_once()

    assert harness.capture_ids() == {"cap-old", PROTECTED_FILLER}


def test_one_successful_deletion_creates_exactly_one_success_observation(
    tmp_path: Path,
) -> None:
    """No deletion produces two success rows, however many runs follow it."""
    harness = _harness(tmp_path)
    harness.add("cap-old")

    harness.service.run_once()
    harness.service.run_once()
    harness.service.run_once()

    successes = [
        observation
        for observation in harness.observations()
        if observation.status == SUCCESS_STATUS
    ]
    assert len(successes) == 1


def test_the_success_observation_carries_no_paths(tmp_path: Path) -> None:
    """An operator learns which capture went, not where the media lived."""
    harness = _harness(tmp_path)
    harness.add("cap-old")

    harness.service.run_once()

    [observation] = harness.observations()
    rendered = json.dumps(observation.payload)
    assert str(harness.root) not in rendered
    assert str(harness.database_path) not in rendered
    assert "absolute_path" not in rendered
    assert set(observation.payload) == {
        "capture_id",
        "filename",
        "filesize_bytes",
        "policy_reason",
        "captured_at",
        "recovered_pending",
    }


def test_runtime_counters_follow_a_completed_run(tmp_path: Path) -> None:
    """Process-lifetime counters reflect what the run actually did."""
    harness = _harness(tmp_path)
    harness.add("cap-a")
    harness.add("cap-b")
    harness.add("cap-new", days_old=1)

    harness.service.run_once()

    snapshot = harness.state.snapshot()
    assert snapshot.state is RetentionState.IDLE
    assert snapshot.total_runs == 1
    assert snapshot.total_captures_deleted == 2
    assert snapshot.total_bytes_reclaimed == 2 * len(PAYLOAD)
    assert snapshot.last_run_candidate_count == 2
    assert snapshot.last_error is None


# --- protection of what retention does not manage ---------------------------


@pytest.mark.parametrize("origin", [None, "manual", "timelapse", "Motion"])
def test_an_unmanaged_capture_can_never_reach_unlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, origin: str | None
) -> None:
    """A manual or unknown-origin capture never reaches the filesystem call.

    The unlink seam is replaced with something that fails the test if it is
    called at all, so this proves the protection at the boundary rather than by
    observing that the file happened to survive.
    """

    def _explode(path: Path) -> None:
        raise AssertionError(f"retention attempted to unlink {path}")

    monkeypatch.setattr(service_module, "_unlink", _explode)
    harness = _harness(tmp_path, minimum_keep_count=1)
    media = harness.add("cap-protected", origin=origin)

    result = harness.service.run_once()

    assert result.deleted_count == 0
    assert result.error_category is None
    assert media.exists()
    assert harness.lifecycle() == {}


def test_an_untracked_neighbouring_file_is_never_deleted(tmp_path: Path) -> None:
    """Only catalogued media is a candidate; the directory is not a source."""
    harness = _harness(tmp_path)
    harness.add("cap-old")
    stray = harness.root / "not_in_the_catalogue.jpg"
    stray.write_bytes(b"stray")
    older_stray = harness.root / "IMG_0001.JPG"
    older_stray.write_bytes(b"also stray")

    harness.service.run_once()

    assert stray.exists()
    assert older_stray.exists()


def test_a_nested_directory_in_the_capture_root_is_never_removed(
    tmp_path: Path,
) -> None:
    """Nothing recursive happens: a subdirectory and its contents survive."""
    harness = _harness(tmp_path)
    harness.add("cap-old")
    nested = harness.root / "archive"
    nested.mkdir()
    (nested / "keep.jpg").write_bytes(b"keep")

    harness.service.run_once()

    assert nested.is_dir()
    assert (nested / "keep.jpg").exists()


# --- the path safety boundary -----------------------------------------------


def _assert_refused(
    harness: _Harness,
    media: Path,
    category: RetentionErrorCategory,
    *,
    expect_file: bool = True,
) -> None:
    """Assert the run refused, left the media alone and wrote no intent."""
    result = harness.service.run_once()

    assert result.error_category is category
    assert result.deleted_count == 0
    assert harness.lifecycle() == {}
    if expect_file:
        assert media.exists()
    [observation] = harness.observations()
    assert observation.status == "failed"
    assert observation.payload["error_category"] == category.value


def test_a_path_outside_the_capture_root_is_refused(tmp_path: Path) -> None:
    """A catalogue row naming a file elsewhere on disk is not deletable."""
    harness = _harness(tmp_path)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    victim = outside / "cap-old.jpg"
    victim.write_bytes(PAYLOAD)
    harness.add("cap-old", absolute_path=str(victim), write_file=False)

    _assert_refused(harness, victim, RetentionErrorCategory.UNSAFE_PATH)


def test_a_traversal_path_is_refused(tmp_path: Path) -> None:
    """``..`` is rejected syntactically, before anything is normalised away."""
    harness = _harness(tmp_path)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    victim = outside / "cap-old.jpg"
    victim.write_bytes(PAYLOAD)
    harness.add(
        "cap-old",
        absolute_path=str(harness.root / ".." / "elsewhere" / "cap-old.jpg"),
        write_file=False,
    )

    _assert_refused(harness, victim, RetentionErrorCategory.UNSAFE_PATH)


def test_a_traversal_that_resolves_inside_the_root_is_still_refused(
    tmp_path: Path,
) -> None:
    """``..`` is refused even when it normalises back inside the capture root.

    Containment alone would accept this path: it resolves to a real capture in
    the real root. The syntactic rejection is what refuses it, and it earns its
    place precisely here -- the capture pipeline never writes a path containing
    ``..``, so a catalogue row carrying one has been tampered with or corrupted
    whatever it resolves to.
    """
    harness = _harness(tmp_path)
    media = harness.add("cap-old")
    (harness.root / "sub").mkdir()
    with database_connection(harness.database_path) as connection:
        connection.execute(
            "UPDATE captures SET absolute_path = ? WHERE id = ?",
            (str(harness.root / "sub" / ".." / "cap-old.jpg"), "cap-old"),
        )

    _assert_refused(harness, media, RetentionErrorCategory.UNSAFE_PATH)


def test_a_relative_path_is_refused(tmp_path: Path) -> None:
    """A non-absolute catalogue path has no defined target to validate."""
    harness = _harness(tmp_path)
    media = harness.add("cap-old", absolute_path="captures/cap-old.jpg")

    _assert_refused(harness, media, RetentionErrorCategory.UNSAFE_PATH)


def test_a_relative_path_is_refused_even_when_the_cwd_would_resolve_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A relative path is refused even standing in the capture root.

    With the process working directory *inside* the capture root, this path
    resolves to a real managed capture and would pass every containment, file
    and size check. Only the absolute-path requirement refuses it -- which is
    the point: a catalogue path whose meaning depends on where the process
    happens to be standing is not an identity anything may be deleted on.
    """
    harness = _harness(tmp_path)
    media = harness.add("cap-old", absolute_path="cap-old.jpg")
    monkeypatch.chdir(harness.root)

    _assert_refused(harness, media, RetentionErrorCategory.UNSAFE_PATH)


def test_a_filename_that_disagrees_with_the_path_is_refused(
    tmp_path: Path,
) -> None:
    """Two catalogue columns naming different files means neither is trusted."""
    harness = _harness(tmp_path)
    media = harness.root / "actual.jpg"
    media.write_bytes(PAYLOAD)
    harness.add(
        "cap-old",
        filename="expected.jpg",
        absolute_path=str(media),
        write_file=False,
    )

    _assert_refused(harness, media, RetentionErrorCategory.UNSAFE_PATH)


def test_a_symlinked_target_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A catalogue path that is itself a link is refused before resolution.

    The seam is replaced rather than a real link created, so the refusal is
    tested on every host including a Windows account without the privilege to
    make one -- which is the machine this code is written on.
    """
    harness = _harness(tmp_path)
    media = harness.add("cap-old")
    monkeypatch.setattr(
        service_module, "_is_symlink", lambda path: Path(path) == media
    )

    _assert_refused(harness, media, RetentionErrorCategory.UNSAFE_PATH)


def test_a_directory_target_is_refused(tmp_path: Path) -> None:
    """A catalogue row pointing at a directory never becomes an ``rmdir``."""
    harness = _harness(tmp_path)
    directory = harness.root / "cap-old.jpg"
    directory.mkdir()
    (directory / "inside.txt").write_bytes(b"inside")
    harness.add("cap-old", write_file=False)

    result = harness.service.run_once()

    assert result.error_category is RetentionErrorCategory.NOT_REGULAR_FILE
    assert directory.is_dir()
    assert (directory / "inside.txt").exists()
    assert harness.lifecycle() == {}


def test_a_non_regular_target_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Anything that is not a plain file is refused, whatever it is.

    A FIFO or device node cannot be created portably, so the seam answers for
    one; the rule under test is the refusal, not the operating system's ability
    to make the object.
    """
    harness = _harness(tmp_path)
    media = harness.add("cap-old")
    monkeypatch.setattr(service_module, "_is_regular_file", lambda path: False)

    _assert_refused(harness, media, RetentionErrorCategory.NOT_REGULAR_FILE)


def test_a_size_mismatch_is_refused(tmp_path: Path) -> None:
    """Catalogue and disk disagreeing means this is not the catalogued file."""
    harness = _harness(tmp_path)
    media = harness.add("cap-old", catalogue_size=len(PAYLOAD) + 1)

    _assert_refused(harness, media, RetentionErrorCategory.SIZE_MISMATCH)


def test_a_capture_root_that_is_not_a_directory_stops_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Containment is meaningless without a root, so the run refuses to start."""
    harness = _harness(tmp_path)
    media = harness.add("cap-old")
    monkeypatch.setattr(service_module, "_is_directory", lambda path: False)

    result = harness.service.run_once()

    assert result.error_category is RetentionErrorCategory.UNSAFE_PATH
    assert media.exists()
    assert harness.lifecycle() == {}


def test_a_relative_capture_root_stops_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A relative configured root cannot anchor a containment check.

    The working directory is set so that ``captures`` really does resolve to the
    real capture root: every downstream check would pass, and the run would
    delete. It is refused anyway, because a containment boundary that moves with
    the process working directory is not a boundary.
    """
    harness = _harness(tmp_path)
    media = harness.add("cap-old")
    monkeypatch.chdir(tmp_path)
    service = RetentionService(
        _config(),
        harness.repository,
        harness.state,
        Path("captures"),
        clock=lambda: NOW,
        database_path=harness.database_path,
        backup_lock_path=harness.backup_lock_path,
    )

    result = service.run_once()

    assert result.error_category is RetentionErrorCategory.UNSAFE_PATH
    assert media.exists()
    assert harness.lifecycle() == {}


# --- present media that is already missing ----------------------------------


def test_missing_media_without_a_pending_intent_is_an_inconsistency(
    tmp_path: Path,
) -> None:
    """``PRESENT`` + missing is unexplained, and is never called a success.

    Marking it deleted would fabricate a retention success for a file retention
    never touched, and would quietly absorb whatever really removed it.
    """
    harness = _harness(tmp_path)
    harness.add("cap-old", write_file=False)

    result = harness.service.run_once()

    assert result.error_category is RetentionErrorCategory.MEDIA_MISSING
    assert result.deleted_count == 0
    assert harness.lifecycle() == {}
    assert all(
        observation.status == "failed" for observation in harness.observations()
    )


# --- unlink failure ---------------------------------------------------------


def test_an_unlink_failure_does_not_become_a_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refused deletion returns the media to ``PRESENT`` and records why."""
    harness = _harness(tmp_path)
    media = harness.add("cap-old")

    def _fail(path: Path) -> None:
        raise PermissionError("in use by another process")

    monkeypatch.setattr(service_module, "_unlink", _fail)

    result = harness.service.run_once()

    assert result.error_category is RetentionErrorCategory.FILESYSTEM_DELETE_FAILED
    assert result.deleted_count == 0
    assert media.exists()
    # The intent was cancelled, so the media is PRESENT again rather than stuck.
    assert harness.lifecycle() == {}
    [observation] = harness.observations()
    assert observation.status == "failed"
    assert (
        observation.payload["error_category"]
        == RetentionErrorCategory.FILESYSTEM_DELETE_FAILED.value
    )


def test_a_failed_cancellation_leaves_the_intent_durable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When recovery itself fails, the pending row stays for the next run.

    Guessing that the cancellation worked would put a lifecycle state in memory
    that outlives nothing -- the durable row is what the next run can act on.
    """
    harness = _harness(tmp_path)
    media = harness.add("cap-old")

    def _fail_unlink(path: Path) -> None:
        raise PermissionError("in use")

    def _fail_cancel(*args: Any, **kwargs: Any) -> bool:
        raise RetentionRepositoryError("the database is unavailable")

    monkeypatch.setattr(service_module, "_unlink", _fail_unlink)
    monkeypatch.setattr(
        harness.repository, "cancel_pending_delete", _fail_cancel
    )

    result = harness.service.run_once()

    assert result.error_category is RetentionErrorCategory.FILESYSTEM_DELETE_FAILED
    assert media.exists()
    assert harness.lifecycle() == {"cap-old": "pending_delete"}


# --- stage C failure and recovery -------------------------------------------


def test_a_finalisation_failure_leaves_the_intent_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crash after unlink is exactly what the durable intent is for."""
    harness = _harness(tmp_path)
    media = harness.add("cap-old")

    def _fail(*args: Any, **kwargs: Any) -> bool:
        raise RetentionRepositoryError("the database went away")

    monkeypatch.setattr(harness.repository, "finalize_deletion", _fail)

    result = harness.service.run_once()

    assert result.error_category is RetentionErrorCategory.FINALIZATION_FAILED
    assert result.deleted_count == 0
    assert not media.exists()
    assert harness.lifecycle() == {"cap-old": "pending_delete"}


def test_a_finalisation_that_matches_no_intent_is_not_a_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A finalisation that moved no row is a failure, not a quiet success.

    This is the *returns-False* path rather than the raising one: the conditional
    ``UPDATE`` matched nothing, so no success observation was written, and
    counting the capture as reclaimed would report a deletion that nothing
    recorded.
    """
    harness = _harness(tmp_path)
    media = harness.add("cap-old")
    monkeypatch.setattr(
        harness.repository, "finalize_deletion", lambda *a, **k: False
    )

    result = harness.service.run_once()

    assert result.error_category is RetentionErrorCategory.FINALIZATION_FAILED
    assert result.deleted_count == 0
    assert result.bytes_reclaimed == 0
    assert not media.exists()
    assert harness.state.snapshot().total_captures_deleted == 0


def test_the_next_run_recovers_an_interrupted_deletion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pending intent proves the removal was authorised; finish the job."""
    harness = _harness(tmp_path)
    harness.add("cap-old")
    failing = {"active": True}
    original = harness.repository.finalize_deletion

    def _maybe_fail(*args: Any, **kwargs: Any) -> bool:
        if failing["active"]:
            raise RetentionRepositoryError("the database went away")
        return original(*args, **kwargs)

    monkeypatch.setattr(harness.repository, "finalize_deletion", _maybe_fail)
    harness.service.run_once()
    assert harness.lifecycle() == {"cap-old": "pending_delete"}

    failing["active"] = False
    result = harness.service.run_once()

    assert result.error_category is None
    assert (result.deleted_count, result.recovered_count) == (1, 1)
    assert harness.lifecycle() == {"cap-old": "deleted"}


def test_recovering_a_missing_file_finalises_exactly_one_success(
    tmp_path: Path,
) -> None:
    """Pending + already gone is a completed deletion awaiting its record."""
    harness = _harness(tmp_path)
    media = harness.add("cap-old")
    harness.repository.claim_pending_delete("cap-old", RetentionReason.AGE)
    media.unlink()

    result = harness.service.run_once()

    assert result.error_category is None
    assert (result.deleted_count, result.recovered_count) == (1, 1)
    assert harness.lifecycle() == {"cap-old": "deleted"}
    successes = [
        observation
        for observation in harness.observations()
        if observation.status == SUCCESS_STATUS
    ]
    assert len(successes) == 1
    assert successes[0].payload["recovered_pending"] is True


def test_recovering_a_still_present_file_completes_the_deletion(
    tmp_path: Path,
) -> None:
    """Pending + still there is finished, after the same full validation."""
    harness = _harness(tmp_path)
    media = harness.add("cap-old")
    harness.repository.claim_pending_delete("cap-old", RetentionReason.AGE)

    result = harness.service.run_once()

    assert result.error_category is None
    assert result.recovered_count == 1
    assert not media.exists()
    assert harness.lifecycle() == {"cap-old": "deleted"}


def test_recovery_ignores_the_current_minimum_keep_count(
    tmp_path: Path,
) -> None:
    """A durable intent was authorised under an earlier policy, and stands.

    Re-deciding it now could only leave a file that may already be gone
    reported as an unexplained inconsistency forever.
    """
    harness = _harness(tmp_path, minimum_keep_count=100, max_age_days=3650)
    media = harness.add("cap-recent", days_old=0.5)
    harness.repository.claim_pending_delete("cap-recent", RetentionReason.AGE)

    result = harness.service.run_once()

    assert result.recovered_count == 1
    assert not media.exists()


def test_an_unsafe_pending_intent_is_not_touched(tmp_path: Path) -> None:
    """An unverifiable pending intent is left standing for a human."""
    harness = _harness(tmp_path)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    victim = outside / "cap-old.jpg"
    victim.write_bytes(PAYLOAD)
    harness.add("cap-old", absolute_path=str(victim), write_file=False)
    harness.repository.claim_pending_delete("cap-old", RetentionReason.AGE)

    result = harness.service.run_once()

    assert result.error_category is RetentionErrorCategory.UNSAFE_PATH
    assert result.deleted_count == 0
    assert victim.exists()
    assert harness.lifecycle() == {"cap-old": "pending_delete"}


def test_a_pending_intent_whose_file_changed_size_is_not_touched(
    tmp_path: Path,
) -> None:
    """A file that no longer matches its record is not the file authorised."""
    harness = _harness(tmp_path)
    media = harness.add("cap-old")
    harness.repository.claim_pending_delete("cap-old", RetentionReason.AGE)
    media.write_bytes(PAYLOAD + b"more")

    result = harness.service.run_once()

    assert result.error_category is RetentionErrorCategory.SIZE_MISMATCH
    assert media.exists()
    assert harness.lifecycle() == {"cap-old": "pending_delete"}


def test_pending_intents_are_recovered_before_new_candidates(
    tmp_path: Path,
) -> None:
    """Interrupted work is settled before the run spends its budget elsewhere."""
    harness = _harness(tmp_path, max_deletions_per_run=1)
    pending_media = harness.add("cap-pending", days_old=100)
    fresh_media = harness.add("cap-fresh", days_old=900)
    harness.add("cap-keep", days_old=1)
    harness.repository.claim_pending_delete("cap-pending", RetentionReason.AGE)

    result = harness.service.run_once()

    assert result.recovered_count == 1
    assert not pending_media.exists()
    # The per-run bound applies to newly selected work only, so the one fresh
    # candidate this run was allowed to choose was also deleted.
    assert not fresh_media.exists()
    assert result.deleted_count == 2


# --- stop on the first failure ----------------------------------------------


def test_a_run_stops_at_the_first_destructive_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deletions already made stand; unattempted candidates are left alone.

    The second capture's media is missing, which stops the run. The first must
    remain deleted and the third must remain entirely untouched -- no lifecycle
    row, no observation, no missing file.
    """
    harness = _harness(tmp_path)
    first = harness.add("cap-1", days_old=903)
    harness.add("cap-2", days_old=902, write_file=False)
    third = harness.add("cap-3", days_old=901)

    result = harness.service.run_once()

    assert result.error_category is RetentionErrorCategory.MEDIA_MISSING
    assert result.deleted_count == 1
    assert not first.exists()
    assert third.exists()
    assert harness.lifecycle() == {"cap-1": "deleted"}


def test_a_stopped_run_still_reports_what_it_reclaimed(
    tmp_path: Path,
) -> None:
    """Stopping on a failure does not un-delete what was already reclaimed."""
    harness = _harness(tmp_path)
    harness.add("cap-1", days_old=903)
    harness.add("cap-2", days_old=902, write_file=False)

    harness.service.run_once()

    snapshot = harness.state.snapshot()
    assert snapshot.state is RetentionState.ERROR
    assert snapshot.total_captures_deleted == 1
    assert snapshot.total_bytes_reclaimed == len(PAYLOAD)
    assert snapshot.last_error is not None


def test_a_catalogue_that_cannot_be_parsed_stops_the_run(
    tmp_path: Path,
) -> None:
    """No deletion happens once catalogue parsing has become untrustworthy."""
    harness = _harness(tmp_path)
    media = harness.add("cap-old")
    with database_connection(harness.database_path) as connection:
        connection.execute("UPDATE captures SET extra_metadata = 'not json'")

    result = harness.service.run_once()

    assert result.error_category is RetentionErrorCategory.CATALOGUE_INVALID
    assert result.deleted_count == 0
    assert media.exists()
    assert harness.lifecycle() == {}


# --- error message hygiene --------------------------------------------------


def test_no_public_error_message_carries_a_path_or_exception_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The published failure is a fixed sentence, not what went wrong."""
    harness = _harness(tmp_path)
    harness.add("cap-old")
    secret = "C:/secret/location/mgo.db"

    def _fail(path: Path) -> None:
        raise PermissionError(f"cannot remove {secret}")

    monkeypatch.setattr(service_module, "_unlink", _fail)

    result = harness.service.run_once()
    message = result.error_message or ""

    assert secret not in message
    assert str(harness.root) not in message
    assert "PermissionError" not in message
    assert message == (
        "A capture's media could not be removed."
    )
    assert harness.state.snapshot().last_error == message


# --- the dry run ------------------------------------------------------------


def test_a_dry_run_mutates_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A preview creates no lifecycle row, no observation and deletes no file."""

    def _explode(path: Path) -> None:
        raise AssertionError("a dry run attempted to unlink a file")

    monkeypatch.setattr(service_module, "_unlink", _explode)
    harness = _harness(tmp_path)
    media = harness.add("cap-old")

    plan = harness.service.dry_run()

    assert [candidate.capture_id for candidate in plan.candidates] == ["cap-old"]
    assert media.exists()
    assert harness.lifecycle() == {}
    assert harness.observations() == []
    assert harness.capture_ids() == {"cap-old", PROTECTED_FILLER}


def test_a_dry_run_moves_no_counter(tmp_path: Path) -> None:
    """Previewing a policy is not a run and is never counted as one."""
    harness = _harness(tmp_path)
    harness.add("cap-old")

    before = harness.state.snapshot()
    harness.service.dry_run()
    harness.service.dry_run()

    assert harness.state.snapshot() == before


def test_a_dry_run_and_the_real_run_agree(tmp_path: Path) -> None:
    """The preview is the same decision the destructive run executes."""
    harness = _harness(tmp_path, max_deletions_per_run=2)
    for index in range(5):
        harness.add(f"cap-{index}", days_old=900 - index)

    plan = harness.service.dry_run()
    result = harness.service.run_once()

    assert result.deleted_count == len(plan.candidates)
    assert result.bytes_reclaimed == plan.projected_bytes_reclaimed
    assert result.more_work_remains == plan.more_work_remains


def test_a_dry_run_is_available_while_retention_is_disabled(
    tmp_path: Path,
) -> None:
    """Previewing is how an operator decides whether to enable retention."""
    harness = _harness(tmp_path, enabled=False)
    media = harness.add("cap-old")

    plan = harness.service.dry_run()

    assert [candidate.capture_id for candidate in plan.candidates] == ["cap-old"]
    assert media.exists()
    assert harness.lifecycle() == {}


# --- the disabled gate ------------------------------------------------------


def test_a_disabled_run_mutates_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Disabled means no filesystem mutation and no database mutation."""

    def _explode(path: Path) -> None:
        raise AssertionError("disabled retention attempted to unlink a file")

    monkeypatch.setattr(service_module, "_unlink", _explode)
    harness = _harness(tmp_path, enabled=False)
    media = harness.add("cap-old")

    result = harness.service.run_once()

    assert result.executed is False
    assert result.enabled is False
    assert result.error_category is None
    assert result.deleted_count == 0
    assert media.exists()
    assert harness.lifecycle() == {}
    assert harness.observations() == []


def test_disabled_retention_does_not_recover_a_pending_intent(
    tmp_path: Path,
) -> None:
    """Recovery is a destructive mutation and is gated with everything else.

    An interrupted deletion stays interrupted until someone turns retention on;
    that is the correct reading of "disabled", and the durable intent is exactly
    what makes waiting safe.
    """
    harness = _harness(tmp_path, enabled=False)
    media = harness.add("cap-old")
    harness.repository.claim_pending_delete("cap-old", RetentionReason.AGE)

    result = harness.service.run_once()

    assert result.executed is False
    assert result.recovered_count == 0
    assert media.exists()
    assert harness.lifecycle() == {"cap-old": "pending_delete"}


def test_a_disabled_runtime_state_stays_disabled(tmp_path: Path) -> None:
    """A disabled subsystem never reports running, idle or a counter."""
    harness = _harness(tmp_path, enabled=False)
    harness.add("cap-old")

    harness.service.run_once()

    snapshot = harness.state.snapshot()
    assert snapshot.state is RetentionState.DISABLED
    assert snapshot.total_runs == 0
    assert snapshot.last_run_at is None


# --- concurrency ------------------------------------------------------------


def test_an_overlapping_run_is_refused_as_busy(tmp_path: Path) -> None:
    """Two destructive runs in one process cannot execute together.

    The second run is attempted from inside the first, at the exact moment the
    first is between its validation and its unlink -- proven by a barrier rather
    than by a sleep.
    """
    harness = _harness(tmp_path)
    media = harness.add("cap-old")
    observed: dict[str, Any] = {}
    real_unlink = service_module._unlink

    def _reentrant(path: Path) -> None:
        observed["result"] = harness.service.run_once()
        real_unlink(path)

    original = service_module._unlink
    try:
        service_module._unlink = _reentrant  # type: ignore[assignment]
        outer = harness.service.run_once()
    finally:
        service_module._unlink = original  # type: ignore[assignment]

    inner = observed["result"]
    assert inner.executed is False
    assert inner.error_category is RetentionErrorCategory.BUSY
    assert inner.deleted_count == 0
    assert outer.deleted_count == 1
    assert not media.exists()


def test_a_busy_run_deletes_nothing_and_records_nothing(
    tmp_path: Path,
) -> None:
    """A refused overlapping run leaves no trace at all.

    The lock is held by a second thread parked on an event, so the refusal is
    deterministic and does not depend on timing.
    """
    harness = _harness(tmp_path)
    media = harness.add("cap-old")
    holding = threading.Event()
    release = threading.Event()

    def _hold() -> None:
        with harness.service._run_lock:
            holding.set()
            release.wait(timeout=10)

    holder = threading.Thread(target=_hold, name="retention-lock-holder")
    holder.start()
    try:
        assert holding.wait(timeout=10)
        result = harness.service.run_once()
    finally:
        release.set()
        holder.join(timeout=10)

    assert result.error_category is RetentionErrorCategory.BUSY
    assert media.exists()
    assert harness.lifecycle() == {}
    assert harness.observations() == []
    assert harness.state.snapshot().total_runs == 0


def test_a_lost_claim_race_stops_the_run_without_deleting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If another execution claimed the capture first, nothing is deleted.

    Continuing would mean acting on a plan the catalogue has already moved past.
    """
    harness = _harness(tmp_path)
    media = harness.add("cap-old")
    monkeypatch.setattr(
        harness.repository, "claim_pending_delete", lambda *a, **k: False
    )

    result = harness.service.run_once()

    assert result.error_category is RetentionErrorCategory.DATABASE_TRANSITION_FAILED
    assert result.deleted_count == 0
    assert media.exists()


def test_a_duplicate_finalisation_cannot_create_two_success_observations(
    tmp_path: Path,
) -> None:
    """One deletion, one success row, however many finalisations are attempted."""
    harness = _harness(tmp_path)
    harness.add("cap-old")
    harness.repository.claim_pending_delete("cap-old", RetentionReason.AGE)

    fields = {
        "kind": OBSERVATION_KIND,
        "source": OBSERVATION_SOURCE,
        "status": SUCCESS_STATUS,
        "summary": "Capture media removed by retention policy",
        "payload": {"capture_id": "cap-old"},
        "correlation_id": "cap-old",
    }
    outcomes = [
        harness.repository.finalize_deletion(
            "cap-old", observation_fields=dict(fields)
        )
        for _ in range(4)
    ]

    assert outcomes == [True, False, False, False]
    successes = [
        observation
        for observation in harness.observations()
        if observation.status == SUCCESS_STATUS
    ]
    assert len(successes) == 1


# --- the per-run destructive bound, end to end ------------------------------


def test_the_run_never_deletes_more_than_its_per_run_bound(
    tmp_path: Path,
) -> None:
    """The hard bound holds against a real catalogue and a real filesystem."""
    harness = _harness(tmp_path, max_deletions_per_run=3)
    media = [harness.add(f"cap-{index}", days_old=900 - index) for index in range(10)]

    result = harness.service.run_once()

    assert result.deleted_count == 3
    assert result.more_work_remains is True
    assert sum(1 for path in media if not path.exists()) == 3


def test_repeated_runs_drain_the_backlog_three_at_a_time(
    tmp_path: Path,
) -> None:
    """Remaining work is real work, and the next run picks it up."""
    harness = _harness(tmp_path, max_deletions_per_run=3, minimum_keep_count=1)
    for index in range(7):
        harness.add(f"cap-{index}", days_old=900 - index)

    counts = [harness.service.run_once().deleted_count for _ in range(4)]

    assert counts == [3, 3, 1, 0]
    # Only the protected newest managed capture is left on disk.
    assert [path.name for path in harness.root.iterdir()] == [
        f"{PROTECTED_FILLER}.jpg"
    ]


# --- what retention must never do -------------------------------------------


def test_the_service_never_touches_the_camera_or_preview(tmp_path: Path) -> None:
    """Retention takes no camera dependency at all.

    Asserted structurally: the module imports nothing from the camera, preview,
    capture or event-capture packages, so it cannot acquire the coordinator,
    stop a preview or run a capture even by accident.
    """
    source = Path(service_module.__file__).read_text(encoding="utf-8")

    for forbidden in (
        "mgo.camera",
        "mgo.captures",
        "mgo.event_capture",
        "mgo.motion",
        "CameraCoordinator",
        "CaptureService",
        "CaptureWorkflow",
    ):
        assert forbidden not in source


def test_the_service_never_deletes_recursively(tmp_path: Path) -> None:
    """No recursive removal call exists anywhere in the retention package."""
    package = Path(service_module.__file__).parent

    for module in package.glob("*.py"):
        source = module.read_text(encoding="utf-8")
        assert "rmtree" not in source
        assert "shutil" not in source
        assert "rmdir" not in source


# --- the execution boundary (Task 14.1 correction round) --------------------
#
# A destructive subsystem that lets an unexpected exception escape leaves its
# caller with no result, its runtime state stuck in ``running``, and its
# lifetime counters missing the deletions it had already completed. These tests
# exist because that is exactly what the first implementation did.


def _explode_unexpectedly(message: str = "unexpected internal failure"):
    """Return an unlink seam that raises an ordinary, unanticipated error."""

    def _seam(path: Path) -> None:
        raise RuntimeError(message)

    return _seam


def test_an_unexpected_exception_becomes_a_bounded_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An ordinary unexpected error is reduced to ``UNEXPECTED``, not raised.

    ``RuntimeError`` is not in any of the handled domains, so before the
    execution boundary existed this escaped ``run_once`` entirely.
    """
    harness = _harness(tmp_path)
    harness.add("cap-old")
    monkeypatch.setattr(service_module, "_unlink", _explode_unexpectedly())

    result = harness.service.run_once()

    assert result.executed is True
    assert result.error_category is RetentionErrorCategory.UNEXPECTED
    assert result.deleted_count == 0
    # The point is that ``run_once`` returned at all: before the execution
    # boundary existed, this line was never reached.
    assert result.enabled is True


def test_an_unexpected_exception_leaves_the_state_in_error_not_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The holder must never be left reporting a run that has finished.

    ``running`` is a claim that a destructive run is executing *now*. Leaving it
    there after the run has ended tells an operator a deletion is in flight that
    is not, and no later run corrects it because none is scheduled.
    """
    harness = _harness(tmp_path)
    harness.add("cap-old")
    monkeypatch.setattr(service_module, "_unlink", _explode_unexpectedly())

    harness.service.run_once()

    snapshot = harness.state.snapshot()
    assert snapshot.state is RetentionState.ERROR
    assert snapshot.state is not RetentionState.RUNNING
    assert snapshot.total_runs == 1


def test_an_unexpected_exception_publishes_only_the_fixed_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exception's own text never reaches the public contract."""
    harness = _harness(tmp_path)
    harness.add("cap-old")
    secret = "C:/secret/location/mgo.db"
    monkeypatch.setattr(
        service_module,
        "_unlink",
        _explode_unexpectedly(f"cannot remove {secret}"),
    )

    result = harness.service.run_once()

    message = result.error_message or ""
    assert message == "The retention run failed unexpectedly."
    assert secret not in message
    assert "RuntimeError" not in message
    assert str(harness.root) not in message
    assert harness.state.snapshot().last_error == message


def test_an_unexpected_exception_releases_the_run_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stranded lock would make every later run report BUSY forever."""
    harness = _harness(tmp_path)
    harness.add("cap-old")
    monkeypatch.setattr(service_module, "_unlink", _explode_unexpectedly())

    harness.service.run_once()

    assert harness.service._run_lock.locked() is False


def test_a_later_run_after_an_unexpected_failure_is_not_busy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The subsystem recovers: the next run executes normally and succeeds."""
    harness = _harness(tmp_path)
    media = harness.add("cap-old")
    monkeypatch.setattr(service_module, "_unlink", _explode_unexpectedly())
    harness.service.run_once()

    monkeypatch.undo()
    result = harness.service.run_once()

    assert result.error_category is not RetentionErrorCategory.BUSY
    assert result.error_category is None
    assert result.deleted_count == 1
    assert not media.exists()
    assert harness.state.snapshot().state is RetentionState.IDLE


def test_an_unexpected_failure_preserves_an_earlier_completed_deletion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deletion that already completed is still counted, and still gone.

    The boundary sits where the partial tally is in hand precisely so this
    stays true: the first capture really was reclaimed, and a run that later
    failed must not report otherwise.
    """
    harness = _harness(tmp_path)
    first = harness.add("cap-1", days_old=903)
    second = harness.add("cap-2", days_old=902)
    third = harness.add("cap-3", days_old=901)
    real_unlink = service_module._unlink
    seen: list[str] = []

    def _fail_on_the_second(path: Path) -> None:
        seen.append(Path(path).name)
        if len(seen) == 1:
            real_unlink(path)
            return
        raise RuntimeError("unexpected internal failure")

    monkeypatch.setattr(service_module, "_unlink", _fail_on_the_second)

    result = harness.service.run_once()

    assert result.error_category is RetentionErrorCategory.UNEXPECTED
    assert result.deleted_count == 1
    assert result.bytes_reclaimed == len(PAYLOAD)
    assert not first.exists()
    # The run stopped at the second candidate, so the third was never attempted.
    assert seen == ["cap-1.jpg", "cap-2.jpg"]
    assert second.exists()
    assert third.exists()
    snapshot = harness.state.snapshot()
    assert snapshot.total_captures_deleted == 1
    assert snapshot.total_bytes_reclaimed == len(PAYLOAD)


def test_an_unexpected_failure_after_unlink_leaves_the_intent_durable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unexpected error between unlink and finalisation stays recoverable."""
    harness = _harness(tmp_path)
    media = harness.add("cap-old")

    def _explode(*args: Any, **kwargs: Any) -> bool:
        raise RuntimeError("unexpected internal failure")

    monkeypatch.setattr(harness.repository, "finalize_deletion", _explode)

    result = harness.service.run_once()

    assert result.error_category is RetentionErrorCategory.UNEXPECTED
    assert not media.exists()
    assert harness.lifecycle() == {"cap-old": "pending_delete"}


def test_process_control_exceptions_are_not_converted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``BaseException`` still propagates; only ``Exception`` is converted.

    Catching ``KeyboardInterrupt`` or ``SystemExit`` to make a status endpoint
    look tidy would suppress a shutdown, so the boundary deliberately does not.
    """

    def _interrupt(path: Path) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(service_module, "_unlink", _interrupt)
    harness = _harness(tmp_path)
    harness.add("cap-old")

    with pytest.raises(KeyboardInterrupt):
        harness.service.run_once()

    # The lock is still released, so the process can be shut down cleanly.
    assert harness.service._run_lock.locked() is False


# --- corrupt catalogue file sizes (Task 14.1 correction round) --------------
#
# SQLite column affinity is a conversion preference, not a constraint: the
# captures table is not STRICT, so a damaged row can hold text here. Before the
# correction that produced a bare ValueError out of catalogue decoding, which
# escaped the run and stranded the runtime state.


@pytest.mark.parametrize(
    ("label", "value"),
    [
        ("non-numeric text", "'not-a-number'"),
        ("empty text", "''"),
        ("a real", "12.5"),
        ("zero", "0"),
        ("negative", "-1"),
    ],
)
def test_a_corrupt_catalogue_filesize_stops_the_run(
    tmp_path: Path, label: str, value: str
) -> None:
    """An unusable catalogue size is ``catalogue_invalid``, and nothing is deleted.

    ``NULL`` is absent from this list on purpose: the ``captures`` schema
    declares ``filesize_bytes`` NOT NULL, so SQLite refuses it here. The decoder
    still rejects it, and that is proved in the repository suite against a table
    without the constraint -- the shape a hand-repaired database can present.

    Zero and negative matter as much as the unparseable forms: the capture
    service only ever catalogues a verified non-empty JPEG, so a non-positive
    size is a corrupt record. Before the correction it was silently accepted and
    then reported as a *size mismatch* against a real file -- a corrupt row
    wearing the costume of an ordinary safety refusal.
    """
    harness = _harness(tmp_path)
    media = harness.add("cap-old")
    with database_connection(harness.database_path) as connection:
        connection.execute(
            f"UPDATE captures SET filesize_bytes = {value} WHERE id = 'cap-old'"
        )

    result = harness.service.run_once()

    assert result.error_category is RetentionErrorCategory.CATALOGUE_INVALID
    assert result.deleted_count == 0
    assert media.exists()
    assert harness.lifecycle() == {}
    assert harness.state.snapshot().state is RetentionState.ERROR


def test_a_corrupt_catalogue_filesize_never_reaches_unlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Decoding fails before any filesystem call is made."""

    def _explode(path: Path) -> None:
        raise AssertionError("retention unlinked against an undecodable catalogue")

    monkeypatch.setattr(service_module, "_unlink", _explode)
    harness = _harness(tmp_path)
    harness.add("cap-old")
    with database_connection(harness.database_path) as connection:
        connection.execute("UPDATE captures SET filesize_bytes = 'bad'")

    assert (
        harness.service.run_once().error_category
        is RetentionErrorCategory.CATALOGUE_INVALID
    )


def test_a_valid_positive_filesize_is_still_accepted(tmp_path: Path) -> None:
    """The control: an ordinary catalogue size still works exactly as before."""
    harness = _harness(tmp_path)
    media = harness.add("cap-old")

    result = harness.service.run_once()

    assert result.error_category is None
    assert result.deleted_count == 1
    assert not media.exists()


# --- failure observations carry no filename (Task 14.1 correction round) ----


@pytest.mark.parametrize(
    "hostile",
    [
        "/secret/location/mgo.db",
        "C:/Windows/System32/config/SAM",
        "../../../etc/shadow",
        "/var/lib/garden-observatory/db/mgo.db",
    ],
)
def test_a_failure_observation_never_persists_an_untrusted_filename(
    tmp_path: Path, hostile: str
) -> None:
    """A rejected filename must not ride into the immutable timeline.

    The failure here *is* the boundary refusing this value, so writing it to an
    observation would persist exactly the string the safety contract exists to
    keep out. A success observation may carry a filename because success means
    the boundary already passed it; a failure has no such guarantee.
    """
    harness = _harness(tmp_path)
    harness.add(
        "cap-bad",
        filename=hostile,
        absolute_path=hostile,
        write_file=False,
    )

    result = harness.service.run_once()

    assert result.error_category is RetentionErrorCategory.UNSAFE_PATH
    [observation] = harness.observations()
    rendered = json.dumps(observation.payload)
    assert hostile not in rendered
    assert "filename" not in observation.payload
    assert "/" not in rendered
    assert "\\" not in rendered


def test_a_failure_observation_keeps_its_category_and_correlation(
    tmp_path: Path,
) -> None:
    """Removing the filename removed nothing an operator needs.

    The capture id identifies the record completely, and it is also the
    correlation that ties the failure to the catalogue row.
    """
    harness = _harness(tmp_path)
    harness.add("cap-old", catalogue_size=len(PAYLOAD) + 1)

    harness.service.run_once()

    [observation] = harness.observations()
    assert observation.payload == {
        "capture_id": "cap-old",
        "error_category": RetentionErrorCategory.SIZE_MISMATCH.value,
        "policy_reason": RetentionReason.AGE.value,
    }
    assert observation.correlation_id == "cap-old"


def test_the_success_observation_still_carries_its_filename(
    tmp_path: Path,
) -> None:
    """The control: success is unchanged, because success passed the boundary."""
    harness = _harness(tmp_path)
    harness.add("cap-old")

    harness.service.run_once()

    [observation] = harness.observations()
    assert observation.status == SUCCESS_STATUS
    assert observation.payload["filename"] == "cap-old.jpg"
    assert set(observation.payload) == {
        "capture_id",
        "filename",
        "filesize_bytes",
        "policy_reason",
        "captured_at",
        "recovered_pending",
    }
