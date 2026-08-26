"""The retention domain service: the executor, its safety boundary and its runs.

This is where a plan becomes a deleted file. Everything destructive in MGO's
retention subsystem happens here and nowhere else, so the module is written to
be read by someone deciding whether to trust it with an ``unlink``.

Four rules shape it:

* **The catalogue nominates; the filesystem only vetoes.** A candidate's
  identity comes from a database row. Every filesystem check below can refuse a
  deletion and none can propose one. Nothing here ever lists a directory and
  decides that what it finds looks disposable.
* **Intent is durable before the file is touched.** Filesystem deletion and
  SQLite cannot share a transaction, so a committed ``pending_delete`` row is
  written first. That row is the difference between a recoverable interruption
  and an unexplained missing file.
* **One attempt, then stop.** A capture gets one deletion attempt per run, and
  the first destructive failure ends the run. Deletions already completed stand;
  candidates not yet attempted are left entirely alone.
* **Disabled means disabled.** With ``retention.enabled = false`` no lifecycle
  row is written, no observation is recorded, no pending intent is recovered and
  no file is removed. Not "nothing is planned" -- nothing is *mutated*.

There is deliberately no scheduler, no timer and no interval. Task 14.1 builds
the service; what invokes it operationally is a later decision, and pre-empting
it here would ship an unattended deletion loop nobody has approved.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mgo.core.config import RetentionConfig
from mgo.core.observations import Observation, record_observation
from mgo.retention.models import (
    CaptureLifecycleRecord,
    MediaLifecycleState,
    RetentionCandidate,
    RetentionErrorCategory,
    RetentionPlan,
    RetentionReason,
    RetentionRunResult,
    RetentionRuntimeState,
    safe_error_message,
)
from mgo.retention.policy import plan_retention
from mgo.retention.repository import (
    RetentionCatalogueError,
    RetentionRepository,
    RetentionRepositoryError,
)

LOGGER = logging.getLogger(__name__)

Clock = Callable[[], datetime]
ObservationRecorder = Callable[..., Observation]

#: The observation vocabulary this subsystem owns. One kind, one source, two
#: statuses -- reusing the existing immutable timeline rather than inventing a
#: deletion log beside it.
OBSERVATION_KIND = "capture_retention"
OBSERVATION_SOURCE = "mgo-retention"
SUCCESS_STATUS = "reclaimed"
SUCCESS_SUMMARY = "Capture media removed by retention policy"
FAILURE_STATUS = "failed"
FAILURE_SUMMARY = "Capture media retention failed"


# -- the filesystem seams ----------------------------------------------------
#
# Every filesystem question the safety boundary asks goes through one of these
# one-line helpers. That is not indirection for its own sake: it lets a test
# prove the *refusal* for a condition the host cannot always create -- a symlink
# on a Windows account without the privilege, a file whose size changes between
# the check and the unlink -- without adding a platform skip that quietly stops
# testing the rule on the developer machine where the code is written.


def _realpath(path: Path) -> Path:
    """Resolve a path fully, following symlinks, without requiring existence."""
    return Path(os.path.realpath(path))


def _is_symlink(path: Path) -> bool:
    """Return whether ``path`` itself is a symbolic link."""
    return path.is_symlink()


def _path_exists(path: Path) -> bool:
    """Return whether ``path`` exists without following a final symlink."""
    return os.path.lexists(path)


def _is_directory(path: Path) -> bool:
    """Return whether ``path`` is a directory."""
    return path.is_dir()


def _is_regular_file(path: Path) -> bool:
    """Return whether ``path`` is a regular file (never following a link)."""
    return path.is_file() and not path.is_symlink()


def _file_size(path: Path) -> int:
    """Return the current on-disk size of ``path`` in bytes."""
    return path.stat().st_size


def _unlink(path: Path) -> None:
    """Remove exactly one file. Never recursive, never a directory."""
    path.unlink()


def _is_within(root: Path, directory: Path) -> bool:
    """Return whether ``directory`` is ``root`` itself or lies beneath it.

    Both arguments are already fully resolved, so this is pure path comparison:
    it cannot be fooled by a link, because following links happened before it
    was called.
    """
    return directory == root or root in directory.parents


def validate_media_path(
    *,
    absolute_path: str,
    filename: str,
    capture_root: Path,
) -> RetentionErrorCategory | None:
    """Return why this path may not be deleted, or ``None`` if it may.

    These are the checks that do not depend on the file existing, so they apply
    identically to a fresh candidate and to a pending intent whose media may
    already be gone.

    The order matters. ``..`` is rejected *syntactically* before anything is
    resolved, because a normalising resolve would erase the evidence that a
    traversal was attempted. The candidate is then rejected if it is itself a
    symlink -- checked before resolution, since resolving a link is exactly how
    a link gets followed out of the capture root. Only the *parent* is resolved,
    which is what makes a symlinked directory in the middle of the path unable
    to smuggle the target somewhere else.
    """
    candidate = Path(absolute_path)

    if not candidate.is_absolute():
        return RetentionErrorCategory.UNSAFE_PATH

    if ".." in candidate.parts:
        return RetentionErrorCategory.UNSAFE_PATH

    # The catalogue stores the filename and the full path separately. If they
    # disagree, one of the two has been tampered with or corrupted and neither
    # can be trusted to name the file that is about to be removed.
    if candidate.name != filename:
        return RetentionErrorCategory.UNSAFE_PATH

    if _is_symlink(candidate):
        return RetentionErrorCategory.UNSAFE_PATH

    if not _is_within(capture_root, _realpath(candidate.parent)):
        return RetentionErrorCategory.UNSAFE_PATH

    return None


def validate_media_file(
    *,
    absolute_path: str,
    filesize_bytes: int,
) -> RetentionErrorCategory | None:
    """Return why this file may not be deleted, or ``None`` if it may.

    Assumes :func:`validate_media_path` has already passed. A directory is
    reported before the general regular-file check so the refusal names what was
    actually found, and the size comparison is last because it is the one check
    that says "this is the right *kind* of thing, but not the right thing":
    catalogue and disk disagreeing about a file's size means the file on disk is
    not the one that was catalogued, whatever its name is.
    """
    if not _path_exists(Path(absolute_path)):
        return RetentionErrorCategory.MEDIA_MISSING

    candidate = Path(absolute_path)

    # Deliberately redundant with the regular-file rule below -- a directory is
    # not a regular file, so either check alone refuses it. It is kept as an
    # explicit statement of the boundary, and for that reason it carries no
    # mutation-register entry of its own: the property "a directory is never
    # unlinked" is registered against the regular-file check, which is the guard
    # that would still be standing if this line were deleted.
    if _is_directory(candidate):
        return RetentionErrorCategory.NOT_REGULAR_FILE

    if not _is_regular_file(candidate):
        return RetentionErrorCategory.NOT_REGULAR_FILE

    if _file_size(candidate) != filesize_bytes:
        return RetentionErrorCategory.SIZE_MISMATCH

    return None


def _utc_now() -> datetime:
    """Return the current timezone-aware UTC instant."""
    return datetime.now(UTC)


@dataclass
class _RunTally:
    """What one run has completed so far. Mutable on purpose.

    A run reports the same counters whether it finished, stopped on a safety
    refusal, or was ended by an unexpected exception -- so the numbers have to
    live somewhere the execution boundary can still read after the stack it was
    accumulated on has unwound.
    """

    deleted_count: int = 0
    bytes_reclaimed: int = 0
    recovered_count: int = 0
    candidate_count: int = 0
    more_work_remains: bool = False


class RetentionDisabledError(RuntimeError):
    """Raised when destructive retention is requested while it is disabled.

    Not used by :meth:`RetentionService.run_once`, which returns a truthful
    disabled result instead: a caller asking a disabled subsystem to run has not
    made an error, and an exception would push callers towards catching it and
    carrying on. The class exists so a future explicit-invocation path can
    refuse loudly if that turns out to be the better contract there.
    """


class RetentionService:
    """Plans and executes capture-media retention. Nothing schedules it.

    Everything it touches is injected -- the configuration, the repository, the
    runtime-state holder, the capture root, the clock and the observation
    recorder -- so the whole subsystem can be exercised without a Raspberry Pi,
    a camera or a real capture directory.

    It never acquires the camera coordinator, never starts or stops preview,
    never invokes the capture service or workflow and never touches a camera
    backend. Retention operates on captures that finished long ago; a capture
    still in progress is not yet catalogued and therefore cannot be a candidate.
    """

    def __init__(
        self,
        config: RetentionConfig,
        repository: RetentionRepository,
        state: RetentionRuntimeState,
        capture_directory: Path,
        *,
        clock: Clock = _utc_now,
        recorder: ObservationRecorder = record_observation,
        database_path: Path | None = None,
    ) -> None:
        self._config = config
        self._repository = repository
        self._state = state
        self._capture_directory = capture_directory
        self._clock = clock
        self._recorder = recorder
        self._database_path = database_path
        # Process-level mutual exclusion. Not a queue: a second overlapping
        # destructive run is refused outright rather than made to wait, because
        # a caller that waited would delete against a plan computed before the
        # run it waited for changed the catalogue underneath it.
        self._run_lock = threading.Lock()

    # -- public API --------------------------------------------------------

    def status(self) -> Any:
        """Return an immutable snapshot of the retention runtime state."""
        return self._state.snapshot()

    def dry_run(self) -> RetentionPlan:
        """Return what the policy would do right now. No MGO-managed state changes.

        Reads the catalogue, evaluates the policy and reports the candidates,
        the projected reclaim and whether the byte target is reachable.

        The claim, stated as the separate facts it is made of: no SQL write, no
        lifecycle row created or modified, no media deleted, no observation
        recorded, no capture altered, no counter moved, no database or parent
        directory created, and no journal-mode change.

        What is deliberately *not* claimed is that nothing on the filesystem
        moves. A database in WAL mode cannot be read at all -- even read-only,
        even with ``mode=ro`` -- without SQLite's ``-shm`` shared-memory index,
        so SQLite may create or use that sidecar. That is SQLite's documented
        read mechanism, not a change this code makes, and the earlier wording
        ("mutates nothing") papered over the difference. ``immutable=1`` would
        avoid the sidecar and is deliberately not used: it asserts the file
        cannot change, which is untrue of a live database and would licence
        SQLite to ignore concurrent WAL state. Reading a live database through
        semantics that may miss committed data is the worse trade.

        Deliberately available whether or not retention is enabled: previewing a
        policy is how an operator decides whether to enable it, and a read-only
        preview is safe from either side of that switch.

        The catalogue is read through the repository's genuinely read-only path,
        not the ordinary read-write one. That distinction is the difference
        between the guarantee above and a preview that quietly creates a
        database, creates a directory, or switches a database's journal mode --
        all of which the read-write helper will do.
        """
        catalogue = self._repository.read_lifecycle_records()
        return plan_retention(catalogue, self._config, now_utc=self._clock())

    def run_once(self) -> RetentionRunResult:
        """Execute one bounded, recoverable, destructive retention run.

        Returns a truthful result rather than raising. The three ways it can
        decline to do anything are distinguishable without inspecting an
        exception: disabled (``executed=False``, no error), busy
        (``executed=False``, ``BUSY``), or executed with nothing to do.

        Order is a safety property: pending intents are recovered *before* new
        candidates are selected, so a capture whose deletion was interrupted is
        settled before the run considers spending its per-run budget on
        something new.
        """
        if not self._config.enabled:
            # Not an error and not a no-op that "would have worked" -- disabled
            # retention performs no filesystem mutation and no database
            # mutation, including no pending-intent recovery. An interrupted
            # deletion stays interrupted until someone turns retention on.
            LOGGER.info("Retention run requested while retention is disabled")
            return RetentionRunResult(
                executed=False,
                enabled=False,
                candidate_count=0,
                deleted_count=0,
                bytes_reclaimed=0,
                recovered_count=0,
                more_work_remains=False,
                error_category=None,
            )

        if not self._run_lock.acquire(blocking=False):
            LOGGER.warning(
                "Retention run refused; another run is already in progress"
            )
            return RetentionRunResult(
                executed=False,
                enabled=True,
                candidate_count=0,
                deleted_count=0,
                bytes_reclaimed=0,
                recovered_count=0,
                more_work_remains=False,
                error_category=RetentionErrorCategory.BUSY,
            )

        try:
            self._state.mark_running()
            # ``_execute`` converts every ordinary failure into a result, so the
            # two lines below always run and the holder can never be left
            # reporting ``running`` for a run that has finished.
            result = self._execute()
            self._state.record_run(
                completed_at=self._clock(),
                candidate_count=result.candidate_count,
                deleted_count=result.deleted_count,
                bytes_reclaimed=result.bytes_reclaimed,
                error=result.error_message,
            )
        finally:
            # Released last, so the state a waiting caller can observe is
            # already the finished state rather than a stale ``running``.
            self._run_lock.release()
        return result

    # -- the run -----------------------------------------------------------

    def _execute(self) -> RetentionRunResult:
        """Execute one run, converting any ordinary failure into a result.

        This is the execution boundary. A destructive subsystem that lets an
        unexpected exception escape leaves its caller with no result, its
        runtime state stuck in ``running`` and its lifetime counters missing the
        deletions it had already completed -- so the boundary is here, where the
        partial tally is still in hand, rather than around the call in
        :meth:`run_once` where it would have been lost.

        Only ``Exception`` is converted. ``KeyboardInterrupt``, ``SystemExit``
        and the rest of ``BaseException`` are process-control signals, not
        retention failures, and are deliberately allowed to propagate: catching
        them to make a status endpoint look tidy would suppress a shutdown.
        """
        tally = _RunTally()
        try:
            return self._execute_tracked(tally)
        except Exception:
            # The raw exception is logged with its traceback here and *only*
            # here. It is arbitrary application data -- it may carry a media
            # path, a database location or an environment value -- so nothing
            # derived from it reaches the result, the status endpoint or an
            # observation.
            LOGGER.exception(
                "The retention run failed unexpectedly and was stopped"
            )
            return self._result(tally, RetentionErrorCategory.UNEXPECTED)

    def _execute_tracked(self, tally: _RunTally) -> RetentionRunResult:
        """Run recovery, then policy, stopping at the first failure.

        Every completed deletion is folded into ``tally`` immediately, so a
        failure -- expected or unexpected -- reports what this run genuinely
        reclaimed rather than discarding it.
        """
        capture_root = self._resolved_capture_root()
        if capture_root is None:
            return self._result(tally, RetentionErrorCategory.UNSAFE_PATH)

        try:
            records = self._repository.list_lifecycle_records()
        except RetentionCatalogueError:
            LOGGER.exception("The capture catalogue could not be parsed safely")
            return self._result(tally, RetentionErrorCategory.CATALOGUE_INVALID)
        except RetentionRepositoryError:
            LOGGER.exception("The capture catalogue could not be read")
            return self._result(tally, RetentionErrorCategory.CATALOGUE_INVALID)

        for record in self._pending_records(records):
            error, reclaimed = self._recover_pending(record, capture_root)
            if error is not None:
                return self._result(tally, error)
            tally.deleted_count += 1
            tally.recovered_count += 1
            tally.bytes_reclaimed += reclaimed

        plan = plan_retention(records, self._config, now_utc=self._clock())
        tally.candidate_count = len(plan.candidates)
        tally.more_work_remains = plan.more_work_remains

        for candidate in plan.candidates:
            error = self._delete_candidate(candidate, capture_root)
            if error is not None:
                return self._result(tally, error)
            tally.deleted_count += 1
            tally.bytes_reclaimed += candidate.filesize_bytes

        return self._result(tally, None)

    @staticmethod
    def _result(
        tally: _RunTally,
        error: RetentionErrorCategory | None,
    ) -> RetentionRunResult:
        """Build the run result. A run that started always reports executed."""
        return RetentionRunResult(
            executed=True,
            enabled=True,
            candidate_count=tally.candidate_count,
            deleted_count=tally.deleted_count,
            bytes_reclaimed=tally.bytes_reclaimed,
            recovered_count=tally.recovered_count,
            more_work_remains=tally.more_work_remains,
            error_category=error,
        )

    def _resolved_capture_root(self) -> Path | None:
        """Return the resolved capture root, or ``None`` if it is unusable.

        Every containment check is relative to this, so a root that is not an
        absolute existing directory makes containment meaningless and the run
        refuses to start rather than deleting against an assumption.
        """
        root = self._capture_directory
        if not root.is_absolute():
            LOGGER.error("The configured capture directory is not absolute")
            return None

        resolved = _realpath(root)
        if not _is_directory(resolved):
            LOGGER.error("The configured capture directory is not a directory")
            return None
        return resolved

    @staticmethod
    def _pending_records(
        records: list[CaptureLifecycleRecord],
    ) -> list[CaptureLifecycleRecord]:
        """Return pending intents oldest-request first, deterministically."""
        pending = [
            record
            for record in records
            if record.lifecycle_state is MediaLifecycleState.PENDING_DELETE
        ]
        return sorted(
            pending,
            key=lambda record: (
                record.requested_at_utc.isoformat()
                if record.requested_at_utc is not None
                else "",
                record.capture_id,
            ),
        )

    # -- recovering an interrupted deletion --------------------------------

    def _recover_pending(
        self, record: CaptureLifecycleRecord, capture_root: Path
    ) -> tuple[RetentionErrorCategory | None, int]:
        """Settle one durable pending intent. Returns (error, bytes reclaimed).

        A pending row means MGO already authorised the removal of this specific
        capture's media and committed that decision. Recovery therefore is not
        governed by the current ``minimum_keep_count``: the destructive decision
        was made under an earlier policy and may already have been carried out.
        Re-deciding it now could only produce a file that is already gone being
        reported as an unexplained inconsistency forever.

        The two outcomes it settles:

        * **the file is gone** -- the unlink succeeded and the process died
          before finalisation. Finalise it, with ``recovered_pending`` recorded
          on the observation so the timeline says which deletion this was.
        * **the file is still there** -- finish the job, but only after the same
          full safety validation a fresh candidate gets.

        Anything unverifiable -- an unsafe path, a directory, a symlink, a size
        that no longer matches -- is not touched at all, and the intent is left
        standing for a human. A pending row is recoverable; a wrong guess about
        what it authorised is not.
        """
        path_error = validate_media_path(
            absolute_path=record.absolute_path,
            filename=record.filename,
            capture_root=capture_root,
        )
        if path_error is not None:
            LOGGER.error(
                "Refusing to recover the pending deletion of capture %s "
                "(category=%s); the durable intent is left intact",
                record.capture_id,
                path_error.value,
            )
            self._record_failure(record.capture_id, path_error)
            return path_error, 0

        if _path_exists(Path(record.absolute_path)):
            file_error = validate_media_file(
                absolute_path=record.absolute_path,
                filesize_bytes=record.filesize_bytes,
            )
            if file_error is not None:
                LOGGER.error(
                    "Refusing to recover the pending deletion of capture %s "
                    "(category=%s); the durable intent is left intact",
                    record.capture_id,
                    file_error.value,
                )
                self._record_failure(record.capture_id, file_error)
                return file_error, 0

            try:
                _unlink(Path(record.absolute_path))
            except OSError:
                LOGGER.exception(
                    "Could not remove the media of pending capture %s",
                    record.capture_id,
                )
                self._record_failure(
                    record.capture_id,
                    RetentionErrorCategory.FILESYSTEM_DELETE_FAILED,
                )
                return RetentionErrorCategory.FILESYSTEM_DELETE_FAILED, 0

        return self._finalize(
            capture_id=record.capture_id,
            filename=record.filename,
            filesize_bytes=record.filesize_bytes,
            captured_at=record.captured_at_utc,
            reason=record.reason,
            recovered_pending=True,
        )

    # -- deleting a freshly planned candidate ------------------------------

    def _delete_candidate(
        self, candidate: RetentionCandidate, capture_root: Path
    ) -> RetentionErrorCategory | None:
        """Run the three-stage deletion for one planned candidate.

        Validation happens twice on purpose. The first pass runs *before* any
        lifecycle row exists, so a candidate whose media is already missing, or
        whose path is unsafe, never acquires a deletion intent at all -- which
        keeps ``PRESENT`` + missing an unexplained inconsistency rather than
        something retention has quietly half-claimed. The second runs as close
        to the ``unlink`` as the code can get it.
        """
        pre_error = self._validate_candidate(candidate, capture_root)
        if pre_error is not None:
            LOGGER.error(
                "Refusing to delete capture %s (category=%s); no deletion "
                "intent was recorded",
                candidate.capture_id,
                pre_error.value,
            )
            self._record_failure(
                candidate.capture_id, pre_error, reason=candidate.reason
            )
            return pre_error

        # Stage A -- durable intent, committed before the filesystem is touched.
        try:
            claimed = self._repository.claim_pending_delete(
                candidate.capture_id, candidate.reason
            )
        except RetentionRepositoryError:
            LOGGER.exception(
                "Could not record a deletion intent for capture %s",
                candidate.capture_id,
            )
            self._record_failure(
                candidate.capture_id,
                RetentionErrorCategory.DATABASE_TRANSITION_FAILED,
                reason=candidate.reason,
            )
            return RetentionErrorCategory.DATABASE_TRANSITION_FAILED

        if not claimed:
            # The catalogue changed between the plan and the claim: something
            # else now owns this capture's lifecycle. Nothing has been touched,
            # and continuing would mean deleting against a stale plan.
            LOGGER.warning(
                "Deletion intent for capture %s was already claimed elsewhere",
                candidate.capture_id,
            )
            self._record_failure(
                candidate.capture_id,
                RetentionErrorCategory.DATABASE_TRANSITION_FAILED,
                reason=candidate.reason,
            )
            return RetentionErrorCategory.DATABASE_TRANSITION_FAILED

        # Stage B -- revalidate, then remove exactly one file. No database
        # transaction is open here; the intent above is already committed.
        revalidation = self._validate_candidate(candidate, capture_root)
        if revalidation is not None:
            LOGGER.error(
                "Capture %s failed revalidation after its deletion intent was "
                "recorded (category=%s)",
                candidate.capture_id,
                revalidation.value,
            )
            return self._cancel(candidate, revalidation)

        try:
            _unlink(Path(candidate.absolute_path))
        except OSError:
            LOGGER.exception(
                "Could not remove the media of capture %s", candidate.capture_id
            )
            return self._cancel(
                candidate, RetentionErrorCategory.FILESYSTEM_DELETE_FAILED
            )

        # Stage C -- transition and observation, committed together.
        error, _ = self._finalize(
            capture_id=candidate.capture_id,
            filename=candidate.filename,
            filesize_bytes=candidate.filesize_bytes,
            captured_at=candidate.captured_at_utc,
            reason=candidate.reason,
            recovered_pending=False,
        )
        return error

    @staticmethod
    def _validate_candidate(
        candidate: RetentionCandidate, capture_root: Path
    ) -> RetentionErrorCategory | None:
        """Apply the full safety boundary to one candidate."""
        path_error = validate_media_path(
            absolute_path=candidate.absolute_path,
            filename=candidate.filename,
            capture_root=capture_root,
        )
        if path_error is not None:
            return path_error
        return validate_media_file(
            absolute_path=candidate.absolute_path,
            filesize_bytes=candidate.filesize_bytes,
        )

    def _cancel(
        self,
        candidate: RetentionCandidate,
        error: RetentionErrorCategory,
    ) -> RetentionErrorCategory:
        """Return a failed candidate's media to ``PRESENT`` and record why.

        Only reached while the file is still on disk, so removing the intent
        restores the truth. The cancellation and the failure observation commit
        together.

        If the cancellation transaction itself fails, the durable
        ``pending_delete`` row is deliberately left standing: the next run can
        recover it, whereas a lifecycle state invented in memory is a lie that
        outlives the process that told it. The originally reported category is
        returned either way -- it is the first failure, and the first failure is
        what a run reports.
        """
        try:
            self._repository.cancel_pending_delete(
                candidate.capture_id,
                observation_fields=self._failure_fields(
                    candidate.capture_id, error, reason=candidate.reason
                ),
            )
        except RetentionRepositoryError:
            LOGGER.exception(
                "Could not cancel the deletion intent for capture %s; the "
                "pending state is left durable for recovery",
                candidate.capture_id,
            )
        return error

    def _finalize(
        self,
        *,
        capture_id: str,
        filename: str,
        filesize_bytes: int,
        captured_at: datetime,
        reason: RetentionReason | None,
        recovered_pending: bool,
    ) -> tuple[RetentionErrorCategory | None, int]:
        """Commit the ``deleted`` transition and its observation together.

        Returns ``(None, bytes)`` on success. A failure here happens *after* the
        media is already gone, so the pending row is left exactly as it is: that
        durable intent is what lets the next run finish the job instead of
        finding an unexplained missing file.
        """
        try:
            finalized = self._repository.finalize_deletion(
                capture_id,
                observation_fields=self._success_fields(
                    capture_id=capture_id,
                    filename=filename,
                    filesize_bytes=filesize_bytes,
                    captured_at=captured_at,
                    reason=reason,
                    recovered_pending=recovered_pending,
                ),
            )
        except RetentionRepositoryError:
            LOGGER.exception(
                "Capture %s media was removed but the deletion could not be "
                "finalised; the pending intent remains recoverable",
                capture_id,
            )
            return RetentionErrorCategory.FINALIZATION_FAILED, 0

        if not finalized:
            # No pending row moved, so no success observation was written. That
            # is the guarantee: one deletion produces exactly one success
            # observation, however many executions raced for it.
            LOGGER.warning(
                "Capture %s had no pending intent to finalise", capture_id
            )
            return RetentionErrorCategory.FINALIZATION_FAILED, 0

        LOGGER.info(
            "Reclaimed the media of capture %s (%d bytes, recovered=%s)",
            capture_id,
            filesize_bytes,
            recovered_pending,
        )
        return None, filesize_bytes

    # -- observations ------------------------------------------------------

    @staticmethod
    def _success_fields(
        *,
        capture_id: str,
        filename: str,
        filesize_bytes: int,
        captured_at: datetime,
        reason: RetentionReason | None,
        recovered_pending: bool,
    ) -> dict[str, Any]:
        """Build the immutable observation for one reclaimed capture.

        The payload carries catalogue facts and the policy word that selected
        it. It deliberately carries no absolute path, no capture directory, no
        database location and no exception text: an operator learns *which
        capture's media went and why*, and nothing about where the application
        lives on disk.
        """
        return {
            "kind": OBSERVATION_KIND,
            "source": OBSERVATION_SOURCE,
            "status": SUCCESS_STATUS,
            "summary": SUCCESS_SUMMARY,
            "payload": {
                "capture_id": capture_id,
                "filename": filename,
                "filesize_bytes": filesize_bytes,
                "policy_reason": reason.value if reason is not None else None,
                "captured_at": captured_at.isoformat(),
                "recovered_pending": recovered_pending,
            },
            # The capture UUID ties this timeline entry to the catalogue record
            # whose media it describes -- the record that deliberately survives.
            "correlation_id": capture_id,
        }

    @staticmethod
    def _failure_fields(
        capture_id: str,
        category: RetentionErrorCategory,
        *,
        reason: RetentionReason | None = None,
    ) -> dict[str, Any]:
        """Build the immutable observation for one refused or failed deletion.

        There is deliberately **no filename here**, and that asymmetry with the
        success payload is the whole point. A success observation is only ever
        written after the full path/filename safety boundary has passed, so its
        filename is a value this code has already verified. A *failure* is
        frequently the boundary refusing that very value -- the catalogue
        filename may itself be an absolute path, a traversal or a database
        location -- and writing it here would put the untrusted string into the
        immutable timeline, which is exactly what the privacy contract forbids.

        A sanitised path or a basename is not offered in its place either: the
        capture id already identifies the record completely, and anything
        derived from the rejected value would still be derived from it.
        """
        return {
            "kind": OBSERVATION_KIND,
            "source": OBSERVATION_SOURCE,
            "status": FAILURE_STATUS,
            "summary": FAILURE_SUMMARY,
            "payload": {
                "capture_id": capture_id,
                "error_category": category.value,
                "policy_reason": reason.value if reason is not None else None,
            },
            "correlation_id": capture_id,
        }

    def _record_failure(
        self,
        capture_id: str,
        category: RetentionErrorCategory,
        *,
        reason: RetentionReason | None = None,
    ) -> None:
        """Record a failure observation on its own. Never raises.

        Used for the failures that change no lifecycle state -- a candidate
        refused before any intent existed, a pending intent left deliberately
        untouched. A timeline write that fails here is a telemetry failure, not
        a retention failure, and must not overwrite the category the run is
        about to report.
        """
        if self._database_path is None:
            return
        try:
            self._recorder(
                self._database_path,
                **self._failure_fields(capture_id, category, reason=reason),
            )
        except Exception:
            LOGGER.exception(
                "The retention failure observation could not be recorded "
                "(category=%s)",
                category.value,
            )


__all__ = [
    "FAILURE_STATUS",
    "FAILURE_SUMMARY",
    "OBSERVATION_KIND",
    "OBSERVATION_SOURCE",
    "SUCCESS_STATUS",
    "SUCCESS_SUMMARY",
    "RetentionDisabledError",
    "RetentionService",
    "safe_error_message",
    "validate_media_file",
    "validate_media_path",
]
