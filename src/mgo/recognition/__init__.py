"""The model-independent foundation for species recognition (Task 15.1).

Recognition begins only after a capture has been durably published to the
capture catalogue, and it observes that catalogue from outside. It is not a
step in the capture workflow, event capture, the capture archive, the camera
transaction, retention or backup, takes none of their locks, and never deletes,
renames, edits or retains media. A capture is exactly as published whether
recognition ever looks at it or not.

What exists here is the durable machinery a real pipeline will run inside:

* :mod:`mgo.recognition.models` -- the job-state, error and outcome
  vocabularies, the claim and result values, and the retry policy;
* :mod:`mgo.recognition.eligibility` -- catalogue-driven eligibility, reusing
  retention's media safety boundary rather than re-implementing it;
* :mod:`mgo.recognition.repository` -- the job queue: enqueue, atomic claim,
  lease renewal, and conditional completion;
* :mod:`mgo.recognition.reconciler` -- idempotent creation of pending jobs;
* :mod:`mgo.recognition.adapter` -- the adapter protocol and the safe media
  open a real adapter must use;
* :mod:`mgo.recognition.runner` -- process at most one job;
* :mod:`mgo.recognition.fake_adapter` -- a deterministic development and test
  stand-in, deliberately not exported here and not wired to anything.

There is no model, no classifier, no pixel decoding, no service, no loop, no
API and no configuration. See ``docs/Recognition.md``.
"""

from __future__ import annotations

from mgo.recognition.adapter import RecognitionAdapter, open_media
from mgo.recognition.eligibility import (
    CatalogueCapture,
    IneligibilityReason,
    evaluate_eligibility,
    resolve_capture_root,
)
from mgo.recognition.models import (
    DEFAULT_LEASE_DURATION,
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_RETRY_POLICY,
    ERROR_DISPOSITION,
    TERMINAL_JOB_STATES,
    RecognitionAdapterError,
    RecognitionConfigurationError,
    RecognitionError,
    RecognitionErrorCategory,
    RecognitionJob,
    RecognitionJobState,
    RecognitionOutcome,
    RecognitionRepositoryError,
    RecognitionRequest,
    RecognitionResult,
    RetryPolicy,
)
from mgo.recognition.reconciler import RecognitionReconciler, ReconcileReport
from mgo.recognition.repository import RECOGNITION_TABLES, RecognitionRepository
from mgo.recognition.runner import RunReport, RunStatus, run_one_job

__all__ = [
    "DEFAULT_LEASE_DURATION",
    "DEFAULT_MAX_ATTEMPTS",
    "DEFAULT_RETRY_POLICY",
    "ERROR_DISPOSITION",
    "RECOGNITION_TABLES",
    "TERMINAL_JOB_STATES",
    "CatalogueCapture",
    "IneligibilityReason",
    "RecognitionAdapter",
    "RecognitionAdapterError",
    "RecognitionConfigurationError",
    "RecognitionError",
    "RecognitionErrorCategory",
    "RecognitionJob",
    "RecognitionJobState",
    "RecognitionOutcome",
    "RecognitionReconciler",
    "RecognitionRepository",
    "RecognitionRepositoryError",
    "RecognitionRequest",
    "RecognitionResult",
    "ReconcileReport",
    "RetryPolicy",
    "RunReport",
    "RunStatus",
    "evaluate_eligibility",
    "open_media",
    "resolve_capture_root",
    "run_one_job",
]
