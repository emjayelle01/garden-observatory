"""Background camera readiness monitoring for MGO.

Mirrors the persistent health-monitor pattern: a long-lived asyncio task that
periodically evaluates readiness, keeps the latest result in runtime state,
and persists an observation only when the readiness *materially* changes.

Detection runs in a worker thread so a slow subprocess never blocks the event
loop or delays shutdown. All failures are isolated so a bad camera check can
never terminate the application.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from mgo.core.camera import (
    CameraDetector,
    CameraReadiness,
    CameraState,
    CameraStatus,
    detect_camera_readiness,
)
from mgo.core.config import MGOConfig
from mgo.core.observations import Observation, record_observation

LOGGER = logging.getLogger(__name__)

ObservationRecorder = Callable[..., Observation]

#: Called with the new readiness whenever it materially changes. The camera
#: monitor stays transport-agnostic: the application wires this to the
#: notification manager, and a listener failure is isolated so it can never
#: break a readiness check.
CameraTransitionListener = Callable[[CameraReadiness], None]

_STATUS_SUMMARIES: dict[CameraStatus, str] = {
    CameraStatus.DISABLED: "Camera disabled by configuration",
    CameraStatus.WAITING_FOR_HARDWARE: "Camera enabled; waiting for hardware",
    CameraStatus.AVAILABLE: "Camera hardware detected",
    CameraStatus.ERROR: "Camera detection error",
}


def is_material_change(
    previous: CameraReadiness | None,
    current: CameraReadiness,
) -> bool:
    """Decide whether a readiness change warrants a new observation.

    Material equality is defined deliberately narrowly: only the ``status``
    and ``available`` fields count. Changes to ``checked_at`` or to harmless
    ``detail`` wording never create observation noise. The first observed
    result (``previous is None``) is always material.
    """
    if previous is None:
        return True
    return (previous.status, previous.available) != (
        current.status,
        current.available,
    )


def _summary(readiness: CameraReadiness) -> str:
    """Return a concise human-readable summary for an observation."""
    return _STATUS_SUMMARIES.get(readiness.status, "Camera status changed")


def _record_camera_observation(
    config: MGOConfig,
    readiness: CameraReadiness,
    *,
    recorder: ObservationRecorder,
) -> None:
    """Persist a camera_status observation via the observation service."""
    recorder(
        config.storage.database_path,
        kind="camera_status",
        source="mgo-camera",
        status=readiness.status.value,
        summary=_summary(readiness),
        payload=readiness.as_dict(),
    )


async def perform_camera_check(
    config: MGOConfig,
    state: CameraState,
    *,
    detector: CameraDetector,
    recorder: ObservationRecorder = record_observation,
    on_material_change: CameraTransitionListener | None = None,
) -> CameraReadiness:
    """Run one readiness check, update state, and persist material changes.

    Detection runs in a worker thread. The latest result is always stored;
    an observation is written -- and ``on_material_change`` invoked -- only
    when the readiness materially changed relative to the previously stored
    result. A listener failure is logged and swallowed so it can never break
    the check itself.
    """
    previous = state.get()
    readiness = await asyncio.to_thread(
        detect_camera_readiness,
        config.camera,
        detector,
    )
    state.set(readiness)

    if is_material_change(previous, readiness):
        _record_camera_observation(config, readiness, recorder=recorder)
        if on_material_change is not None:
            try:
                on_material_change(readiness)
            except Exception:
                LOGGER.exception("Camera transition listener failed")

    return readiness


async def _safe_check(
    config: MGOConfig,
    state: CameraState,
    *,
    detector: CameraDetector,
    recorder: ObservationRecorder,
    on_material_change: CameraTransitionListener | None,
) -> None:
    """Run one readiness check, isolating failures from the monitor loop."""
    try:
        await perform_camera_check(
            config,
            state,
            detector=detector,
            recorder=recorder,
            on_material_change=on_material_change,
        )
    except asyncio.CancelledError:
        LOGGER.info("Camera monitoring cancelled")
        raise
    except Exception:
        LOGGER.exception("Camera readiness check failed")


async def run_camera_monitor(
    config: MGOConfig,
    state: CameraState,
    stop_event: asyncio.Event,
    *,
    detector: CameraDetector,
    recorder: ObservationRecorder = record_observation,
    run_initial: bool = True,
    on_material_change: CameraTransitionListener | None = None,
) -> None:
    """Monitor camera readiness until the supplied stop event is set.

    When ``run_initial`` is true the monitor performs one immediate check so
    runtime state is populated (useful for standalone use). The application
    lifespan already runs the initial check before serving, so it starts the
    monitor with ``run_initial=False`` to avoid a duplicate back-to-back probe:
    the monitor then waits one interval before its first periodic recheck.
    Exceptions from a single check are logged and swallowed so the monitor --
    and the application -- survive.
    """
    interval = config.camera.detection_interval_seconds
    LOGGER.info(
        "Camera monitoring started with a %s-second interval", interval
    )

    if run_initial:
        await _safe_check(
            config,
            state,
            detector=detector,
            recorder=recorder,
            on_material_change=on_material_change,
        )

    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except TimeoutError:
            await _safe_check(
                config,
                state,
                detector=detector,
                recorder=recorder,
                on_material_change=on_material_change,
            )
        else:
            break

    LOGGER.info("Camera monitoring stopped")
