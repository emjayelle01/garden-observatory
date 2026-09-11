"""Tests for the recognition job queue, its leases and the single-job runner.

Every database and capture root is temporary. Recognition itself is the
deterministic fake adapter, which never opens the media, so nothing here
decodes an image, loads a model or touches the network. Time is an injected
clock that only moves when a test moves it, so every lease expiry and retry
boundary is exact rather than a sleep.
"""

from __future__ import annotations

import ast
import errno
import json
import os
import sqlite3
import stat
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

import mgo.recognition.repository as repository_module
from mgo.core.database import apply_migrations, database_connection, utc_now_iso
from mgo.core.observations import list_observations
from mgo.operations.locking import OperationLock
from mgo.recognition.adapter import open_media
from mgo.recognition.fake_adapter import (
    FakeRecognitionAdapter,
    SimulatedAdapterCrash,
    synthetic_outcome,
)
from mgo.recognition.models import (
    ERROR_DISPOSITION,
    RecognitionAdapterError,
    RecognitionConfigurationError,
    RecognitionErrorCategory,
    RecognitionJob,
    RecognitionJobState,
    RecognitionOutcome,
    RecognitionRepositoryError,
    RecognitionRequest,
    RecognitionResult,
    RetryPolicy,
)
from mgo.recognition.reconciler import RecognitionReconciler
from mgo.recognition.repository import RecognitionRepository, format_timestamp
from mgo.recognition.runner import RunReport, RunStatus, run_one_job

NOW = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)
WATERMARK = datetime(2026, 9, 1, tzinfo=UTC)
PIPELINE = "fake-0"
PAYLOAD = b"jpeg-bytes-stand-in"
LEASE = timedelta(minutes=10)
RETRY = RetryPolicy(base_delay=timedelta(minutes=1), max_delay=timedelta(hours=1))
SOURCE_DIRECTORY = Path(__file__).resolve().parents[1] / "src" / "mgo" / "recognition"


class _Clock:
    """A clock that moves only when told to."""

    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


class _Interrupted(BaseException):
    """A worker killed mid-attempt: not an ``Exception``, so nothing catches it."""


class _Harness:
    """A temporary database, capture root, clock and fake pipeline."""

    def __init__(self, tmp_path: Path, *, max_attempts: int = 3) -> None:
        self.tmp_path = tmp_path
        self.root = tmp_path / "captures"
        self.root.mkdir()
        self.database_path = tmp_path / "mgo.db"
        apply_migrations(self.database_path)
        self.clock = _Clock(NOW)
        self.max_attempts = max_attempts
        self.repository = self.worker_repository()

    def worker_repository(
        self, busy_timeout_seconds: float = 5.0
    ) -> RecognitionRepository:
        return RecognitionRepository(
            self.database_path,
            clock=self.clock,
            busy_timeout_seconds=busy_timeout_seconds,
        )

    def add(self, identifier: str, *, payload: bytes = PAYLOAD) -> Path:
        media = self.root / f"{identifier}.jpg"
        media.write_bytes(payload)
        stamp = (NOW - timedelta(hours=1)).isoformat()
        with database_connection(self.database_path) as connection:
            connection.execute(
                """
                INSERT INTO captures (
                    id, filename, absolute_path, captured_at_utc, width, height,
                    filesize_bytes, camera_backend, created_at_utc, extra_metadata
                )
                VALUES (?, ?, ?, ?, 4608, 2592, ?, 'picamera2', ?, ?)
                """,
                (
                    identifier,
                    media.name,
                    str(media),
                    stamp,
                    len(payload),
                    stamp,
                    json.dumps({"origin": "motion"}),
                ),
            )
        return media

    def enqueue(self, *identifiers: str, pipeline_version: str = PIPELINE) -> None:
        for identifier in identifiers:
            if not (self.root / f"{identifier}.jpg").exists():
                self.add(identifier)
        report = RecognitionReconciler(
            self.repository,
            pipeline_version=pipeline_version,
            capture_directory=self.root,
            enrolment_watermark=WATERMARK,
            max_attempts=self.max_attempts,
        ).reconcile()
        assert report.created >= 1

    def add_lifecycle(self, capture_id: str, state: str = "pending_delete") -> None:
        deleted = utc_now_iso() if state == "deleted" else None
        with database_connection(self.database_path) as connection:
            connection.execute(
                "INSERT INTO capture_media_lifecycle VALUES (?, ?, ?, ?, 'age')",
                (capture_id, state, utc_now_iso(), deleted),
            )

    def claim(
        self,
        repository: RecognitionRepository | None = None,
        *,
        worker_id: str = "worker-a",
        pipeline_version: str = PIPELINE,
    ) -> RecognitionJob | None:
        return (repository or self.repository).claim_next(
            pipeline_version=pipeline_version,
            worker_id=worker_id,
            lease_duration=LEASE,
        )

    def run(
        self,
        adapter: FakeRecognitionAdapter | None = None,
        *,
        repository: RecognitionRepository | None = None,
        worker_id: str = "worker-a",
    ) -> RunReport:
        return run_one_job(
            repository or self.repository,
            adapter or FakeRecognitionAdapter(),
            capture_directory=self.root,
            worker_id=worker_id,
            lease_duration=LEASE,
            retry_policy=RETRY,
        )

    def job(
        self, capture_id: str = "cap-1", pipeline_version: str = PIPELINE
    ) -> dict[str, Any]:
        with database_connection(self.database_path) as connection:
            row = connection.execute(
                "SELECT * FROM recognition_jobs "
                "WHERE capture_id = ? AND pipeline_version = ?",
                (capture_id, pipeline_version),
            ).fetchone()
        assert row is not None
        return dict(row)

    def results(self) -> list[dict[str, Any]]:
        with database_connection(self.database_path) as connection:
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT r.*, j.capture_id, j.pipeline_version "
                    "FROM recognition_results AS r "
                    "JOIN recognition_jobs AS j ON j.id = r.job_id "
                    "ORDER BY j.capture_id, j.pipeline_version"
                )
            ]

    def catalogue(self) -> tuple[list[tuple[Any, ...]], ...]:
        with database_connection(self.database_path) as connection:
            return tuple(
                [
                    tuple(row)
                    for row in connection.execute(f"SELECT * FROM {table} ORDER BY 1")
                ]
                for table in (
                    "captures",
                    "capture_media_lifecycle",
                    "observations",
                    "schema_migrations",
                )
            )


@pytest.fixture
def harness(tmp_path: Path) -> _Harness:
    return _Harness(tmp_path)


# --- claiming -----------------------------------------------------------------


def test_one_pending_job_is_claimed_exactly_once(harness: _Harness) -> None:
    harness.enqueue("cap-1")

    job = harness.claim()
    again = harness.claim(worker_id="worker-b")

    assert job is not None
    assert again is None
    assert job.capture_id == "cap-1"
    assert job.attempt_count == 1
    assert job.lease_expires_at == NOW + LEASE
    row = harness.job()
    assert row["state"] == RecognitionJobState.RUNNING.value
    assert row["attempt_count"] == 1
    assert row["lease_owner"] == job.lease_owner
    assert row["lease_expires_at"] == format_timestamp(NOW + LEASE)
    assert row["started_at"] == format_timestamp(NOW)


def test_an_empty_queue_claims_nothing(harness: _Harness) -> None:
    assert harness.claim() is None


def test_each_claim_gets_a_lease_owner_unique_to_it(harness: _Harness) -> None:
    """Even the same worker id reclaiming its own expired job is a new claim."""
    harness.enqueue("cap-1")
    first = harness.claim()
    harness.clock.advance(LEASE)
    second = harness.claim()

    assert first is not None and second is not None
    assert first.lease_owner != second.lease_owner
    assert first.lease_owner.startswith("worker-a:")
    assert second.lease_owner.startswith("worker-a:")


def test_claims_follow_a_deterministic_order(harness: _Harness) -> None:
    """Due time first, then age, then id -- never incidental row order."""
    harness.enqueue("cap-c", "cap-a")  # one instant: job id order decides
    harness.clock.advance(timedelta(seconds=1))
    harness.enqueue("cap-b")  # due one second later

    claimed = []
    for _ in range(3):
        job = harness.claim()
        assert job is not None
        claimed.append(job)

    same_instant = sorted((harness.job("cap-a")["id"], harness.job("cap-c")["id"]))
    assert [job.id for job in claimed] == [*same_instant, harness.job("cap-b")["id"]]


def test_a_retry_that_became_due_earlier_is_claimed_before_newer_work(
    harness: _Harness,
) -> None:
    harness.enqueue("cap-1")
    job = harness.claim()
    assert job is not None
    harness.repository.record_failure(
        job, RecognitionErrorCategory.TIMEOUT, retry_policy=RETRY
    )
    harness.clock.advance(timedelta(minutes=5))
    harness.enqueue("cap-2")  # created after cap-1's retry came due

    next_job = harness.claim()

    assert next_job is not None
    assert next_job.capture_id == "cap-1"


def test_a_retry_is_not_claimed_before_its_time(harness: _Harness) -> None:
    harness.enqueue("cap-1")
    job = harness.claim()
    assert job is not None
    harness.repository.record_failure(
        job, RecognitionErrorCategory.TIMEOUT, retry_policy=RETRY
    )
    assert harness.job()["next_attempt_at"] == format_timestamp(
        NOW + timedelta(minutes=1)
    )

    harness.clock.advance(timedelta(minutes=1) - timedelta(microseconds=1))
    assert harness.claim() is None

    harness.clock.advance(timedelta(microseconds=1))
    retried = harness.claim()
    assert retried is not None
    assert retried.attempt_count == 2


def test_a_valid_running_lease_cannot_be_stolen(harness: _Harness) -> None:
    harness.enqueue("cap-1")
    assert harness.claim() is not None

    harness.clock.advance(LEASE - timedelta(microseconds=1))

    assert harness.claim(worker_id="worker-b") is None
    assert harness.job()["attempt_count"] == 1


def test_an_expired_lease_is_recovered_by_the_next_claim(harness: _Harness) -> None:
    harness.enqueue("cap-1")
    first = harness.claim()
    assert first is not None

    harness.clock.advance(LEASE)  # expiry is inclusive
    second = harness.claim(worker_id="worker-b")

    assert second is not None
    assert second.id == first.id
    assert second.attempt_count == 2
    assert second.lease_owner.startswith("worker-b:")
    assert harness.job()["lease_owner"] == second.lease_owner


def test_concurrent_claims_cannot_both_win_one_job(harness: _Harness) -> None:
    harness.enqueue("cap-1")
    barrier = threading.Barrier(6)
    claims: list[RecognitionJob | None] = []
    errors: list[BaseException] = []

    def _claim(index: int) -> None:
        repository = harness.worker_repository()
        barrier.wait()
        try:
            claims.append(harness.claim(repository, worker_id=f"worker-{index}"))
        except BaseException as exc:  # recorded and asserted on below
            errors.append(exc)

    threads = [threading.Thread(target=_claim, args=(index,)) for index in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert errors == []
    winners = [claim for claim in claims if claim is not None]
    assert len(claims) == 6
    assert len(winners) == 1
    assert harness.job()["lease_owner"] == winners[0].lease_owner


def test_a_competing_writer_is_held_off_between_select_and_update(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The claim reserves the database before it reads: nobody can slip in."""
    harness.enqueue("cap-1")
    real_select = repository_module._select_claimable
    competitor = harness.worker_repository(busy_timeout_seconds=0.2)
    outcomes: list[object] = []

    def _competitor_tries_to_claim(*args: Any, **kwargs: Any) -> Any:
        row = real_select(*args, **kwargs)
        monkeypatch.setattr(repository_module, "_select_claimable", real_select)
        try:
            outcomes.append(harness.claim(competitor, worker_id="worker-b"))
        except RecognitionRepositoryError as exc:
            outcomes.append(exc)
        return row

    monkeypatch.setattr(
        repository_module, "_select_claimable", _competitor_tries_to_claim
    )

    job = harness.claim()

    assert job is not None
    assert len(outcomes) == 1
    assert isinstance(outcomes[0], RecognitionRepositoryError)
    assert harness.job()["lease_owner"] == job.lease_owner
    assert harness.job()["attempt_count"] == 1


def test_claims_are_separated_by_pipeline_version(harness: _Harness) -> None:
    """The other version's job is due first, so only the filter can skip it."""
    harness.enqueue("cap-1", pipeline_version="fake-0")
    harness.clock.advance(timedelta(seconds=1))
    harness.enqueue("cap-1", pipeline_version="fake-1")

    job = harness.claim(pipeline_version="fake-1")

    assert job is not None
    assert job.pipeline_version == "fake-1"
    assert harness.job(pipeline_version="fake-0")["state"] == "pending"
    assert harness.claim(pipeline_version="fake-1") is None


# --- attempts, retries and exhaustion -----------------------------------------


def test_the_attempt_count_increments_once_per_claim(harness: _Harness) -> None:
    harness.enqueue("cap-1")
    attempts = []
    for _ in range(3):
        job = harness.claim()
        assert job is not None
        attempts.append((job.attempt_count, harness.job()["attempt_count"]))
        harness.repository.record_failure(
            job, RecognitionErrorCategory.TIMEOUT, retry_policy=RETRY
        )
        harness.clock.advance(timedelta(hours=1))

    assert attempts == [(1, 1), (2, 2), (3, 3)]


def test_retry_delays_back_off_to_the_cap() -> None:
    policy = RetryPolicy(
        base_delay=timedelta(minutes=1), max_delay=timedelta(minutes=5)
    )

    assert [policy.delay_after(attempt) for attempt in range(1, 6)] == [
        timedelta(minutes=1),
        timedelta(minutes=2),
        timedelta(minutes=4),
        timedelta(minutes=5),
        timedelta(minutes=5),
    ]


@pytest.mark.parametrize(
    "category",
    [
        RecognitionErrorCategory.TIMEOUT,
        RecognitionErrorCategory.RESOURCE_LIMIT,
        RecognitionErrorCategory.MODEL_UNAVAILABLE,
        RecognitionErrorCategory.UNEXPECTED,
    ],
)
def test_retryable_failures_become_terminal_when_attempts_run_out(
    tmp_path: Path, category: RecognitionErrorCategory
) -> None:
    harness = _Harness(tmp_path, max_attempts=2)
    harness.enqueue("cap-1")
    states = []
    for _ in range(2):
        job = harness.claim()
        assert job is not None
        states.append(
            harness.repository.record_failure(job, category, retry_policy=RETRY)
        )
        harness.clock.advance(timedelta(hours=1))

    assert states == [RecognitionJobState.PENDING, RecognitionJobState.FAILED]
    row = harness.job()
    assert row["state"] == "failed"
    assert row["error_category"] == category.value
    assert row["finished_at"] is not None
    assert harness.claim() is None


def test_an_expired_lease_on_the_final_attempt_ends_the_job(tmp_path: Path) -> None:
    """A job that kills its worker every time still runs out of attempts."""
    harness = _Harness(tmp_path, max_attempts=1)
    harness.enqueue("cap-1")
    assert harness.claim() is not None

    harness.clock.advance(LEASE)

    assert harness.claim(worker_id="worker-b") is None
    row = harness.job()
    assert row["state"] == "failed"
    assert row["error_category"] == "unexpected"
    assert row["finished_at"] == format_timestamp(NOW + LEASE)
    assert row["lease_owner"] is None
    assert row["attempt_count"] == 1


def test_an_unexpired_final_attempt_is_not_ended_early(tmp_path: Path) -> None:
    harness = _Harness(tmp_path, max_attempts=1)
    harness.enqueue("cap-1")
    assert harness.claim() is not None

    harness.clock.advance(LEASE - timedelta(microseconds=1))

    assert harness.claim(worker_id="worker-b") is None
    assert harness.job()["state"] == "running"


@pytest.mark.parametrize(
    ("category", "state"),
    [
        (RecognitionErrorCategory.MEDIA_MISSING, RecognitionJobState.SKIPPED),
        (RecognitionErrorCategory.UNSAFE_PATH, RecognitionJobState.FAILED),
        (RecognitionErrorCategory.SIZE_MISMATCH, RecognitionJobState.FAILED),
        (RecognitionErrorCategory.DECODE_ERROR, RecognitionJobState.FAILED),
        (RecognitionErrorCategory.TIMEOUT, None),
        (RecognitionErrorCategory.RESOURCE_LIMIT, None),
        (RecognitionErrorCategory.MODEL_UNAVAILABLE, None),
        (RecognitionErrorCategory.UNEXPECTED, None),
    ],
)
def test_the_error_disposition_is_exactly_the_documented_contract(
    category: RecognitionErrorCategory, state: RecognitionJobState | None
) -> None:
    assert ERROR_DISPOSITION[category] is state
    assert set(ERROR_DISPOSITION) == set(RecognitionErrorCategory)


# --- lease ownership ----------------------------------------------------------


def _result() -> RecognitionResult:
    return RecognitionResult(outcome=RecognitionOutcome.NO_BIRD)


def test_a_worker_that_lost_its_lease_cannot_complete_the_job(
    harness: _Harness,
) -> None:
    harness.enqueue("cap-1")
    stale = harness.claim()
    harness.clock.advance(LEASE)
    current = harness.claim(worker_id="worker-b")
    assert stale is not None and current is not None

    assert harness.repository.complete_success(stale, _result()) is False
    assert (
        harness.repository.record_failure(
            stale, RecognitionErrorCategory.DECODE_ERROR, retry_policy=RETRY
        )
        is None
    )
    assert harness.repository.renew_lease(stale, LEASE) is False

    row = harness.job()
    assert row["state"] == "running"
    assert row["lease_owner"] == current.lease_owner
    assert harness.results() == []


def test_a_stale_worker_cannot_overwrite_a_later_success(harness: _Harness) -> None:
    harness.enqueue("cap-1")
    stale = harness.claim()
    harness.clock.advance(LEASE)
    current = harness.claim(worker_id="worker-b")
    assert stale is not None and current is not None
    assert harness.repository.complete_success(
        current, RecognitionResult(outcome=RecognitionOutcome.SPECIES)
    )

    assert harness.repository.complete_success(stale, _result()) is False
    assert (
        harness.repository.record_failure(
            stale, RecognitionErrorCategory.TIMEOUT, retry_policy=RETRY
        )
        is None
    )

    [result] = harness.results()
    assert result["outcome"] == "species"
    assert harness.job()["state"] == "succeeded"


def test_a_worker_with_an_expired_but_unclaimed_lease_may_still_finish(
    harness: _Harness,
) -> None:
    """Nobody else holds the attempt, so its result is still the only one."""
    harness.enqueue("cap-1")
    job = harness.claim()
    assert job is not None
    harness.clock.advance(LEASE * 2)

    assert harness.repository.complete_success(job, _result()) is True
    assert harness.job()["state"] == "succeeded"


def test_a_heartbeat_extends_a_live_lease_only(harness: _Harness) -> None:
    harness.enqueue("cap-1")
    job = harness.claim()
    assert job is not None

    harness.clock.advance(timedelta(minutes=9))
    assert harness.repository.renew_lease(job, LEASE) is True
    assert harness.job()["lease_expires_at"] == format_timestamp(
        NOW + timedelta(minutes=9) + LEASE
    )

    harness.clock.advance(timedelta(minutes=9))
    assert harness.claim(worker_id="worker-b") is None  # renewed lease held

    harness.clock.advance(LEASE)
    assert harness.repository.renew_lease(job, LEASE) is False


# --- the runner ---------------------------------------------------------------


def test_the_fake_pipeline_succeeds_deterministically(harness: _Harness) -> None:
    harness.enqueue("cap-1")

    report = harness.run()

    expected = synthetic_outcome("cap-1", PIPELINE)
    assert report == RunReport(
        status=RunStatus.SUCCEEDED,
        job_id=harness.job()["id"],
        capture_id="cap-1",
        attempt=1,
        outcome=expected,
    )
    row = harness.job()
    assert row["state"] == "succeeded"
    assert row["error_category"] is None
    assert row["lease_owner"] is None
    assert row["finished_at"] == format_timestamp(NOW)
    [result] = harness.results()
    assert result["outcome"] == expected.value
    assert result["detector_model_id"] == "mgo-fake-detector"
    assert len(result["detector_model_sha256"]) == 64
    assert result["image_width"] is None
    assert result["created_at"] == format_timestamp(NOW)


def test_the_same_capture_gets_the_same_fake_outcome_every_time(
    tmp_path: Path,
) -> None:
    outcomes = set()
    for index in range(3):
        (tmp_path / f"run-{index}").mkdir()
        harness = _Harness(tmp_path / f"run-{index}")
        harness.enqueue("cap-1")
        outcomes.add(harness.run().outcome)

    assert outcomes == {synthetic_outcome("cap-1", PIPELINE)}
    assert synthetic_outcome("cap-1", PIPELINE) is synthetic_outcome("cap-1", PIPELINE)


def test_an_idle_queue_runs_nothing(harness: _Harness) -> None:
    adapter = FakeRecognitionAdapter()

    assert harness.run(adapter) == RunReport(status=RunStatus.IDLE)
    assert adapter.calls == []


def test_the_result_and_the_succeeded_state_commit_together(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure writing the result leaves the job running and result-free."""
    harness.enqueue("cap-1")

    def _fail(*args: Any, **kwargs: Any) -> None:
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(repository_module, "_insert_result", _fail)

    with pytest.raises(RecognitionRepositoryError):
        harness.run()

    row = harness.job()
    assert row["state"] == "running"
    assert row["finished_at"] is None
    assert harness.results() == []

    monkeypatch.undo()
    harness.clock.advance(LEASE)
    assert harness.run(worker_id="worker-b").status is RunStatus.SUCCEEDED
    assert len(harness.results()) == 1
    assert harness.job()["attempt_count"] == 2


def test_an_adapter_crash_is_bounded_and_retried(harness: _Harness) -> None:
    harness.enqueue("cap-1")
    adapter = FakeRecognitionAdapter(crashes={"cap-1"})

    report = harness.run(adapter)

    assert report.status is RunStatus.RETRY_SCHEDULED
    assert report.error_category is RecognitionErrorCategory.UNEXPECTED
    row = harness.job()
    assert row["state"] == "pending"
    assert row["error_category"] == "unexpected"
    assert row["next_attempt_at"] == format_timestamp(NOW + timedelta(minutes=1))
    assert harness.results() == []


def test_a_persistent_crash_ends_failed_after_max_attempts(harness: _Harness) -> None:
    harness.enqueue("cap-1")
    adapter = FakeRecognitionAdapter(crashes={"cap-1"})
    statuses = []
    for _ in range(4):
        statuses.append(harness.run(adapter).status)
        harness.clock.advance(timedelta(hours=1))

    assert statuses == [
        RunStatus.RETRY_SCHEDULED,
        RunStatus.RETRY_SCHEDULED,
        RunStatus.FAILED,
        RunStatus.IDLE,
    ]
    assert len(adapter.calls) == 3
    assert harness.job()["state"] == "failed"


def test_no_exception_text_reaches_the_database(
    harness: _Harness, caplog: pytest.LogCaptureFixture
) -> None:
    """The traceback is logged; only the category is stored."""
    harness.enqueue("cap-1")

    def _explode(request: RecognitionRequest) -> None:
        raise SimulatedAdapterCrash(f"secret detail at {request.media_path}")

    harness.run(FakeRecognitionAdapter(before_result=_explode))

    assert "secret detail" in caplog.text
    dump = json.dumps(harness.job())
    assert "secret detail" not in dump
    assert str(harness.root) not in dump


def test_an_interrupted_worker_leaves_a_recoverable_claim(harness: _Harness) -> None:
    """Killed between claim and result: nothing recorded, then recovered once."""
    harness.enqueue("cap-1")

    def _die(request: RecognitionRequest) -> None:
        raise _Interrupted

    with pytest.raises(_Interrupted):
        harness.run(FakeRecognitionAdapter(before_result=_die))

    row = harness.job()
    assert row["state"] == "running"
    assert harness.results() == []

    assert harness.run(worker_id="worker-b") == RunReport(status=RunStatus.IDLE)

    harness.clock.advance(LEASE)
    report = harness.run(worker_id="worker-b")

    assert report.status is RunStatus.SUCCEEDED
    assert report.attempt == 2
    assert len(harness.results()) == 1
    assert harness.run(worker_id="worker-c").status is RunStatus.IDLE
    assert len(harness.results()) == 1


@pytest.mark.parametrize(
    ("category", "status"),
    [
        (RecognitionErrorCategory.DECODE_ERROR, RunStatus.FAILED),
        (RecognitionErrorCategory.SIZE_MISMATCH, RunStatus.FAILED),
        (RecognitionErrorCategory.UNSAFE_PATH, RunStatus.FAILED),
        (RecognitionErrorCategory.MEDIA_MISSING, RunStatus.SKIPPED),
        (RecognitionErrorCategory.TIMEOUT, RunStatus.RETRY_SCHEDULED),
        (RecognitionErrorCategory.RESOURCE_LIMIT, RunStatus.RETRY_SCHEDULED),
        (RecognitionErrorCategory.MODEL_UNAVAILABLE, RunStatus.RETRY_SCHEDULED),
    ],
)
def test_adapter_categories_follow_the_disposition_contract(
    harness: _Harness, category: RecognitionErrorCategory, status: RunStatus
) -> None:
    harness.enqueue("cap-1")

    report = harness.run(FakeRecognitionAdapter(failures={"cap-1": category}))

    assert report.status is status
    assert report.error_category is category
    row = harness.job()
    assert row["error_category"] == category.value
    assert row["attempt_count"] == 1
    assert harness.results() == []
    if status is RunStatus.RETRY_SCHEDULED:
        assert row["state"] == "pending"
        assert row["finished_at"] is None
    else:
        assert row["state"] == status.value
        assert row["finished_at"] == format_timestamp(NOW)
        harness.clock.advance(timedelta(days=1))
        assert harness.claim() is None


def test_media_removed_after_reconciliation_is_skipped_without_inference(
    harness: _Harness,
) -> None:
    media = harness.add("cap-1")
    harness.enqueue("cap-1")
    media.unlink()
    adapter = FakeRecognitionAdapter()

    report = harness.run(adapter)

    assert report.status is RunStatus.SKIPPED
    assert report.error_category is RecognitionErrorCategory.MEDIA_MISSING
    assert adapter.calls == []
    row = harness.job()
    assert (row["state"], row["error_category"]) == ("skipped", "media_missing")
    harness.clock.advance(timedelta(days=30))
    assert harness.run(adapter).status is RunStatus.IDLE


@pytest.mark.parametrize("state", ["pending_delete", "deleted"])
def test_a_lifecycle_row_added_after_reconciliation_skips_the_job(
    harness: _Harness, state: str
) -> None:
    """The file is still there; the lifecycle row alone forbids looking at it."""
    media = harness.add("cap-1")
    harness.enqueue("cap-1")
    harness.add_lifecycle("cap-1", state)
    adapter = FakeRecognitionAdapter()

    report = harness.run(adapter)

    assert report.status is RunStatus.SKIPPED
    assert report.error_category is RecognitionErrorCategory.MEDIA_MISSING
    assert adapter.calls == []
    assert media.read_bytes() == PAYLOAD


def test_media_disappearing_during_inference_does_not_corrupt_the_queue(
    harness: _Harness,
) -> None:
    """Recognition takes no lock against retention; the result simply stands."""
    media = harness.add("cap-1")
    harness.enqueue("cap-1")

    def _retention_reclaims(request: RecognitionRequest) -> None:
        with database_connection(harness.database_path) as connection:
            connection.execute(
                "INSERT INTO capture_media_lifecycle VALUES "
                "('cap-1', 'pending_delete', ?, NULL, 'age')",
                (utc_now_iso(),),
            )
        media.unlink()

    report = harness.run(FakeRecognitionAdapter(before_result=_retention_reclaims))

    assert report.status is RunStatus.SUCCEEDED
    assert harness.job()["state"] == "succeeded"
    assert len(harness.results()) == 1


def test_an_existing_result_is_unchanged_by_later_media_removal(
    harness: _Harness,
) -> None:
    media = harness.add("cap-1")
    harness.enqueue("cap-1")
    harness.run()
    before = (harness.job(), harness.results())

    harness.add_lifecycle("cap-1", "deleted")
    media.unlink()
    harness.clock.advance(timedelta(days=1))
    harness.run()

    assert (harness.job(), harness.results()) == before


def test_a_tampered_catalogue_path_is_refused_at_run_time(harness: _Harness) -> None:
    """A queued job whose capture now points outside the root is never inferred."""
    harness.enqueue("cap-1")
    outside = harness.tmp_path / "outside"
    outside.mkdir()
    (outside / "cap-1.jpg").write_bytes(PAYLOAD)
    with database_connection(harness.database_path) as connection:
        connection.execute(
            "UPDATE captures SET absolute_path = ? WHERE id = 'cap-1'",
            (str(outside / "cap-1.jpg"),),
        )
    adapter = FakeRecognitionAdapter()

    report = harness.run(adapter)

    assert report.status is RunStatus.FAILED
    assert report.error_category is RecognitionErrorCategory.UNSAFE_PATH
    assert adapter.calls == []
    harness.clock.advance(timedelta(days=1))
    assert harness.claim() is None


def test_media_replaced_with_different_bytes_is_refused_at_run_time(
    harness: _Harness,
) -> None:
    media = harness.add("cap-1")
    harness.enqueue("cap-1")
    media.write_bytes(PAYLOAD + b"-altered")
    adapter = FakeRecognitionAdapter()

    report = harness.run(adapter)

    assert report.status is RunStatus.FAILED
    assert report.error_category is RecognitionErrorCategory.SIZE_MISMATCH
    assert adapter.calls == []


def test_an_adapter_that_returns_no_result_is_unexpected(harness: _Harness) -> None:
    harness.enqueue("cap-1")

    class _Broken(FakeRecognitionAdapter):
        def recognise(self, request: RecognitionRequest) -> Any:
            return None

    report = harness.run(_Broken())

    assert report.status is RunStatus.RETRY_SCHEDULED
    assert report.error_category is RecognitionErrorCategory.UNEXPECTED


def test_a_worker_whose_claim_is_taken_during_inference_records_nothing(
    harness: _Harness,
) -> None:
    harness.enqueue("cap-1")
    takeover: list[RunReport] = []

    def _lease_lapses_and_another_worker_finishes(request: RecognitionRequest) -> None:
        harness.clock.advance(LEASE)
        takeover.append(
            harness.run(repository=harness.worker_repository(), worker_id="worker-b")
        )

    report = harness.run(
        FakeRecognitionAdapter(before_result=_lease_lapses_and_another_worker_finishes)
    )

    assert takeover[0].status is RunStatus.SUCCEEDED
    assert report.status is RunStatus.CLAIM_LOST
    assert len(harness.results()) == 1


def test_an_adapter_can_keep_its_claim_alive(harness: _Harness) -> None:
    harness.enqueue("cap-1")
    renewals: list[bool] = []

    def _long_inference(request: RecognitionRequest) -> None:
        for _ in range(3):
            harness.clock.advance(timedelta(minutes=8))
            renewals.append(request.renew_lease())
            assert (
                harness.claim(harness.worker_repository(), worker_id="worker-b") is None
            )

    report = harness.run(FakeRecognitionAdapter(before_result=_long_inference))

    assert renewals == [True, True, True]
    assert report.status is RunStatus.SUCCEEDED


def test_an_unusable_capture_directory_claims_nothing(harness: _Harness) -> None:
    harness.enqueue("cap-1")

    with pytest.raises(RecognitionConfigurationError):
        run_one_job(
            harness.repository,
            FakeRecognitionAdapter(),
            capture_directory=harness.tmp_path / "missing",
            worker_id="worker-a",
        )

    assert harness.job()["state"] == "pending"
    assert harness.job()["attempt_count"] == 0


# --- transaction boundaries ---------------------------------------------------


def test_no_write_transaction_is_open_while_the_adapter_runs(
    harness: _Harness,
) -> None:
    """Another writer gets the database immediately, with no wait at all."""
    harness.enqueue("cap-1")
    probes: list[bool] = []

    def _probe(request: RecognitionRequest) -> None:
        connection = sqlite3.connect(harness.database_path, timeout=0)
        try:
            connection.execute("PRAGMA busy_timeout = 0")
            connection.isolation_level = None
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("ROLLBACK")
            probes.append(True)
        finally:
            connection.close()

    report = harness.run(FakeRecognitionAdapter(before_result=_probe))

    assert probes == [True]
    assert report.status is RunStatus.SUCCEEDED


def test_the_queue_accepts_new_work_while_the_adapter_runs(
    harness: _Harness,
) -> None:
    harness.enqueue("cap-1")
    harness.add("cap-2")

    def _reconcile_meanwhile(request: RecognitionRequest) -> None:
        RecognitionReconciler(
            harness.worker_repository(busy_timeout_seconds=0.1),
            pipeline_version=PIPELINE,
            capture_directory=harness.root,
            enrolment_watermark=WATERMARK,
        ).reconcile()

    harness.run(FakeRecognitionAdapter(before_result=_reconcile_meanwhile))

    assert harness.job("cap-2")["state"] == "pending"


def test_recognition_connections_cannot_write_capture_tables(
    harness: _Harness,
) -> None:
    """The authorizer makes the catalogue read-only to recognition."""
    harness.add("cap-1")
    connection = harness.repository._connect()
    try:
        for statement in (
            "UPDATE captures SET filesize_bytes = 1",
            "DELETE FROM captures",
            "INSERT INTO capture_media_lifecycle VALUES "
            "('cap-1', 'pending_delete', 'x', NULL, 'age')",
            "DELETE FROM observations",
            "DELETE FROM schema_migrations",
            "DROP TABLE capture_media_lifecycle",
            "CREATE TABLE sightings (id TEXT)",
        ):
            with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
                connection.execute(statement)
        connection.execute("SELECT COUNT(*) FROM captures").fetchone()
        connection.execute("DELETE FROM recognition_results")
    finally:
        connection.close()

    with database_connection(harness.database_path) as check:
        assert check.execute("SELECT filesize_bytes FROM captures").fetchone()[
            0
        ] == len(PAYLOAD)


def test_a_full_lifecycle_never_changes_the_catalogue_or_the_media(
    harness: _Harness,
) -> None:
    media = [harness.add(f"cap-{index}") for index in range(4)]
    harness.enqueue("cap-0", "cap-1", "cap-2", "cap-3")
    before = harness.catalogue()
    listing = sorted(path.name for path in harness.root.iterdir())
    stats = [(path.read_bytes(), path.stat().st_mtime_ns) for path in media]
    adapter = FakeRecognitionAdapter(
        failures={"cap-1": RecognitionErrorCategory.TIMEOUT},
        crashes={"cap-2"},
    )

    for _ in range(12):
        harness.run(adapter)
        harness.clock.advance(timedelta(hours=1))

    assert harness.catalogue() == before
    assert list_observations(harness.database_path) == []
    assert sorted(path.name for path in harness.root.iterdir()) == listing
    assert [(path.read_bytes(), path.stat().st_mtime_ns) for path in media] == stats


def test_recognition_takes_no_operations_lock(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Neither the retention nor the backup lock is ever acquired.

    Both are :class:`OperationLock` files, so refusing every acquisition, and
    finding no lock file afterwards, covers both.
    """

    def _forbidden(self: OperationLock) -> Any:
        raise AssertionError(f"recognition acquired {self.path.name}")

    monkeypatch.setattr(OperationLock, "acquire", _forbidden)
    monkeypatch.setattr(OperationLock, "_create", _forbidden)
    harness.enqueue("cap-1", "cap-2")

    assert harness.run().status is RunStatus.SUCCEEDED
    assert harness.run().status is RunStatus.SUCCEEDED
    assert [
        path.name for path in harness.tmp_path.rglob("*") if "lock" in path.name
    ] == []


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_the_recognition_package_is_decoupled_from_capture_and_locking() -> None:
    """No camera, capture, event-capture, backup, locking, imaging or network."""
    forbidden_prefixes = (
        "mgo.camera",
        "mgo.captures",
        "mgo.event_capture",
        "mgo.motion",
        "mgo.operations",
        "mgo.notifications",
        "mgo.api",
        "PIL",
        "socket",
        "urllib",
        "http",
        "requests",
        "subprocess",
        "fcntl",
        "msvcrt",
    )
    sources = sorted(SOURCE_DIRECTORY.glob("*.py"))
    assert sources

    for source in sources:
        for module in _imported_modules(source):
            assert not module.startswith(forbidden_prefixes), (source.name, module)


def test_nothing_in_the_application_constructs_the_fake_adapter() -> None:
    application = SOURCE_DIRECTORY.parent
    for source in application.rglob("*.py"):
        if source.parent == SOURCE_DIRECTORY:
            continue
        assert "fake_adapter" not in source.read_text(encoding="utf-8"), source


def test_no_path_is_stored_or_reported(harness: _Harness) -> None:
    harness.enqueue("cap-1", "cap-2")
    reports = [
        harness.run(),
        harness.run(FakeRecognitionAdapter(crashes={"cap-2"})),
    ]

    with database_connection(harness.database_path) as connection:
        stored = [
            json.dumps(
                [dict(row) for row in connection.execute(f"SELECT * FROM {table}")]
            )
            for table in ("recognition_jobs", "recognition_results")
        ]
    for text in (*stored, *(repr(report) for report in reports)):
        assert str(harness.tmp_path) not in text
        assert "cap-1.jpg" not in text


# --- pipeline versions and duplicates -----------------------------------------


def test_repeat_processing_cannot_create_a_duplicate_result(harness: _Harness) -> None:
    harness.enqueue("cap-1")

    statuses = [harness.run().status for _ in range(3)]
    harness.clock.advance(timedelta(days=1))
    statuses.append(harness.run().status)

    assert statuses == [
        RunStatus.SUCCEEDED,
        RunStatus.IDLE,
        RunStatus.IDLE,
        RunStatus.IDLE,
    ]
    assert len(harness.results()) == 1


def test_a_second_pipeline_version_is_processed_independently(
    harness: _Harness,
) -> None:
    harness.enqueue("cap-1", pipeline_version="fake-0")
    first = harness.run(FakeRecognitionAdapter(pipeline_version="fake-0"))
    harness.enqueue("cap-1", pipeline_version="fake-1")

    idle = harness.run(FakeRecognitionAdapter(pipeline_version="fake-0"))
    second = harness.run(FakeRecognitionAdapter(pipeline_version="fake-1"))

    assert (first.status, idle.status, second.status) == (
        RunStatus.SUCCEEDED,
        RunStatus.IDLE,
        RunStatus.SUCCEEDED,
    )
    results = harness.results()
    assert [result["pipeline_version"] for result in results] == ["fake-0", "fake-1"]
    assert harness.job(pipeline_version="fake-0")["state"] == "succeeded"
    assert results[0]["outcome"] == synthetic_outcome("cap-1", "fake-0").value
    assert results[1]["outcome"] == synthetic_outcome("cap-1", "fake-1").value


# --- values -------------------------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"detector_model_id": "d"},
        {"detector_model_id": "d", "detector_model_sha256": "A" * 64},
        {"taxonomy_id": "ebird"},
        {"image_width": 10},
        {"inference_duration_ms": -1},
        {"peak_rss_bytes": True},
        {"peak_rss_bytes": 2**63},
        {"preprocessing_version": ""},
        {"taxonomy_id": "\x00ebird", "taxonomy_version": "1"},
    ],
)
def test_an_incoherent_result_value_is_refused(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        RecognitionResult(outcome=RecognitionOutcome.NO_BIRD, **overrides)


@pytest.mark.parametrize("value", ["", "has space", "a/b", "x" * 65, "-leading"])
def test_an_unsafe_pipeline_or_worker_identifier_is_refused(
    harness: _Harness, value: str
) -> None:
    with pytest.raises(ValueError):
        harness.claim(worker_id=value)
    with pytest.raises(ValueError):
        FakeRecognitionAdapter(pipeline_version=value)


def test_a_naive_clock_is_refused(harness: _Harness) -> None:
    harness.enqueue("cap-1")
    repository = RecognitionRepository(
        harness.database_path, clock=lambda: datetime(2026, 9, 11, 12, 0)
    )

    with pytest.raises(RecognitionRepositoryError):
        harness.claim(repository)


# --- opening media for a real adapter -----------------------------------------


def _request(path: Path, size: int) -> RecognitionRequest:
    return RecognitionRequest(
        job_id="job",
        capture_id="cap-1",
        pipeline_version=PIPELINE,
        attempt=1,
        media_path=path,
        expected_size_bytes=size,
        renew_lease=lambda: True,
    )


def test_open_media_reads_the_catalogued_file(tmp_path: Path) -> None:
    media = tmp_path / "cap-1.jpg"
    media.write_bytes(PAYLOAD)

    with open_media(_request(media, len(PAYLOAD))) as stream:
        assert stream.read() == PAYLOAD


def test_open_media_refuses_to_follow_a_final_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    media = tmp_path / "cap-1.jpg"
    media.write_bytes(PAYLOAD)
    flags_seen: list[int] = []
    real_open = os.open

    def _recording_open(path: Any, flags: int, *args: Any) -> int:
        flags_seen.append(flags)
        return real_open(path, flags, *args)

    monkeypatch.setattr(os, "open", _recording_open)
    with open_media(_request(media, len(PAYLOAD))):
        pass

    if hasattr(os, "O_NOFOLLOW"):
        assert flags_seen[0] & os.O_NOFOLLOW
    if hasattr(os, "O_NONBLOCK"):
        assert flags_seen[0] & os.O_NONBLOCK
    if hasattr(os, "O_BINARY"):
        assert flags_seen[0] & os.O_BINARY

    def _loop(path: Any, flags: int, *args: Any) -> int:
        raise OSError(errno.ELOOP, "Too many levels of symbolic links")

    monkeypatch.setattr(os, "open", _loop)
    with pytest.raises(RecognitionAdapterError) as excinfo:
        open_media(_request(media, len(PAYLOAD)))
    assert excinfo.value.category is RecognitionErrorCategory.UNSAFE_PATH


@pytest.mark.skipif(os.name != "posix", reason="O_NOFOLLOW exists on POSIX only")
def test_open_media_refuses_a_real_symlink_on_posix(tmp_path: Path) -> None:
    target = tmp_path / "elsewhere.jpg"
    target.write_bytes(PAYLOAD)
    link = tmp_path / "cap-1.jpg"
    os.symlink(target, link)

    with pytest.raises(RecognitionAdapterError) as excinfo:
        open_media(_request(link, len(PAYLOAD)))
    assert excinfo.value.category is RecognitionErrorCategory.UNSAFE_PATH


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFOs exist on POSIX only")
def test_open_media_refuses_a_real_fifo_without_blocking_on_posix(
    tmp_path: Path,
) -> None:
    """A FIFO swapped in after validation fails fast instead of hanging."""
    fifo = tmp_path / "cap-1.jpg"
    os.mkfifo(fifo)
    outcome: list[object] = []

    def _open() -> None:
        try:
            open_media(_request(fifo, len(PAYLOAD)))
        except RecognitionAdapterError as exc:
            outcome.append(exc.category)

    worker = threading.Thread(target=_open, daemon=True)
    worker.start()
    worker.join(timeout=10)

    assert not worker.is_alive(), "open_media blocked on a FIFO"
    assert outcome == [RecognitionErrorCategory.UNSAFE_PATH]


@pytest.mark.parametrize(
    ("prepare", "category"),
    [
        (lambda path: None, RecognitionErrorCategory.MEDIA_MISSING),
        (
            lambda path: path.write_bytes(PAYLOAD + b"!"),
            RecognitionErrorCategory.SIZE_MISMATCH,
        ),
    ],
)
def test_open_media_refuses_missing_or_different_media(
    tmp_path: Path,
    prepare: Callable[[Path], object],
    category: RecognitionErrorCategory,
) -> None:
    media = tmp_path / "cap-1.jpg"
    prepare(media)

    with pytest.raises(RecognitionAdapterError) as excinfo:
        open_media(_request(media, len(PAYLOAD)))
    assert excinfo.value.category is category


def test_open_media_checks_the_open_descriptor_not_the_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    media = tmp_path / "cap-1.jpg"
    media.write_bytes(PAYLOAD)
    real_fstat = os.fstat
    closed: list[int] = []
    real_close = os.close

    class _NotRegular:
        def __init__(self, result: os.stat_result) -> None:
            self.st_mode = stat.S_IFIFO | 0o644
            self.st_size = result.st_size

    monkeypatch.setattr(os, "fstat", lambda fd: _NotRegular(real_fstat(fd)))
    monkeypatch.setattr(os, "close", lambda fd: (closed.append(fd), real_close(fd)))

    with pytest.raises(RecognitionAdapterError) as excinfo:
        open_media(_request(media, len(PAYLOAD)))

    assert excinfo.value.category is RecognitionErrorCategory.UNSAFE_PATH
    assert len(closed) == 1


def test_an_adapter_error_carries_only_its_category() -> None:
    error = RecognitionAdapterError(RecognitionErrorCategory.DECODE_ERROR)

    assert str(error) == "Recognition attempt failed: decode_error"
