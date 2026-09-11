"""Tests for catalogue-driven recognition eligibility and reconciliation.

Every capture root, media file and database here is created by the test under
``tmp_path``. Nothing reads the repository's ``data/`` directory or any real
capture, and nothing decodes an image: "media" is a few stand-in bytes.

Where the host cannot create the condition under test -- a symlink on a Windows
account without the privilege, a device node, an unreadable file -- the
retention filesystem seam is replaced rather than the test skipped. Replacing
the *retention* seam is deliberate: it is what proves recognition goes through
retention's own safety boundary instead of a copy of it.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

import mgo.recognition.reconciler as reconciler_module
import mgo.retention.service as retention_service
from mgo.core.database import apply_migrations, database_connection, utc_now_iso
from mgo.event_capture.admission import AUTOMATIC_ORIGIN
from mgo.event_capture.models import MotionTrigger
from mgo.motion.models import MotionStatus
from mgo.recognition.eligibility import (
    CatalogueCapture,
    IneligibilityReason,
    evaluate_eligibility,
    resolve_capture_root,
)
from mgo.recognition.models import (
    RecognitionConfigurationError,
    RecognitionJobState,
    RecognitionRepositoryError,
)
from mgo.recognition.reconciler import RecognitionReconciler, ReconcileReport
from mgo.recognition.repository import RecognitionRepository, format_timestamp
from mgo.retention.policy import MANAGED_ORIGIN

NOW = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)
WATERMARK = datetime(2026, 9, 1, tzinfo=UTC)
PIPELINE = "fake-0"
PAYLOAD = b"jpeg-bytes-stand-in"

_UNSET: Any = object()


class _Harness:
    """A temporary database, capture root and reconciler, wired together."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.root = tmp_path / "captures"
        self.root.mkdir()
        self.database_path = tmp_path / "mgo.db"
        apply_migrations(self.database_path)
        self.now = NOW
        self.repository = RecognitionRepository(
            self.database_path, clock=lambda: self.now
        )

    def add(
        self,
        identifier: str,
        *,
        captured_at: datetime = NOW - timedelta(hours=1),
        origin: str | None = "motion",
        extra_metadata: object = _UNSET,
        payload: bytes = PAYLOAD,
        filename: str | None = None,
        absolute_path: str | None = None,
        catalogue_size: object = _UNSET,
        captured_at_text: object = _UNSET,
        write_file: bool = True,
        camera_backend: str = "picamera2",
    ) -> Path:
        """Catalogue one capture and, by default, write its media."""
        name = filename if filename is not None else f"{identifier}.jpg"
        media = self.root / name
        if write_file:
            media.write_bytes(payload)
        if extra_metadata is _UNSET:
            extra_metadata = json.dumps({} if origin is None else {"origin": origin})
        stamp = (
            captured_at.isoformat() if captured_at_text is _UNSET else captured_at_text
        )
        with database_connection(self.database_path) as connection:
            connection.execute(
                """
                INSERT INTO captures (
                    id, filename, absolute_path, captured_at_utc, width, height,
                    filesize_bytes, camera_backend, created_at_utc, extra_metadata
                )
                VALUES (?, ?, ?, ?, 4608, 2592, ?, ?, ?, ?)
                """,
                (
                    identifier,
                    name,
                    absolute_path if absolute_path is not None else str(media),
                    stamp,
                    len(payload) if catalogue_size is _UNSET else catalogue_size,
                    camera_backend,
                    captured_at.isoformat(),
                    extra_metadata,
                ),
            )
        return media

    def add_lifecycle(self, capture_id: str, state: str = "pending_delete") -> None:
        deleted = utc_now_iso() if state == "deleted" else None
        with database_connection(self.database_path) as connection:
            connection.execute(
                "INSERT INTO capture_media_lifecycle VALUES (?, ?, ?, ?, 'age')",
                (capture_id, state, utc_now_iso(), deleted),
            )

    def reconciler(
        self,
        *,
        pipeline_version: str = PIPELINE,
        watermark: datetime = WATERMARK,
        capture_directory: Path | None = None,
        repository: RecognitionRepository | None = None,
    ) -> RecognitionReconciler:
        return RecognitionReconciler(
            repository or self.repository,
            pipeline_version=pipeline_version,
            capture_directory=capture_directory or self.root,
            enrolment_watermark=watermark,
            max_attempts=3,
        )

    def reconcile(self, **kwargs: Any) -> ReconcileReport:
        return self.reconciler(**kwargs).reconcile()

    def jobs(self) -> list[dict[str, Any]]:
        with database_connection(self.database_path) as connection:
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM recognition_jobs "
                    "ORDER BY capture_id, pipeline_version"
                )
            ]

    def queued(self, pipeline_version: str = PIPELINE) -> set[str]:
        return {
            str(job["capture_id"])
            for job in self.jobs()
            if job["pipeline_version"] == pipeline_version
        }

    def catalogue(self) -> tuple[list[tuple[Any, ...]], list[tuple[Any, ...]]]:
        with database_connection(self.database_path) as connection:
            captures = [
                tuple(row)
                for row in connection.execute("SELECT * FROM captures ORDER BY id")
            ]
            lifecycle = [
                tuple(row)
                for row in connection.execute(
                    "SELECT * FROM capture_media_lifecycle ORDER BY capture_id"
                )
            ]
        return captures, lifecycle


@pytest.fixture
def harness(tmp_path: Path) -> _Harness:
    return _Harness(tmp_path)


def _reasons(report: ReconcileReport) -> dict[IneligibilityReason, int]:
    return dict(report.ineligible)


def _assert_ineligible(
    harness: _Harness, identifier: str, reason: IneligibilityReason
) -> None:
    report = harness.reconcile()
    assert identifier not in harness.queued()
    assert report.created == 0
    assert _reasons(report).get(reason) == 1, _reasons(report)


# --- the origin contract ------------------------------------------------------


def test_recognition_uses_the_origin_capture_and_retention_agree_on() -> None:
    """One definition of "an automatic capture", shared by all three subsystems."""
    trigger = MotionTrigger(
        status=MotionStatus.MOTION_DETECTED,
        score=0.5,
        threshold=0.1,
        evaluated_at=NOW,
    )

    assert MANAGED_ORIGIN == AUTOMATIC_ORIGIN == "motion"
    assert trigger.capture_metadata()["origin"] == MANAGED_ORIGIN


def test_metadata_written_by_event_capture_is_eligible(harness: _Harness) -> None:
    """The real writer's metadata, not a hand-written imitation of it."""
    trigger = MotionTrigger(
        status=MotionStatus.MOTION_DETECTED,
        score=0.5,
        threshold=0.1,
        evaluated_at=NOW,
    )
    harness.add("cap-1", extra_metadata=json.dumps(trigger.capture_metadata()))

    report = harness.reconcile()

    assert harness.queued() == {"cap-1"}
    assert (report.examined, report.eligible, report.created) == (1, 1, 1)


# --- a valid capture ----------------------------------------------------------


def test_a_valid_motion_capture_receives_one_pending_job(harness: _Harness) -> None:
    harness.add("cap-1")

    report = harness.reconcile()

    assert report.created == 1
    [job] = harness.jobs()
    assert job["capture_id"] == "cap-1"
    assert job["pipeline_version"] == PIPELINE
    assert job["state"] == RecognitionJobState.PENDING.value
    assert job["attempt_count"] == 0
    assert job["max_attempts"] == 3
    assert job["next_attempt_at"] == format_timestamp(NOW)
    assert job["created_at"] == format_timestamp(NOW)
    assert job["camera_id"] is None
    for column in (
        "lease_owner",
        "lease_expires_at",
        "started_at",
        "finished_at",
        "error_category",
    ):
        assert job[column] is None


# --- metadata and origin ------------------------------------------------------


@pytest.mark.parametrize(
    "extra_metadata",
    [
        pytest.param("{not json", id="not-json"),
        pytest.param("", id="empty"),
        pytest.param('["motion"]', id="array"),
        pytest.param('"motion"', id="string"),
        pytest.param("null", id="null"),
        pytest.param("42", id="number"),
        # Python's integer-digit limit raises ValueError, not JSONDecodeError.
        pytest.param('{"origin": "motion", "n": ' + "9" * 5000 + "}", id="huge-int"),
        # Pathological nesting raises RecursionError.
        pytest.param('{"origin": "motion", "n": ' + "[" * 100_000 + "}", id="deep"),
    ],
)
def test_malformed_or_non_object_metadata_is_ineligible(
    harness: _Harness, extra_metadata: str
) -> None:
    harness.add("cap-1", extra_metadata=extra_metadata)

    _assert_ineligible(harness, "cap-1", IneligibilityReason.MALFORMED_METADATA)


def test_non_text_metadata_is_ineligible(harness: _Harness) -> None:
    """The column is not STRICT, so a number can be stored in it."""
    harness.add("cap-1", extra_metadata=12)

    _assert_ineligible(harness, "cap-1", IneligibilityReason.MALFORMED_METADATA)


def test_one_malformed_row_does_not_stop_enrolment_of_the_rest(
    harness: _Harness,
) -> None:
    """Refusal is per row: retention's fail-the-run rule is not borrowed."""
    harness.add("cap-bad", extra_metadata="{not json")
    harness.add("cap-huge", extra_metadata='{"n": ' + "9" * 5000 + "}")
    harness.add("cap-deep", extra_metadata="[" * 100_000)
    harness.add("cap-good")

    report = harness.reconcile()

    assert harness.queued() == {"cap-good"}
    assert _reasons(report) == {IneligibilityReason.MALFORMED_METADATA: 3}


def test_a_capture_with_no_origin_is_ineligible(harness: _Harness) -> None:
    harness.add("cap-1", origin=None)

    _assert_ineligible(harness, "cap-1", IneligibilityReason.NOT_MOTION_ORIGIN)


@pytest.mark.parametrize("origin", ["manual", "Motion", "motion ", "motion-test"])
def test_only_exactly_motion_origin_is_eligible(harness: _Harness, origin: str) -> None:
    harness.add("cap-1", origin=origin)

    _assert_ineligible(harness, "cap-1", IneligibilityReason.NOT_MOTION_ORIGIN)


@pytest.mark.parametrize("origin", [["motion"], {"origin": "motion"}, 1, True])
def test_a_non_string_origin_is_ineligible(harness: _Harness, origin: object) -> None:
    harness.add("cap-1", extra_metadata=json.dumps({"origin": origin}))

    _assert_ineligible(harness, "cap-1", IneligibilityReason.NOT_MOTION_ORIGIN)


# --- the production legacy catalogue ------------------------------------------


def _legacy_catalogue(harness: _Harness) -> None:
    """The three legacy row families the Task 15.0 audit found, twelve rows.

    Represented synthetically and portably: the historical paths lived under a
    pytest temporary directory and a relocated ``/home/pi/Projects`` checkout,
    which are reproduced here as absolute directories *outside* this test's
    capture root, because being outside the managed directory is the property
    that matters.
    """
    pytest_root = harness.tmp_path / "pytest-of-pi" / "pytest-7" / "test_capture0"
    relocated = harness.tmp_path / "home" / "pi" / "Projects" / "mgo" / "captures"
    pytest_root.mkdir(parents=True)
    relocated.mkdir(parents=True)

    # Four historical mock/pytest rows: mock backend, no origin, media never
    # present under the managed directory.
    for index in range(4):
        name = f"mock-{index}.jpg"
        harness.add(
            f"legacy-mock-{index}",
            origin=None,
            extra_metadata=json.dumps({"source": "pytest"}) if index % 2 else "{}",
            filename=name,
            absolute_path=str(pytest_root / name),
            write_file=False,
            camera_backend="mock",
            captured_at=NOW - timedelta(days=60),
        )
    # Four relocated-development rows: two absent, two present elsewhere.
    for index in range(4):
        name = f"dev-{index}.jpg"
        if index >= 2:
            (relocated / name).write_bytes(PAYLOAD)
        harness.add(
            f"legacy-dev-{index}",
            origin=None,
            filename=name,
            absolute_path=str(relocated / name),
            write_file=False,
            captured_at=NOW - timedelta(days=50),
        )
    # Four legacy rows inside the managed directory, media present, no origin.
    for index in range(4):
        harness.add(
            f"legacy-inside-{index}",
            origin=None,
            captured_at=NOW - timedelta(days=40),
        )


def test_every_legacy_catalogue_shape_is_ineligible(harness: _Harness) -> None:
    _legacy_catalogue(harness)

    report = harness.reconcile()

    assert harness.jobs() == []
    assert report.examined == 12
    assert _reasons(report) == {IneligibilityReason.NOT_MOTION_ORIGIN: 12}


def test_legacy_rows_are_excluded_even_if_the_watermark_is_moved_back(
    harness: _Harness,
) -> None:
    """Origin, not age, is what excludes them: an explicit backfill cannot."""
    _legacy_catalogue(harness)

    harness.reconcile(watermark=datetime(2000, 1, 1, tzinfo=UTC))

    assert harness.jobs() == []


@pytest.mark.parametrize("family", ["mock", "relocated"])
def test_a_legacy_shaped_path_with_a_motion_origin_is_still_unsafe(
    harness: _Harness, family: str
) -> None:
    """Defence in depth: a relocated path is refused by containment on its own."""
    outside = harness.tmp_path / family
    outside.mkdir()
    (outside / "cap-1.jpg").write_bytes(PAYLOAD)
    harness.add("cap-1", absolute_path=str(outside / "cap-1.jpg"), write_file=False)

    _assert_ineligible(harness, "cap-1", IneligibilityReason.UNSAFE_PATH)


# --- the enrolment watermark --------------------------------------------------


def test_a_capture_before_the_watermark_is_ineligible(harness: _Harness) -> None:
    harness.add("cap-1", captured_at=WATERMARK - timedelta(microseconds=1))

    _assert_ineligible(harness, "cap-1", IneligibilityReason.BEFORE_WATERMARK)


def test_a_capture_exactly_at_the_watermark_is_eligible(harness: _Harness) -> None:
    """The boundary is inclusive."""
    harness.add("cap-1", captured_at=WATERMARK)

    harness.reconcile()

    assert harness.queued() == {"cap-1"}


def test_a_capture_after_the_watermark_is_eligible(harness: _Harness) -> None:
    harness.add("cap-1", captured_at=WATERMARK + timedelta(seconds=1))

    harness.reconcile()

    assert harness.queued() == {"cap-1"}


def test_the_watermark_compares_instants_not_text(harness: _Harness) -> None:
    """A stored non-UTC offset is converted, not string-compared.

    Each row is chosen so that comparing the text would give the wrong answer.
    """
    # 01:30 UTC on the watermark day, but its text sorts before the watermark.
    harness.add("cap-1", captured_at_text="2026-08-31T23:30:00-02:00")
    # 23:00 UTC the day before, but its text sorts after the watermark.
    harness.add("cap-2", captured_at_text="2026-09-01T01:00:00+02:00")

    report = harness.reconcile()

    assert harness.queued() == {"cap-1"}
    assert _reasons(report) == {IneligibilityReason.BEFORE_WATERMARK: 1}


def test_protected_evidence_is_only_enrolled_by_an_explicit_earlier_watermark(
    harness: _Harness,
) -> None:
    """Task 13.2-style motion rows before the watermark are left alone."""
    evidence = [
        harness.add(f"evidence-{index}", captured_at=datetime(2026, 8, 3, tzinfo=UTC))
        for index in range(3)
    ]
    before = harness.catalogue()
    bytes_before = [path.read_bytes() for path in evidence]

    harness.reconcile()
    assert harness.jobs() == []

    harness.reconcile(watermark=datetime(2026, 8, 1, tzinfo=UTC))
    assert harness.queued() == {"evidence-0", "evidence-1", "evidence-2"}
    assert harness.catalogue() == before
    assert [path.read_bytes() for path in evidence] == bytes_before


def test_a_naive_watermark_is_refused(harness: _Harness) -> None:
    with pytest.raises(RecognitionConfigurationError):
        harness.reconciler(watermark=datetime(2026, 9, 1))


@pytest.mark.parametrize(
    "captured_at_text", ["2026-09-11T11:00:00", "yesterday", 20260911]
)
def test_an_unusable_capture_timestamp_is_ineligible(
    harness: _Harness, captured_at_text: object
) -> None:
    harness.add("cap-1", captured_at_text=captured_at_text)

    _assert_ineligible(harness, "cap-1", IneligibilityReason.INVALID_RECORD)


# --- media lifecycle ----------------------------------------------------------


@pytest.mark.parametrize("state", ["pending_delete", "deleted"])
def test_a_capture_with_a_lifecycle_row_is_ineligible(
    harness: _Harness, state: str
) -> None:
    media = harness.add("cap-1")
    harness.add_lifecycle("cap-1", state)

    _assert_ineligible(harness, "cap-1", IneligibilityReason.LIFECYCLE_RECORDED)
    assert media.read_bytes() == PAYLOAD


def test_a_lifecycle_row_committed_during_reconciliation_still_stops_the_job(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The insert re-checks the lifecycle table inside its own transaction."""
    harness.add("cap-1")
    real_enqueue = reconciler_module._enqueue

    def _retention_claims_it_first(*args: Any, **kwargs: Any) -> int:
        harness.add_lifecycle("cap-1")
        return real_enqueue(*args, **kwargs)

    monkeypatch.setattr(reconciler_module, "_enqueue", _retention_claims_it_first)

    report = harness.reconcile()

    assert report.eligible == 1
    assert report.created == 0
    assert harness.jobs() == []


# --- the media safety boundary ------------------------------------------------


def test_a_capture_whose_media_is_missing_is_ineligible(harness: _Harness) -> None:
    harness.add("cap-1", write_file=False)

    _assert_ineligible(harness, "cap-1", IneligibilityReason.MEDIA_MISSING)


def test_a_path_outside_the_capture_root_is_ineligible(harness: _Harness) -> None:
    outside = harness.tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "cap-1.jpg").write_bytes(PAYLOAD)
    harness.add("cap-1", absolute_path=str(outside / "cap-1.jpg"), write_file=False)

    _assert_ineligible(harness, "cap-1", IneligibilityReason.UNSAFE_PATH)


def test_a_traversal_that_escapes_the_root_is_ineligible(harness: _Harness) -> None:
    outside = harness.tmp_path / "outside"
    outside.mkdir()
    (outside / "cap-1.jpg").write_bytes(PAYLOAD)
    traversal = f"{harness.root}{os.sep}..{os.sep}outside{os.sep}cap-1.jpg"
    harness.add("cap-1", absolute_path=traversal, write_file=False)

    _assert_ineligible(harness, "cap-1", IneligibilityReason.UNSAFE_PATH)


def test_a_traversal_is_ineligible_even_when_it_resolves_inside_the_root(
    harness: _Harness,
) -> None:
    """``..`` is refused syntactically: resolving would erase the evidence."""
    (harness.root / "sub").mkdir()
    harness.add("cap-1")
    traversal = f"{harness.root}{os.sep}sub{os.sep}..{os.sep}cap-1.jpg"
    harness.add(
        "cap-2", filename="cap-1.jpg", absolute_path=traversal, write_file=False
    )

    report = harness.reconcile()

    assert harness.queued() == {"cap-1"}
    assert _reasons(report) == {IneligibilityReason.UNSAFE_PATH: 1}


def test_a_filename_that_disagrees_with_the_path_is_ineligible(
    harness: _Harness,
) -> None:
    media = harness.root / "actual.jpg"
    media.write_bytes(PAYLOAD)
    harness.add(
        "cap-1", filename="expected.jpg", absolute_path=str(media), write_file=False
    )

    _assert_ineligible(harness, "cap-1", IneligibilityReason.UNSAFE_PATH)


def test_a_symlinked_target_is_ineligible(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    media = harness.add("cap-1")
    monkeypatch.setattr(
        retention_service, "_is_symlink", lambda path: Path(path) == media
    )

    _assert_ineligible(harness, "cap-1", IneligibilityReason.UNSAFE_PATH)


@pytest.mark.skipif(
    not hasattr(os, "symlink") or os.name != "posix",
    reason="a real symlink needs POSIX here; the seam test covers Windows",
)
def test_a_real_symlink_is_ineligible_on_posix(harness: _Harness) -> None:
    outside = harness.tmp_path / "outside.jpg"
    outside.write_bytes(PAYLOAD)
    link = harness.root / "cap-1.jpg"
    os.symlink(outside, link)
    harness.add("cap-1", write_file=False)

    _assert_ineligible(harness, "cap-1", IneligibilityReason.UNSAFE_PATH)


def test_a_directory_at_the_catalogued_path_is_ineligible(harness: _Harness) -> None:
    (harness.root / "cap-1.jpg").mkdir()
    harness.add("cap-1", write_file=False)

    _assert_ineligible(harness, "cap-1", IneligibilityReason.UNSAFE_PATH)


def test_a_non_regular_target_is_ineligible(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A device node or socket: exists, is not a directory, is not a file."""
    harness.add("cap-1")
    monkeypatch.setattr(retention_service, "_is_regular_file", lambda path: False)

    _assert_ineligible(harness, "cap-1", IneligibilityReason.UNSAFE_PATH)


def test_a_size_mismatch_is_ineligible(harness: _Harness) -> None:
    harness.add("cap-1", catalogue_size=len(PAYLOAD) + 1)

    _assert_ineligible(harness, "cap-1", IneligibilityReason.SIZE_MISMATCH)


@pytest.mark.parametrize("size", [0, -5, "nineteen", 19.5])
def test_an_unusable_catalogue_size_is_ineligible(
    harness: _Harness, size: object
) -> None:
    harness.add("cap-1", catalogue_size=size)

    _assert_ineligible(harness, "cap-1", IneligibilityReason.INVALID_RECORD)


def test_a_relative_path_is_ineligible(harness: _Harness) -> None:
    harness.add("cap-1", absolute_path="captures/cap-1.jpg")

    _assert_ineligible(harness, "cap-1", IneligibilityReason.UNSAFE_PATH)


def test_a_relative_path_is_ineligible_even_when_the_cwd_would_resolve_it(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Standing in the capture root, only the absolute-path rule refuses it."""
    harness.add("cap-1", absolute_path="cap-1.jpg")
    monkeypatch.chdir(harness.root)

    _assert_ineligible(harness, "cap-1", IneligibilityReason.UNSAFE_PATH)


def test_media_the_host_will_not_describe_is_ineligible(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``stat`` refused by the host is not evidence the media is safe."""
    harness.add("cap-1")

    def _refuse(path: Path) -> int:
        raise PermissionError("denied")

    monkeypatch.setattr(retention_service, "_file_size", _refuse)

    _assert_ineligible(harness, "cap-1", IneligibilityReason.MEDIA_UNREADABLE)


# --- discovery is catalogue-driven --------------------------------------------


def test_an_unreferenced_file_is_invisible_to_recognition(harness: _Harness) -> None:
    (harness.root / "orphan.jpg").write_bytes(PAYLOAD)
    (harness.root / "nested").mkdir()
    (harness.root / "nested" / "orphan.jpg").write_bytes(PAYLOAD)

    report = harness.reconcile()

    assert report.examined == 0
    assert harness.jobs() == []


def test_reconciliation_never_lists_a_directory(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No directory-enumeration primitive is reached during a pass."""
    harness.add("cap-1")

    def _forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("recognition listed a directory")

    for name in ("listdir", "scandir", "walk"):
        monkeypatch.setattr(os, name, _forbidden)
    for name in ("iterdir", "glob", "rglob", "walk"):
        monkeypatch.setattr(Path, name, _forbidden)

    report = harness.reconcile()

    assert report.created == 1


# --- idempotency and concurrency ----------------------------------------------


def test_repeated_reconciliation_creates_nothing_new(harness: _Harness) -> None:
    harness.add("cap-1")
    harness.add("cap-2")

    first = harness.reconcile()
    second = harness.reconcile()
    third = harness.reconcile()

    assert (first.created, second.created, third.created) == (2, 0, 0)
    assert (second.examined, third.examined) == (0, 0)
    assert len(harness.jobs()) == 2


def test_an_interleaved_second_reconciler_cannot_duplicate_a_job(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both read before either inserts; the conflict clause settles it."""
    harness.add("cap-1")
    real_enqueue = reconciler_module._enqueue
    competitor = harness.reconciler(
        repository=RecognitionRepository(harness.database_path, clock=lambda: NOW)
    )
    competing: list[ReconcileReport] = []

    def _competitor_runs_first(*args: Any, **kwargs: Any) -> int:
        if not competing:
            monkeypatch.setattr(reconciler_module, "_enqueue", real_enqueue)
            competing.append(competitor.reconcile())
        return real_enqueue(*args, **kwargs)

    monkeypatch.setattr(reconciler_module, "_enqueue", _competitor_runs_first)

    report = harness.reconcile()

    assert competing[0].created == 1
    assert report.eligible == 1
    assert report.created == 0
    assert len(harness.jobs()) == 1


def test_concurrent_reconcilers_create_each_job_exactly_once(
    harness: _Harness,
) -> None:
    for index in range(40):
        harness.add(f"cap-{index:02d}")
    barrier = threading.Barrier(4)
    reports: list[ReconcileReport] = []
    errors: list[BaseException] = []

    def _run() -> None:
        reconciler = harness.reconciler(
            repository=RecognitionRepository(harness.database_path, clock=lambda: NOW)
        )
        barrier.wait()
        try:
            reports.append(reconciler.reconcile())
        except BaseException as exc:  # recorded and asserted on below
            errors.append(exc)

    threads = [threading.Thread(target=_run) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert errors == []
    assert sum(report.created for report in reports) == 40
    assert len(harness.jobs()) == 40


def test_a_new_pipeline_version_is_queued_separately(harness: _Harness) -> None:
    """Reprocessing is a new job; the earlier job is retained unchanged."""
    harness.add("cap-1")
    harness.reconcile(pipeline_version="fake-0")
    [original] = harness.jobs()

    report = harness.reconcile(pipeline_version="fake-1")

    assert report.created == 1
    jobs = harness.jobs()
    assert {job["pipeline_version"] for job in jobs} == {"fake-0", "fake-1"}
    assert next(job for job in jobs if job["pipeline_version"] == "fake-0") == original
    assert harness.reconcile(pipeline_version="fake-1").created == 0


# --- boundaries ---------------------------------------------------------------


def test_reconciliation_never_changes_the_catalogue_or_the_media(
    harness: _Harness,
) -> None:
    media = [harness.add(f"cap-{index}") for index in range(3)]
    harness.add("cap-old", captured_at=datetime(2026, 1, 1, tzinfo=UTC))
    harness.add("cap-manual", origin="manual")
    harness.add_lifecycle("cap-manual")
    before = harness.catalogue()
    listing = sorted(path.name for path in harness.root.iterdir())
    stats = [(path.stat().st_size, path.stat().st_mtime_ns) for path in media]

    harness.reconcile()
    harness.reconcile(pipeline_version="fake-1")

    assert harness.catalogue() == before
    assert sorted(path.name for path in harness.root.iterdir()) == listing
    assert [(path.stat().st_size, path.stat().st_mtime_ns) for path in media] == stats


@pytest.mark.parametrize("directory", ["relative/captures", "missing"])
def test_an_unusable_capture_directory_stops_reconciliation_before_reading(
    harness: _Harness, directory: str
) -> None:
    harness.add("cap-1")
    target = (
        Path(directory)
        if directory.startswith("relative")
        else harness.tmp_path / directory
    )

    with pytest.raises(RecognitionConfigurationError):
        harness.reconcile(capture_directory=target)

    assert harness.jobs() == []


def test_the_resolved_capture_root_follows_links_to_a_real_directory(
    tmp_path: Path,
) -> None:
    assert resolve_capture_root(tmp_path) == Path(os.path.realpath(tmp_path))
    assert resolve_capture_root(Path("relative")) is None
    assert resolve_capture_root(tmp_path / "absent") is None


def test_evaluate_eligibility_accepts_a_projection_directly(tmp_path: Path) -> None:
    """The rules are callable without a database, for later reuse."""
    (tmp_path / "cap-1.jpg").write_bytes(PAYLOAD)
    capture = CatalogueCapture(
        capture_id="cap-1",
        filename="cap-1.jpg",
        absolute_path=str(tmp_path / "cap-1.jpg"),
        captured_at_utc=NOW.isoformat(),
        filesize_bytes=len(PAYLOAD),
        extra_metadata='{"origin": "motion"}',
        lifecycle_recorded=False,
    )
    root = resolve_capture_root(tmp_path)
    assert root is not None

    assert (
        evaluate_eligibility(capture, capture_root=root, enrolment_watermark=WATERMARK)
        is None
    )


def test_a_reconciler_against_an_unmigrated_database_fails_cleanly(
    tmp_path: Path,
) -> None:
    """A schema-3 database has no job table; the error is bounded, not raw.

    The database is a full, current catalogue with only the version-4 tables
    removed, so the missing job table is the only thing that can fail.
    """
    database_path = tmp_path / "old.db"
    apply_migrations(database_path)
    connection = sqlite3.connect(database_path)
    connection.execute("DROP TABLE recognition_results")
    connection.execute("DROP TABLE recognition_jobs")
    connection.execute("DELETE FROM schema_migrations WHERE version = 4")
    connection.commit()
    connection.close()
    root = tmp_path / "captures"
    root.mkdir()

    reconciler = RecognitionReconciler(
        RecognitionRepository(database_path),
        pipeline_version=PIPELINE,
        capture_directory=root,
        enrolment_watermark=WATERMARK,
    )

    with pytest.raises(RecognitionRepositoryError):
        reconciler.reconcile()
