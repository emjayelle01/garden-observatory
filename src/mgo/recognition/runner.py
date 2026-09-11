"""Process at most one recognition job.

This is a callable, not a worker: no loop, no daemon, no thread and no schedule.
Whatever eventually runs recognition calls :func:`run_one_job` repeatedly; that
decision, and the service that makes it, belong to a later task.

One call is three phases with nothing held between them:

1. **Claim.** One short ``BEGIN IMMEDIATE`` transaction moves one due job to
   ``running`` under a lease unique to this claim, and commits.
2. **Work.** With no transaction open, the capture is re-read, its lifecycle
   re-checked and its media re-validated against retention's safety boundary;
   then the adapter runs. It may take minutes. Nothing else waits on it.
3. **Finish.** One short transaction records the result and the ``succeeded``
   state together, or records a bounded failure category -- and either write
   happens only if this worker still holds its claim.

If the process dies anywhere in phase 2 the job stays ``running`` until its
lease expires, and the next claim recovers it. Because a claim increments the
attempt count, a job that kills its worker every time still runs out of
attempts and ends ``failed``.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from functools import partial
from pathlib import Path
from types import MappingProxyType

from mgo.recognition.adapter import RecognitionAdapter
from mgo.recognition.eligibility import (
    MediaReference,
    decode_media_reference,
    media_refusal,
    resolve_capture_root,
)
from mgo.recognition.models import (
    DEFAULT_LEASE_DURATION,
    DEFAULT_RETRY_POLICY,
    RecognitionAdapterError,
    RecognitionConfigurationError,
    RecognitionErrorCategory,
    RecognitionJob,
    RecognitionJobState,
    RecognitionOutcome,
    RecognitionRequest,
    RecognitionResult,
    RetryPolicy,
)
from mgo.recognition.repository import RecognitionRepository

LOGGER = logging.getLogger(__name__)


class RunStatus(StrEnum):
    """What one call to :func:`run_one_job` did."""

    IDLE = "idle"
    SUCCEEDED = "succeeded"
    RETRY_SCHEDULED = "retry_scheduled"
    SKIPPED = "skipped"
    FAILED = "failed"
    CLAIM_LOST = "claim_lost"


_STATUS_FOR_STATE: Mapping[RecognitionJobState, RunStatus] = MappingProxyType(
    {
        RecognitionJobState.PENDING: RunStatus.RETRY_SCHEDULED,
        RecognitionJobState.SKIPPED: RunStatus.SKIPPED,
        RecognitionJobState.FAILED: RunStatus.FAILED,
    }
)


@dataclass(frozen=True)
class RunReport:
    """The outcome of one call. Identifiers and vocabularies only -- no paths."""

    status: RunStatus
    job_id: str | None = None
    capture_id: str | None = None
    attempt: int | None = None
    error_category: RecognitionErrorCategory | None = None
    outcome: RecognitionOutcome | None = None


def _claimed_media(
    repository: RecognitionRepository,
    job: RecognitionJob,
    capture_root: Path,
) -> MediaReference | RecognitionErrorCategory:
    """Re-check a claimed job's capture at run time.

    Reconciliation checked all of this once, but a queued job can wait a long
    time. Retention may have claimed or reclaimed the media since: any
    lifecycle row now means the media is not to be looked at, whether or not
    the file happens to still be there, and the job is skipped as
    ``media_missing`` rather than raced against the deletion.
    """
    capture = repository.read_capture(job.capture_id)
    if capture is None:
        # The foreign key makes this unreachable: a capture with a job cannot
        # be removed from the catalogue.
        return RecognitionErrorCategory.UNEXPECTED
    if capture.lifecycle_recorded:
        return RecognitionErrorCategory.MEDIA_MISSING
    media = decode_media_reference(capture)
    if media is None:
        return RecognitionErrorCategory.UNSAFE_PATH
    refusal = media_refusal(media, capture_root=capture_root)
    return media if refusal is None else refusal


def run_one_job(
    repository: RecognitionRepository,
    adapter: RecognitionAdapter,
    *,
    capture_directory: Path,
    worker_id: str,
    lease_duration: timedelta = DEFAULT_LEASE_DURATION,
    retry_policy: RetryPolicy = DEFAULT_RETRY_POLICY,
) -> RunReport:
    """Claim, validate, recognise and record at most one job.

    Raises :class:`RecognitionConfigurationError` before claiming anything if
    the capture directory is unusable, and lets
    :class:`~mgo.recognition.models.RecognitionRepositoryError` propagate: a
    database that cannot record the outcome leaves the claim to expire and be
    recovered, which is safer than guessing what was written.

    An exception from the adapter that is not a
    :class:`RecognitionAdapterError` is caught -- ``Exception`` only, never
    ``BaseException`` -- logged with its traceback, and recorded as
    ``unexpected``. That catch is the one place an arbitrary model failure is
    turned into a bounded category; an interrupt still stops the worker.
    """
    capture_root = resolve_capture_root(capture_directory)
    if capture_root is None:
        raise RecognitionConfigurationError(
            "The capture directory is not an absolute, existing directory"
        )

    job = repository.claim_next(
        pipeline_version=adapter.pipeline_version,
        worker_id=worker_id,
        lease_duration=lease_duration,
    )
    if job is None:
        return RunReport(status=RunStatus.IDLE)

    # The claim is committed. No transaction is open from here until the
    # finishing write.
    media = _claimed_media(repository, job, capture_root)
    if isinstance(media, RecognitionErrorCategory):
        return _finish_with_error(repository, job, media, retry_policy)

    request = RecognitionRequest(
        job_id=job.id,
        capture_id=job.capture_id,
        pipeline_version=job.pipeline_version,
        attempt=job.attempt_count,
        media_path=Path(media.absolute_path),
        expected_size_bytes=media.filesize_bytes,
        renew_lease=partial(repository.renew_lease, job, lease_duration),
    )

    try:
        result = adapter.recognise(request)
    except RecognitionAdapterError as exc:
        return _finish_with_error(repository, job, exc.category, retry_policy)
    except Exception:
        LOGGER.exception(
            "Recognition adapter failed unexpectedly on job %s attempt %s",
            job.id,
            job.attempt_count,
        )
        return _finish_with_error(
            repository, job, RecognitionErrorCategory.UNEXPECTED, retry_policy
        )

    if not isinstance(result, RecognitionResult):
        LOGGER.error(
            "Recognition adapter returned no result for job %s attempt %s",
            job.id,
            job.attempt_count,
        )
        return _finish_with_error(
            repository, job, RecognitionErrorCategory.UNEXPECTED, retry_policy
        )

    if not repository.complete_success(job, result):
        return _report(job, RunStatus.CLAIM_LOST)
    return _report(job, RunStatus.SUCCEEDED, outcome=result.outcome)


def _finish_with_error(
    repository: RecognitionRepository,
    job: RecognitionJob,
    category: RecognitionErrorCategory,
    retry_policy: RetryPolicy,
) -> RunReport:
    """Record a bounded failure and report what it did to the job."""
    state = repository.record_failure(job, category, retry_policy=retry_policy)
    if state is None:
        return _report(job, RunStatus.CLAIM_LOST, error_category=category)
    LOGGER.info(
        "Recognition job %s attempt %s ended %s (%s)",
        job.id,
        job.attempt_count,
        state.value,
        category.value,
    )
    return _report(job, _STATUS_FOR_STATE[state], error_category=category)


def _report(
    job: RecognitionJob,
    status: RunStatus,
    *,
    error_category: RecognitionErrorCategory | None = None,
    outcome: RecognitionOutcome | None = None,
) -> RunReport:
    return RunReport(
        status=status,
        job_id=job.id,
        capture_id=job.capture_id,
        attempt=job.attempt_count,
        error_category=error_category,
        outcome=outcome,
    )


__all__ = ["RunReport", "RunStatus", "run_one_job"]
