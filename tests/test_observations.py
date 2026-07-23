"""Tests for the MGO Observation Engine."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from mgo.core.database import apply_migrations
from mgo.core.observations import list_observations, record_observation


def test_observation_can_be_recorded_and_retrieved(
    tmp_path: Path,
) -> None:
    """A persisted observation should retain its complete evidence."""
    database_path = tmp_path / "test.db"
    apply_migrations(database_path)

    recorded = record_observation(
        database_path,
        kind="camera_status",
        source="test-camera",
        status="waiting",
        summary="Camera hardware not yet connected",
        payload={"enabled": False},
        correlation_id="test-session",
    )

    observations = list_observations(database_path)

    assert len(observations) == 1
    assert observations[0] == recorded


def test_observations_are_returned_newest_first(
    tmp_path: Path,
) -> None:
    """Timeline results should be ordered newest first."""
    database_path = tmp_path / "test.db"
    apply_migrations(database_path)

    older = datetime(2026, 7, 23, 10, 0, tzinfo=UTC)
    newer = datetime(2026, 7, 23, 11, 0, tzinfo=UTC)

    record_observation(
        database_path,
        kind="system_event",
        source="test",
        status="success",
        summary="Older event",
        observed_at=older,
    )
    record_observation(
        database_path,
        kind="system_event",
        source="test",
        status="success",
        summary="Newer event",
        observed_at=newer,
    )

    observations = list_observations(database_path)

    assert [item.summary for item in observations] == [
        "Newer event",
        "Older event",
    ]


def test_naive_observation_timestamp_is_rejected(
    tmp_path: Path,
) -> None:
    """Stored observation times must always include timezone information."""
    database_path = tmp_path / "test.db"
    apply_migrations(database_path)

    with pytest.raises(
        ValueError,
        match="timestamps must be timezone-aware",
    ):
        record_observation(
            database_path,
            kind="invalid",
            source="test",
            status="failed",
            summary="Invalid timestamp",
            observed_at=datetime(2026, 7, 23, 10, 0),
        )


def test_observations_can_be_filtered_by_kind(
    tmp_path: Path,
) -> None:
    """The timeline should support filtering by observation kind."""
    database_path = tmp_path / "test.db"
    apply_migrations(database_path)

    record_observation(
        database_path,
        kind="camera_status",
        source="test",
        status="waiting",
        summary="Camera waiting",
    )
    record_observation(
        database_path,
        kind="health_status",
        source="test",
        status="healthy",
        summary="System healthy",
    )

    observations = list_observations(
        database_path,
        kind="camera_status",
    )

    assert len(observations) == 1
    assert observations[0].summary == "Camera waiting"
