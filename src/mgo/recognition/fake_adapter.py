"""A deterministic stand-in for a recognition pipeline. Development and tests only.

It exists so the queue, the leases and the transaction boundaries can be proven
before any model exists. It is not wired into configuration, the API or any
service, and nothing in the application constructs it.

It never opens the media, never decodes a pixel, imports no imaging or model
library and touches no network. Its outcome is a pure function of the pipeline
version and the capture id, so the same job always produces the same result.
Its provenance is honest about what it is: identities named ``mgo-fake-*``
with digests of those names, and no image dimensions, because it never looked.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable, Mapping

from mgo.recognition.models import (
    RecognitionAdapterError,
    RecognitionErrorCategory,
    RecognitionOutcome,
    RecognitionRequest,
    RecognitionResult,
    validate_identifier,
)

FAKE_PIPELINE_VERSION = "fake-0"

FAKE_DETECTOR_ID = "mgo-fake-detector"
FAKE_CLASSIFIER_ID = "mgo-fake-classifier"
FAKE_LABEL_SET_ID = "mgo-fake-labels"
FAKE_PREPROCESSING_VERSION = "fake-none"
FAKE_THRESHOLDS_VERSION = "fake-none"


def _digest(name: str) -> str:
    return hashlib.sha256(name.encode("utf-8")).hexdigest()


def synthetic_outcome(capture_id: str, pipeline_version: str) -> RecognitionOutcome:
    """Return the fixed outcome the fake pipeline gives this capture."""
    digest = hashlib.sha256(f"{pipeline_version}\x00{capture_id}".encode()).digest()
    outcomes = tuple(RecognitionOutcome)
    return outcomes[digest[0] % len(outcomes)]


class SimulatedAdapterCrash(RuntimeError):
    """An exception no adapter contract anticipates, for crash-path tests."""


class FakeRecognitionAdapter:
    """A configurable, deterministic :class:`RecognitionAdapter`.

    ``failures`` makes an attempt on a capture raise
    :class:`RecognitionAdapterError` with that category. ``crashes`` makes it
    raise :class:`SimulatedAdapterCrash` instead. ``before_result`` runs first
    on every attempt, which is where a test removes media, adds a lifecycle row,
    probes for an open transaction or interrupts the worker outright.
    """

    def __init__(
        self,
        *,
        pipeline_version: str = FAKE_PIPELINE_VERSION,
        failures: Mapping[str, RecognitionErrorCategory] | None = None,
        crashes: Iterable[str] = (),
        before_result: Callable[[RecognitionRequest], None] | None = None,
    ) -> None:
        self._pipeline_version = validate_identifier(
            pipeline_version, "pipeline_version"
        )
        self._failures = dict(failures or {})
        self._crashes = frozenset(crashes)
        self._before_result = before_result
        #: Job ids in call order: which attempts actually reached the adapter.
        self.calls: list[str] = []

    @property
    def pipeline_version(self) -> str:
        return self._pipeline_version

    def recognise(self, request: RecognitionRequest) -> RecognitionResult:
        self.calls.append(request.job_id)
        if self._before_result is not None:
            self._before_result(request)
        if request.capture_id in self._crashes:
            raise SimulatedAdapterCrash("simulated adapter crash")
        category = self._failures.get(request.capture_id)
        if category is not None:
            raise RecognitionAdapterError(category)
        return RecognitionResult(
            outcome=synthetic_outcome(request.capture_id, self._pipeline_version),
            detector_model_id=FAKE_DETECTOR_ID,
            detector_model_sha256=_digest(FAKE_DETECTOR_ID),
            classifier_model_id=FAKE_CLASSIFIER_ID,
            classifier_model_sha256=_digest(FAKE_CLASSIFIER_ID),
            label_set_id=FAKE_LABEL_SET_ID,
            label_set_sha256=_digest(FAKE_LABEL_SET_ID),
            preprocessing_version=FAKE_PREPROCESSING_VERSION,
            thresholds_version=FAKE_THRESHOLDS_VERSION,
            inference_duration_ms=0,
        )


__all__ = [
    "FAKE_PIPELINE_VERSION",
    "FakeRecognitionAdapter",
    "SimulatedAdapterCrash",
    "synthetic_outcome",
]
