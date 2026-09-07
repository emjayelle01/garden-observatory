"""Admission control for automatic (motion-origin) still capture.

Task 14.4 proved the capture foundation and found that nothing bounded it: a
windy afternoon could produce captures until the SD card filled. This module
is the bound. Before a motion trigger is allowed to touch the camera it must be
*admitted*, and admission is refused -- creating no image, no catalogue row and
no lifecycle row -- whenever any of the hard limits would be breached.

Three properties are load-bearing:

* **The durable count is the catalogue plus the reservation ledger.** A
  successful automatic capture is a ``captures`` row with
  ``extra_metadata.origin == "motion"`` and a UTC ``captured_at_utc``. Every
  *attempt* that was admitted is, in addition, a **durable reservation**: a
  marker file written beside the database before the camera is touched (Task
  14.5A). Quotas are computed from rows plus unreleased reservations every
  time, so a service restart, a crash, a clock-window rollover or a capture
  that produced a JPEG but no row cannot reset them: the rows and the markers
  are still there. Nothing about the quota lives only in process memory.
* **Nothing here fails open.** A database error, a filesystem probe error, a
  missing capture directory, a malformed historical row inside the window, a
  reservation that cannot be written -- each one refuses admission or counts
  against the quota. The direction of every doubt is "do not capture".
* **Admission is the only place that counts.** The worker asks once, gets one
  decision, and releases the reservation when the attempt ends -- *removing*
  it only when the catalogue row that supersedes it has been committed. No
  other component increments a counter or reads one.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import shutil
import sqlite3
import threading
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path

from mgo.core.config import EventCaptureConfig
from mgo.core.database import database_connection

LOGGER = logging.getLogger(__name__)

Clock = Callable[[], datetime]
DiskUsage = Callable[[Path], shutil._ntuple_diskusage]
CameraAvailable = Callable[[], bool]

#: The one ``extra_metadata["origin"]`` value that counts against automatic
#: quotas. Exact equality, matching :data:`mgo.retention.policy.MANAGED_ORIGIN`
#: -- the two subsystems must agree on what "an automatic capture" is.
AUTOMATIC_ORIGIN = "motion"

#: The rolling window the hourly quota is measured over.
HOUR = timedelta(hours=1)

#: Where durable reservations live: a directory beside the database, named so
#: a listing of the database directory says what it is. Beside the database
#: rather than beside the media, because it is quota state, not media, and
#: because a database restored from a backup must not silently discard the
#: attempts made since that backup.
RESERVATION_DIRECTORY_NAME = ".mgo-capture-reservations"

#: The suffix every reservation marker carries. Anything else in the directory
#: is ignored, so a stray editor file cannot become a phantom capture.
RESERVATION_SUFFIX = ".reservation"

#: The timestamp layout embedded in a marker's name: lexically sortable,
#: filesystem-safe, microsecond precision, and parsed back with the same
#: format string rather than trusted from the file's mtime.
_RESERVATION_STAMP = "%Y%m%dT%H%M%S%fZ"

#: A reservation older than this can lie inside no quota window -- the UTC
#: day is at most 24 hours long -- and is swept. Two days leaves a margin for
#: a clock that stepped, in the direction of counting for longer, never less.
RESERVATION_SWEEP_AFTER = timedelta(hours=48)


class SuppressionReason(StrEnum):
    """Why an automatic capture was not admitted or not published.

    These are the only reason strings that ever reach the status endpoint or
    an observation. They are fixed vocabulary: nothing derived from a path, an
    exception message or a configuration value can ride out on them.
    """

    COOLDOWN = "cooldown"
    HOURLY_LIMIT = "hourly_limit"
    DAILY_LIMIT = "daily_limit"
    STORAGE_RESERVE = "storage_reserve"
    CAPTURE_BUSY = "capture_busy"
    GLOBAL_SCENE_CHANGE = "global_scene_change"
    CAMERA_UNAVAILABLE = "camera_unavailable"
    ADMISSION_ERROR = "admission_error"
    OVERSIZE_CAPTURE = "oversize_capture"


class AdmissionState(StrEnum):
    """What the admission gate would say to the next trigger.

    * ``DISABLED`` -- automatic capture is off; no gate exists;
    * ``OPEN`` -- the last evaluation admitted, or would admit;
    * ``SUPPRESSED`` -- the last evaluation refused;
    * ``UNKNOWN`` -- no evaluation has run yet in this process.
    """

    DISABLED = "disabled"
    OPEN = "open"
    SUPPRESSED = "suppressed"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class AdmissionDecision:
    """The outcome of one admission evaluation, with the facts behind it.

    Counts *include* any in-flight reservation, so they are what the gate
    actually compared against the limits. ``storage_free_bytes`` is ``None``
    when the probe failed, and ``storage_reserve_ok`` is then ``False``.
    """

    admitted: bool
    reason: SuppressionReason | None
    evaluated_at: datetime
    hourly_count: int
    hourly_limit: int
    daily_count: int
    daily_limit: int
    storage_free_bytes: int | None
    storage_reserve_ok: bool
    minimum_free_bytes: int
    maximum_capture_bytes: int

    @property
    def hourly_remaining(self) -> int:
        return max(0, self.hourly_limit - self.hourly_count)

    @property
    def daily_remaining(self) -> int:
        return max(0, self.daily_limit - self.daily_count)


class QuotaLedger:
    """Counts automatic captures in the catalogue. Read-only.

    Rows are selected by the indexed ``captured_at_utc`` column and then
    re-parsed in Python, so the decision never rests on how SQLite collates
    two timestamp strings. A row whose metadata is not valid JSON, or whose
    timestamp cannot be parsed, is counted: within the window it is more likely
    an automatic capture than not, and counting it can only make the gate
    stricter.
    """

    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path

    def count_since(self, cutoff: datetime) -> int:
        """Return the number of automatic captures at or after ``cutoff``."""
        cutoff_utc = cutoff.astimezone(UTC)
        # Timestamps are stored as ``isoformat()`` of a UTC-aware datetime, so a
        # string compare against the same rendering is a correct pre-filter;
        # the exact comparison happens below on parsed values.
        with database_connection(self._database_path) as connection:
            rows = connection.execute(
                """
                SELECT captured_at_utc,
                       CASE WHEN json_valid(extra_metadata)
                            THEN json_type(extra_metadata)
                            ELSE NULL END AS kind,
                       CASE WHEN json_valid(extra_metadata)
                            THEN json_extract(extra_metadata, '$.origin')
                            ELSE NULL END AS origin
                FROM captures
                WHERE captured_at_utc >= ?
                """,
                (cutoff_utc.isoformat(),),
            ).fetchall()
        counted = 0
        for row in rows:
            # Malformed JSON and valid JSON that is not an object both fail
            # closed: the archive only ever writes an object, so anything else
            # is a row this code cannot vouch for, and it counts.
            automatic = (
                row["kind"] != "object" or row["origin"] == AUTOMATIC_ORIGIN
            )
            if automatic and _captured_at(row["captured_at_utc"], cutoff_utc):
                counted += 1
        return counted

    def newest_automatic_capture_at(self) -> datetime | None:
        """Return when the most recent automatic capture happened, if any."""
        with database_connection(self._database_path) as connection:
            row = connection.execute(
                """
                SELECT captured_at_utc
                FROM captures
                WHERE json_valid(extra_metadata)
                  AND json_extract(extra_metadata, '$.origin') = ?
                ORDER BY captured_at_utc DESC
                LIMIT 1
                """,
                (AUTOMATIC_ORIGIN,),
            ).fetchone()
        if row is None:
            return None
        try:
            parsed = datetime.fromisoformat(str(row["captured_at_utc"]))
        except ValueError:
            return None
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _captured_at(raw: object, cutoff_utc: datetime) -> bool:
    """Return whether a stored timestamp is at or after the cutoff.

    Unparseable and naive timestamps count: the row passed the string
    pre-filter, and a timestamp this code cannot interpret is not evidence
    that the capture happened outside the window.
    """
    try:
        parsed = datetime.fromisoformat(str(raw))
    except ValueError:
        return True
    if parsed.tzinfo is None:
        return True
    return parsed.astimezone(UTC) >= cutoff_utc


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _start_of_utc_day(now: datetime) -> datetime:
    now_utc = now.astimezone(UTC)
    return now_utc.replace(hour=0, minute=0, second=0, microsecond=0)


@dataclass(frozen=True)
class Reservation:
    """One durable, unreleased automatic-capture attempt."""

    path: Path
    reserved_at: datetime


def _fsync_directory(directory: Path) -> None:
    """Flush a directory entry to disk where the platform allows it.

    A crash of the *process* keeps the marker regardless; this is the extra
    step for a power cut. Windows cannot open a directory for ``fsync`` and
    simply skips it -- the production host is Linux.
    """
    if os.name == "nt":
        return
    try:
        descriptor = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


class ReservationLedger:
    """Durable reservations for admitted automatic-capture attempts (Task 14.5A).

    The problem this solves: between "the camera wrote a JPEG" and "the
    catalogue row is committed" a process can die, and the JPEG then exists
    on disk with nothing counting it. An in-memory reservation dies with the
    process. So the reservation is a **file**, created with ``O_EXCL`` before
    the camera is touched, and *released* -- unlinked -- only when the
    catalogue row that supersedes it has been committed. Any other ending
    (a camera failure, an oversize refusal, an archive failure that keeps the
    JPEG, a crash, a kill, a restart) leaves the marker in place, and the next
    process counts it exactly as it counts a row.

    Consequences, all deliberate:

    * an admitted attempt counts against the hourly and daily quotas for the
      full window whether or not it produced a row; a broken camera cannot be
      hammered, and a JPEG the archive failed to catalogue is still counted;
    * a marker and its row may both exist for the instant between commit and
      release (or for good, if the process dies in that instant); that counts
      twice, which is the conservative direction, and the marker is swept
      once it is older than every window;
    * a marker whose timestamp cannot be read counts, by mtime if possible and
      unconditionally otherwise: an unreadable reservation is not evidence
      that the attempt happened outside the window.
    """

    def __init__(self, directory: Path) -> None:
        self._directory = directory

    @property
    def directory(self) -> Path:
        return self._directory

    def reserve(self, now: datetime) -> Reservation:
        """Write one marker for an attempt that starts at ``now``.

        Raises ``OSError`` when the marker cannot be made durable, and the
        caller refuses admission: an attempt that cannot be counted must not
        start.
        """
        stamp = now.astimezone(UTC).strftime(_RESERVATION_STAMP)
        self._directory.mkdir(parents=True, exist_ok=True, mode=0o750)
        name = f"{stamp}-{secrets.token_hex(8)}{RESERVATION_SUFFIX}"
        path = self._directory / name
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o640)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                payload = {
                    "reserved_at": now.astimezone(UTC).isoformat(),
                    "pid": os.getpid(),
                }
                json.dump(payload, handle)
                handle.flush()
                os.fsync(handle.fileno())
        except OSError:
            # A marker that could not be written whole is removed so it cannot
            # be mistaken for a reservation that was made; the refusal follows.
            with suppress(OSError):
                path.unlink()
            raise
        _fsync_directory(self._directory)
        return Reservation(path=path, reserved_at=now.astimezone(UTC))

    def release(self, reservation: Reservation) -> None:
        """Remove a marker whose attempt is now a committed catalogue row."""
        try:
            reservation.path.unlink()
        except FileNotFoundError:
            return
        _fsync_directory(self._directory)

    def _markers(self) -> list[Path]:
        try:
            return [
                path
                for path in self._directory.iterdir()
                if path.name.endswith(RESERVATION_SUFFIX)
            ]
        except FileNotFoundError:
            return []

    @staticmethod
    def _reserved_at(path: Path) -> datetime | None:
        """Return when a marker was made, or ``None`` if that cannot be read."""
        stamp = path.name.split("-", 1)[0]
        try:
            return datetime.strptime(stamp, _RESERVATION_STAMP).replace(tzinfo=UTC)
        except ValueError:
            pass
        try:
            return datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
        except (OSError, OverflowError, ValueError):
            return None

    def count_since(self, cutoff: datetime) -> int:
        """Return the number of unreleased reservations at or after ``cutoff``.

        Raises ``OSError`` if the directory cannot be listed; the caller
        treats that as an admission error.
        """
        cutoff_utc = cutoff.astimezone(UTC)
        counted = 0
        for path in self._markers():
            reserved_at = self._reserved_at(path)
            if reserved_at is None or reserved_at >= cutoff_utc:
                counted += 1
        return counted

    def newest_at(self) -> datetime | None:
        """Return when the most recent unreleased reservation was made."""
        newest: datetime | None = None
        for path in self._markers():
            reserved_at = self._reserved_at(path)
            if reserved_at is not None and (newest is None or reserved_at > newest):
                newest = reserved_at
        return newest

    def sweep(self, now: datetime) -> int:
        """Remove markers older than every window. Returns how many went.

        A marker whose age cannot be established is left in place and keeps
        counting; a failure to unlink is logged and the marker keeps counting.
        Either way the error is in the direction of refusing a capture.
        """
        horizon = now.astimezone(UTC) - RESERVATION_SWEEP_AFTER
        swept = 0
        for path in self._markers():
            reserved_at = self._reserved_at(path)
            if reserved_at is None or reserved_at >= horizon:
                continue
            try:
                path.unlink()
            except OSError:
                LOGGER.warning("A stale capture reservation could not be swept")
                continue
            swept += 1
        if swept:
            _fsync_directory(self._directory)
        return swept


class CaptureAdmissionController:
    """Decides whether one automatic capture may start, and holds the reservation.

    Evaluation order (Task 14.5 §7.4): cooldown, rolling-hour quota, UTC-day
    quota, storage reserve, camera prerequisite, reservation. Every step runs
    under the controller lock so two admission attempts cannot both see the
    same counts and both be admitted; with the single event-capture worker the
    lock is never contended, but the guarantee does not depend on that.

    The reservation is durable (:class:`ReservationLedger`): it is a marker
    file, and it is counted from the filesystem exactly as rows are counted
    from the catalogue. ``release(succeeded=True)`` removes the marker because
    the committed row now carries the count; ``release(succeeded=False)``
    leaves it, so a failed or interrupted attempt keeps counting until it is
    older than every window.
    """

    def __init__(
        self,
        config: EventCaptureConfig,
        *,
        cooldown_seconds: float,
        database_path: Path,
        capture_directory: Path,
        clock: Clock = _utc_now,
        disk_usage: DiskUsage = shutil.disk_usage,
        camera_available: CameraAvailable | None = None,
        reservation_directory: Path | None = None,
    ) -> None:
        if (
            config.max_captures_per_hour is None
            or config.max_captures_per_day is None
            or config.minimum_free_bytes is None
        ):
            # Configuration validation makes this unreachable for an enabled
            # feature; it is checked again here because this object is the
            # last line, and a controller built without limits is not a gate.
            raise ValueError(
                "Automatic capture admission requires every hard limit"
            )
        self._hourly_limit = config.max_captures_per_hour
        self._daily_limit = config.max_captures_per_day
        self._minimum_free_bytes = config.minimum_free_bytes
        self._maximum_capture_bytes = config.maximum_capture_bytes
        self._cooldown = timedelta(seconds=cooldown_seconds)
        self._ledger = QuotaLedger(database_path)
        self._reservations = ReservationLedger(
            reservation_directory
            if reservation_directory is not None
            else database_path.parent / RESERVATION_DIRECTORY_NAME
        )
        self._capture_directory = capture_directory
        self._clock = clock
        self._disk_usage = disk_usage
        self._camera_available = camera_available
        self._lock = threading.Lock()
        # The reservations this process holds and has not yet released, newest
        # last. They are also on disk; this list only says which markers are
        # *ours* to remove on success.
        self._held: list[Reservation] = []

    @property
    def maximum_capture_bytes(self) -> int:
        return self._maximum_capture_bytes

    @property
    def minimum_free_bytes(self) -> int:
        return self._minimum_free_bytes

    @property
    def reservation_directory(self) -> Path:
        """Where this controller's durable reservations are written."""
        return self._reservations.directory

    @property
    def in_flight(self) -> int:
        """Reservations this process holds and has not released. Informational."""
        with self._lock:
            return len(self._held)

    def evaluate(self) -> AdmissionDecision:
        """Evaluate the gate without taking a reservation.

        Reads the catalogue and the reservation ledger and probes the
        filesystem; the only thing it may write is the removal of reservation
        markers older than every window, which changes no count.
        """
        with self._lock:
            return self._decide(reserve=False)

    def admit(self) -> AdmissionDecision:
        """Evaluate the gate and, if admitted, hold one durable reservation.

        The caller must pair an admitted decision with :meth:`release`, and
        must say whether the attempt ended in a committed catalogue row.
        """
        with self._lock:
            return self._decide(reserve=True)

    def release(self, *, succeeded: bool) -> None:
        """End the newest attempt this process admitted.

        ``succeeded`` means the catalogue row exists: the marker is removed,
        because the row now carries the count. Anything else keeps the marker,
        so the attempt stays counted until it is older than every window.
        Safe to call with nothing held.
        """
        with self._lock:
            if not self._held:
                return
            reservation = self._held.pop()
            if not succeeded:
                LOGGER.info(
                    "Automatic capture attempt kept its reservation; it counts "
                    "until it ages out of every quota window"
                )
                return
            try:
                self._reservations.release(reservation)
            except OSError:
                # The row is committed and the marker could not go: the attempt
                # counts twice until the sweep. Conservative, and logged.
                LOGGER.warning(
                    "A released capture reservation could not be removed",
                    exc_info=True,
                )

    # -- the decision ------------------------------------------------------

    def _decide(self, *, reserve: bool) -> AdmissionDecision:
        now = self._clock()
        try:
            self._reservations.sweep(now)
            newest = _later(
                self._ledger.newest_automatic_capture_at(),
                self._reservations.newest_at(),
            )
            hourly_cutoff = now - HOUR
            daily_cutoff = _start_of_utc_day(now)
            # Rows plus unreleased reservations. Both are on durable storage,
            # so a restarted process computes exactly what this one does.
            hourly = self._ledger.count_since(hourly_cutoff)
            hourly += self._reservations.count_since(hourly_cutoff)
            daily = self._ledger.count_since(daily_cutoff)
            daily += self._reservations.count_since(daily_cutoff)
        except (sqlite3.Error, OSError, ValueError):
            LOGGER.exception(
                "Automatic capture admission could not read the catalogue or "
                "the reservation ledger"
            )
            return self._refusal(
                SuppressionReason.ADMISSION_ERROR, now, 0, 0, None, False
            )

        free = self._free_bytes()
        storage_ok = (
            free is not None
            and free - self._maximum_capture_bytes >= self._minimum_free_bytes
        )

        reason: SuppressionReason | None = None
        if (
            self._cooldown > timedelta(0)
            and newest is not None
            and now - newest < self._cooldown
        ):
            # A newest capture stamped later than "now" gives a negative gap,
            # which is inside any positive cooldown: a backwards clock waits.
            reason = SuppressionReason.COOLDOWN
        elif hourly >= self._hourly_limit:
            reason = SuppressionReason.HOURLY_LIMIT
        elif daily >= self._daily_limit:
            reason = SuppressionReason.DAILY_LIMIT
        elif not storage_ok:
            reason = SuppressionReason.STORAGE_RESERVE
        elif self._camera_available is not None and not self._camera_available():
            reason = SuppressionReason.CAMERA_UNAVAILABLE

        if reason is not None:
            return self._refusal(reason, now, hourly, daily, free, storage_ok)

        if reserve:
            # The marker is written BEFORE the caller may touch the camera, so
            # from this instant the attempt is counted by every process that
            # can read the directory, including the one that starts after a
            # crash. A marker that cannot be written is a refusal.
            try:
                self._held.append(self._reservations.reserve(now))
            except OSError:
                LOGGER.exception(
                    "Automatic capture refused: the reservation could not be "
                    "made durable"
                )
                return self._refusal(
                    SuppressionReason.ADMISSION_ERROR,
                    now,
                    hourly,
                    daily,
                    free,
                    storage_ok,
                )
            hourly += 1
            daily += 1
        return AdmissionDecision(
            admitted=True,
            reason=None,
            evaluated_at=now,
            hourly_count=hourly,
            hourly_limit=self._hourly_limit,
            daily_count=daily,
            daily_limit=self._daily_limit,
            storage_free_bytes=free,
            storage_reserve_ok=storage_ok,
            minimum_free_bytes=self._minimum_free_bytes,
            maximum_capture_bytes=self._maximum_capture_bytes,
        )

    def _refusal(
        self,
        reason: SuppressionReason,
        now: datetime,
        hourly: int,
        daily: int,
        free: int | None,
        storage_ok: bool,
    ) -> AdmissionDecision:
        return AdmissionDecision(
            admitted=False,
            reason=reason,
            evaluated_at=now,
            hourly_count=hourly,
            hourly_limit=self._hourly_limit,
            daily_count=daily,
            daily_limit=self._daily_limit,
            storage_free_bytes=free,
            storage_reserve_ok=storage_ok,
            minimum_free_bytes=self._minimum_free_bytes,
            maximum_capture_bytes=self._maximum_capture_bytes,
        )

    def _free_bytes(self) -> int | None:
        """Return free bytes on the filesystem the next capture would land on.

        ``None`` on any failure -- no usable probe directory, a path that is not
        a directory, a probe error -- and ``None`` never admits.
        """
        free = free_bytes_for_capture(
            self._capture_directory, disk_usage=self._disk_usage
        )
        if free is None:
            LOGGER.warning(
                "Automatic capture refused: free space could not be established"
            )
        return free


def _later(first: datetime | None, second: datetime | None) -> datetime | None:
    """Return the later of two optional instants."""
    if first is None:
        return second
    if second is None:
        return first
    return first if first >= second else second


def _probe_directory(capture_directory: Path) -> Path | None:
    """Return the directory whose filesystem the next capture would use.

    The capture directory itself when it exists. When it does not -- the
    capture service creates it on first use -- the nearest existing ancestor,
    because that is the filesystem a newly created directory would inherit.
    A path that exists but is not a directory, or a chain with no existing
    ancestor at all, yields ``None``: nothing about where a file would land
    can be established, so nothing may be admitted.
    """
    candidate = capture_directory
    for _ in range(len(candidate.parts) + 1):
        if candidate.exists():
            return candidate if candidate.is_dir() else None
        parent = candidate.parent
        if parent == candidate:
            return None
        candidate = parent
    return None


def free_bytes_for_capture(
    capture_directory: Path,
    *,
    disk_usage: DiskUsage = shutil.disk_usage,
) -> int | None:
    """Return free bytes where a capture would be written, or ``None``.

    The probe is made on the capture directory (or its nearest existing
    ancestor), never on ``/``: the media may live on a different filesystem
    from the root, and the free space that matters is where the next JPEG
    would land. ``None`` means the answer could not be established, and every
    caller treats ``None`` as "do not capture".
    """
    try:
        directory = _probe_directory(capture_directory)
        if directory is None:
            return None
        usage = disk_usage(directory)
        free = int(usage.free)
    except (OSError, AttributeError, TypeError, ValueError):
        LOGGER.exception("The capture free-space probe failed")
        return None
    return free if free >= 0 else None


def storage_floor_breached(
    capture_directory: Path,
    minimum_free_bytes: int,
    *,
    disk_usage: DiskUsage = shutil.disk_usage,
) -> bool:
    """Return whether the media filesystem is already below the safe floor.

    The one storage condition shared with *manual* capture: when the free
    space is already below ``minimum_free_bytes`` -- or cannot be established
    at all -- continuing any capture would threaten the filesystem, so the
    answer is ``True``. Manual capture applies no reservation and no quota;
    it only declines to make a bad situation worse.
    """
    free = free_bytes_for_capture(capture_directory, disk_usage=disk_usage)
    return free is None or free < minimum_free_bytes


__all__ = [
    "AUTOMATIC_ORIGIN",
    "HOUR",
    "RESERVATION_DIRECTORY_NAME",
    "RESERVATION_SUFFIX",
    "RESERVATION_SWEEP_AFTER",
    "AdmissionDecision",
    "AdmissionState",
    "CaptureAdmissionController",
    "QuotaLedger",
    "Reservation",
    "ReservationLedger",
    "SuppressionReason",
    "storage_floor_breached",
]
