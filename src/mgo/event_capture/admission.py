"""Admission control for automatic (motion-origin) still capture.

Task 14.4 proved the capture foundation and found that nothing bounded it: a
windy afternoon could produce captures until the SD card filled. This module
is the bound. Before a motion trigger is allowed to touch the camera it must be
*admitted*, and admission is refused -- creating no image, no catalogue row and
no lifecycle row -- whenever any of the hard limits would be breached.

Three properties are load-bearing:

* **The durable count is the catalogue.** A successful automatic capture is a
  ``captures`` row with ``extra_metadata.origin == "motion"`` and a UTC
  ``captured_at_utc``. Quotas are computed from those rows every time, so a
  service restart, a crash or a clock-window rollover cannot reset them: the
  rows are still there. The only process-local state is the *reservation* for
  a capture that is executing right now, which cannot outlive the process
  because the capture cannot either.
* **Nothing here fails open.** A database error, a filesystem probe error, a
  missing capture directory, a malformed historical row inside the window --
  each one refuses admission or counts against the quota. The direction of
  every doubt is "do not capture".
* **Admission is the only place that counts.** The worker asks once, gets one
  decision, and releases the reservation when the attempt ends. No other
  component increments a counter or reads one.
"""

from __future__ import annotations

import logging
import shutil
import sqlite3
import threading
from collections.abc import Callable
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


class CaptureAdmissionController:
    """Decides whether one automatic capture may start, and holds the reservation.

    Evaluation order (Task 14.5 §7.4): cooldown, rolling-hour quota, UTC-day
    quota, storage reserve, camera prerequisite, reservation. Every step runs
    under the controller lock so two admission attempts cannot both see the
    same counts and both be admitted; with the single event-capture worker the
    lock is never contended, but the guarantee does not depend on that.
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
        self._capture_directory = capture_directory
        self._clock = clock
        self._disk_usage = disk_usage
        self._camera_available = camera_available
        self._lock = threading.Lock()
        self._in_flight = 0

    @property
    def maximum_capture_bytes(self) -> int:
        return self._maximum_capture_bytes

    @property
    def minimum_free_bytes(self) -> int:
        return self._minimum_free_bytes

    @property
    def in_flight(self) -> int:
        """Reservations currently held. Informational only."""
        with self._lock:
            return self._in_flight

    def evaluate(self) -> AdmissionDecision:
        """Evaluate the gate without taking a reservation. Read-only."""
        with self._lock:
            return self._decide(reserve=False)

    def admit(self) -> AdmissionDecision:
        """Evaluate the gate and, if admitted, hold one reservation.

        The caller must pair an admitted decision with :meth:`release`.
        """
        with self._lock:
            return self._decide(reserve=True)

    def release(self) -> None:
        """Return one reservation. Safe to call only after an admission."""
        with self._lock:
            if self._in_flight > 0:
                self._in_flight -= 1

    # -- the decision ------------------------------------------------------

    def _decide(self, *, reserve: bool) -> AdmissionDecision:
        now = self._clock()
        try:
            newest = self._ledger.newest_automatic_capture_at()
            hourly = self._ledger.count_since(now - HOUR) + self._in_flight
            daily = (
                self._ledger.count_since(_start_of_utc_day(now)) + self._in_flight
            )
        except (sqlite3.Error, OSError, ValueError):
            LOGGER.exception("Automatic capture admission could not read the catalogue")
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
            self._in_flight += 1
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
    "AdmissionDecision",
    "AdmissionState",
    "CaptureAdmissionController",
    "QuotaLedger",
    "SuppressionReason",
    "storage_floor_breached",
]
