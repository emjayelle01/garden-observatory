"""Recognition vocabularies and the values that cross the adapter boundary.

Two vocabularies are kept apart on purpose. :class:`RecognitionJobState` says
what happened to the *work*: queued, running, finished, given up on.
:class:`RecognitionOutcome` says what a finished piece of work *concluded*
about the picture. A job that failed has a state and no outcome; a job that
succeeded has both. Folding them together is how "the model crashed" ends up
counted as "no bird".

Every vocabulary here is mirrored by a ``CHECK`` constraint in
``migrations/004_recognition_jobs.sql``, so a value this module does not know
cannot be stored by anything else either.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType


class RecognitionJobState(StrEnum):
    """Where one recognition job stands. Work state, never biology."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    SUPERSEDED = "superseded"


#: States a job never leaves. ``superseded`` is reserved by the schema for a
#: later reprocessing campaign; nothing in Task 15.1 writes it.
TERMINAL_JOB_STATES: frozenset[RecognitionJobState] = frozenset(
    {
        RecognitionJobState.SUCCEEDED,
        RecognitionJobState.FAILED,
        RecognitionJobState.SKIPPED,
        RecognitionJobState.SUPERSEDED,
    }
)


class RecognitionErrorCategory(StrEnum):
    """The bounded reasons an attempt can end without a result.

    Only these values are ever persisted. A raw exception message or a media
    path is never stored: an exception that is not one of the recognised
    conditions is ``UNEXPECTED``, which is the honest answer rather than a
    diagnosis invented from its text.
    """

    MEDIA_MISSING = "media_missing"
    UNSAFE_PATH = "unsafe_path"
    SIZE_MISMATCH = "size_mismatch"
    DECODE_ERROR = "decode_error"
    MODEL_UNAVAILABLE = "model_unavailable"
    TIMEOUT = "timeout"
    RESOURCE_LIMIT = "resource_limit"
    UNEXPECTED = "unexpected"


#: What a failure in each category does to its job.
#:
#: A state is terminal at once. ``None`` means the attempt may be retried, until
#: the job has used ``max_attempts`` and becomes terminal ``failed`` instead.
#:
#: ``media_missing`` is a *skip* rather than a failure because it is an expected
#: consequence of retention reclaiming media, not something wrong: once retention
#: has claimed or reclaimed media it is not recognition's to look at, so retrying
#: could only spend attempts. (A ``pending_delete`` intent that retention later
#: cancels leaves the job skipped; requeueing that capture is a deliberate,
#: separate action -- see ``docs/Recognition.md`` section 10.) The other
#: terminal categories describe media that exists but must not or cannot be
#: read -- an unsafe target, a file that is not the catalogued one, bytes that
#: do not decode -- and retrying reads the same bytes again, so they fail at
#: once and wait for a human. The retryable categories describe the pipeline
#: rather than the media, and a later attempt can genuinely differ.
#:
#: ``unexpected`` is retryable, bounded by ``max_attempts`` like every other
#: retryable category: an unrecognised exception is as likely to be transient as
#: permanent, and the bound is what stops a poison job retrying forever.
ERROR_DISPOSITION: Mapping[RecognitionErrorCategory, RecognitionJobState | None] = (
    MappingProxyType(
        {
            RecognitionErrorCategory.MEDIA_MISSING: RecognitionJobState.SKIPPED,
            RecognitionErrorCategory.UNSAFE_PATH: RecognitionJobState.FAILED,
            RecognitionErrorCategory.SIZE_MISMATCH: RecognitionJobState.FAILED,
            RecognitionErrorCategory.DECODE_ERROR: RecognitionJobState.FAILED,
            RecognitionErrorCategory.MODEL_UNAVAILABLE: None,
            RecognitionErrorCategory.TIMEOUT: None,
            RecognitionErrorCategory.RESOURCE_LIMIT: None,
            RecognitionErrorCategory.UNEXPECTED: None,
        }
    )
)


class RecognitionOutcome(StrEnum):
    """What a successfully completed attempt concluded about its capture."""

    SPECIES = "species"
    UNCERTAIN = "uncertain"
    UNKNOWN_SPECIES = "unknown_species"
    NO_BIRD = "no_bird"
    PERSON_PRESENT_ONLY = "person_present_only"


#: Defaults for the queue. Deliberately modest: a Raspberry Pi 5 recognising one
#: capture at a time needs minutes of lease, not seconds, and a handful of
#: attempts is enough to ride out a transient failure without letting a poison
#: capture occupy the queue.
DEFAULT_MAX_ATTEMPTS = 3
MAX_MAX_ATTEMPTS = 100
DEFAULT_LEASE_DURATION = timedelta(minutes=10)

#: Identifiers stored in bounded columns: pipeline versions and worker ids.
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:+-]{0,63}")

_SHA256 = re.compile(r"[0-9a-f]{64}")


def validate_identifier(value: object, label: str) -> str:
    """Return ``value`` if it is a bounded, printable identifier.

    Pipeline versions and worker ids end up in ``CHECK``-bounded columns and in
    log lines. Refusing anything outside a small alphabet here keeps both free
    of whitespace, control characters and path separators.
    """
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError(
            f"{label} must be 1-64 characters: letters, digits and . _ : + -"
        )
    return value


def validate_max_attempts(value: object) -> int:
    """Return ``value`` if it is a usable attempt bound."""
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= MAX_MAX_ATTEMPTS
    ):
        raise ValueError(
            f"max_attempts must be an integer from 1 to {MAX_MAX_ATTEMPTS}"
        )
    return value


def validate_lease_duration(value: timedelta) -> timedelta:
    """Return ``value`` if it is a positive lease length."""
    if value <= timedelta(0):
        raise ValueError("A recognition lease duration must be positive")
    return value


@dataclass(frozen=True)
class RetryPolicy:
    """How long a retryable failure waits before its next attempt.

    Exponential from ``base_delay`` and capped at ``max_delay``, so repeated
    model-unavailable failures back off instead of spinning, without a single
    job ever waiting longer than the cap between attempts.
    """

    base_delay: timedelta = timedelta(minutes=1)
    max_delay: timedelta = timedelta(hours=1)

    def __post_init__(self) -> None:
        if self.base_delay <= timedelta(0):
            raise ValueError("RetryPolicy.base_delay must be positive")
        if self.max_delay < self.base_delay:
            raise ValueError("RetryPolicy.max_delay must not be below base_delay")

    def delay_after(self, attempt_count: int) -> timedelta:
        """Return the wait after the ``attempt_count``-th attempt failed."""
        exponent = min(max(attempt_count - 1, 0), 20)
        return min(self.base_delay * (1 << exponent), self.max_delay)


DEFAULT_RETRY_POLICY = RetryPolicy()


class RecognitionError(RuntimeError):
    """Base class for recognition failures raised to callers."""


class RecognitionConfigurationError(RecognitionError):
    """Raised when recognition is asked to run against an unusable setting."""


class RecognitionRepositoryError(RecognitionError):
    """Raised when a recognition database operation cannot be completed."""


class RecognitionAdapterError(RecognitionError):
    """Raised by an adapter to end an attempt with a recognised category.

    The message is fixed text derived from the category alone, so an adapter
    cannot smuggle a path or a library's exception text through it.
    """

    def __init__(self, category: RecognitionErrorCategory) -> None:
        super().__init__(f"Recognition attempt failed: {category.value}")
        self.category = category


@dataclass(frozen=True)
class RecognitionJob:
    """A job exactly as this worker claimed it.

    ``lease_owner`` and ``attempt_count`` together identify *this* claim. Every
    later write about the job -- a heartbeat, a result, a failure -- must match
    both, so a worker whose lease expired and was taken over can never write
    over the worker that took it.
    """

    id: str
    capture_id: str
    pipeline_version: str
    attempt_count: int
    max_attempts: int
    lease_owner: str
    lease_expires_at: datetime


@dataclass(frozen=True)
class RecognitionRequest:
    """What an adapter is given for one attempt.

    ``media_path`` has already passed the catalogue safety boundary when the
    adapter receives it, but the file can still change afterwards: an adapter
    that reads it must open it with :func:`mgo.recognition.adapter.open_media`,
    which refuses a final-component symlink and re-checks type and size on the
    open descriptor.

    ``renew_lease`` extends the claim and returns ``False`` once the claim has
    been lost; an adapter doing long work calls it and stops when it fails.

    The request is internal to the worker. It carries an absolute path and is
    never a shape for an API response.
    """

    job_id: str
    capture_id: str
    pipeline_version: str
    attempt: int
    media_path: Path
    expected_size_bytes: int
    renew_lease: Callable[[], bool] = field(repr=False, compare=False)


#: SQLite's INTEGER storage bound. A larger Python int cannot be bound at all,
#: and the driver's ``OverflowError`` is not a ``sqlite3.Error``.
_MAX_STORED_INTEGER = 2**63 - 1


def _check_text(value: str | None, label: str, max_length: int) -> None:
    if value is None:
        return
    if not isinstance(value, str) or not 1 <= len(value) <= max_length:
        raise ValueError(f"{label} must be 1-{max_length} characters")
    # SQLite's length() stops at a NUL, so the schema's bound cannot see one.
    if "\x00" in value:
        raise ValueError(f"{label} must not contain a NUL character")


def _check_sha256(value: str | None, label: str) -> None:
    if value is None:
        return
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{label} must be 64 lowercase hexadecimal characters")


def _check_pair(identity: object, digest: object, label: str) -> None:
    if (identity is None) != (digest is None):
        raise ValueError(f"{label} identity and digest must be given together")


def _check_count(value: int | None, label: str, *, minimum: int) -> None:
    if value is None:
        return
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= _MAX_STORED_INTEGER
    ):
        raise ValueError(
            f"{label} must be an integer from {minimum} to {_MAX_STORED_INTEGER}"
        )


@dataclass(frozen=True)
class RecognitionResult:
    """A successful attempt's conclusion and the provenance behind it.

    Provenance is optional field by field because a development adapter, or a
    pipeline stage that does not exist yet, cannot honestly supply it -- and a
    made-up digest is worse than a missing one. Identities and digests are
    paired, as they are in the schema.
    """

    outcome: RecognitionOutcome
    detector_model_id: str | None = None
    detector_model_sha256: str | None = None
    classifier_model_id: str | None = None
    classifier_model_sha256: str | None = None
    label_set_id: str | None = None
    label_set_sha256: str | None = None
    taxonomy_id: str | None = None
    taxonomy_version: str | None = None
    preprocessing_version: str | None = None
    thresholds_version: str | None = None
    inference_duration_ms: int | None = None
    peak_rss_bytes: int | None = None
    cpu_time_ms: int | None = None
    image_width: int | None = None
    image_height: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, RecognitionOutcome):
            raise ValueError("outcome must be a RecognitionOutcome")
        for label, identity, digest in (
            ("detector_model", self.detector_model_id, self.detector_model_sha256),
            (
                "classifier_model",
                self.classifier_model_id,
                self.classifier_model_sha256,
            ),
            ("label_set", self.label_set_id, self.label_set_sha256),
        ):
            _check_text(identity, f"{label}_id", 128)
            _check_sha256(digest, f"{label}_sha256")
            _check_pair(identity, digest, label)
        _check_text(self.taxonomy_id, "taxonomy_id", 128)
        _check_text(self.taxonomy_version, "taxonomy_version", 64)
        _check_pair(self.taxonomy_id, self.taxonomy_version, "taxonomy")
        _check_text(self.preprocessing_version, "preprocessing_version", 64)
        _check_text(self.thresholds_version, "thresholds_version", 64)
        _check_count(self.inference_duration_ms, "inference_duration_ms", minimum=0)
        _check_count(self.peak_rss_bytes, "peak_rss_bytes", minimum=0)
        _check_count(self.cpu_time_ms, "cpu_time_ms", minimum=0)
        _check_count(self.image_width, "image_width", minimum=1)
        _check_count(self.image_height, "image_height", minimum=1)
        _check_pair(self.image_width, self.image_height, "image dimensions")


__all__ = [
    "DEFAULT_LEASE_DURATION",
    "DEFAULT_MAX_ATTEMPTS",
    "DEFAULT_RETRY_POLICY",
    "ERROR_DISPOSITION",
    "MAX_MAX_ATTEMPTS",
    "TERMINAL_JOB_STATES",
    "RecognitionAdapterError",
    "RecognitionConfigurationError",
    "RecognitionError",
    "RecognitionErrorCategory",
    "RecognitionJob",
    "RecognitionJobState",
    "RecognitionOutcome",
    "RecognitionRepositoryError",
    "RecognitionRequest",
    "RecognitionResult",
    "RetryPolicy",
    "validate_identifier",
    "validate_lease_duration",
    "validate_max_attempts",
]
