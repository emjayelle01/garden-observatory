"""Persistent system-health monitoring for MGO."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from mgo.core.config import MGOConfig
from mgo.core.health import collect_health
from mgo.core.observations import Observation, record_observation

LOGGER = logging.getLogger(__name__)


def _normalise_payload(value: Any) -> Any:
    """Convert health values into JSON-compatible structures."""
    if isinstance(value, dict):
        return {
            str(key): _normalise_payload(item)
            for key, item in value.items()
        }
    if isinstance(value, tuple | list):
        return [_normalise_payload(item) for item in value]
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    return str(value)


def record_health_observation(config: MGOConfig) -> Observation:
    """Collect and persist one system-health observation."""
    health = collect_health(config)
    status = str(health.get("status", "unknown"))

    return record_observation(
        config.storage.database_path,
        kind="health_snapshot",
        source="mgo-health",
        status=status,
        summary=f"System health: {status}",
        payload=_normalise_payload(health),
    )


async def run_health_monitor(
    config: MGOConfig,
    stop_event: asyncio.Event,
) -> None:
    """Record health until the supplied stop event is set."""
    if not config.health.enabled:
        LOGGER.info("Health monitoring is disabled")
        return

    interval = config.health.collection_interval_seconds
    LOGGER.info("Health monitoring started with a %s-second interval", interval)

    while not stop_event.is_set():
        try:
            record_health_observation(config)
        except Exception:
            LOGGER.exception("Health snapshot collection failed")

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except TimeoutError:
            continue

    LOGGER.info("Health monitoring stopped")
