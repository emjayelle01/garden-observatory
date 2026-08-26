"""Typed value objects for capture-media retention.

Everything here is a small, immutable, JSON-serialisable value plus the one
mutable runtime-state holder the application owns for the life of the process.
None of it knows about FastAPI, SQLite, the filesystem or the camera -- it only
describes *what the policy decided*, *what a run concluded* and *what the
subsystem is currently doing*.

Three rules shape the shapes below:

* **Nothing raw is ever exposed.** A failure is reduced to one of a fixed set of
  categories, each with a fixed public message. Exception text, ``repr``,
  tracebacks, capture roots, database locations and absolute media paths go to
  the log and stay there.
* **A reason is a vocabulary word, not prose.** Policy reasons are three fixed
  values enforced by the database, so a lifecycle row can never carry an
  operator's free-text explanation of why something was deleted.
* **The absence of a lifecycle row means PRESENT.** The state vocabulary makes
  that explicit rather than leaving it as an implication of a ``LEFT JOIN``.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any


class MediaLifecycleState(StrEnum):
    """What has become of one catalogued capture's media on disk.

    * ``PRESENT`` -- no lifecycle row exists. The JPEG is expected on disk and
      the capture is a candidate for policy evaluation. This value is never
      stored: it *is* the absence of a row, which is what keeps a database with
      no retention history indistinguishable from one where retention has run
      and selected nothing.
    * ``PENDING_DELETE`` -- a durable statement of destructive intent, written
      and committed *before* the filesystem is touched. It is the only reason a
      crash between unlink and finalisation is recoverable rather than an
      unexplained missing file.
    * ``DELETED`` -- the media was reclaimed and the immutable observation
      recording that was committed in the same transaction.

    The historical ``captures`` row survives all three. Retention never deletes
    a capture record: a capture that happened goes on having happened.
    """

    PRESENT = "present"
    PENDING_DELETE = "pending_delete"
    DELETED = "deleted"


#: The two states actually stored in ``capture_media_lifecycle``, which the
#: table's own ``CHECK`` constraint enforces. ``PRESENT`` is deliberately absent:
#: storing it would turn "no row" into "one of two ways to say present".
STORED_LIFECYCLE_STATES = frozenset(
    {MediaLifecycleState.PENDING_DELETE, MediaLifecycleState.DELETED}
)


class RetentionReason(StrEnum):
    """Why the policy selected one capture. A closed, three-word vocabulary.

    ``AGE_AND_MANAGED_BYTES`` is not a hedge -- it is the deterministic answer
    for a capture that both rules independently reach: it is older than the age
    bound *and* falls inside the oldest span the byte bound alone would have had
    to reclaim. Recording only one of the two would misattribute the deletion,
    and recording free-form prose would put an operator's sentence into a column
    that decisions are later read back out of.
    """

    AGE = "age"
    MANAGED_BYTES = "managed_bytes"
    AGE_AND_MANAGED_BYTES = "age_and_managed_bytes"


class RetentionState(StrEnum):
    """The truthful states the retention subsystem can report.

    * ``DISABLED`` -- retention is off by configuration. Nothing may mutate.
    * ``IDLE`` -- enabled and not currently executing a run.
    * ``RUNNING`` -- one destructive run is executing now.
    * ``ERROR`` -- the most recent run stopped on a failure. A later successful
      run returns the state to ``IDLE``; nothing retries on its own, because
      nothing schedules a run at all.
    """

    DISABLED = "disabled"
    IDLE = "idle"
    RUNNING = "running"
    ERROR = "error"


class RetentionErrorCategory(StrEnum):
    """The failure categories a retention run can end in.

    Each one names a *recognised* condition. There is deliberately no category
    meaning "something to do with the filesystem": a failure that is not one of
    these is ``UNEXPECTED``, which is the honest answer rather than a guess
    dressed up as a diagnosis.
    """

    UNSAFE_PATH = "unsafe_path"
    MEDIA_MISSING = "media_missing"
    NOT_REGULAR_FILE = "not_regular_file"
    SIZE_MISMATCH = "size_mismatch"
    FILESYSTEM_DELETE_FAILED = "filesystem_delete_failed"
    DATABASE_TRANSITION_FAILED = "database_transition_failed"
    FINALIZATION_FAILED = "finalization_failed"
    CATALOGUE_INVALID = "catalogue_invalid"
    BUSY = "busy"
    UNEXPECTED = "unexpected"


#: The one public sentence each category is allowed to say. These are the only
#: strings that ever reach ``GET /retention/status`` or a retention observation.
#: They are fixed text, so no exception message, capture-root path, database
#: location, media filename or configuration value can ride out on them.
SAFE_ERROR_MESSAGES: dict[RetentionErrorCategory, str] = {
    RetentionErrorCategory.UNSAFE_PATH: (
        "A capture's media path failed retention safety validation."
    ),
    RetentionErrorCategory.MEDIA_MISSING: (
        "A capture's media is missing without a recorded deletion intent."
    ),
    RetentionErrorCategory.NOT_REGULAR_FILE: (
        "A capture's media target is not a regular file."
    ),
    RetentionErrorCategory.SIZE_MISMATCH: (
        "A capture's media size does not match its catalogue record."
    ),
    RetentionErrorCategory.FILESYSTEM_DELETE_FAILED: (
        "A capture's media could not be removed."
    ),
    RetentionErrorCategory.DATABASE_TRANSITION_FAILED: (
        "A retention lifecycle transition could not be recorded."
    ),
    RetentionErrorCategory.FINALIZATION_FAILED: (
        "A completed media deletion could not be finalised."
    ),
    RetentionErrorCategory.CATALOGUE_INVALID: (
        "The capture catalogue could not be read safely."
    ),
    RetentionErrorCategory.BUSY: (
        "A retention run is already in progress."
    ),
    RetentionErrorCategory.UNEXPECTED: (
        "The retention run failed unexpectedly."
    ),
}

#: Upper bound on any human-readable error string this package publishes. The
#: fixed messages above are far shorter; the bound exists so the contract holds
#: even if a future category is added carelessly.
_MAX_ERROR_LENGTH = 200


def safe_error_message(category: RetentionErrorCategory) -> str:
    """Return the bounded public message for ``category``."""
    return SAFE_ERROR_MESSAGES[category][:_MAX_ERROR_LENGTH]


@dataclass(frozen=True)
class CaptureLifecycleRecord:
    """One catalogued capture joined to whatever the lifecycle table says.

    This is the retention repository's purpose-specific projection, not a second
    :class:`~mgo.captures.models.Capture`. It carries exactly what the planner
    and the executor need and nothing else, so retention never becomes a reason
    to widen the capture archive's public API.

    ``origin`` is the parsed ``extra_metadata["origin"]`` value, or ``None`` when
    the key is absent or is not a string. Malformed *JSON* never reaches here at
    all -- the repository fails closed instead, because a catalogue that cannot
    be parsed is not a catalogue anything may be deleted on the strength of.
    """

    capture_id: str
    filename: str
    absolute_path: str
    captured_at_utc: datetime
    created_at_utc: datetime
    filesize_bytes: int
    origin: str | None
    lifecycle_state: MediaLifecycleState
    requested_at_utc: datetime | None
    deleted_at_utc: datetime | None
    reason: RetentionReason | None


@dataclass(frozen=True)
class RetentionCandidate:
    """One capture the policy selected, and the single reason it did."""

    capture_id: str
    filename: str
    absolute_path: str
    captured_at_utc: datetime
    filesize_bytes: int
    reason: RetentionReason


@dataclass(frozen=True)
class RetentionPlan:
    """What the policy would do, computed without touching the filesystem.

    A plan is a pure function of the catalogue, the configuration and the
    current instant. It is what a dry run returns and what a destructive run
    executes, so the two can never disagree about what was eligible.

    ``byte_target_satisfiable`` is the truthful answer to a question the policy
    is not allowed to force: when the preserved newest ``minimum_keep_count``
    captures alone exceed ``max_managed_bytes``, no permitted deletion reaches
    the target. The plan says so rather than violating the preservation floor to
    hit a number.
    """

    candidates: tuple[RetentionCandidate, ...]
    managed_present_count: int
    managed_present_bytes: int
    protected_count: int
    projected_bytes_reclaimed: int
    projected_managed_bytes: int
    more_work_remains: bool
    byte_target_satisfiable: bool

    def as_dict(self) -> dict[str, Any]:
        """Return the plan as JSON-compatible values. Not an operator projection.

        The absolute path is deliberately dropped: it is needed to *delete* a
        file and never needed to *describe* the decision. The raw catalogue
        ``filename`` is retained, for the domain model and for callers that
        already depend on it.

        That retained filename is why this is **not** an operator-safe or public
        projection, and must not be treated as one. It has been validated only
        as a non-empty string; it has not passed the destructive path/filename
        safety boundary, because that boundary belongs to execution rather than
        to description. A damaged or hand-edited catalogue can therefore hold a
        "filename" that is really a path -- Task 14.2 demonstrated absolute,
        traversal and Windows-style values surviving into output.

        The bounded operator projection is the ``mgo-retention`` command's own
        ``_operator_plan()``, which omits the filename. Anything else publishing
        a plan to an operator needs its own equivalent; this method is the
        domain rendering, not that.
        """
        return {
            "candidates": [
                {
                    "capture_id": candidate.capture_id,
                    "filename": candidate.filename,
                    "captured_at": candidate.captured_at_utc.isoformat(),
                    "filesize_bytes": candidate.filesize_bytes,
                    "policy_reason": candidate.reason.value,
                }
                for candidate in self.candidates
            ],
            "candidate_count": len(self.candidates),
            "managed_present_count": self.managed_present_count,
            "managed_present_bytes": self.managed_present_bytes,
            "protected_count": self.protected_count,
            "projected_bytes_reclaimed": self.projected_bytes_reclaimed,
            "projected_managed_bytes": self.projected_managed_bytes,
            "more_work_remains": self.more_work_remains,
            "byte_target_satisfiable": self.byte_target_satisfiable,
        }


@dataclass(frozen=True)
class RetentionRunResult:
    """The outcome of one destructive run, reduced to publishable facts.

    ``executed`` is ``False`` for the two ways a run can decline to start --
    retention is disabled, or another run holds the process-level lock -- so a
    caller can distinguish "ran and deleted nothing" from "never ran" without
    having to interpret the error category.
    """

    executed: bool
    enabled: bool
    candidate_count: int
    deleted_count: int
    bytes_reclaimed: int
    recovered_count: int
    more_work_remains: bool
    error_category: RetentionErrorCategory | None

    @property
    def error_message(self) -> str | None:
        """Return the bounded public message for the failure, if any."""
        if self.error_category is None:
            return None
        return safe_error_message(self.error_category)


@dataclass(frozen=True)
class RetentionStatus:
    """An immutable snapshot of the retention runtime state.

    Counters are *process-lifetime* only and are never persisted: they describe
    what this running process has done since it started. Restarting the service
    resets them, and the durable history remains the lifecycle table and the
    observation timeline -- which is much of the point of having a lifecycle
    table at all.
    """

    enabled: bool
    state: RetentionState
    total_runs: int
    total_captures_deleted: int
    total_bytes_reclaimed: int
    last_run_at: datetime | None
    last_run_candidate_count: int
    last_run_deleted_count: int
    last_run_bytes_reclaimed: int
    last_error: str | None

    def as_dict(self) -> dict[str, Any]:
        """Return the snapshot as JSON-compatible values.

        No filesystem path, no configuration value and no raw exception text
        appears here -- only counters, a bounded category message and timestamps
        rendered as ISO-8601 UTC, matching every other MGO status endpoint.
        """
        return {
            "enabled": self.enabled,
            "state": self.state.value,
            "total_runs": self.total_runs,
            "total_captures_deleted": self.total_captures_deleted,
            "total_bytes_reclaimed": self.total_bytes_reclaimed,
            "last_run_at": _isoformat(self.last_run_at),
            "last_run_candidate_count": self.last_run_candidate_count,
            "last_run_deleted_count": self.last_run_deleted_count,
            "last_run_bytes_reclaimed": self.last_run_bytes_reclaimed,
            "last_error": self.last_error,
        }


def _isoformat(value: datetime | None) -> str | None:
    """Render an optional timestamp the way every MGO endpoint renders one."""
    return value.isoformat() if value is not None else None


class RetentionRuntimeState:
    """Holds the live retention state for the life of the process.

    Unlike :class:`~mgo.event_capture.models.EventCaptureRuntimeState`, this one
    takes a lock. That is not defensiveness for its own sake: a retention run is
    *synchronous, blocking* work -- SQLite transactions and ``unlink`` calls --
    so it is expected to be driven from a worker thread while the status
    endpoint reads the same holder from the event loop. Counters updated without
    a lock across two threads are a torn read waiting for the one moment an
    operator is watching.

    A holder constructed with ``enabled=False`` starts -- and stays -- in
    ``DISABLED``: nothing may move it, because a disabled subsystem reporting
    ``running`` would be claiming a mutation that cannot happen.
    """

    def __init__(self, *, enabled: bool) -> None:
        self._lock = threading.Lock()
        self._enabled = enabled
        self._state = (
            RetentionState.IDLE if enabled else RetentionState.DISABLED
        )
        self._total_runs = 0
        self._total_captures_deleted = 0
        self._total_bytes_reclaimed = 0
        self._last_run_at: datetime | None = None
        self._last_run_candidate_count = 0
        self._last_run_deleted_count = 0
        self._last_run_bytes_reclaimed = 0
        self._last_error: str | None = None

    @property
    def enabled(self) -> bool:
        """Whether retention is enabled by configuration."""
        return self._enabled

    def mark_running(self) -> None:
        """Enter ``RUNNING``. Ignored entirely while disabled."""
        with self._lock:
            if not self._enabled:
                return
            self._state = RetentionState.RUNNING

    def record_run(
        self,
        *,
        completed_at: datetime,
        candidate_count: int,
        deleted_count: int,
        bytes_reclaimed: int,
        error: str | None,
    ) -> None:
        """Fold one finished run into the process-lifetime counters.

        ``error`` is already a bounded category message when present; this
        holder never sees an exception and so can never publish one. A run that
        deleted some captures *and then* failed contributes both its successful
        counters and its error: stopping on the first failure does not un-delete
        what was already reclaimed, and the status must not imply that it did.
        """
        with self._lock:
            if not self._enabled:
                return
            self._total_runs += 1
            self._total_captures_deleted += deleted_count
            self._total_bytes_reclaimed += bytes_reclaimed
            self._last_run_at = completed_at
            self._last_run_candidate_count = candidate_count
            self._last_run_deleted_count = deleted_count
            self._last_run_bytes_reclaimed = bytes_reclaimed
            self._last_error = error
            self._state = (
                RetentionState.ERROR
                if error is not None
                else RetentionState.IDLE
            )

    def snapshot(self) -> RetentionStatus:
        """Return an immutable copy of the current state. Side-effect free."""
        with self._lock:
            return RetentionStatus(
                enabled=self._enabled,
                state=self._state,
                total_runs=self._total_runs,
                total_captures_deleted=self._total_captures_deleted,
                total_bytes_reclaimed=self._total_bytes_reclaimed,
                last_run_at=self._last_run_at,
                last_run_candidate_count=self._last_run_candidate_count,
                last_run_deleted_count=self._last_run_deleted_count,
                last_run_bytes_reclaimed=self._last_run_bytes_reclaimed,
                last_error=self._last_error,
            )


__all__ = [
    "SAFE_ERROR_MESSAGES",
    "STORED_LIFECYCLE_STATES",
    "CaptureLifecycleRecord",
    "MediaLifecycleState",
    "RetentionCandidate",
    "RetentionErrorCategory",
    "RetentionPlan",
    "RetentionReason",
    "RetentionRunResult",
    "RetentionRuntimeState",
    "RetentionState",
    "RetentionStatus",
    "safe_error_message",
]
