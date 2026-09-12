"""Recognition's only database access: the job queue and its results.

Everything recognition writes goes through this module, and it writes only
``recognition_jobs`` and ``recognition_results``. That is enforced rather than
promised: every connection opened here carries a SQLite authorizer that denies
any ``INSERT``, ``UPDATE`` or ``DELETE`` against any other table -- which also
denies schema changes, since those write ``sqlite_master``. The capture
catalogue, the media-lifecycle table and the observation timeline are
therefore read-only to recognition at the SQLite boundary, whatever a later
change to this file tries to do.

Three rules shape the rest:

* **Every write is one short ``BEGIN IMMEDIATE`` transaction.** ``IMMEDIATE``
  takes SQLite's write reservation before the first read, so a transaction
  that selects a job and then updates it cannot interleave with another writer
  doing the same. It is supported by every SQLite version Python ships with,
  and it does not depend on ``RETURNING``.
* **No connection outlives its method.** Each operation opens, commits (or
  rolls back) and closes. A worker holding a claim holds a *row state*, not a
  transaction, so adapter work never runs inside one.
* **Every write about a claimed job is conditional on that claim.** A claim is
  identified by its lease owner *and* its attempt number, and each heartbeat,
  result and failure matches both. A worker whose lease expired and was taken
  over matches nothing and is told so.
"""

from __future__ import annotations

import logging
import sqlite3
import uuid
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from mgo.core.database import DEFAULT_BUSY_TIMEOUT_SECONDS, connect_database
from mgo.recognition.eligibility import CatalogueCapture
from mgo.recognition.models import (
    ERROR_DISPOSITION,
    RecognitionErrorCategory,
    RecognitionJob,
    RecognitionJobState,
    RecognitionRepositoryError,
    RecognitionResult,
    RetryPolicy,
    validate_identifier,
    validate_lease_duration,
    validate_max_attempts,
)

LOGGER = logging.getLogger(__name__)

Clock = Callable[[], datetime]

#: The only tables a recognition connection may write.
RECOGNITION_TABLES: frozenset[str] = frozenset(
    {"recognition_jobs", "recognition_results"}
)

_WRITE_ACTIONS: frozenset[int] = frozenset(
    {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE}
)

#: Enqueue in bounded batches so a large enrolment never holds the write
#: reservation for long.
ENQUEUE_BATCH_SIZE = 100


def _authorise(
    action: int,
    table: str | None,
    _column: str | None,
    _database: str | None,
    _source: str | None,
) -> int:
    """Deny every row write outside the recognition tables."""
    if action in _WRITE_ACTIONS and table not in RECOGNITION_TABLES:
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def _utc_now() -> datetime:
    """Return the current timezone-aware UTC instant."""
    return datetime.now(UTC)


def format_timestamp(value: datetime) -> str:
    """Return the one stored layout for a recognition timestamp.

    Always UTC, always microseconds, always ``+00:00`` -- the layout the schema
    enforces, and the only one under which comparing the text compares the
    instants.
    """
    if value.tzinfo is None:
        raise ValueError("Recognition timestamps must be timezone-aware")
    return value.astimezone(UTC).isoformat(timespec="microseconds")


#: The catalogue projection recognition reads. ``lifecycle_recorded`` is the
#: presence of *any* lifecycle row: pending deletion and completed deletion
#: are the same answer to "may this media be looked at".
_CAPTURE_PROJECTION = """
    SELECT
        c.id AS capture_id,
        c.filename AS filename,
        c.absolute_path AS absolute_path,
        c.captured_at_utc AS captured_at_utc,
        c.filesize_bytes AS filesize_bytes,
        c.extra_metadata AS extra_metadata,
        l.capture_id IS NOT NULL AS lifecycle_recorded
    FROM captures AS c
    LEFT JOIN capture_media_lifecycle AS l
        ON l.capture_id = c.id
"""

_UNQUEUED_CAPTURES_SQL = (
    _CAPTURE_PROJECTION
    + """
    WHERE NOT EXISTS (
        SELECT 1 FROM recognition_jobs AS j
        WHERE j.capture_id = c.id
          AND j.pipeline_version = :pipeline_version
    )
    ORDER BY c.captured_at_utc ASC, c.created_at_utc ASC, c.id ASC
"""
)

_CAPTURE_BY_ID_SQL = _CAPTURE_PROJECTION + "    WHERE c.id = ?\n"

#: The lifecycle table is consulted again inside the write, so a deletion
#: intent committed between reconciliation's read and its insert still stops
#: the job being created. The conflict clause makes the unique constraint the
#: last word on duplicates: however two reconcilers interleave, the second
#: insert for a (capture, pipeline version) pair does nothing.
_ENQUEUE_SQL = """
    INSERT INTO recognition_jobs (
        id, capture_id, pipeline_version, camera_id, state, attempt_count,
        max_attempts, next_attempt_at, lease_owner, lease_expires_at,
        created_at, started_at, finished_at, error_category
    )
    SELECT :id, c.id, :pipeline_version, NULL, 'pending', 0,
        :max_attempts, :now, NULL, NULL,
        :now, NULL, NULL, NULL
    FROM captures AS c
    WHERE c.id = :capture_id
      AND NOT EXISTS (
          SELECT 1 FROM capture_media_lifecycle AS l WHERE l.capture_id = c.id
      )
    ON CONFLICT(capture_id, pipeline_version) DO NOTHING
"""

#: A running job whose lease expired on its final attempt. Its worker is gone
#: and it has no attempt left to be recovered into, so it ends here -- as
#: ``unexpected``, because nothing recorded why the worker never reported back.
_EXPIRE_EXHAUSTED_SQL = """
    UPDATE recognition_jobs
    SET state = 'failed', error_category = 'unexpected', finished_at = :now,
        lease_owner = NULL, lease_expires_at = NULL
    WHERE state = 'running' AND pipeline_version = :pipeline_version
      AND lease_expires_at <= :expired_before
      AND attempt_count >= max_attempts
"""

#: Due work for one pipeline version: pending jobs whose retry time has come,
#: and running jobs whose lease has expired with an attempt still left. Ordered
#: by when each became due, then by age, then by id, so the order is total.
_SELECT_CLAIMABLE_SQL = """
    SELECT id, capture_id, pipeline_version, state, attempt_count, max_attempts
    FROM recognition_jobs
    WHERE pipeline_version = :pipeline_version
      AND attempt_count < max_attempts
      AND (
            (state = 'pending' AND next_attempt_at <= :now)
         OR (state = 'running' AND lease_expires_at <= :now)
      )
    ORDER BY
        CASE state WHEN 'pending' THEN next_attempt_at ELSE lease_expires_at END ASC,
        created_at ASC,
        id ASC
    LIMIT 1
"""

_CLAIM_SQL = """
    UPDATE recognition_jobs
    SET state = 'running', attempt_count = attempt_count + 1,
        lease_owner = :lease_owner, lease_expires_at = :lease_expires_at,
        started_at = :now, error_category = NULL
    WHERE id = :id AND state = :observed_state
      AND attempt_count = :observed_attempts
"""

#: The claim a write must still hold. Shared by every write about a claimed
#: job, so there is one definition of "this worker still owns this attempt".
_OWNED_CLAIM = (
    "id = :id AND state = 'running'"
    " AND lease_owner = :lease_owner AND attempt_count = :attempt_count"
)

_RENEW_SQL = f"""
    UPDATE recognition_jobs
    SET lease_expires_at = :lease_expires_at
    WHERE {_OWNED_CLAIM}
      AND lease_expires_at > :now
"""

_SUCCEED_SQL = f"""
    UPDATE recognition_jobs
    SET state = 'succeeded', error_category = NULL, finished_at = :now,
        lease_owner = NULL, lease_expires_at = NULL
    WHERE {_OWNED_CLAIM}
"""

_RETRY_SQL = f"""
    UPDATE recognition_jobs
    SET state = 'pending', error_category = :error_category,
        next_attempt_at = :next_attempt_at,
        lease_owner = NULL, lease_expires_at = NULL
    WHERE {_OWNED_CLAIM}
"""

_TERMINATE_SQL = f"""
    UPDATE recognition_jobs
    SET state = :state, error_category = :error_category, finished_at = :now,
        lease_owner = NULL, lease_expires_at = NULL
    WHERE {_OWNED_CLAIM}
"""

_INSERT_RESULT_SQL = """
    INSERT INTO recognition_results (
        id, job_id, outcome,
        detector_model_id, detector_model_sha256,
        classifier_model_id, classifier_model_sha256,
        label_set_id, label_set_sha256,
        taxonomy_id, taxonomy_version,
        preprocessing_version, thresholds_version,
        inference_duration_ms, peak_rss_bytes, cpu_time_ms,
        image_width, image_height, created_at
    )
    VALUES (
        :id, :job_id, :outcome,
        :detector_model_id, :detector_model_sha256,
        :classifier_model_id, :classifier_model_sha256,
        :label_set_id, :label_set_sha256,
        :taxonomy_id, :taxonomy_version,
        :preprocessing_version, :thresholds_version,
        :inference_duration_ms, :peak_rss_bytes, :cpu_time_ms,
        :image_width, :image_height, :created_at
    )
"""


def _capture_from_row(row: sqlite3.Row) -> CatalogueCapture | None:
    """Return the projection row, or ``None`` if it has no usable identity."""
    identifier = row["capture_id"]
    if not isinstance(identifier, str) or not identifier:
        return None
    return CatalogueCapture(
        capture_id=identifier,
        filename=row["filename"],
        absolute_path=row["absolute_path"],
        captured_at_utc=row["captured_at_utc"],
        filesize_bytes=row["filesize_bytes"],
        extra_metadata=row["extra_metadata"],
        lifecycle_recorded=bool(row["lifecycle_recorded"]),
    )


def _select_claimable(
    connection: sqlite3.Connection, *, pipeline_version: str, now: str
) -> sqlite3.Row | None:
    """Return the next due job inside the caller's claim transaction.

    A module-level function so a test can interpose a competing writer between
    the select and the update and prove it is held off.
    """
    row: sqlite3.Row | None = connection.execute(
        _SELECT_CLAIMABLE_SQL,
        {"pipeline_version": pipeline_version, "now": now},
    ).fetchone()
    return row


def _insert_result(
    connection: sqlite3.Connection,
    *,
    job_id: str,
    result: RecognitionResult,
    created_at: str,
) -> None:
    """Insert one result inside the caller's completion transaction."""
    connection.execute(
        _INSERT_RESULT_SQL,
        {
            "id": str(uuid.uuid4()),
            "job_id": job_id,
            "outcome": result.outcome.value,
            "detector_model_id": result.detector_model_id,
            "detector_model_sha256": result.detector_model_sha256,
            "classifier_model_id": result.classifier_model_id,
            "classifier_model_sha256": result.classifier_model_sha256,
            "label_set_id": result.label_set_id,
            "label_set_sha256": result.label_set_sha256,
            "taxonomy_id": result.taxonomy_id,
            "taxonomy_version": result.taxonomy_version,
            "preprocessing_version": result.preprocessing_version,
            "thresholds_version": result.thresholds_version,
            "inference_duration_ms": result.inference_duration_ms,
            "peak_rss_bytes": result.peak_rss_bytes,
            "cpu_time_ms": result.cpu_time_ms,
            "image_width": result.image_width,
            "image_height": result.image_height,
            "created_at": created_at,
        },
    )


class RecognitionRepository:
    """The durable recognition job queue.

    Holds only the database path, a busy timeout and a clock; each operation
    opens its own bounded connection and closes it. There is no process-wide
    state: two repositories on one database are two independent workers, which
    is exactly how the concurrency tests use them.
    """

    def __init__(
        self,
        database_path: Path,
        *,
        clock: Clock = _utc_now,
        busy_timeout_seconds: float = DEFAULT_BUSY_TIMEOUT_SECONDS,
    ) -> None:
        self._database_path = database_path
        self._clock = clock
        self._busy_timeout_seconds = busy_timeout_seconds

    # -- connections and transactions ---------------------------------------

    def _connect(self) -> sqlite3.Connection:
        """Open an autocommit connection that can write only recognition tables.

        ``create_parents=False``: recognition never brings a directory into
        existence. Autocommit mode so the explicit ``BEGIN IMMEDIATE`` below
        means exactly what it says.
        """
        connection = connect_database(
            self._database_path,
            busy_timeout_seconds=self._busy_timeout_seconds,
            create_parents=False,
        )
        connection.isolation_level = None
        connection.set_authorizer(_authorise)
        return connection

    @contextmanager
    def _write_transaction(self) -> Iterator[sqlite3.Connection]:
        """Run one short ``BEGIN IMMEDIATE`` transaction, then close."""
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except BaseException:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
            connection.execute("COMMIT")
        finally:
            connection.close()

    def _read(
        self, sql: str, parameters: Mapping[str, object] | tuple[object, ...]
    ) -> list[sqlite3.Row]:
        """Run one read in autocommit mode and close; no lock outlives it."""
        connection = self._connect()
        try:
            rows: list[sqlite3.Row] = connection.execute(sql, parameters).fetchall()
        finally:
            connection.close()
        return rows

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None:
            raise RecognitionRepositoryError(
                "The recognition clock must be timezone-aware"
            )
        return now.astimezone(UTC)

    # -- reconciliation -----------------------------------------------------

    def list_unqueued_captures(self, pipeline_version: str) -> list[CatalogueCapture]:
        """Return committed captures with no job yet for ``pipeline_version``."""
        validate_identifier(pipeline_version, "pipeline_version")
        try:
            rows = self._read(
                _UNQUEUED_CAPTURES_SQL, {"pipeline_version": pipeline_version}
            )
        except sqlite3.Error as exc:
            LOGGER.error("Could not read the recognition catalogue projection: %s", exc)
            raise RecognitionRepositoryError(
                "Could not read the capture catalogue for recognition"
            ) from exc
        captures = [_capture_from_row(row) for row in rows]
        return [capture for capture in captures if capture is not None]

    def enqueue(
        self,
        capture_ids: Sequence[str],
        *,
        pipeline_version: str,
        max_attempts: int,
    ) -> int:
        """Create a pending job for each capture that still has none.

        Returns how many jobs *this* call created. Captures that gained a job
        or a lifecycle row since they were read are left alone silently.
        """
        validate_identifier(pipeline_version, "pipeline_version")
        validate_max_attempts(max_attempts)
        created = 0
        for start in range(0, len(capture_ids), ENQUEUE_BATCH_SIZE):
            batch = capture_ids[start : start + ENQUEUE_BATCH_SIZE]
            created += self._enqueue_batch(
                batch, pipeline_version=pipeline_version, max_attempts=max_attempts
            )
        return created

    def _enqueue_batch(
        self,
        capture_ids: Iterable[str],
        *,
        pipeline_version: str,
        max_attempts: int,
    ) -> int:
        now = format_timestamp(self._now())
        created = 0
        try:
            with self._write_transaction() as connection:
                for capture_id in capture_ids:
                    cursor = connection.execute(
                        _ENQUEUE_SQL,
                        {
                            "id": str(uuid.uuid4()),
                            "capture_id": capture_id,
                            "pipeline_version": pipeline_version,
                            "max_attempts": max_attempts,
                            "now": now,
                        },
                    )
                    created += cursor.rowcount
        except sqlite3.Error as exc:
            LOGGER.error("Could not enqueue recognition jobs: %s", exc)
            raise RecognitionRepositoryError(
                "Could not enqueue recognition jobs"
            ) from exc
        return created

    # -- claiming -----------------------------------------------------------

    def claim_next(
        self,
        *,
        pipeline_version: str,
        worker_id: str,
        lease_duration: timedelta,
    ) -> RecognitionJob | None:
        """Atomically claim the next due job for ``pipeline_version``.

        In one ``BEGIN IMMEDIATE`` transaction: end any job whose lease expired
        on its final attempt, select the next due job, and move it to
        ``running`` under a lease owned by a token unique to this claim. The
        transaction commits before this returns, so the caller holds a durable
        claim and no open transaction. Returns ``None`` when nothing is due.
        """
        validate_identifier(pipeline_version, "pipeline_version")
        validate_identifier(worker_id, "worker_id")
        validate_lease_duration(lease_duration)
        now = self._now()
        now_text = format_timestamp(now)
        expires = now + lease_duration
        lease_owner = f"{worker_id}:{uuid.uuid4().hex}"

        try:
            with self._write_transaction() as connection:
                expired = connection.execute(
                    _EXPIRE_EXHAUSTED_SQL,
                    {
                        "pipeline_version": pipeline_version,
                        "now": now_text,
                        "expired_before": now_text,
                    },
                ).rowcount
                if expired:
                    LOGGER.warning(
                        "%s recognition job(s) lost their final attempt's lease "
                        "and were marked failed",
                        expired,
                    )

                row = _select_claimable(
                    connection, pipeline_version=pipeline_version, now=now_text
                )
                if row is None:
                    return None

                cursor = connection.execute(
                    _CLAIM_SQL,
                    {
                        "id": row["id"],
                        "observed_state": row["state"],
                        "observed_attempts": row["attempt_count"],
                        "lease_owner": lease_owner,
                        "lease_expires_at": format_timestamp(expires),
                        "now": now_text,
                    },
                )
                if cursor.rowcount != 1:
                    # Unreachable while the transaction holds the write
                    # reservation; refusing is the only safe reading if it
                    # ever happens.
                    raise RecognitionRepositoryError(
                        "A recognition job changed while it was being claimed"
                    )
        except sqlite3.Error as exc:
            LOGGER.error("Could not claim a recognition job: %s", exc)
            raise RecognitionRepositoryError(
                "Could not claim a recognition job"
            ) from exc

        return RecognitionJob(
            id=str(row["id"]),
            capture_id=str(row["capture_id"]),
            pipeline_version=str(row["pipeline_version"]),
            attempt_count=int(row["attempt_count"]) + 1,
            max_attempts=int(row["max_attempts"]),
            lease_owner=lease_owner,
            lease_expires_at=expires,
        )

    def renew_lease(self, job: RecognitionJob, lease_duration: timedelta) -> bool:
        """Extend a claim's lease. Returns ``False`` if the claim is gone.

        Renewal requires the lease to be unexpired as well as owned: once a
        lease has lapsed another worker is entitled to take the job, and a
        heartbeat arriving late must not quietly reclaim it.
        """
        validate_lease_duration(lease_duration)
        now = self._now()
        try:
            with self._write_transaction() as connection:
                cursor = connection.execute(
                    _RENEW_SQL,
                    {
                        **_claim_parameters(job),
                        "now": format_timestamp(now),
                        "lease_expires_at": format_timestamp(now + lease_duration),
                    },
                )
                renewed = cursor.rowcount == 1
        except sqlite3.Error as exc:
            LOGGER.error(
                "Could not renew the lease on recognition job %s: %s", job.id, exc
            )
            raise RecognitionRepositoryError(
                f"Could not renew the lease on recognition job {job.id}"
            ) from exc
        return renewed

    # -- completion ---------------------------------------------------------

    def read_capture(self, capture_id: str) -> CatalogueCapture | None:
        """Return one capture's projection, read at run time."""
        try:
            rows = self._read(_CAPTURE_BY_ID_SQL, (capture_id,))
        except sqlite3.Error as exc:
            LOGGER.error(
                "Could not read capture %s for recognition: %s", capture_id, exc
            )
            raise RecognitionRepositoryError(
                f"Could not read capture {capture_id} for recognition"
            ) from exc
        return _capture_from_row(rows[0]) if rows else None

    def complete_success(self, job: RecognitionJob, result: RecognitionResult) -> bool:
        """Record a result and mark its job succeeded, atomically.

        Both writes share one transaction: a reader can never see a succeeded
        job without its result, nor a result for a job that did not succeed.
        Returns ``False`` -- writing nothing -- when this worker no longer holds
        the claim, which is also what keeps a stale worker from adding a second
        result after another worker's success.
        """
        now = format_timestamp(self._now())
        try:
            with self._write_transaction() as connection:
                still_owned = (
                    connection.execute(
                        _SUCCEED_SQL, {**_claim_parameters(job), "now": now}
                    ).rowcount
                    == 1
                )
                if not still_owned:
                    LOGGER.warning(
                        "Recognition job %s attempt %s no longer holds its claim; "
                        "its result was discarded",
                        job.id,
                        job.attempt_count,
                    )
                    return False
                _insert_result(connection, job_id=job.id, result=result, created_at=now)
        except sqlite3.Error as exc:
            LOGGER.error(
                "Could not record the result of recognition job %s: %s", job.id, exc
            )
            raise RecognitionRepositoryError(
                f"Could not record the result of recognition job {job.id}"
            ) from exc
        return True

    def record_failure(
        self,
        job: RecognitionJob,
        category: RecognitionErrorCategory,
        *,
        retry_policy: RetryPolicy,
    ) -> RecognitionJobState | None:
        """End an attempt without a result. Returns the job's new state.

        A terminal category ends the job at once. A retryable one returns it to
        ``pending`` after the policy's delay -- unless this was its last
        permitted attempt, in which case it is terminal ``failed``. Returns
        ``None``, writing nothing, when this worker no longer holds the claim.
        """
        state = ERROR_DISPOSITION[category]
        if state is None and job.attempt_count >= job.max_attempts:
            state = RecognitionJobState.FAILED

        now = self._now()
        try:
            with self._write_transaction() as connection:
                if state is None:
                    retry_at = now + retry_policy.delay_after(job.attempt_count)
                    cursor = connection.execute(
                        _RETRY_SQL,
                        {
                            **_claim_parameters(job),
                            "error_category": category.value,
                            "next_attempt_at": format_timestamp(retry_at),
                        },
                    )
                    new_state = RecognitionJobState.PENDING
                else:
                    cursor = connection.execute(
                        _TERMINATE_SQL,
                        {
                            **_claim_parameters(job),
                            "state": state.value,
                            "error_category": category.value,
                            "now": format_timestamp(now),
                        },
                    )
                    new_state = state
                recorded = cursor.rowcount == 1
        except sqlite3.Error as exc:
            LOGGER.error(
                "Could not record the failure of recognition job %s: %s", job.id, exc
            )
            raise RecognitionRepositoryError(
                f"Could not record the failure of recognition job {job.id}"
            ) from exc

        if not recorded:
            LOGGER.warning(
                "Recognition job %s attempt %s no longer holds its claim; "
                "its failure (%s) was not recorded",
                job.id,
                job.attempt_count,
                category.value,
            )
            return None
        return new_state


def _claim_parameters(job: RecognitionJob) -> dict[str, object]:
    """The parameters :data:`_OWNED_CLAIM` matches on."""
    return {
        "id": job.id,
        "lease_owner": job.lease_owner,
        "attempt_count": job.attempt_count,
    }


__all__ = [
    "ENQUEUE_BATCH_SIZE",
    "RECOGNITION_TABLES",
    "RecognitionRepository",
    "format_timestamp",
]
