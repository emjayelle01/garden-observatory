"""Background database-health monitoring for MGO.

Mirrors the camera monitor: a long-lived asyncio task that periodically runs
the read-only health check, keeps the latest result in runtime state, and
persists an observation only when the health *materially* changes. The check
runs in a worker thread so SQLite I/O never blocks the event loop.

**Observation policy.** Recording "the database is unhealthy" *into that
database* cannot work -- the write fails for the same reason the check did --
and retrying it on every poll would produce a write storm against a struggling
SD card. So the policy is asymmetric and deliberate:

* a material transition **into** a usable state (``healthy`` or ``degraded``),
  including recovery from ``unhealthy``, is persisted as an observation;
* a material transition **into** ``unhealthy`` is logged at warning level only;
* an unchanged status persists and logs nothing, however often it is polled.

The unhealthy period is therefore still visible in the timeline: the recovery
observation records the status it recovered *from* and the service log carries
the failure itself.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from mgo.core.config import MGOConfig
from mgo.core.database_health import (
    DatabaseHealth,
    DatabaseHealthState,
    DatabaseStatus,
    check_database_health,
)
from mgo.core.observations import Observation, record_observation

LOGGER = logging.getLogger(__name__)

ObservationRecorder = Callable[..., Observation]

DatabaseHealthChecker = Callable[[MGOConfig], DatabaseHealth]

#: States in which the database can still accept a write, and therefore the
#: only states in which persisting an observation is a sensible thing to try.
_PERSISTABLE = frozenset({DatabaseStatus.HEALTHY, DatabaseStatus.DEGRADED})


def is_material_change(
    previous: DatabaseHealth | None,
    current: DatabaseHealth,
) -> bool:
    """Decide whether a health change warrants an observation or a log line.

    Material equality is defined narrowly -- only ``status`` and
    ``migration_status`` count. A new ``checked_at``, a reworded ``detail`` or a
    changed journal mode string never creates noise on its own. The first
    result (``previous is None``) is always material.
    """
    if previous is None:
        return True
    return (previous.status, previous.migration_status) != (
        current.status,
        current.migration_status,
    )


def _record_database_observation(
    config: MGOConfig,
    health: DatabaseHealth,
    previous: DatabaseHealth | None,
    *,
    recorder: ObservationRecorder,
) -> None:
    """Persist a ``database_health`` observation, isolating any failure.

    A failure to record is logged and swallowed: the observation is a timeline
    convenience, and losing it must never break the monitor or the application.
    """
    payload = health.as_dict()
    if previous is not None:
        payload["previous_status"] = previous.status.value

    try:
        recorder(
            config.storage.database_path,
            kind="database_health",
            source="mgo-database",
            status=health.status.value,
            summary=f"Database health: {health.status.value}",
            payload=payload,
        )
    except Exception:
        LOGGER.exception("Could not persist the database-health observation")


def perform_database_check(
    config: MGOConfig,
    state: DatabaseHealthState,
    *,
    checker: DatabaseHealthChecker = check_database_health,
    recorder: ObservationRecorder = record_observation,
) -> DatabaseHealth:
    """Run one health check, update state, and act on a material change.

    The latest result is always stored. On a material change the result is
    logged, and an observation is persisted only when the database is in a
    state that can actually accept the write.
    """
    previous = state.get()
    health = checker(config)
    state.set(health)

    if not is_material_change(previous, health):
        return health

    if health.status is DatabaseStatus.HEALTHY:
        LOGGER.info("Database health: %s -- %s", health.status.value, health.detail)
    else:
        LOGGER.warning(
            "Database health: %s -- %s", health.status.value, health.detail
        )

    if health.status in _PERSISTABLE:
        _record_database_observation(config, health, previous, recorder=recorder)

    return health


async def _safe_check(
    config: MGOConfig,
    state: DatabaseHealthState,
    *,
    checker: DatabaseHealthChecker,
    recorder: ObservationRecorder,
) -> None:
    """Run one check off the event loop, isolating failures from the loop."""
    try:
        await asyncio.to_thread(
            perform_database_check,
            config,
            state,
            checker=checker,
            recorder=recorder,
        )
    except asyncio.CancelledError:
        LOGGER.info("Database monitoring cancelled")
        raise
    except Exception:
        LOGGER.exception("Database health check failed")


async def run_database_monitor(
    config: MGOConfig,
    state: DatabaseHealthState,
    stop_event: asyncio.Event,
    *,
    checker: DatabaseHealthChecker = check_database_health,
    recorder: ObservationRecorder = record_observation,
    run_initial: bool = True,
) -> None:
    """Monitor database health until the supplied stop event is set.

    When ``run_initial`` is true the monitor performs one immediate check. The
    application lifespan already runs the initial check before serving, so it
    starts the monitor with ``run_initial=False`` to avoid a duplicate
    back-to-back check. Exceptions from a single check are logged and swallowed
    so the monitor -- and the application -- survive a bad database.
    """
    interval = config.database.health_check_interval_seconds
    LOGGER.info(
        "Database monitoring started with a %s-second interval", interval
    )

    if run_initial:
        await _safe_check(config, state, checker=checker, recorder=recorder)

    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except TimeoutError:
            await _safe_check(config, state, checker=checker, recorder=recorder)
        else:
            break

    LOGGER.info("Database monitoring stopped")
