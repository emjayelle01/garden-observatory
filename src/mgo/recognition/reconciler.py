"""Turning committed catalogue rows into pending recognition jobs.

Reconciliation is the only way work enters the queue. It reads the capture
catalogue *after* publication, from outside the capture workflow, so nothing it
does can slow, fail, reorder or gate a capture: a capture that is published and
never reconciled is exactly as published as one that is.

It is idempotent, safe to repeat and safe to run concurrently with itself:

* the read excludes captures that already have a job for this pipeline version;
* the insert re-checks the lifecycle table inside its own transaction;
* the database's ``UNIQUE (capture_id, pipeline_version)`` constraint, with a
  ``DO NOTHING`` conflict clause, is the final word -- however two reconcilers
  interleave, a capture gets one job per pipeline version.

Filesystem checks run between the read and the insert, with no transaction
open. The read and each insert batch are separate short transactions.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from types import MappingProxyType

from mgo.recognition.eligibility import (
    IneligibilityReason,
    evaluate_eligibility,
    resolve_capture_root,
)
from mgo.recognition.models import (
    DEFAULT_MAX_ATTEMPTS,
    RecognitionConfigurationError,
    validate_identifier,
    validate_max_attempts,
)
from mgo.recognition.repository import RecognitionRepository

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReconcileReport:
    """What one reconciliation pass found and did. Carries no paths."""

    examined: int
    eligible: int
    created: int
    ineligible: Mapping[IneligibilityReason, int] = field(
        default_factory=lambda: MappingProxyType({})
    )


class RecognitionReconciler:
    """Creates one pending job per eligible capture for one pipeline version."""

    def __init__(
        self,
        repository: RecognitionRepository,
        *,
        pipeline_version: str,
        capture_directory: Path,
        enrolment_watermark: datetime,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    ) -> None:
        if enrolment_watermark.tzinfo is None:
            raise RecognitionConfigurationError(
                "The recognition enrolment watermark must be timezone-aware"
            )
        self._repository = repository
        self._pipeline_version = validate_identifier(
            pipeline_version, "pipeline_version"
        )
        self._capture_directory = capture_directory
        self._watermark = enrolment_watermark
        self._max_attempts = validate_max_attempts(max_attempts)

    def reconcile(self) -> ReconcileReport:
        """Run one pass. Raises before reading anything if the root is unusable."""
        capture_root = resolve_capture_root(self._capture_directory)
        if capture_root is None:
            raise RecognitionConfigurationError(
                "The capture directory is not an absolute, existing directory"
            )

        captures = self._repository.list_unqueued_captures(self._pipeline_version)

        eligible: list[str] = []
        ineligible: Counter[IneligibilityReason] = Counter()
        for capture in captures:
            reason = evaluate_eligibility(
                capture,
                capture_root=capture_root,
                enrolment_watermark=self._watermark,
            )
            if reason is None:
                eligible.append(capture.capture_id)
            else:
                ineligible[reason] += 1

        created = _enqueue(
            self._repository,
            eligible,
            pipeline_version=self._pipeline_version,
            max_attempts=self._max_attempts,
        )

        if created:
            LOGGER.info(
                "Queued %s recognition job(s) for pipeline %s",
                created,
                self._pipeline_version,
            )
        return ReconcileReport(
            examined=len(captures),
            eligible=len(eligible),
            created=created,
            ineligible=MappingProxyType(dict(ineligible)),
        )


def _enqueue(
    repository: RecognitionRepository,
    capture_ids: list[str],
    *,
    pipeline_version: str,
    max_attempts: int,
) -> int:
    """Insert the eligible jobs.

    A module-level seam so a test can run a competing reconciler, or commit a
    lifecycle row, between the read and the insert.
    """
    if not capture_ids:
        return 0
    return repository.enqueue(
        capture_ids, pipeline_version=pipeline_version, max_attempts=max_attempts
    )


__all__ = ["RecognitionReconciler", "ReconcileReport"]
