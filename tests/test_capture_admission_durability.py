"""Crash durability of automatic-capture admission (Task 14.5A, brief §7-§8).

The invariant under test:

    Every automatic-capture attempt that can consume persistent media capacity
    must remain represented after restart by either a durable counted
    capture/reservation, or a safely reconciled media artefact included in
    admission accounting.

A "crash" here is the only thing a single-process test can make it: the
controller that admitted the attempt is abandoned without a release, and a
brand-new controller -- the process that starts after the crash -- is built
over the same database, the same reservation directory and the same capture
directory. Whatever the new controller counts is what the invariant delivers.

Every boundary in the brief's list is a row in the table below. The real
:class:`CaptureWorkflow` is driven with a coordinator double that writes a
real file and an archive double that inserts a real row (or fails), so the
sequence "camera, guard, archive" is the shipped one.
"""

from __future__ import annotations

import json
import os
import stat
import uuid
from collections import namedtuple
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from mgo.camera.models import CaptureResult
from mgo.captures.archive import CaptureArchiveError
from mgo.captures.workflow import CapturePublicationRefused, CaptureWorkflow
from mgo.core.config import EventCaptureConfig
from mgo.core.database import apply_migrations, database_connection
from mgo.event_capture.admission import (
    RESERVATION_DIRECTORY_NAME,
    RESERVATION_SUFFIX,
    RESERVATION_SWEEP_AFTER,
    CaptureAdmissionController,
    ReservationLedger,
    SuppressionReason,
)

_Usage = namedtuple("_Usage", "total used free")

NOW = datetime(2026, 9, 7, 12, 0, 0, tzinfo=UTC)
JPEG = b"\xff\xd8" + b"x" * 500 + b"\xff\xd9"


class _Clock:
    def __init__(self, start: datetime = NOW) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now = self.now + timedelta(**delta)


def _config(
    *, hourly: int = 1, daily: int = 1, reserve: int = 10_000
) -> EventCaptureConfig:
    return EventCaptureConfig(
        enabled=True,
        max_captures_per_hour=hourly,
        max_captures_per_day=daily,
        minimum_free_bytes=1,
        maximum_capture_bytes=reserve,
    )


class _Paths:
    """The durable locations one "process" after another shares."""

    def __init__(self, tmp_path: Path) -> None:
        self.database = tmp_path / "db" / "mgo.db"
        self.database.parent.mkdir()
        apply_migrations(self.database)
        self.captures = tmp_path / "captures"
        self.captures.mkdir()
        self.reservations = self.database.parent / RESERVATION_DIRECTORY_NAME

    def controller(
        self, clock: _Clock, config: EventCaptureConfig | None = None
    ) -> CaptureAdmissionController:
        """A fresh process: no memory of anything, only the disk."""
        return CaptureAdmissionController(
            config or _config(),
            cooldown_seconds=0,
            database_path=self.database,
            capture_directory=self.captures,
            clock=clock,
            disk_usage=lambda path: _Usage(10**9, 0, 10**9),
        )

    def markers(self) -> list[Path]:
        if not self.reservations.exists():
            return []
        return sorted(
            p for p in self.reservations.iterdir() if p.suffix == RESERVATION_SUFFIX
        )

    def rows(self) -> int:
        with database_connection(self.database) as connection:
            return int(
                connection.execute("SELECT count(*) FROM captures").fetchone()[0]
            )

    def jpegs(self) -> list[Path]:
        return sorted(p for p in self.captures.iterdir() if p.suffix == ".jpg")


class _Coordinator:
    """Writes a real JPEG the way the capture service does, then returns."""

    def __init__(self, paths: _Paths, clock: _Clock, *, size: int = len(JPEG)) -> None:
        self._paths = paths
        self._clock = clock
        self._size = size

    def capture_image(self) -> CaptureResult:
        stamp = self._clock().strftime("%Y-%m-%dT%H-%M-%S.%fZ")
        destination = self._paths.captures / f"{stamp}.jpg"
        destination.write_bytes(b"x" * self._size)
        return CaptureResult(
            success=True,
            filename=destination.name,
            absolute_path=destination.resolve(),
            timestamp=self._clock(),
            width=4608,
            height=2592,
            filesize_bytes=self._size,
            backend="double",
        )


class _Archive:
    """Inserts a real motion-origin row, or fails the way the archive fails."""

    def __init__(self, paths: _Paths, *, fail: bool = False) -> None:
        self._paths = paths
        self._fail = fail

    def record_capture(
        self, result: CaptureResult, *, extra_metadata: Any = None
    ) -> Any:
        if self._fail:
            raise CaptureArchiveError("catalogue unavailable")
        identifier = str(uuid.uuid4())
        stamp = result.timestamp.astimezone(UTC).isoformat()
        with database_connection(self._paths.database) as connection:
            connection.execute(
                """
                INSERT INTO captures (
                    id, filename, absolute_path, captured_at_utc, width, height,
                    filesize_bytes, camera_backend, created_at_utc, extra_metadata
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    identifier,
                    result.filename,
                    str(result.absolute_path),
                    stamp,
                    result.width,
                    result.height,
                    result.filesize_bytes,
                    result.backend,
                    stamp,
                    json.dumps(extra_metadata or {}),
                ),
            )
        return SimpleNamespace(id=identifier, filename=result.filename)


def _oversize_guard(limit: int) -> Any:
    def guard(result: CaptureResult) -> None:
        if result.filesize_bytes > limit:
            raise CapturePublicationRefused("too large")

    return guard


# --- the boundary table -----------------------------------------------------------


def _crash_before_reservation(paths: _Paths, clock: _Clock) -> None:
    """Boundary 1: the trigger was refused, or the process died before admit."""
    controller = paths.controller(clock, _config(hourly=1))
    controller.evaluate()  # looked, did not reserve


def _crash_after_reservation(paths: _Paths, clock: _Clock) -> None:
    """Boundary 2: admitted, died before the camera ran."""
    assert paths.controller(clock).admit().admitted


def _crash_after_still_exists(paths: _Paths, clock: _Clock) -> None:
    """Boundary 3/5/6: admitted, the camera wrote the JPEG, died before archiving."""
    assert paths.controller(clock).admit().admitted
    _Coordinator(paths, clock).capture_image()


def _crash_after_size_validation(paths: _Paths, clock: _Clock) -> None:
    """Boundary 4: the guard passed; died between the guard and the archive."""
    assert paths.controller(clock).admit().admitted
    result = _Coordinator(paths, clock).capture_image()
    _oversize_guard(10_000)(result)


def _crash_after_commit_before_release(paths: _Paths, clock: _Clock) -> None:
    """Boundary 7: the row is committed; died before the marker was released."""
    controller = paths.controller(clock)
    assert controller.admit().admitted
    workflow = CaptureWorkflow(_Coordinator(paths, clock), _Archive(paths))  # type: ignore[arg-type]
    workflow.capture(extra_metadata={"origin": "motion"})
    # no controller.release(...): the process is gone


def _archive_failure_keeps_the_jpeg(paths: _Paths, clock: _Clock) -> None:
    """Boundary 10: the archive failed and the JPEG is deliberately kept.

    Not a crash -- the worker survives -- so the worker does what the shipped
    worker does: releases with ``succeeded=False``.
    """
    controller = paths.controller(clock)
    assert controller.admit().admitted
    workflow = CaptureWorkflow(  # type: ignore[arg-type]
        _Coordinator(paths, clock), _Archive(paths, fail=True)
    )
    with pytest.raises(CaptureArchiveError):
        workflow.capture(extra_metadata={"origin": "motion"})
    controller.release(succeeded=False)


BOUNDARIES = [
    pytest.param(_crash_before_reservation, 0, 0, 0, id="1-before-reservation"),
    pytest.param(_crash_after_reservation, 1, 0, 0, id="2-after-reservation"),
    pytest.param(_crash_after_still_exists, 1, 1, 0, id="3-still-exists"),
    pytest.param(_crash_after_size_validation, 1, 1, 0, id="4-after-size-validation"),
    pytest.param(_crash_after_still_exists, 1, 1, 0, id="5-after-publication"),
    pytest.param(_crash_after_still_exists, 1, 1, 0, id="6-before-row-insert"),
    pytest.param(_crash_after_commit_before_release, 1, 1, 1, id="7-after-commit"),
    pytest.param(_crash_after_reservation, 1, 0, 0, id="8-termination"),
    pytest.param(_crash_after_still_exists, 1, 1, 0, id="9-restart"),
    pytest.param(_archive_failure_keeps_the_jpeg, 1, 1, 0, id="10-archive-failure"),
]


@pytest.mark.parametrize(("boundary", "markers", "jpegs", "rows"), BOUNDARIES)
def test_every_boundary_leaves_the_attempt_represented_after_restart(
    tmp_path: Path, boundary: Any, markers: int, jpegs: int, rows: int
) -> None:
    """After the crash, the next process counts the attempt whenever it may
    have consumed media capacity -- and only then."""
    paths = _Paths(tmp_path)
    clock = _Clock()

    boundary(paths, clock)

    assert len(paths.markers()) == markers
    assert len(paths.jpegs()) == jpegs
    assert paths.rows() == rows

    restarted = paths.controller(clock, _config(hourly=1, daily=1))
    decision = restarted.evaluate()
    counted = markers + rows
    assert decision.hourly_count == counted
    assert decision.daily_count == counted
    if counted:
        assert decision.admitted is False
        assert decision.reason is SuppressionReason.HOURLY_LIMIT
    else:
        assert decision.admitted is True


def test_a_crash_after_the_camera_and_before_the_row_still_counts(
    tmp_path: Path,
) -> None:
    """The exact finding: JPEG on disk, no row, process gone -- and the next
    process must NOT admit again under a limit of one."""
    paths = _Paths(tmp_path)
    clock = _Clock()
    _crash_after_still_exists(paths, clock)
    assert paths.jpegs() and paths.rows() == 0

    restarted = paths.controller(clock, _config(hourly=1, daily=1))

    refused = restarted.admit()
    assert refused.admitted is False
    assert refused.reason is SuppressionReason.HOURLY_LIMIT
    assert refused.hourly_count == 1
    # And it wrote nothing of its own.
    assert len(paths.markers()) == 1


def test_a_crash_loop_cannot_exceed_the_quota(tmp_path: Path) -> None:
    """Restart, admit, crash, restart ... admits exactly ``hourly`` times."""
    paths = _Paths(tmp_path)
    clock = _Clock()
    admitted = 0
    for _ in range(10):
        controller = paths.controller(clock, _config(hourly=3, daily=10))
        decision = controller.admit()
        if decision.admitted:
            admitted += 1
            _Coordinator(paths, clock).capture_image()
        clock.advance(seconds=1)
        # crash: no release

    assert admitted == 3
    assert len(paths.jpegs()) == 3
    assert len(paths.markers()) == 3


def test_the_window_expires_a_crashed_attempt_exactly_as_it_would_a_row(
    tmp_path: Path,
) -> None:
    paths = _Paths(tmp_path)
    clock = _Clock()
    _crash_after_still_exists(paths, clock)

    later = _Clock(clock.now + timedelta(hours=1, seconds=1))
    restarted = paths.controller(later, _config(hourly=1, daily=1))

    decision = restarted.evaluate()
    assert decision.hourly_count == 0
    assert decision.daily_count == 1  # still the same UTC day
    assert decision.reason is SuppressionReason.DAILY_LIMIT


def test_a_double_count_after_commit_is_conservative_and_swept(tmp_path: Path) -> None:
    """Boundary 7 counts twice until the marker is older than every window."""
    paths = _Paths(tmp_path)
    clock = _Clock()
    _crash_after_commit_before_release(paths, clock)
    assert (
        paths.controller(clock, _config(hourly=5, daily=5)).evaluate().hourly_count == 2
    )

    clock.advance(hours=48, seconds=1)
    swept = paths.controller(clock, _config(hourly=5, daily=5))
    swept.evaluate()

    assert paths.markers() == []


# --- the oversize path ---------------------------------------------------------------


def test_an_oversize_still_is_removed_and_its_attempt_still_counts(
    tmp_path: Path,
) -> None:
    paths = _Paths(tmp_path)
    clock = _Clock()
    controller = paths.controller(clock, _config(hourly=1, daily=1, reserve=100))
    assert controller.admit().admitted
    workflow = CaptureWorkflow(  # type: ignore[arg-type]
        _Coordinator(paths, clock, size=200), _Archive(paths)
    )

    with pytest.raises(CapturePublicationRefused) as caught:
        workflow.capture(publication_guard=_oversize_guard(100))
    controller.release(succeeded=False)

    assert caught.value.discarded is True
    assert paths.jpegs() == []
    assert paths.rows() == 0
    assert len(paths.markers()) == 1
    assert paths.controller(clock, _config(hourly=1)).evaluate().admitted is False


def test_an_oversize_cleanup_failure_is_surfaced_and_still_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A JPEG that could not be removed stays counted -- by the reservation for
    the window, and by the free-space probe for as long as it exists."""
    paths = _Paths(tmp_path)
    clock = _Clock()
    controller = paths.controller(clock, _config(hourly=1, daily=1, reserve=100))
    assert controller.admit().admitted
    workflow = CaptureWorkflow(  # type: ignore[arg-type]
        _Coordinator(paths, clock, size=200), _Archive(paths)
    )

    def refuse_unlink(self: Path, *args: Any, **kwargs: Any) -> None:
        raise PermissionError("held open by another process")

    monkeypatch.setattr(Path, "unlink", refuse_unlink)
    with pytest.raises(CapturePublicationRefused) as caught:
        workflow.capture(publication_guard=_oversize_guard(100))
    monkeypatch.undo()
    controller.release(succeeded=False)

    assert caught.value.discarded is False
    assert len(paths.jpegs()) == 1  # the orphan is visible for reconciliation
    assert len(paths.markers()) == 1
    assert paths.controller(clock, _config(hourly=1)).evaluate().admitted is False


def test_a_refused_capture_that_is_not_a_regular_file_is_left_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one unlink in the workflow never follows a link or hits a directory.

    A symlink cannot be created by every account on every host, so the kernel
    is stood in for: ``lstat`` reports a symbolic link at the capture path.
    """
    paths = _Paths(tmp_path)
    clock = _Clock()
    workflow = CaptureWorkflow(  # type: ignore[arg-type]
        _Coordinator(paths, clock, size=200), _Archive(paths)
    )
    real_lstat = os.lstat

    def lstat_as_link(path: Any, *args: Any, **kwargs: Any) -> Any:
        details = real_lstat(path, *args, **kwargs)
        mode = stat.S_IFLNK | (details.st_mode & 0o777)
        return os.stat_result((mode, *tuple(details)[1:]))

    monkeypatch.setattr("mgo.captures.workflow.os.lstat", lstat_as_link)

    with pytest.raises(CapturePublicationRefused) as caught:
        workflow.capture(publication_guard=_oversize_guard(100))

    assert caught.value.discarded is False
    assert len(paths.jpegs()) == 1  # nothing was unlinked


# --- the ledger itself ---------------------------------------------------------------


def test_the_ledger_writes_beside_the_database_by_default(tmp_path: Path) -> None:
    paths = _Paths(tmp_path)
    controller = paths.controller(_Clock())
    assert controller.reservation_directory == paths.reservations
    assert controller.admit().admitted
    (marker,) = paths.markers()
    assert marker.parent == paths.reservations
    payload = json.loads(marker.read_text(encoding="utf-8"))
    assert payload["reserved_at"] == NOW.isoformat()
    assert marker.name.startswith(NOW.strftime("%Y%m%dT%H%M%S%fZ"))


def test_stray_files_in_the_ledger_directory_are_ignored(tmp_path: Path) -> None:
    ledger = ReservationLedger(tmp_path / "ledger")
    (tmp_path / "ledger").mkdir()
    (tmp_path / "ledger" / "notes.txt").write_text("not a reservation")
    (tmp_path / "ledger" / ".DS_Store").write_bytes(b"")

    assert ledger.count_since(NOW - timedelta(days=365)) == 0
    assert ledger.newest_at() is None


def test_a_marker_whose_name_cannot_be_parsed_counts_by_its_mtime(
    tmp_path: Path,
) -> None:
    ledger = ReservationLedger(tmp_path / "ledger")
    (tmp_path / "ledger").mkdir()
    odd = tmp_path / "ledger" / f"garbled{RESERVATION_SUFFIX}"
    odd.write_text("{}")

    assert ledger.count_since(datetime.now(UTC) - timedelta(minutes=5)) == 1
    assert ledger.count_since(datetime.now(UTC) + timedelta(minutes=5)) == 0


def test_a_marker_whose_age_cannot_be_read_at_all_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = ReservationLedger(tmp_path / "ledger")
    (tmp_path / "ledger").mkdir()
    (tmp_path / "ledger" / f"garbled{RESERVATION_SUFFIX}").write_text("{}")
    monkeypatch.setattr(
        ReservationLedger, "_reserved_at", staticmethod(lambda path: None)
    )

    assert ledger.count_since(NOW) == 1
    assert ledger.sweep(NOW + timedelta(days=30)) == 0  # never swept blind


def test_the_sweep_removes_only_markers_older_than_every_window(tmp_path: Path) -> None:
    ledger = ReservationLedger(tmp_path / "ledger")
    old = ledger.reserve(NOW - RESERVATION_SWEEP_AFTER - timedelta(seconds=1))
    edge = ledger.reserve(NOW - RESERVATION_SWEEP_AFTER)
    fresh = ledger.reserve(NOW)
    future = ledger.reserve(NOW + timedelta(hours=3))

    assert ledger.sweep(NOW) == 1

    assert not old.path.exists()
    assert edge.path.exists() and fresh.path.exists() and future.path.exists()


def test_an_unlistable_ledger_refuses_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _Paths(tmp_path)
    controller = paths.controller(_Clock())

    def explode(self: ReservationLedger) -> list[Path]:
        raise PermissionError("ledger unreadable")

    monkeypatch.setattr(ReservationLedger, "_markers", explode)

    decision = controller.admit()
    assert decision.admitted is False
    assert decision.reason is SuppressionReason.ADMISSION_ERROR


def test_an_unwritable_ledger_refuses_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An attempt that cannot be made durable does not start."""
    paths = _Paths(tmp_path)
    controller = paths.controller(_Clock())

    def explode(self: ReservationLedger, now: datetime) -> Any:
        raise PermissionError("ledger read-only")

    monkeypatch.setattr(ReservationLedger, "reserve", explode)

    decision = controller.admit()
    assert decision.admitted is False
    assert decision.reason is SuppressionReason.ADMISSION_ERROR
    assert controller.in_flight == 0
    assert paths.markers() == []


def test_a_reservation_also_spaces_the_cooldown(tmp_path: Path) -> None:
    """A failed attempt is "the newest capture" for cooldown purposes too."""
    paths = _Paths(tmp_path)
    clock = _Clock()
    controller = CaptureAdmissionController(
        _config(hourly=5, daily=5),
        cooldown_seconds=30,
        database_path=paths.database,
        capture_directory=paths.captures,
        clock=clock,
        disk_usage=lambda path: _Usage(10**9, 0, 10**9),
    )
    assert controller.admit().admitted
    controller.release(succeeded=False)

    clock.advance(seconds=10)
    assert controller.admit().reason is SuppressionReason.COOLDOWN
    clock.advance(seconds=21)
    assert controller.admit().admitted


def test_a_database_restored_from_backup_does_not_forget_attempts(
    tmp_path: Path,
) -> None:
    """Markers live beside the database, not inside it: a restore that drops
    today's rows still finds today's reservations."""
    paths = _Paths(tmp_path)
    clock = _Clock()
    _crash_after_still_exists(paths, clock)
    # "Restore": a fresh database with none of today's rows.
    paths.database.unlink()
    apply_migrations(paths.database)

    decision = paths.controller(clock, _config(hourly=1)).evaluate()

    assert decision.hourly_count == 1
    assert decision.admitted is False
