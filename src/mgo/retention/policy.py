"""The retention policy planner: deterministic, pure, and filesystem-free.

This module decides *what would be deleted*. It never opens a file, never stats
a path, never touches SQLite and never mutates anything, which is what makes a
dry run and a destructive run provably the same decision: they call this with
the same inputs and get the same plan.

Planning uses the **catalogue** as its authority -- catalogue timestamps and
catalogue byte sizes -- not the filesystem. That is deliberate. A planner that
sized files on disk would be making destructive decisions from a directory
listing, and a directory listing is exactly the thing that must never be able to
nominate a file for deletion. The filesystem gets a vote later, in the executor,
and it is only ever a veto.

Two safety properties are enforced here rather than downstream:

* only ``origin = "motion"`` captures are managed at all;
* the newest ``minimum_keep_count`` managed captures are never selected.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from mgo.core.config import RetentionConfig
from mgo.retention.models import (
    CaptureLifecycleRecord,
    MediaLifecycleState,
    RetentionCandidate,
    RetentionPlan,
    RetentionReason,
)

#: The one ``extra_metadata["origin"]`` value automatic retention manages.
#:
#: Exact equality, and nothing else. A manual capture, a capture with no origin
#: at all, and a capture from some future origin this build has never heard of
#: are all *protected*, because the only safe reading of an unrecognised record
#: is that something else owns it. Widening this is a deliberate policy decision
#: for a future task, not a convenience for this one.
MANAGED_ORIGIN = "motion"


def is_managed(record: CaptureLifecycleRecord) -> bool:
    """Return whether automatic retention manages this capture's media.

    Both halves matter. The origin must be exactly :data:`MANAGED_ORIGIN`, and
    the media must still be ``PRESENT`` -- a capture already reclaimed has no
    media to plan about, and one with a pending intent is already claimed by the
    recovery path and must not be selected a second time.
    """
    return (
        record.origin == MANAGED_ORIGIN
        and record.lifecycle_state is MediaLifecycleState.PRESENT
    )


def _ordering_key(record: CaptureLifecycleRecord) -> tuple[str, str, str]:
    """Return the deterministic oldest-first sort key for a capture.

    Three levels, because two are not enough: captures a second apart share a
    ``captured_at_utc`` far more often than one might expect on a camera driven
    by motion, and SQLite's incidental row order is not a tie-break -- it is the
    absence of one. Falling through to the primary key makes the order total, so
    the same catalogue always produces the same plan.

    Timestamps sort as ISO-8601 strings normalised to UTC, which orders
    identically to the instants themselves and cannot be perturbed by a stored
    offset.
    """
    return (
        record.captured_at_utc.isoformat(),
        record.created_at_utc.isoformat(),
        record.capture_id,
    )


def _age_cutoff(now_utc: datetime, max_age_days: int) -> datetime:
    """Return the instant at or before which a capture is age-expired."""
    return now_utc - timedelta(days=max_age_days)


def plan_retention(
    records: list[CaptureLifecycleRecord],
    config: RetentionConfig,
    *,
    now_utc: datetime,
) -> RetentionPlan:
    """Return the deletions the current policy would make, in order.

    The selection is built in four steps, each of which can only ever *narrow*
    what the previous one allowed:

    1. **Manage.** Keep only ``origin = "motion"`` captures whose media is
       ``PRESENT``. Everything else -- manual, unknown, absent-origin, already
       deleted, already pending -- leaves the calculation here and can no longer
       be selected by anything below.
    2. **Preserve.** Order oldest first and set aside the newest
       ``minimum_keep_count``. Nothing in that set is eligible, whatever the age
       or byte pressure says. This floor is never traded away for a byte target;
       when it makes the target unreachable, the plan reports that instead.
    3. **Select.** Take the age-expired eligible captures, then -- if the
       projected managed total is still above ``max_managed_bytes`` -- extend
       oldest-first until it is not. Because both rules act on the same
       oldest-first ordering, each selects a prefix of the eligible list, so the
       union is simply the longer prefix and no capture can be chosen twice.
    4. **Bound.** Truncate to ``max_deletions_per_run``. Anything beyond the cap
       is reported as remaining work, never silently dropped and never quietly
       deleted anyway.

    The reason recorded against a candidate is decided by which rules reach it
    *independently*: age alone, byte pressure alone, or both.
    """
    managed = [record for record in records if is_managed(record)]
    ordered = sorted(managed, key=_ordering_key)

    managed_present_bytes = sum(record.filesize_bytes for record in ordered)

    # Step 2. ``minimum_keep_count`` is validated >= 1, so this always preserves
    # something; slicing to a floor of zero keeps a keep-count larger than the
    # catalogue from producing a negative index and wrapping the list.
    eligible_count = max(0, len(ordered) - config.minimum_keep_count)
    eligible = ordered[:eligible_count]
    protected_count = len(ordered) - eligible_count

    age_selected = _select_by_age(eligible, config, now_utc=now_utc)
    byte_selected = _select_by_managed_bytes(
        eligible, config, managed_present_bytes=managed_present_bytes
    )

    selected = [
        record
        for record in eligible
        if record.capture_id in age_selected or record.capture_id in byte_selected
    ]

    # Step 4. The hard destructive bound. ``more_work_remains`` is what stops a
    # truncated plan from reading as a completed one.
    capped = selected[: config.max_deletions_per_run]
    more_work_remains = len(selected) > len(capped)

    candidates = tuple(
        RetentionCandidate(
            capture_id=record.capture_id,
            filename=record.filename,
            absolute_path=record.absolute_path,
            captured_at_utc=record.captured_at_utc,
            filesize_bytes=record.filesize_bytes,
            reason=_classify(record, age_selected, byte_selected),
        )
        for record in capped
    )

    projected_bytes_reclaimed = sum(
        candidate.filesize_bytes for candidate in candidates
    )

    return RetentionPlan(
        candidates=candidates,
        managed_present_count=len(ordered),
        managed_present_bytes=managed_present_bytes,
        protected_count=protected_count,
        projected_bytes_reclaimed=projected_bytes_reclaimed,
        projected_managed_bytes=managed_present_bytes - projected_bytes_reclaimed,
        more_work_remains=more_work_remains,
        byte_target_satisfiable=_byte_target_satisfiable(
            ordered, eligible, config
        ),
    )


def _select_by_age(
    eligible: list[CaptureLifecycleRecord],
    config: RetentionConfig,
    *,
    now_utc: datetime,
) -> frozenset[str]:
    """Return the ids of eligible captures older than the configured age.

    The boundary is inclusive: a capture taken *exactly* ``max_age_days`` ago is
    expired. An exclusive boundary would leave a capture sitting one microsecond
    inside the limit undeleted for a further whole evaluation, which is a
    surprise with no benefit; inclusive is also the reading an operator gives
    "keep seven days of captures".
    """
    if config.max_age_days is None:
        return frozenset()

    cutoff = _age_cutoff(now_utc, config.max_age_days)
    return frozenset(
        record.capture_id
        for record in eligible
        if record.captured_at_utc <= cutoff
    )


def _select_by_managed_bytes(
    eligible: list[CaptureLifecycleRecord],
    config: RetentionConfig,
    *,
    managed_present_bytes: int,
) -> frozenset[str]:
    """Return the ids the byte bound alone would have to reclaim.

    Computed independently of the age rule so a candidate's reason can say
    truthfully whether byte pressure reached it too. It walks the eligible
    captures oldest first and stops the moment the running total is within the
    limit, so it selects the *minimal* oldest prefix -- never one capture more
    than the bound requires.

    The boundary is inclusive on the permitted side: a managed total exactly
    equal to ``max_managed_bytes`` is within the limit and selects nothing.
    """
    if config.max_managed_bytes is None:
        return frozenset()

    running = managed_present_bytes
    selected: set[str] = set()
    for record in eligible:
        if running <= config.max_managed_bytes:
            break
        selected.add(record.capture_id)
        running -= record.filesize_bytes
    return frozenset(selected)


def _classify(
    record: CaptureLifecycleRecord,
    age_selected: frozenset[str],
    byte_selected: frozenset[str],
) -> RetentionReason:
    """Return the one deterministic reason recorded for a candidate."""
    by_age = record.capture_id in age_selected
    by_bytes = record.capture_id in byte_selected

    if by_age and by_bytes:
        return RetentionReason.AGE_AND_MANAGED_BYTES
    if by_age:
        return RetentionReason.AGE
    return RetentionReason.MANAGED_BYTES


def _byte_target_satisfiable(
    ordered: list[CaptureLifecycleRecord],
    eligible: list[CaptureLifecycleRecord],
    config: RetentionConfig,
) -> bool:
    """Return whether the byte bound is reachable without breaking the floor.

    Deleting *every* eligible capture is the most the policy is ever permitted
    to do, so what remains is the preserved set. If that alone still exceeds
    ``max_managed_bytes``, the target cannot be met by any permitted action --
    and saying so is the only honest answer available. The alternative, deleting
    into the preservation floor to make a number come out right, is precisely
    what the floor exists to prevent.

    A run may still legitimately be over the limit *after* it finishes because
    of ``max_deletions_per_run``; that is reported by ``more_work_remains``, and
    is a different fact from the target being unreachable.
    """
    if config.max_managed_bytes is None:
        return True

    eligible_ids = {record.capture_id for record in eligible}
    preserved_bytes = sum(
        record.filesize_bytes
        for record in ordered
        if record.capture_id not in eligible_ids
    )
    return preserved_bytes <= config.max_managed_bytes


__all__ = [
    "MANAGED_ORIGIN",
    "is_managed",
    "plan_retention",
]
