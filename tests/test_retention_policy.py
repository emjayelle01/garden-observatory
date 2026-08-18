"""Tests for the deterministic retention policy planner.

The planner is a pure function of the catalogue, the configuration and one
instant, so everything here is built from in-memory projections. No database, no
temporary directory, no file and no clock: if any test in this module needed one
of those, the planner would have stopped being pure and that is itself the bug.

The safety properties under test are the ones that decide whether a file lives:
which captures are managed at all, which are preserved regardless, where the age
and byte boundaries actually fall, and that a truncated plan says so.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from mgo.core.config import RetentionConfig
from mgo.retention.models import (
    CaptureLifecycleRecord,
    MediaLifecycleState,
    RetentionReason,
)
from mgo.retention.policy import MANAGED_ORIGIN, is_managed, plan_retention

NOW = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)


def _record(
    identifier: str,
    *,
    days_old: float = 0.0,
    origin: str | None = MANAGED_ORIGIN,
    size: int = 1_000_000,
    state: MediaLifecycleState = MediaLifecycleState.PRESENT,
    created_offset_seconds: float = 0.0,
) -> CaptureLifecycleRecord:
    """Build one catalogue projection for the planner."""
    captured_at = NOW - timedelta(days=days_old)
    return CaptureLifecycleRecord(
        capture_id=identifier,
        filename=f"{identifier}.jpg",
        absolute_path=f"/captures/{identifier}.jpg",
        captured_at_utc=captured_at,
        created_at_utc=captured_at + timedelta(seconds=created_offset_seconds),
        filesize_bytes=size,
        origin=origin,
        lifecycle_state=state,
        requested_at_utc=None,
        deleted_at_utc=None,
        reason=None,
    )


def _config(
    *,
    max_age_days: int | None = None,
    max_managed_bytes: int | None = None,
    minimum_keep_count: int = 1,
    max_deletions_per_run: int = 100,
) -> RetentionConfig:
    """Build a retention configuration for the planner."""
    return RetentionConfig(
        enabled=True,
        max_age_days=max_age_days,
        max_managed_bytes=max_managed_bytes,
        minimum_keep_count=minimum_keep_count,
        max_deletions_per_run=max_deletions_per_run,
    )


def _selected(plan: object) -> list[str]:
    """Return the selected capture ids, in plan order."""
    return [candidate.capture_id for candidate in plan.candidates]  # type: ignore[attr-defined]


# --- nothing to do ----------------------------------------------------------


def test_empty_catalogue_selects_nothing() -> None:
    """A catalogue with no captures produces an empty, truthful plan."""
    plan = plan_retention([], _config(max_age_days=1), now_utc=NOW)

    assert plan.candidates == ()
    assert plan.managed_present_count == 0
    assert plan.managed_present_bytes == 0
    assert plan.more_work_remains is False
    assert plan.byte_target_satisfiable is True


def test_managed_captures_below_every_limit_select_nothing() -> None:
    """Recent motion captures inside the byte budget are left alone."""
    records = [_record(f"m{index}", days_old=index) for index in range(5)]

    plan = plan_retention(
        records,
        _config(max_age_days=30, max_managed_bytes=10_000_000),
        now_utc=NOW,
    )

    assert plan.candidates == ()
    assert plan.managed_present_count == 5
    assert plan.projected_bytes_reclaimed == 0


# --- what is managed at all -------------------------------------------------


def test_only_manual_captures_selects_nothing() -> None:
    """A catalogue of manual captures is entirely protected.

    They are ancient and far over any byte budget; nothing selects them, because
    an absent origin is not an invitation.
    """
    records = [
        _record(f"manual{index}", days_old=900, origin=None, size=9_000_000)
        for index in range(5)
    ]

    plan = plan_retention(
        records,
        _config(max_age_days=1, max_managed_bytes=1),
        now_utc=NOW,
    )

    assert plan.candidates == ()
    assert plan.managed_present_count == 0
    assert plan.managed_present_bytes == 0


def test_only_unknown_origin_captures_selects_nothing() -> None:
    """An origin this build has never heard of is protected, not disposable."""
    records = [
        _record(f"u{index}", days_old=900, origin="timelapse", size=9_000_000)
        for index in range(5)
    ]

    plan = plan_retention(
        records,
        _config(max_age_days=1, max_managed_bytes=1),
        now_utc=NOW,
    )

    assert plan.candidates == ()


@pytest.mark.parametrize(
    "origin",
    [None, "", "manual", "Motion", "MOTION", "motion_burst", " motion", "motion "],
)
def test_only_exactly_motion_is_managed(origin: str | None) -> None:
    """Managed origin is exact equality -- case, spacing and prefixes included.

    A near-miss is a different origin, and a different origin belongs to
    something else. Fuzzy matching here is how an unrelated subsystem's media
    gets deleted by a feature that was never told about it.
    """
    record = _record("x", days_old=900, origin=origin)

    assert is_managed(record) is False
    plan = plan_retention([record], _config(max_age_days=1), now_utc=NOW)
    assert plan.candidates == ()


def test_exact_motion_origin_is_managed() -> None:
    """The one managed value is the one Task 13 writes."""
    assert is_managed(_record("x", origin="motion")) is True


def test_manual_captures_do_not_count_towards_the_managed_byte_total() -> None:
    """``max_managed_bytes`` measures managed media, not the filesystem.

    A manual capture ten times the byte budget must not make a motion capture
    eligible: the policy controls the media the automatic feature generates, and
    saying otherwise would be a filesystem-utilisation policy wearing this
    setting's name.
    """
    records = [
        _record("manual", days_old=900, origin=None, size=100_000_000),
        _record("m0", days_old=1, size=10),
    ]

    plan = plan_retention(records, _config(max_managed_bytes=100), now_utc=NOW)

    assert plan.managed_present_bytes == 10
    assert plan.candidates == ()


# --- age policy -------------------------------------------------------------


def test_age_policy_selects_only_expired_captures() -> None:
    """Captures older than the bound are selected; newer ones are not."""
    records = [
        _record("old1", days_old=10),
        _record("old2", days_old=9),
        _record("young", days_old=2),
    ]

    plan = plan_retention(
        records, _config(max_age_days=7, minimum_keep_count=1), now_utc=NOW
    )

    assert _selected(plan) == ["old1", "old2"]
    assert all(
        candidate.reason is RetentionReason.AGE for candidate in plan.candidates
    )


def test_the_age_boundary_is_inclusive() -> None:
    """A capture taken exactly ``max_age_days`` ago is expired.

    The three records straddle the cutoff by one microsecond either side, so
    this fails if the comparison moves in either direction.
    """
    cutoff = NOW - timedelta(days=7)
    records = [
        CaptureLifecycleRecord(
            capture_id=identifier,
            filename=f"{identifier}.jpg",
            absolute_path=f"/captures/{identifier}.jpg",
            captured_at_utc=captured_at,
            created_at_utc=captured_at,
            filesize_bytes=10,
            origin=MANAGED_ORIGIN,
            lifecycle_state=MediaLifecycleState.PRESENT,
            requested_at_utc=None,
            deleted_at_utc=None,
            reason=None,
        )
        for identifier, captured_at in (
            ("before", cutoff - timedelta(microseconds=1)),
            ("exactly", cutoff),
            ("after", cutoff + timedelta(microseconds=1)),
        )
    ]

    plan = plan_retention(
        records, _config(max_age_days=7, minimum_keep_count=1), now_utc=NOW
    )

    assert _selected(plan) == ["before", "exactly"]


def test_age_policy_alone_ignores_the_byte_total() -> None:
    """With no byte bound configured, size is irrelevant to selection."""
    records = [
        _record("huge_new", days_old=1, size=500_000_000),
        _record("tiny_old", days_old=90, size=1),
    ]

    plan = plan_retention(
        records, _config(max_age_days=7, minimum_keep_count=1), now_utc=NOW
    )

    assert _selected(plan) == ["tiny_old"]


# --- managed-byte policy ----------------------------------------------------


def test_managed_bytes_policy_selects_the_oldest_until_within_the_limit() -> None:
    """Byte pressure reclaims oldest-first and stops as soon as it fits."""
    records = [
        _record(f"m{index}", days_old=10 - index, size=100)
        for index in range(5)
    ]

    plan = plan_retention(
        records, _config(max_managed_bytes=300, minimum_keep_count=1), now_utc=NOW
    )

    assert _selected(plan) == ["m0", "m1"]
    assert all(
        candidate.reason is RetentionReason.MANAGED_BYTES
        for candidate in plan.candidates
    )
    assert plan.projected_managed_bytes == 300


def test_the_managed_byte_boundary_is_inclusive_of_the_limit() -> None:
    """A managed total exactly at the limit is within it and selects nothing."""
    records = [
        _record(f"m{index}", days_old=10 - index, size=100)
        for index in range(3)
    ]

    at_limit = plan_retention(
        records, _config(max_managed_bytes=300, minimum_keep_count=1), now_utc=NOW
    )
    one_byte_over = plan_retention(
        records, _config(max_managed_bytes=299, minimum_keep_count=1), now_utc=NOW
    )

    assert at_limit.candidates == ()
    assert _selected(one_byte_over) == ["m0"]


def test_the_byte_policy_selects_no_more_than_the_bound_requires() -> None:
    """It stops at the first capture that brings the total within the limit."""
    records = [
        _record(f"m{index}", days_old=10 - index, size=100)
        for index in range(10)
    ]

    plan = plan_retention(
        records, _config(max_managed_bytes=999, minimum_keep_count=1), now_utc=NOW
    )

    assert _selected(plan) == ["m0"]


# --- combined policy --------------------------------------------------------


def test_combined_policy_marks_a_capture_reached_by_both_rules() -> None:
    """A capture both rules independently select records the combined reason.

    ``m0`` is age-expired *and* inside the oldest span byte pressure alone would
    have had to reclaim. ``m1`` is only reached by byte pressure. Recording one
    reason for both would misattribute a deletion.
    """
    records = [
        _record("m0", days_old=40, size=100),
        _record("m1", days_old=2, size=100),
        _record("m2", days_old=1, size=100),
    ]

    plan = plan_retention(
        records,
        _config(max_age_days=7, max_managed_bytes=100, minimum_keep_count=1),
        now_utc=NOW,
    )

    assert _selected(plan) == ["m0", "m1"]
    assert plan.candidates[0].reason is RetentionReason.AGE_AND_MANAGED_BYTES
    assert plan.candidates[1].reason is RetentionReason.MANAGED_BYTES


def test_combined_policy_keeps_an_age_only_selection_labelled_age() -> None:
    """Age expiry the byte bound never needed stays reason ``age``.

    The managed total is comfortably inside the budget, so byte pressure selects
    nothing at all and the one expired capture is attributed to age alone.
    """
    records = [
        _record("m0", days_old=40, size=10),
        _record("m1", days_old=1, size=10),
    ]

    plan = plan_retention(
        records,
        _config(max_age_days=7, max_managed_bytes=500, minimum_keep_count=1),
        now_utc=NOW,
    )

    assert _selected(plan) == ["m0"]
    assert plan.candidates[0].reason is RetentionReason.AGE


def test_no_capture_is_selected_twice_under_the_combined_policy() -> None:
    """The union of two overlapping prefixes selects each capture once."""
    records = [
        _record(f"m{index}", days_old=40 - index, size=100)
        for index in range(6)
    ]

    plan = plan_retention(
        records,
        _config(max_age_days=1, max_managed_bytes=100, minimum_keep_count=1),
        now_utc=NOW,
    )

    ids = _selected(plan)
    assert len(ids) == len(set(ids))


# --- ordering ---------------------------------------------------------------


def test_selection_is_oldest_first_whatever_the_input_order() -> None:
    """The plan does not inherit the order the catalogue happened to arrive in."""
    records = [
        _record("c", days_old=5),
        _record("a", days_old=30),
        _record("b", days_old=20),
    ]

    plan = plan_retention(
        records, _config(max_age_days=1, minimum_keep_count=1), now_utc=NOW
    )

    assert _selected(plan) == ["a", "b"]


def test_identical_capture_timestamps_break_on_creation_time() -> None:
    """Captures sharing an instant order by when their record was written.

    The identifiers deliberately sort the *opposite* way to the creation times,
    so this fails if the creation-time level is dropped and the order silently
    falls through to the capture id.
    """
    records = [
        _record("aaa-late", days_old=30, created_offset_seconds=9),
        _record("zzz-early", days_old=30, created_offset_seconds=1),
        _record("newest", days_old=1),
    ]

    plan = plan_retention(
        records, _config(max_age_days=1, minimum_keep_count=1), now_utc=NOW
    )

    assert _selected(plan) == ["zzz-early", "aaa-late"]


def test_identical_timestamps_fall_through_to_the_capture_id() -> None:
    """When both timestamps tie, the primary key makes the order total.

    Without this last level the plan would depend on SQLite's incidental row
    order, which is not a tie-break -- it is the absence of one.
    """
    records = [
        _record("bbb", days_old=30),
        _record("aaa", days_old=30),
        _record("ccc", days_old=30),
    ]

    plan = plan_retention(
        records, _config(max_age_days=1, minimum_keep_count=1), now_utc=NOW
    )

    assert _selected(plan) == ["aaa", "bbb"]


def test_the_plan_is_stable_across_repeated_evaluation() -> None:
    """The same inputs produce the same plan, every time."""
    records = [_record(f"m{index}", days_old=30) for index in range(8)]
    config = _config(max_age_days=1, minimum_keep_count=2, max_deletions_per_run=3)

    first = plan_retention(records, config, now_utc=NOW)
    second = plan_retention(list(reversed(records)), config, now_utc=NOW)

    assert first == second


# --- the preservation floor -------------------------------------------------


def test_minimum_keep_count_protects_the_newest_managed_captures() -> None:
    """The newest N are never selected, however expired they are."""
    records = [_record(f"m{index}", days_old=100 - index) for index in range(5)]

    plan = plan_retention(
        records, _config(max_age_days=1, minimum_keep_count=2), now_utc=NOW
    )

    assert _selected(plan) == ["m0", "m1", "m2"]
    assert plan.protected_count == 2


def test_a_keep_count_larger_than_the_catalogue_protects_everything() -> None:
    """Fewer managed captures than the floor means nothing is eligible."""
    records = [_record(f"m{index}", days_old=900) for index in range(3)]

    plan = plan_retention(
        records, _config(max_age_days=1, minimum_keep_count=100), now_utc=NOW
    )

    assert plan.candidates == ()
    assert plan.protected_count == 3


def test_the_floor_is_never_broken_to_satisfy_the_byte_limit() -> None:
    """Byte pressure does not out-rank preservation.

    The preserved captures alone are ten times the budget and the policy still
    refuses to touch them; it reports the target as unreachable instead.
    """
    records = [
        _record(f"m{index}", days_old=100 - index, size=1000)
        for index in range(5)
    ]

    plan = plan_retention(
        records, _config(max_managed_bytes=100, minimum_keep_count=3), now_utc=NOW
    )

    assert _selected(plan) == ["m0", "m1"]
    assert plan.byte_target_satisfiable is False
    assert plan.projected_managed_bytes == 3000


def test_a_reachable_byte_target_is_reported_as_satisfiable() -> None:
    """When the preserved set fits inside the budget, the target is reachable."""
    records = [
        _record(f"m{index}", days_old=100 - index, size=100)
        for index in range(5)
    ]

    plan = plan_retention(
        records, _config(max_managed_bytes=300, minimum_keep_count=3), now_utc=NOW
    )

    assert plan.byte_target_satisfiable is True


def test_satisfiability_is_true_when_no_byte_bound_is_configured() -> None:
    """With no byte target there is nothing to be unable to satisfy."""
    records = [_record(f"m{index}", days_old=900, size=10**9) for index in range(3)]

    plan = plan_retention(records, _config(max_age_days=1), now_utc=NOW)

    assert plan.byte_target_satisfiable is True


# --- the per-run destructive bound ------------------------------------------


def test_max_deletions_per_run_caps_the_plan() -> None:
    """A run may never newly select more than its configured bound."""
    records = [_record(f"m{index:02d}", days_old=100 - index) for index in range(20)]

    plan = plan_retention(
        records,
        _config(max_age_days=1, minimum_keep_count=1, max_deletions_per_run=4),
        now_utc=NOW,
    )

    assert len(plan.candidates) == 4
    assert _selected(plan) == ["m00", "m01", "m02", "m03"]


def test_a_capped_plan_reports_that_more_work_remains() -> None:
    """Truncation is reported, never silent."""
    records = [_record(f"m{index:02d}", days_old=100) for index in range(10)]

    plan = plan_retention(
        records,
        _config(max_age_days=1, minimum_keep_count=1, max_deletions_per_run=2),
        now_utc=NOW,
    )

    assert plan.more_work_remains is True


def test_an_uncapped_plan_reports_no_remaining_work() -> None:
    """A plan that fits inside the bound says so."""
    records = [_record(f"m{index}", days_old=100) for index in range(3)]

    plan = plan_retention(
        records,
        _config(max_age_days=1, minimum_keep_count=1, max_deletions_per_run=10),
        now_utc=NOW,
    )

    assert plan.more_work_remains is False


def test_a_plan_exactly_at_the_bound_reports_no_remaining_work() -> None:
    """Selecting exactly ``max_deletions_per_run`` leaves nothing behind."""
    records = [_record(f"m{index}", days_old=100) for index in range(4)]

    plan = plan_retention(
        records,
        _config(max_age_days=1, minimum_keep_count=1, max_deletions_per_run=3),
        now_utc=NOW,
    )

    assert len(plan.candidates) == 3
    assert plan.more_work_remains is False


def test_projected_reclaim_counts_only_the_capped_candidates() -> None:
    """The projection describes this run, not the whole eligible backlog."""
    records = [_record(f"m{index}", days_old=100, size=100) for index in range(10)]

    plan = plan_retention(
        records,
        _config(max_age_days=1, minimum_keep_count=1, max_deletions_per_run=3),
        now_utc=NOW,
    )

    assert plan.projected_bytes_reclaimed == 300
    assert plan.projected_managed_bytes == 700


# --- captures already claimed by the lifecycle table ------------------------


def test_already_deleted_captures_are_excluded_from_planning() -> None:
    """Reclaimed media has nothing left to plan about."""
    records = [
        _record("gone", days_old=900, state=MediaLifecycleState.DELETED, size=10**9),
        _record("here", days_old=900, size=100),
    ]

    plan = plan_retention(
        records, _config(max_age_days=1, minimum_keep_count=0 + 1), now_utc=NOW
    )

    assert _selected(plan) == []
    assert plan.managed_present_count == 1
    assert plan.managed_present_bytes == 100


def test_pending_captures_are_excluded_from_new_planning() -> None:
    """A capture with a durable intent belongs to the recovery path.

    Planning it again would let one deletion be claimed twice, which is exactly
    what the conditional lifecycle transitions exist to make impossible.
    """
    records = [
        _record(
            "claimed",
            days_old=900,
            state=MediaLifecycleState.PENDING_DELETE,
        ),
        _record("free", days_old=900),
        _record("young", days_old=1),
    ]

    plan = plan_retention(
        records, _config(max_age_days=1, minimum_keep_count=1), now_utc=NOW
    )

    assert _selected(plan) == ["free"]


def test_deleted_captures_do_not_count_towards_the_managed_byte_total() -> None:
    """Media that is already gone cannot be occupying the byte budget."""
    records = [
        _record("gone", days_old=900, state=MediaLifecycleState.DELETED, size=10**9),
        _record("here", days_old=1, size=50),
    ]

    plan = plan_retention(
        records, _config(max_managed_bytes=100, minimum_keep_count=1), now_utc=NOW
    )

    assert plan.managed_present_bytes == 50
    assert plan.candidates == ()


# --- the plan projection ----------------------------------------------------


def test_the_plan_projection_carries_no_media_paths() -> None:
    """A plan describes a decision; a path is only needed to act on one."""
    records = [_record("m0", days_old=900)]

    plan = plan_retention(
        records, _config(max_age_days=1, minimum_keep_count=0 + 1), now_utc=NOW
    )
    payload = plan.as_dict()

    assert "absolute_path" not in str(payload)
    assert "/captures/" not in str(payload)


def test_the_plan_projection_reports_every_decision_field() -> None:
    """A dry run's caller can see the whole decision without the objects."""
    records = [
        _record(f"m{index}", days_old=100 - index, size=100)
        for index in range(4)
    ]

    payload = plan_retention(
        records,
        _config(max_age_days=1, minimum_keep_count=1, max_deletions_per_run=2),
        now_utc=NOW,
    ).as_dict()

    assert payload["candidate_count"] == 2
    assert payload["managed_present_count"] == 4
    assert payload["managed_present_bytes"] == 400
    assert payload["protected_count"] == 1
    assert payload["projected_bytes_reclaimed"] == 200
    assert payload["projected_managed_bytes"] == 200
    assert payload["more_work_remains"] is True
    assert payload["byte_target_satisfiable"] is True
    assert [entry["policy_reason"] for entry in payload["candidates"]] == [
        "age",
        "age",
    ]
