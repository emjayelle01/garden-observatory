"""Core camera readiness domain for Matt's Garden Observatory.

This module holds pure, hardware-agnostic camera logic:

* the typed readiness result and its status vocabulary,
* the detector protocol the core depends on,
* the core readiness function that turns detector evidence into a result,
* a small runtime-state holder for the latest readiness.

It deliberately knows nothing about subprocesses, Raspberry Pi commands, or
image capture. Operating-system specifics live in ``camera_detection``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum, StrEnum
from typing import Any, Protocol

from mgo.core.config import CameraConfig

LOGGER = logging.getLogger(__name__)

_MAX_DETAIL_LENGTH = 500


class CameraStatus(StrEnum):
    """Truthful camera readiness states."""

    DISABLED = "disabled"
    WAITING_FOR_HARDWARE = "waiting_for_hardware"
    AVAILABLE = "available"
    ERROR = "error"


class DetectionOutcome(Enum):
    """What a detector concluded about camera hardware."""

    DETECTED = "detected"
    NOT_DETECTED = "not_detected"
    ERROR = "error"


@dataclass(frozen=True)
class DetectionEvidence:
    """Evidence returned by a camera detector adapter.

    ``outcome`` records whether a supported device was positively enumerated,
    was absent, or whether detection failed unexpectedly. ``detail`` is a
    bounded, human-readable explanation safe to surface through the API.
    """

    outcome: DetectionOutcome
    detail: str


class CameraDetector(Protocol):
    """A hardware/OS-specific camera detection adapter.

    Implementations inspect the environment for evidence that a supported
    camera device is enumerated. They must not capture images or start video.
    """

    def detect(self, config: CameraConfig) -> DetectionEvidence:
        """Return evidence about camera hardware availability."""
        ...


@dataclass(frozen=True)
class CameraReadiness:
    """A typed, truthful snapshot of camera readiness."""

    enabled: bool
    backend: str
    status: CameraStatus
    available: bool
    detail: str
    checked_at: datetime

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable representation of the readiness."""
        return {
            "enabled": self.enabled,
            "backend": self.backend,
            "status": self.status.value,
            "available": self.available,
            "detail": self.detail,
            "checked_at": self.checked_at.isoformat(),
        }


def _bounded(detail: str) -> str:
    """Trim detail text to a safe, log-friendly length."""
    collapsed = " ".join(detail.split())
    if len(collapsed) <= _MAX_DETAIL_LENGTH:
        return collapsed
    return collapsed[: _MAX_DETAIL_LENGTH - 1].rstrip() + "…"


def _now(now: datetime | None) -> datetime:
    """Return a timezone-aware UTC timestamp for a readiness result."""
    return now if now is not None else datetime.now(UTC)


def default_readiness(
    config: CameraConfig,
    *,
    now: datetime | None = None,
) -> CameraReadiness:
    """Return a safe readiness result for use before any detection has run."""
    checked_at = _now(now)

    if not config.enabled:
        return CameraReadiness(
            enabled=False,
            backend=config.backend,
            status=CameraStatus.DISABLED,
            available=False,
            detail="Camera functionality is disabled by configuration.",
            checked_at=checked_at,
        )

    return CameraReadiness(
        enabled=True,
        backend=config.backend,
        status=CameraStatus.WAITING_FOR_HARDWARE,
        available=False,
        detail="Camera readiness has not been evaluated yet.",
        checked_at=checked_at,
    )


def detect_camera_readiness(
    config: CameraConfig,
    detector: CameraDetector,
    *,
    now: datetime | None = None,
) -> CameraReadiness:
    """Resolve camera readiness from configuration and detector evidence.

    This is the single source of readiness truth. It never raises: an
    unexpected detector failure is captured as an ``error`` readiness so the
    surrounding application keeps running.
    """
    checked_at = _now(now)

    if not config.enabled:
        return CameraReadiness(
            enabled=False,
            backend=config.backend,
            status=CameraStatus.DISABLED,
            available=False,
            detail="Camera functionality is disabled by configuration.",
            checked_at=checked_at,
        )

    try:
        evidence = detector.detect(config)
    except Exception as exc:
        LOGGER.exception("Camera detection raised an unexpected error")
        return CameraReadiness(
            enabled=True,
            backend=config.backend,
            status=CameraStatus.ERROR,
            available=False,
            detail=_bounded(f"Camera detection failed: {exc}"),
            checked_at=checked_at,
        )

    if evidence.outcome is DetectionOutcome.ERROR:
        return CameraReadiness(
            enabled=True,
            backend=config.backend,
            status=CameraStatus.ERROR,
            available=False,
            detail=_bounded(evidence.detail),
            checked_at=checked_at,
        )

    if evidence.outcome is DetectionOutcome.DETECTED:
        return CameraReadiness(
            enabled=True,
            backend=config.backend,
            status=CameraStatus.AVAILABLE,
            available=True,
            detail=_bounded(evidence.detail),
            checked_at=checked_at,
        )

    return CameraReadiness(
        enabled=True,
        backend=config.backend,
        status=CameraStatus.WAITING_FOR_HARDWARE,
        available=False,
        detail=_bounded(evidence.detail),
        checked_at=checked_at,
    )


class CameraState:
    """Holds the most recently monitored camera readiness result.

    A single asyncio event loop reads and writes this holder, so a plain
    attribute is sufficient; no locking is required.
    """

    def __init__(self) -> None:
        self._latest: CameraReadiness | None = None

    def get(self) -> CameraReadiness | None:
        """Return the latest readiness, or ``None`` if none recorded yet."""
        return self._latest

    def set(self, readiness: CameraReadiness) -> None:
        """Replace the latest readiness result."""
        self._latest = readiness
