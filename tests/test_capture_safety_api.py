"""API tests for the Task 14.5 status additions and the manual-capture floor.

The additions are additive: every field the endpoints carried before is still
present with its meaning unchanged, and the new fields are typed, default
safely when the feature is disabled, reflect the last admission decision
exactly, survive a probe failure, and carry no path or secret.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException

import mgo.api.app as app_module
from mgo.api.app import (
    EventCaptureStatusResponse,
    MotionStatusResponse,
    app,
    camera_capture,
    event_capture_status,
    motion_status,
)
from mgo.camera import CaptureService, MockBackend
from mgo.core.config import (
    DEFAULT_CONFIG_PATH,
    CameraConfig,
    EventCaptureConfig,
    MGOConfig,
    MotionConfig,
    load_config,
)
from mgo.event_capture import (
    AdmissionDecision,
    EventCaptureRuntimeState,
    SuppressionReason,
)
from mgo.motion.models import MotionResult, MotionStatus

_T0 = datetime(2026, 9, 7, 9, 0, tzinfo=UTC)


def _request(**state: Any) -> SimpleNamespace:
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(**state)))


def _decision(
    admitted: bool, reason: SuppressionReason | None = None
) -> AdmissionDecision:
    return AdmissionDecision(
        admitted=admitted,
        reason=reason,
        evaluated_at=_T0,
        hourly_count=2,
        hourly_limit=3,
        daily_count=7,
        daily_limit=10,
        storage_free_bytes=4_000_000_000,
        storage_reserve_ok=True,
        minimum_free_bytes=2_000_000_000,
        maximum_capture_bytes=16_777_216,
    )


# --- event-capture status ---------------------------------------------------------


def test_the_pre_existing_fields_are_still_present_and_first() -> None:
    fields = list(EventCaptureStatusResponse.model_fields)
    assert fields[:11] == [
        "enabled",
        "state",
        "pending_triggers",
        "total_triggers_received",
        "total_captures_succeeded",
        "total_captures_failed",
        "total_triggers_dropped",
        "last_trigger_at",
        "last_capture_id",
        "last_capture_at",
        "last_error",
    ]


def test_every_new_field_has_a_default() -> None:
    """Additive: a client built against the old shape sees a superset."""
    for name in (
        "admission_state",
        "total_triggers_suppressed",
        "last_suppression_reason",
        "last_suppressed_at",
        "hourly_count",
        "hourly_limit",
        "hourly_remaining",
        "daily_count",
        "daily_limit",
        "daily_remaining",
        "storage_reserve_ok",
        "storage_free_bytes",
        "minimum_free_bytes",
        "maximum_capture_bytes",
        "last_admitted_at",
        "worker_busy",
        "total_global_scene_changes",
    ):
        assert not EventCaptureStatusResponse.model_fields[name].is_required(), name


def test_a_disabled_feature_reports_disabled_admission_and_nulls() -> None:
    payload = event_capture_status(
        _request(event_capture_state=EventCaptureRuntimeState(enabled=False))
    ).model_dump()

    assert payload["admission_state"] == "disabled"
    assert payload["total_triggers_suppressed"] == 0
    assert payload["last_suppression_reason"] is None
    assert payload["hourly_count"] == 0
    assert payload["hourly_limit"] is None
    assert payload["hourly_remaining"] is None
    assert payload["daily_limit"] is None
    assert payload["storage_reserve_ok"] is None
    assert payload["storage_free_bytes"] is None
    assert payload["last_admitted_at"] is None
    assert payload["worker_busy"] is False
    assert payload["total_global_scene_changes"] == 0


def test_an_enabled_feature_before_any_evaluation_is_unknown() -> None:
    payload = event_capture_status(
        _request(event_capture_state=EventCaptureRuntimeState(enabled=True))
    ).model_dump()

    assert payload["admission_state"] == "unknown"
    assert payload["hourly_limit"] is None


def test_the_last_decision_is_reported_exactly() -> None:
    state = EventCaptureRuntimeState(enabled=True)
    state.apply_decision(_decision(False, SuppressionReason.HOURLY_LIMIT))
    state.record_suppression(SuppressionReason.HOURLY_LIMIT, _T0)
    state.last_admitted_at = _T0

    payload = event_capture_status(_request(event_capture_state=state)).model_dump()

    assert payload["admission_state"] == "suppressed"
    assert payload["last_suppression_reason"] == "hourly_limit"
    assert payload["last_suppressed_at"] == _T0.isoformat()
    assert payload["total_triggers_suppressed"] == 1
    assert payload["hourly_count"] == 2
    assert payload["hourly_limit"] == 3
    assert payload["hourly_remaining"] == 1
    assert payload["daily_count"] == 7
    assert payload["daily_limit"] == 10
    assert payload["daily_remaining"] == 3
    assert payload["storage_reserve_ok"] is True
    assert payload["storage_free_bytes"] == 4_000_000_000
    assert payload["minimum_free_bytes"] == 2_000_000_000
    assert payload["maximum_capture_bytes"] == 16_777_216
    assert payload["last_admitted_at"] == _T0.isoformat()


def test_an_admitted_decision_reports_open() -> None:
    state = EventCaptureRuntimeState(enabled=True)
    state.apply_decision(_decision(True))

    payload = event_capture_status(_request(event_capture_state=state)).model_dump()

    assert payload["admission_state"] == "open"
    assert payload["last_suppression_reason"] is None


def test_a_probe_failure_is_reported_as_not_ok_with_no_number() -> None:
    state = EventCaptureRuntimeState(enabled=True)
    state.apply_decision(
        AdmissionDecision(
            admitted=False,
            reason=SuppressionReason.STORAGE_RESERVE,
            evaluated_at=_T0,
            hourly_count=0,
            hourly_limit=3,
            daily_count=0,
            daily_limit=10,
            storage_free_bytes=None,
            storage_reserve_ok=False,
            minimum_free_bytes=1,
            maximum_capture_bytes=1,
        )
    )

    payload = event_capture_status(_request(event_capture_state=state)).model_dump()

    assert payload["admission_state"] == "suppressed"
    assert payload["storage_reserve_ok"] is False
    assert payload["storage_free_bytes"] is None


def test_the_status_carries_no_path_or_directory() -> None:
    state = EventCaptureRuntimeState(enabled=True)
    state.apply_decision(_decision(False, SuppressionReason.STORAGE_RESERVE))
    payload = event_capture_status(_request(event_capture_state=state)).model_dump()

    for name, value in payload.items():
        if isinstance(value, str):
            assert "/" not in value and "\\" not in value, name
            assert not value.endswith((".db", ".toml", ".jpg")), name


def test_requesting_the_status_moves_no_counter() -> None:
    state = EventCaptureRuntimeState(enabled=True)
    state.apply_decision(_decision(False, SuppressionReason.HOURLY_LIMIT))
    before = state.snapshot().as_dict()

    for _ in range(5):
        event_capture_status(_request(event_capture_state=state))

    assert state.snapshot().as_dict() == before


def test_the_openapi_document_describes_the_new_fields() -> None:
    schema = app.openapi()["components"]["schemas"]["EventCaptureStatusResponse"]
    for name in ("admission_state", "hourly_remaining", "storage_reserve_ok"):
        assert name in schema["properties"], name
    assert "admission_state" not in schema.get("required", [])


# --- motion status -------------------------------------------------------------------


def test_motion_status_carries_the_diagnostics_with_defaults() -> None:
    fields = MotionStatusResponse.model_fields
    for name in ("raw_score", "luminance_shift", "global_change_threshold"):
        assert not fields[name].is_required(), name

    result = MotionResult(
        status=MotionStatus.GLOBAL_CHANGE,
        detected=False,
        score=0.97,
        threshold=0.08,
        frames_available=True,
        detail="Whole-frame change; scene re-baselined.",
        evaluated_at=_T0,
        raw_score=0.99,
        luminance_shift=42.0,
        global_change_threshold=0.5,
    )
    payload = motion_status(
        _request(motion_state=SimpleNamespace(get=lambda: result))
    ).model_dump()

    assert payload["status"] == "global_change"
    assert payload["enabled"] is True
    assert payload["detected"] is False
    assert payload["raw_score"] == 0.99
    assert payload["luminance_shift"] == 42.0
    assert payload["global_change_threshold"] == 0.5


def test_motion_status_default_result_reports_the_ceiling() -> None:
    config = MotionConfig(
        enabled=False,
        analysis_interval_seconds=1.0,
        analysis_width=160,
        analysis_height=90,
        pixel_difference_threshold=20,
        changed_pixel_ratio_threshold=0.08,
        cooldown_seconds=5.0,
        global_change_ratio_threshold=0.6,
    )
    from mgo.motion.models import default_motion_result

    result = default_motion_result(config, now=_T0)
    payload = motion_status(
        _request(motion_state=SimpleNamespace(get=lambda: result))
    ).model_dump()

    assert payload["status"] == "disabled"
    assert payload["global_change_threshold"] == 0.6
    assert payload["raw_score"] == 0.0


# --- the manual-capture floor ---------------------------------------------------


def _capture_service(tmp_path: Path) -> CaptureService:
    return CaptureService(
        CameraConfig(
            enabled=True,
            backend="mock",
            device_index=None,
            detection_interval_seconds=30,
            capture_directory=tmp_path / "captures",
        ),
        MockBackend(width=4608, height=2592, name="mock"),
    )


def _config_with_floor(floor: int | None, tmp_path: Path) -> MGOConfig:
    """The real configuration shape, with only the floor and media root changed."""
    base = load_config(DEFAULT_CONFIG_PATH)
    return replace(
        base,
        event_capture=EventCaptureConfig(
            enabled=False,
            max_captures_per_hour=1,
            max_captures_per_day=1,
            minimum_free_bytes=floor,
        ),
        camera=replace(base.camera, capture_directory=tmp_path / "captures"),
    )


def test_a_manual_capture_is_refused_below_the_floor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app_module, "config", _config_with_floor(10**9, tmp_path))
    monkeypatch.setattr(app_module, "storage_floor_breached", lambda *a, **k: True)
    service = _capture_service(tmp_path)

    with pytest.raises(HTTPException) as refused:
        asyncio.run(camera_capture(_request(capture_service=service)))

    assert refused.value.status_code == 507
    assert "free-space floor" in refused.value.detail
    assert not (tmp_path / "captures").exists()


def test_a_manual_capture_proceeds_above_the_floor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app_module, "config", _config_with_floor(10**9, tmp_path))
    monkeypatch.setattr(app_module, "storage_floor_breached", lambda *a, **k: False)
    service = _capture_service(tmp_path)

    result = asyncio.run(camera_capture(_request(capture_service=service)))

    assert result["success"] is True


def test_no_floor_means_the_probe_is_never_consulted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The route behaves exactly as before when no floor is configured."""
    monkeypatch.setattr(app_module, "config", _config_with_floor(None, tmp_path))

    def explode(*args: Any, **kwargs: Any) -> bool:
        raise AssertionError("the floor probe must not run without a floor")

    monkeypatch.setattr(app_module, "storage_floor_breached", explode)
    service = _capture_service(tmp_path)

    result = asyncio.run(camera_capture(_request(capture_service=service)))

    assert result["success"] is True
