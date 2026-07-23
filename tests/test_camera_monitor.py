"""Tests for the background camera readiness monitor and observations."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from mgo.core.camera import (
    CameraReadiness,
    CameraState,
    CameraStatus,
    DetectionEvidence,
    DetectionOutcome,
)
from mgo.core.camera_monitor import (
    is_material_change,
    perform_camera_check,
    run_camera_monitor,
)
from mgo.core.config import (
    ApplicationConfig,
    CameraConfig,
    MGOConfig,
    StorageConfig,
    load_config,
)
from mgo.core.database import apply_migrations
from mgo.core.observations import list_observations


class _Detector:
    """A detector returning fixed evidence, tracking invocation count."""

    def __init__(self, evidence: DetectionEvidence) -> None:
        self.evidence = evidence
        self.calls = 0

    def detect(self, config: CameraConfig) -> DetectionEvidence:
        self.calls += 1
        return self.evidence


def _config(database_path: Path, *, interval: int = 30) -> MGOConfig:
    """Build an isolated configuration with an enabled camera."""
    base = load_config()
    return MGOConfig(
        application=ApplicationConfig(
            name=base.application.name,
            environment="test",
            host="127.0.0.1",
            port=8080,
        ),
        storage=StorageConfig(
            data_directory=database_path.parent,
            log_directory=database_path.parent / "logs",
            database_path=database_path,
        ),
        camera=CameraConfig(
            enabled=True,
            backend="null",
            device_index=None,
            detection_interval_seconds=interval,
            capture_directory=database_path.parent / "captures",
        ),
        health=base.health,
    )


def _prepared(tmp_path: Path, *, interval: int = 30) -> tuple[MGOConfig, Path]:
    """Create a migrated database and return the config plus its path."""
    database_path = tmp_path / "test.db"
    apply_migrations(database_path)
    return _config(database_path, interval=interval), database_path


def _readiness(status: CameraStatus, available: bool) -> CameraReadiness:
    """Build a readiness result for material-change tests."""
    return CameraReadiness(
        enabled=True,
        backend="null",
        status=status,
        available=available,
        detail="detail",
        checked_at=datetime.now(UTC),
    )


# --- material change semantics --------------------------------------------


def test_first_result_is_material() -> None:
    """The first observed readiness is always material."""
    assert is_material_change(None, _readiness(CameraStatus.DISABLED, False))


def test_identical_status_is_not_material() -> None:
    """Identical status and availability is not a material change."""
    previous = _readiness(CameraStatus.AVAILABLE, True)
    current = _readiness(CameraStatus.AVAILABLE, True)

    assert is_material_change(previous, current) is False


def test_changed_checked_at_only_is_not_material() -> None:
    """Only ``checked_at`` differing must not count as material."""
    previous = _readiness(CameraStatus.WAITING_FOR_HARDWARE, False)
    current = replace(
        previous,
        checked_at=previous.checked_at + timedelta(minutes=5),
    )

    assert is_material_change(previous, current) is False


def test_changed_detail_only_is_not_material() -> None:
    """Only ``detail`` wording differing must not count as material."""
    previous = _readiness(CameraStatus.WAITING_FOR_HARDWARE, False)
    current = replace(previous, detail="different wording")

    assert is_material_change(previous, current) is False


def test_status_change_is_material() -> None:
    """A status transition is material."""
    previous = _readiness(CameraStatus.WAITING_FOR_HARDWARE, False)
    current = _readiness(CameraStatus.AVAILABLE, True)

    assert is_material_change(previous, current) is True


# --- perform_camera_check + observations ----------------------------------


def test_initial_check_populates_state(tmp_path: Path) -> None:
    """The initial readiness check must make state available immediately."""
    config, _ = _prepared(tmp_path)
    state = CameraState()
    detector = _Detector(
        DetectionEvidence(DetectionOutcome.NOT_DETECTED, "no camera")
    )

    readiness = asyncio.run(
        perform_camera_check(config, state, detector=detector)
    )

    assert state.get() == readiness
    assert readiness.status is CameraStatus.WAITING_FOR_HARDWARE


def test_material_change_persists_one_observation(tmp_path: Path) -> None:
    """A material transition persists exactly one camera_status observation."""
    config, database_path = _prepared(tmp_path)
    state = CameraState()
    waiting = _Detector(
        DetectionEvidence(DetectionOutcome.NOT_DETECTED, "no camera")
    )
    available = _Detector(
        DetectionEvidence(DetectionOutcome.DETECTED, "imx708")
    )

    asyncio.run(perform_camera_check(config, state, detector=waiting))
    asyncio.run(perform_camera_check(config, state, detector=available))

    observations = list_observations(database_path, kind="camera_status")
    assert len(observations) == 2  # None->waiting, waiting->available
    latest = observations[0]
    assert latest.source == "mgo-camera"
    assert latest.status == "available"
    assert latest.payload["available"] is True


def test_unchanged_state_creates_no_duplicate(tmp_path: Path) -> None:
    """Repeated identical checks must not create duplicate observations."""
    config, database_path = _prepared(tmp_path)
    state = CameraState()
    detector = _Detector(
        DetectionEvidence(DetectionOutcome.DETECTED, "imx708")
    )

    for _ in range(4):
        asyncio.run(perform_camera_check(config, state, detector=detector))

    observations = list_observations(database_path, kind="camera_status")
    assert len(observations) == 1
    assert detector.calls == 4


def test_disabled_state_records_once(tmp_path: Path) -> None:
    """A disabled camera records its initial state exactly once."""
    config, database_path = _prepared(tmp_path)
    disabled_config = MGOConfig(
        application=config.application,
        storage=config.storage,
        camera=replace(config.camera, enabled=False),
        health=config.health,
    )
    state = CameraState()
    detector = _Detector(
        DetectionEvidence(DetectionOutcome.DETECTED, "unused")
    )

    asyncio.run(
        perform_camera_check(disabled_config, state, detector=detector)
    )
    asyncio.run(
        perform_camera_check(disabled_config, state, detector=detector)
    )

    observations = list_observations(database_path, kind="camera_status")
    assert len(observations) == 1
    assert observations[0].status == "disabled"
    assert detector.calls == 0


# --- monitor lifecycle ----------------------------------------------------


def test_monitor_runs_initial_check_and_stops(tmp_path: Path) -> None:
    """The monitor populates state on startup and exits cleanly on stop."""
    config, _ = _prepared(tmp_path)
    state = CameraState()
    detector = _Detector(
        DetectionEvidence(DetectionOutcome.NOT_DETECTED, "no camera")
    )

    async def _run() -> None:
        stop_event = asyncio.Event()
        task = asyncio.create_task(
            run_camera_monitor(
                config, state, stop_event, detector=detector
            )
        )
        # Allow the initial check to complete.
        while state.get() is None:
            await asyncio.sleep(0)
        stop_event.set()
        await asyncio.wait_for(task, timeout=5)
        assert task.done()
        assert not task.cancelled()

    asyncio.run(_run())

    assert state.get() is not None
    assert state.get().status is CameraStatus.WAITING_FOR_HARDWARE


def test_monitor_cancellation_exits_cleanly(tmp_path: Path) -> None:
    """Cancelling the monitor task terminates it without leaking."""
    config, _ = _prepared(tmp_path, interval=3600)
    state = CameraState()
    detector = _Detector(
        DetectionEvidence(DetectionOutcome.NOT_DETECTED, "no camera")
    )

    async def _run() -> bool:
        stop_event = asyncio.Event()
        task = asyncio.create_task(
            run_camera_monitor(
                config, state, stop_event, detector=detector
            )
        )
        while state.get() is None:
            await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            return True
        return False

    assert asyncio.run(_run()) is True


def test_startup_runs_single_detection_before_interval(tmp_path: Path) -> None:
    """The lifespan sequence probes hardware exactly once before an interval.

    Mirrors the application lifespan: one pre-serve check, then a monitor
    started with ``run_initial=False``. With a long interval, no second probe
    occurs until that interval elapses -- so only one detector invocation
    happens at startup.
    """
    config, _ = _prepared(tmp_path, interval=3600)
    state = CameraState()
    detector = _Detector(
        DetectionEvidence(DetectionOutcome.NOT_DETECTED, "no camera")
    )

    async def _run() -> int:
        # Lifespan performs the single initial check before serving.
        await perform_camera_check(config, state, detector=detector)
        assert detector.calls == 1

        stop_event = asyncio.Event()
        task = asyncio.create_task(
            run_camera_monitor(
                config,
                state,
                stop_event,
                detector=detector,
                run_initial=False,
            )
        )
        # Give the monitor time to start and reach its interval wait.
        await asyncio.sleep(0.05)
        calls_before_interval = detector.calls

        stop_event.set()
        await asyncio.wait_for(task, timeout=5)
        assert task.done()
        assert not task.cancelled()
        return calls_before_interval

    assert asyncio.run(_run()) == 1


def test_monitor_rechecks_after_interval(tmp_path: Path) -> None:
    """After the interval elapses the monitor performs a periodic recheck."""
    config, _ = _prepared(tmp_path, interval=0)
    state = CameraState()
    detector = _Detector(
        DetectionEvidence(DetectionOutcome.NOT_DETECTED, "no camera")
    )

    async def _run() -> int:
        stop_event = asyncio.Event()
        task = asyncio.create_task(
            run_camera_monitor(
                config,
                state,
                stop_event,
                detector=detector,
                run_initial=False,
            )
        )
        # With run_initial=False the first probe only happens after the
        # (here immediate) interval elapses.
        while detector.calls < 1:
            await asyncio.sleep(0)
        stop_event.set()
        await asyncio.wait_for(task, timeout=5)
        assert task.done()
        assert not task.cancelled()
        return detector.calls

    assert asyncio.run(_run()) >= 1
    assert state.get() is not None
