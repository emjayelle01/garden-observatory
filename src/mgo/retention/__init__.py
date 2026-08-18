"""Capture-media retention for Matt's Garden Observatory.

Task 13 proved MGO can perform the whole automatic-capture transaction against
real hardware. It stays disabled in production for one reason: the media has no
lifecycle. During the bounded Task 13.2 validation window, ambient garden motion
alone produced 17 captures and 42,660,151 bytes of JPEG with nothing to reclaim
any of it. That is a point-in-time measurement rather than a long-term rate, and
it is already enough to show that an unattended capture pipeline cannot be
switched on responsibly while captured media accumulates without bound.

This package is the software foundation for reclaiming that media *safely*. It
is deliberately not the feature that reclaims it in production:

* retention is **disabled by default**, and disabled means no filesystem
  mutation and no database mutation of any kind, including no recovery;
* **nothing schedules it** -- there is no timer, no interval and no loop;
* **nothing exposes it destructively** -- there is a read-only status endpoint
  and no ``POST`` that deletes;
* only ``origin = "motion"`` captures are managed. Manual captures, captures
  with no origin and captures from any origin this build does not recognise are
  protected;
* the historical ``captures`` catalogue is never deleted from. Reclaiming a JPEG
  does not un-happen the capture that produced it, so media lifecycle lives in
  its own table and a capture record survives its media.

The modules:

* :mod:`mgo.retention.models` -- the state vocabularies, the plan and result
  values, the runtime-state holder and the fixed safe error messages;
* :mod:`mgo.retention.policy` -- the deterministic, filesystem-free planner;
* :mod:`mgo.retention.repository` -- the retention projection and the
  conditional lifecycle transitions;
* :mod:`mgo.retention.service` -- the safety boundary, the recoverable deletion
  state machine and the bounded destructive run.

Task 14.1 does not authorise production retention, does not enable event capture
and does not make the capture pipeline ready for permanent unattended operation.
See ``docs/Retention.md``.
"""

from __future__ import annotations

from mgo.retention.models import (
    SAFE_ERROR_MESSAGES,
    STORED_LIFECYCLE_STATES,
    CaptureLifecycleRecord,
    MediaLifecycleState,
    RetentionCandidate,
    RetentionErrorCategory,
    RetentionPlan,
    RetentionReason,
    RetentionRunResult,
    RetentionRuntimeState,
    RetentionState,
    RetentionStatus,
    safe_error_message,
)
from mgo.retention.policy import MANAGED_ORIGIN, is_managed, plan_retention
from mgo.retention.repository import (
    RetentionCatalogueError,
    RetentionRepository,
    RetentionRepositoryError,
)
from mgo.retention.service import (
    FAILURE_STATUS,
    FAILURE_SUMMARY,
    OBSERVATION_KIND,
    OBSERVATION_SOURCE,
    SUCCESS_STATUS,
    SUCCESS_SUMMARY,
    RetentionDisabledError,
    RetentionService,
    validate_media_file,
    validate_media_path,
)

__all__ = [
    "FAILURE_STATUS",
    "FAILURE_SUMMARY",
    "MANAGED_ORIGIN",
    "OBSERVATION_KIND",
    "OBSERVATION_SOURCE",
    "SAFE_ERROR_MESSAGES",
    "STORED_LIFECYCLE_STATES",
    "SUCCESS_STATUS",
    "SUCCESS_SUMMARY",
    "CaptureLifecycleRecord",
    "MediaLifecycleState",
    "RetentionCandidate",
    "RetentionCatalogueError",
    "RetentionDisabledError",
    "RetentionErrorCategory",
    "RetentionPlan",
    "RetentionReason",
    "RetentionRepository",
    "RetentionRepositoryError",
    "RetentionRunResult",
    "RetentionRuntimeState",
    "RetentionService",
    "RetentionState",
    "RetentionStatus",
    "is_managed",
    "plan_retention",
    "safe_error_message",
    "validate_media_file",
    "validate_media_path",
]
