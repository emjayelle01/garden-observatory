"""Tests for automatic-capture admission control (Task 14.5).

Every quota decision here is made against a real SQLite catalogue created by
the migration runner in ``tmp_path``, with a controllable clock and an
injectable free-space probe. Nothing touches a camera, the repository's
``data/`` directory, or any path outside the test root. Wall-clock time is
never the mechanism: the clock is a closure the test advances.
"""

from __future__ import annotations

import json
import shutil
import threading
import uuid
from collections import namedtuple
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from mgo.camera.models import CaptureResult
from mgo.captures.workflow import CapturePublicationRefused, CaptureWorkflow
from mgo.core.config import EventCaptureConfig
from mgo.core.database import apply_migrations, database_connection
from mgo.event_capture.admission import (
    AUTOMATIC_ORIGIN,
    AdmissionDecision,
    CaptureAdmissionController,
    QuotaLedger,
    SuppressionReason,
    free_bytes_for_capture,
    storage_floor_breached,
)

_Usage = namedtuple("_Usage", "total used free")

NOW = datetime(2026, 9, 7, 12, 0, 0, tzinfo=UTC)
FLOOR = 1_000
RESERVE = 100


class _Clock:
    def __init__(self, start: datetime = NOW) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now = self.now + timedelta(**delta)


def _config(
    *,
    hourly: int = 3,
    daily: int = 5,
    floor: int = FLOOR,
    reserve: int = RESERVE,
) -> EventCaptureConfig:
    return EventCaptureConfig(
        enabled=True,
        max_captures_per_hour=hourly,
        max_captures_per_day=daily,
        minimum_free_bytes=floor,
        maximum_capture_bytes=reserve,
    )


def _database(tmp_path: Path) -> Path:
    path = tmp_path / "mgo.db"
    apply_migrations(path)
    return path


def _insert(
    database: Path,
    captured_at: datetime | str,
    *,
    origin: str | None = AUTOMATIC_ORIGIN,
    metadata: str | None = None,
) -> str:
    """Catalogue one capture directly, the way the archive would."""
    identifier = str(uuid.uuid4())
    stored = (
        captured_at.astimezone(UTC).isoformat()
        if isinstance(captured_at, datetime)
        else captured_at
    )
    if metadata is None:
        metadata = json.dumps({} if origin is None else {"origin": origin})
    with database_connection(database) as connection:
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
                f"{identifier}.jpg",
                f"/captures/{identifier}.jpg",
                stored,
                4608,
                2592,
                1_000,
                "simulator",
                stored,
                metadata,
            ),
        )
    return identifier


def _row_count(database: Path) -> int:
    with database_connection(database) as connection:
        return int(connection.execute("SELECT count(*) FROM captures").fetchone()[0])


def _controller(
    tmp_path: Path,
    *,
    config: EventCaptureConfig | None = None,
    clock: _Clock | None = None,
    free: int | None = 10_000,
    cooldown: float = 5.0,
    camera_available: Callable[[], bool] | None = None,
    database: Path | None = None,
    capture_directory: Path | None = None,
    disk_usage: Any = None,
) -> tuple[CaptureAdmissionController, Path, _Clock]:
    database_path = database if database is not None else _database(tmp_path)
    directory = (
        capture_directory if capture_directory is not None else tmp_path / "captures"
    )
    directory.mkdir(parents=True, exist_ok=True)
    clock = clock or _Clock()

    if disk_usage is None:

        def disk_usage(path: Path) -> _Usage:
            if free is None:
                raise OSError("probe failed")
            return _Usage(total=1_000_000, used=1_000_000 - free, free=free)

    controller = CaptureAdmissionController(
        config or _config(),
        cooldown_seconds=cooldown,
        database_path=database_path,
        capture_directory=directory,
        clock=clock,
        disk_usage=disk_usage,
        camera_available=camera_available,
    )
    return controller, database_path, clock


# --- construction ------------------------------------------------------------


def test_a_controller_without_every_limit_cannot_be_built(tmp_path: Path) -> None:
    """A gate with a missing limit is not a gate."""
    for missing in (
        "max_captures_per_hour",
        "max_captures_per_day",
        "minimum_free_bytes",
    ):
        values: dict[str, Any] = {
            "enabled": True,
            "max_captures_per_hour": 1,
            "max_captures_per_day": 1,
            "minimum_free_bytes": 1,
        }
        values[missing] = None
        with pytest.raises(ValueError, match="every hard limit"):
            CaptureAdmissionController(
                EventCaptureConfig(**values),
                cooldown_seconds=0,
                database_path=tmp_path / "mgo.db",
                capture_directory=tmp_path,
            )


# --- first capture and reservations -------------------------------------------


def test_the_first_capture_is_admitted_and_reserved(tmp_path: Path) -> None:
    controller, database, _ = _controller(tmp_path)

    decision = controller.admit()

    assert decision.admitted is True
    assert decision.reason is None
    # The reservation is counted as the capture it is about to become.
    assert decision.hourly_count == 1
    assert decision.daily_count == 1
    assert decision.hourly_remaining == 2
    assert decision.daily_remaining == 4
    assert controller.in_flight == 1
    # Admission wrote nothing: the catalogue is exactly as it was.
    assert _row_count(database) == 0


def test_evaluate_reserves_nothing(tmp_path: Path) -> None:
    controller, _, _ = _controller(tmp_path)

    decision = controller.evaluate()

    assert decision.admitted is True
    assert decision.hourly_count == 0
    assert controller.in_flight == 0


def test_a_failed_capture_keeps_its_reservation_for_the_window(
    tmp_path: Path,
) -> None:
    """An admitted attempt that produced no row still counts -- until it ages out.

    Task 14.5A: the reservation is durable and is removed only by a committed
    catalogue row. A failure keeps the marker, so a camera that fails on every
    attempt cannot be hammered inside the window; the window then expires it,
    exactly as it would a row.
    """
    controller, _, clock = _controller(
        tmp_path, config=_config(hourly=1, daily=2), cooldown=0
    )

    first = controller.admit()
    assert first.admitted
    blocked = controller.admit()
    assert blocked.admitted is False
    assert blocked.reason is SuppressionReason.HOURLY_LIMIT

    controller.release(succeeded=False)  # the attempt failed; no row was written

    assert controller.in_flight == 0
    still_blocked = controller.admit()
    assert still_blocked.admitted is False
    assert still_blocked.reason is SuppressionReason.HOURLY_LIMIT
    assert still_blocked.hourly_count == 1

    clock.advance(hours=1, seconds=1)
    again = controller.admit()
    assert again.admitted is True
    assert again.hourly_count == 1  # the new reservation alone
    assert again.daily_count == 2  # the failed attempt still counts for the day


def test_a_catalogued_capture_hands_its_count_to_the_row(tmp_path: Path) -> None:
    """On success the marker goes and the row carries the count: never two."""
    controller, database, clock = _controller(
        tmp_path, config=_config(hourly=2, daily=2), cooldown=0
    )
    admitted = controller.admit()
    assert admitted.admitted
    _insert(database, clock.now)  # the archive committed the row

    controller.release(succeeded=True)

    assert controller.in_flight == 0
    assert list(controller.reservation_directory.iterdir()) == []
    decision = controller.evaluate()
    assert decision.hourly_count == 1
    assert decision.daily_count == 1


def test_release_with_nothing_held_is_a_no_op(tmp_path: Path) -> None:
    controller, _, _ = _controller(tmp_path)
    controller.release(succeeded=False)
    controller.release(succeeded=True)
    assert controller.in_flight == 0
    assert not controller.reservation_directory.exists()


# --- rolling hour ----------------------------------------------------------------


def test_a_row_exactly_one_hour_old_still_counts(tmp_path: Path) -> None:
    """The window is closed at its start: ``captured_at >= now - 1h``."""
    controller, database, clock = _controller(tmp_path, config=_config(hourly=1))
    _insert(database, clock.now - timedelta(hours=1))

    decision = controller.admit()

    assert decision.admitted is False
    assert decision.reason is SuppressionReason.HOURLY_LIMIT
    assert decision.hourly_count == 1


def test_a_row_just_older_than_an_hour_has_expired(tmp_path: Path) -> None:
    controller, database, clock = _controller(tmp_path, config=_config(hourly=1))
    _insert(database, clock.now - timedelta(hours=1, microseconds=1))

    decision = controller.admit()

    assert decision.admitted is True
    assert decision.hourly_count == 1  # the reservation only


def test_the_hourly_limit_plus_one_is_refused(tmp_path: Path) -> None:
    controller, database, clock = _controller(
        tmp_path, config=_config(hourly=2, daily=10)
    )
    _insert(database, clock.now - timedelta(minutes=30))
    _insert(database, clock.now - timedelta(minutes=10))

    decision = controller.admit()

    assert decision.admitted is False
    assert decision.reason is SuppressionReason.HOURLY_LIMIT
    assert decision.hourly_count == 2
    assert decision.hourly_remaining == 0
    assert controller.in_flight == 0


def test_the_rolling_window_expires_captures_as_time_passes(tmp_path: Path) -> None:
    controller, database, clock = _controller(
        tmp_path, config=_config(hourly=1, daily=10)
    )
    _insert(database, clock.now - timedelta(minutes=59))

    assert controller.admit().reason is SuppressionReason.HOURLY_LIMIT

    clock.advance(minutes=2)

    assert controller.admit().admitted is True


# --- UTC day ---------------------------------------------------------------------


def test_the_daily_limit_plus_one_is_refused(tmp_path: Path) -> None:
    controller, database, clock = _controller(
        tmp_path, config=_config(hourly=10, daily=2)
    )
    _insert(database, clock.now - timedelta(hours=5))
    _insert(database, clock.now - timedelta(hours=3))

    decision = controller.admit()

    assert decision.admitted is False
    assert decision.reason is SuppressionReason.DAILY_LIMIT
    assert decision.daily_count == 2
    assert decision.hourly_count == 0


def test_the_day_boundary_is_utc_midnight(tmp_path: Path) -> None:
    """A capture at 00:00:00Z today counts; one at 23:59:59Z yesterday does not."""
    controller, database, clock = _controller(
        tmp_path, config=_config(hourly=10, daily=1)
    )
    midnight = clock.now.replace(hour=0, minute=0, second=0, microsecond=0)
    _insert(database, midnight - timedelta(seconds=1))

    assert controller.evaluate().daily_count == 0

    _insert(database, midnight)

    decision = controller.admit()
    assert decision.reason is SuppressionReason.DAILY_LIMIT


def test_the_daily_count_resets_at_utc_midnight_but_the_hour_does_not(
    tmp_path: Path,
) -> None:
    clock = _Clock(datetime(2026, 9, 7, 23, 30, tzinfo=UTC))
    controller, database, _ = _controller(
        tmp_path, config=_config(hourly=1, daily=1), clock=clock
    )
    _insert(database, clock.now - timedelta(minutes=10))

    assert controller.evaluate().reason is SuppressionReason.HOURLY_LIMIT

    clock.advance(minutes=45)  # 00:15Z next day; the row is 55 minutes old

    decision = controller.evaluate()
    assert decision.daily_count == 0
    assert decision.hourly_count == 1
    assert decision.reason is SuppressionReason.HOURLY_LIMIT


# --- durability ----------------------------------------------------------------


def test_a_new_process_reconstructs_the_count_from_rows(tmp_path: Path) -> None:
    """Restart reconstruction: nothing about the quota lives in memory."""
    database = _database(tmp_path)
    first, _, clock = _controller(tmp_path, config=_config(hourly=1), database=database)
    _insert(database, clock.now - timedelta(minutes=5))

    # A brand-new controller -- a restarted service -- with no memory at all.
    second, _, _ = _controller(
        tmp_path, config=_config(hourly=1), database=database, clock=clock
    )

    assert first.evaluate().reason is SuppressionReason.HOURLY_LIMIT
    assert second.evaluate().reason is SuppressionReason.HOURLY_LIMIT


def test_two_concurrent_admissions_cannot_both_pass(tmp_path: Path) -> None:
    controller, _, _ = _controller(tmp_path, config=_config(hourly=1, daily=1))
    barrier = threading.Barrier(2)
    decisions: list[AdmissionDecision] = []

    def attempt() -> None:
        barrier.wait(timeout=5)
        decisions.append(controller.admit())

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert sorted(d.admitted for d in decisions) == [False, True]
    assert controller.in_flight == 1


# --- clock and provenance edge cases -------------------------------------------


def test_a_row_stamped_in_the_future_counts(tmp_path: Path) -> None:
    """Clock reversal over-counts, never under-counts."""
    controller, database, clock = _controller(
        tmp_path, config=_config(hourly=1), cooldown=0
    )
    _insert(database, clock.now + timedelta(hours=3))

    assert controller.admit().reason is SuppressionReason.HOURLY_LIMIT


def test_a_row_stamped_in_the_future_is_inside_the_cooldown(tmp_path: Path) -> None:
    """With a cooldown, a future-stamped newest capture is "just now"."""
    controller, database, clock = _controller(tmp_path, cooldown=5.0)
    _insert(database, clock.now + timedelta(hours=3))

    assert controller.admit().reason is SuppressionReason.COOLDOWN


@pytest.mark.parametrize(
    "metadata",
    ["not json", '{"origin": ', "[1, 2]", '"motion"', "null", "42"],
)
def test_unparseable_or_non_object_metadata_inside_the_window_counts(
    tmp_path: Path, metadata: str
) -> None:
    """The archive only writes objects; anything else fails closed and counts."""
    controller, database, clock = _controller(
        tmp_path, config=_config(hourly=1), cooldown=0
    )
    _insert(database, clock.now - timedelta(minutes=1), metadata=metadata)

    decision = controller.admit()

    assert decision.reason is SuppressionReason.HOURLY_LIMIT


@pytest.mark.parametrize("origin", [None, "manual", "MOTION", "motion "])
def test_only_the_exact_automatic_origin_counts(
    tmp_path: Path, origin: str | None
) -> None:
    controller, database, clock = _controller(tmp_path, config=_config(hourly=1))
    _insert(database, clock.now - timedelta(minutes=1), origin=origin)

    decision = controller.admit()

    assert decision.admitted is True
    assert decision.hourly_count == 1  # the reservation only


def test_an_unparseable_timestamp_inside_the_prefilter_counts(tmp_path: Path) -> None:
    controller, database, _ = _controller(
        tmp_path, config=_config(hourly=1), cooldown=0
    )
    # Sorts after the cutoff string but is not a datetime.
    _insert(database, "9999-not-a-timestamp")

    assert controller.admit().reason is SuppressionReason.HOURLY_LIMIT


def test_a_naive_timestamp_inside_the_prefilter_counts(tmp_path: Path) -> None:
    controller, database, clock = _controller(
        tmp_path, config=_config(hourly=1), cooldown=0
    )
    _insert(database, clock.now.replace(tzinfo=None).isoformat())

    assert controller.admit().reason is SuppressionReason.HOURLY_LIMIT


def test_newest_automatic_capture_ignores_manual_rows(tmp_path: Path) -> None:
    database = _database(tmp_path)
    ledger = QuotaLedger(database)
    assert ledger.newest_automatic_capture_at() is None
    _insert(database, NOW, origin="manual")
    assert ledger.newest_automatic_capture_at() is None
    _insert(database, NOW - timedelta(hours=2))
    assert ledger.newest_automatic_capture_at() == NOW - timedelta(hours=2)


# --- cooldown -------------------------------------------------------------------


def test_a_capture_inside_the_cooldown_is_refused(tmp_path: Path) -> None:
    controller, database, clock = _controller(tmp_path, cooldown=5.0)
    _insert(database, clock.now - timedelta(seconds=2))

    decision = controller.admit()

    assert decision.admitted is False
    assert decision.reason is SuppressionReason.COOLDOWN


def test_a_capture_after_the_cooldown_is_admitted(tmp_path: Path) -> None:
    controller, database, clock = _controller(tmp_path, cooldown=5.0)
    _insert(database, clock.now - timedelta(seconds=5))

    assert controller.admit().admitted is True


def test_cooldown_is_checked_before_the_quotas(tmp_path: Path) -> None:
    controller, database, clock = _controller(
        tmp_path, config=_config(hourly=1), cooldown=60.0
    )
    _insert(database, clock.now - timedelta(seconds=10))

    assert controller.admit().reason is SuppressionReason.COOLDOWN


# --- database failure ------------------------------------------------------------


def test_a_database_error_refuses_admission(tmp_path: Path) -> None:
    broken = tmp_path / "not-a-database"
    broken.mkdir()
    controller, _, _ = _controller(tmp_path, database=broken)

    decision = controller.admit()

    assert decision.admitted is False
    assert decision.reason is SuppressionReason.ADMISSION_ERROR
    assert controller.in_flight == 0


# --- storage reserve ---------------------------------------------------------------


def test_free_space_exactly_at_the_reserve_is_admitted(tmp_path: Path) -> None:
    controller, _, _ = _controller(tmp_path, free=FLOOR + RESERVE)

    decision = controller.admit()

    assert decision.admitted is True
    assert decision.storage_reserve_ok is True
    assert decision.storage_free_bytes == FLOOR + RESERVE


def test_one_byte_short_of_the_reserve_is_refused(tmp_path: Path) -> None:
    controller, _, _ = _controller(tmp_path, free=FLOOR + RESERVE - 1)

    decision = controller.admit()

    assert decision.admitted is False
    assert decision.reason is SuppressionReason.STORAGE_RESERVE
    assert decision.storage_reserve_ok is False
    assert controller.in_flight == 0


def test_a_failed_free_space_probe_refuses(tmp_path: Path) -> None:
    controller, _, _ = _controller(tmp_path, free=None)

    decision = controller.admit()

    assert decision.admitted is False
    assert decision.reason is SuppressionReason.STORAGE_RESERVE
    assert decision.storage_free_bytes is None
    assert decision.storage_reserve_ok is False


def test_a_probe_returning_nonsense_refuses(tmp_path: Path) -> None:
    def usage(path: Path) -> Any:
        return object()

    controller, _, _ = _controller(tmp_path, disk_usage=usage)

    assert controller.admit().reason is SuppressionReason.STORAGE_RESERVE


def test_a_missing_capture_directory_probes_its_nearest_ancestor(
    tmp_path: Path,
) -> None:
    """The capture service creates the directory on first use; the probe must
    answer for the filesystem it would be created on."""
    probed: list[Path] = []

    def usage(path: Path) -> _Usage:
        probed.append(path)
        return _Usage(1, 0, 10_000)

    missing = tmp_path / "media" / "captures"
    controller, _, _ = _controller(
        tmp_path, capture_directory=tmp_path / "unrelated", disk_usage=usage
    )
    # Rebuild against the missing directory without creating it.
    controller = CaptureAdmissionController(
        _config(),
        cooldown_seconds=0,
        database_path=_database(tmp_path / "db"),
        capture_directory=missing,
        clock=_Clock(),
        disk_usage=usage,
    )

    assert controller.admit().admitted is True
    assert probed == [tmp_path]
    assert not missing.exists()


def test_a_capture_path_that_is_a_file_refuses(tmp_path: Path) -> None:
    blocker = tmp_path / "captures"
    blocker.write_bytes(b"not a directory")
    controller = CaptureAdmissionController(
        _config(),
        cooldown_seconds=0,
        database_path=_database(tmp_path),
        capture_directory=blocker,
        clock=_Clock(),
        disk_usage=lambda path: _Usage(1, 0, 10_000),
    )

    assert controller.admit().reason is SuppressionReason.STORAGE_RESERVE


def test_the_probe_runs_on_the_capture_directory_not_the_root(tmp_path: Path) -> None:
    probed: list[Path] = []

    def usage(path: Path) -> _Usage:
        probed.append(path)
        return _Usage(1, 0, 10_000)

    controller, _, _ = _controller(tmp_path, disk_usage=usage)
    controller.admit()

    assert probed == [tmp_path / "captures"]


# --- camera prerequisite -------------------------------------------------------------


def test_an_unavailable_camera_refuses_before_reserving(tmp_path: Path) -> None:
    controller, _, _ = _controller(tmp_path, camera_available=lambda: False)

    decision = controller.admit()

    assert decision.admitted is False
    assert decision.reason is SuppressionReason.CAMERA_UNAVAILABLE
    assert controller.in_flight == 0


def test_storage_is_checked_before_the_camera(tmp_path: Path) -> None:
    controller, _, _ = _controller(tmp_path, free=0, camera_available=lambda: False)

    assert controller.admit().reason is SuppressionReason.STORAGE_RESERVE


# --- refusal side effects -----------------------------------------------------------


def test_a_refusal_writes_nothing(tmp_path: Path) -> None:
    controller, database, clock = _controller(tmp_path, config=_config(hourly=1))
    _insert(database, clock.now - timedelta(minutes=1))
    directory = tmp_path / "captures"
    before_files = sorted(directory.iterdir())
    before_rows = _row_count(database)

    controller.admit()
    controller.evaluate()

    assert sorted(directory.iterdir()) == before_files
    assert _row_count(database) == before_rows


# --- the shared manual-capture floor ------------------------------------------------


def test_the_floor_is_breached_below_the_minimum(tmp_path: Path) -> None:
    directory = tmp_path / "captures"
    directory.mkdir()
    assert storage_floor_breached(
        directory, 1_000, disk_usage=lambda path: _Usage(1, 0, 999)
    )
    assert not storage_floor_breached(
        directory, 1_000, disk_usage=lambda path: _Usage(1, 0, 1_000)
    )


def test_the_floor_is_breached_when_the_probe_fails(tmp_path: Path) -> None:
    def usage(path: Path) -> _Usage:
        raise OSError("probe failed")

    assert storage_floor_breached(tmp_path, 1, disk_usage=usage)


def test_free_bytes_for_capture_uses_the_real_probe_by_default(tmp_path: Path) -> None:
    """The default probe is ``shutil.disk_usage``, run on an existing directory."""
    free = free_bytes_for_capture(tmp_path)
    assert free is not None
    assert free >= 0
    assert abs(free - shutil.disk_usage(tmp_path).free) < 64 * 1024 * 1024


# --- publication boundary -----------------------------------------------------------


class _Coordinator:
    def __init__(self, result: CaptureResult) -> None:
        self._result = result

    def capture_image(self) -> CaptureResult:
        return self._result


class _Archive:
    def __init__(self) -> None:
        self.calls = 0

    def record_capture(
        self, result: CaptureResult, *, extra_metadata: Any = None
    ) -> Any:
        self.calls += 1
        raise AssertionError("a refused capture must never be catalogued")


def _result(path: Path, size: int) -> CaptureResult:
    path.write_bytes(b"x" * size)
    return CaptureResult(
        success=True,
        filename=path.name,
        absolute_path=path,
        timestamp=NOW,
        width=4608,
        height=2592,
        filesize_bytes=size,
        backend="simulator",
    )


def test_a_refused_publication_removes_the_new_file_and_catalogues_nothing(
    tmp_path: Path,
) -> None:
    pre_existing = tmp_path / "older.jpg"
    pre_existing.write_bytes(b"evidence")
    fresh = tmp_path / "fresh.jpg"
    archive = _Archive()
    workflow = CaptureWorkflow(_Coordinator(_result(fresh, 200)), archive)  # type: ignore[arg-type]

    def guard(result: CaptureResult) -> None:
        if result.filesize_bytes > 100:
            raise CapturePublicationRefused("too large")

    with pytest.raises(CapturePublicationRefused):
        workflow.capture(publication_guard=guard)

    assert not fresh.exists()
    assert pre_existing.read_bytes() == b"evidence"
    assert archive.calls == 0


def test_an_accepted_publication_is_catalogued_normally(tmp_path: Path) -> None:
    fresh = tmp_path / "fresh.jpg"
    result = _result(fresh, 50)

    class _RecordingArchive:
        def __init__(self) -> None:
            self.recorded: list[CaptureResult] = []

        def record_capture(
            self, result: CaptureResult, *, extra_metadata: Any = None
        ) -> Any:
            self.recorded.append(result)
            return SimpleNamespace(filename=result.filename, id="catalogued")

    archive = _RecordingArchive()
    workflow = CaptureWorkflow(_Coordinator(result), archive)  # type: ignore[arg-type]
    seen: list[int] = []

    workflow.capture(publication_guard=lambda r: seen.append(r.filesize_bytes))

    assert seen == [50]
    assert archive.recorded == [result]
    assert fresh.exists()


def test_a_guard_that_raises_something_else_propagates_without_deleting(
    tmp_path: Path,
) -> None:
    fresh = tmp_path / "fresh.jpg"
    workflow = CaptureWorkflow(_Coordinator(_result(fresh, 10)), _Archive())  # type: ignore[arg-type]

    def guard(result: CaptureResult) -> None:
        raise RuntimeError("defect")

    with pytest.raises(RuntimeError):
        workflow.capture(publication_guard=guard)

    assert fresh.exists()
