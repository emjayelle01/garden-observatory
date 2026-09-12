"""Tests for the capture media lifecycle table and the retention repository.

Everything here runs against temporary SQLite databases in ``tmp_path``. No
Raspberry Pi hardware, no network access and no live/production database path is
involved, so the suite is deterministic on Windows, Linux and CI alike.

The load-bearing property this module protects is that a capture record and its
media are two different facts. Retention may reclaim the second; it may never
erase the first. A ``captures`` row deleted by retention would destroy evidence
that a capture happened, and no byte budget is worth that.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from mgo.core.database import (
    CURRENT_SCHEMA_VERSION,
    MIGRATIONS_DIRECTORY,
    apply_migrations,
    database_connection,
    read_schema_version,
)
from mgo.core.observations import list_observations
from mgo.retention.models import (
    MediaLifecycleState,
    RetentionReason,
)
from mgo.retention.repository import (
    RetentionCatalogueError,
    RetentionRepository,
    RetentionRepositoryError,
)

NOW = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)


def _database(tmp_path: Path) -> Path:
    """Return a freshly migrated temporary database."""
    database_path = tmp_path / "mgo.db"
    apply_migrations(database_path)
    return database_path


def _insert_capture(
    database_path: Path,
    identifier: str,
    *,
    days_old: float = 1.0,
    origin: str | None = "motion",
    size: int = 1000,
    metadata_json: str | None = None,
) -> str:
    """Insert one capture catalogue row and return its id."""
    timestamp = (NOW - timedelta(days=days_old)).isoformat()
    if metadata_json is None:
        metadata = {} if origin is None else {"origin": origin}
        metadata_json = json.dumps(metadata)

    with database_connection(database_path) as connection:
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
                timestamp,
                4608,
                2592,
                size,
                "simulator",
                timestamp,
                metadata_json,
            ),
        )
    return identifier


def _observation_fields(capture_id: str, status: str = "reclaimed") -> dict[str, Any]:
    """Build minimal observation fields for a lifecycle transition."""
    return {
        "kind": "capture_retention",
        "source": "mgo-retention",
        "status": status,
        "summary": "Capture media removed by retention policy",
        "payload": {"capture_id": capture_id},
        "correlation_id": capture_id,
    }


def _lifecycle_rows(database_path: Path) -> list[tuple[Any, ...]]:
    """Return every lifecycle row as a plain tuple."""
    with database_connection(database_path) as connection:
        return [
            tuple(row)
            for row in connection.execute(
                "SELECT capture_id, state, requested_at_utc, deleted_at_utc, "
                "reason FROM capture_media_lifecycle ORDER BY capture_id"
            ).fetchall()
        ]


# --- the migration ----------------------------------------------------------


def test_migration_003_creates_the_lifecycle_table(tmp_path: Path) -> None:
    """A migrated database has the lifecycle table and its index."""
    database_path = _database(tmp_path)

    with database_connection(database_path) as connection:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        indexes = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            )
        }

    assert "capture_media_lifecycle" in tables
    assert "idx_capture_media_lifecycle_state" in indexes
    assert read_schema_version(database_path) == 4


def test_migration_003_is_additive_to_the_captures_table(tmp_path: Path) -> None:
    """The version-2 ``captures`` shape is untouched.

    This is what keeps an unversioned version-2 database adoptable: the legacy
    schema logic compares an exact column set, so a retention column added to
    ``captures`` would have made every pre-existing database unrecognisable.
    """
    database_path = _database(tmp_path)

    with database_connection(database_path) as connection:
        columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(captures)")
        }

    assert columns == {
        "id",
        "filename",
        "absolute_path",
        "captured_at_utc",
        "width",
        "height",
        "filesize_bytes",
        "camera_backend",
        "created_at_utc",
        "extra_metadata",
    }


def test_a_version_two_database_upgrades_preserving_every_row(
    tmp_path: Path,
) -> None:
    """Existing capture and observation rows survive the upgrade byte for byte."""
    database_path = tmp_path / "v2.db"
    for name in ("001_initial_observation_engine", "002_capture_archive"):
        with database_connection(database_path) as connection:
            connection.executescript(
                (MIGRATIONS_DIRECTORY / f"{name}.sql").read_text(encoding="utf-8")
            )
    with database_connection(database_path) as connection:
        connection.execute(
            "DELETE FROM schema_migrations WHERE version > 2"
        )
        connection.execute(
            "INSERT OR REPLACE INTO schema_migrations (version, name, applied_at) "
            "VALUES (1, '001.sql', ?), (2, '002.sql', ?)",
            (NOW.isoformat(), NOW.isoformat()),
        )
        connection.execute(
            """
            INSERT INTO observations (
                id, observed_at, kind, source, status, summary, payload_json,
                correlation_id, created_at
            )
            VALUES ('obs-1', ?, 'legacy', 'test', 'ok', 'kept', '{"a": 1}',
                    NULL, ?)
            """,
            (NOW.isoformat(), NOW.isoformat()),
        )
    _insert_capture(database_path, "cap-1")

    with database_connection(database_path) as connection:
        before = [
            tuple(row) for row in connection.execute("SELECT * FROM captures")
        ]
        observations_before = [
            tuple(row) for row in connection.execute("SELECT * FROM observations")
        ]

    applied = apply_migrations(database_path)

    assert applied == [3, 4]
    assert read_schema_version(database_path) == 4
    with database_connection(database_path) as connection:
        after = [
            tuple(row) for row in connection.execute("SELECT * FROM captures")
        ]
        observations_after = [
            tuple(row) for row in connection.execute("SELECT * FROM observations")
        ]
    assert after == before
    assert observations_after == observations_before


def test_repeated_migration_application_is_idempotent(tmp_path: Path) -> None:
    """Re-running migration on a current database changes nothing."""
    database_path = _database(tmp_path)
    _insert_capture(database_path, "cap-1")

    assert apply_migrations(database_path) == []
    assert apply_migrations(database_path) == []
    assert read_schema_version(database_path) == CURRENT_SCHEMA_VERSION


def test_foreign_keys_remain_enabled_on_operational_connections(
    tmp_path: Path,
) -> None:
    """Foreign-key enforcement is what makes the lifecycle reference real."""
    database_path = _database(tmp_path)

    with database_connection(database_path) as connection:
        assert bool(connection.execute("PRAGMA foreign_keys").fetchone()[0])


# --- what the table refuses to store ----------------------------------------


def test_the_table_rejects_an_unknown_state(tmp_path: Path) -> None:
    """Only the two stored states exist; ``present`` is the absence of a row."""
    database_path = _database(tmp_path)
    _insert_capture(database_path, "cap-1")

    for state in ("present", "archived", "", "PENDING_DELETE"):
        with (
            pytest.raises(sqlite3.IntegrityError),
            database_connection(database_path) as connection,
        ):
                connection.execute(
                    "INSERT INTO capture_media_lifecycle VALUES (?, ?, ?, NULL, ?)",
                    ("cap-1", state, NOW.isoformat(), "age"),
                )


def test_the_table_rejects_an_unknown_reason(tmp_path: Path) -> None:
    """Policy reasons are a closed vocabulary, enforced by the database.

    An operator's free-text explanation stored here would be read back as a
    decision, so the schema refuses to hold one.
    """
    database_path = _database(tmp_path)
    _insert_capture(database_path, "cap-1")

    with (
        pytest.raises(sqlite3.IntegrityError),
        database_connection(database_path) as connection,
    ):
            connection.execute(
                "INSERT INTO capture_media_lifecycle VALUES (?, ?, ?, NULL, ?)",
                ("cap-1", "pending_delete", NOW.isoformat(), "operator asked"),
            )


def test_a_pending_row_may_not_carry_a_deletion_timestamp(tmp_path: Path) -> None:
    """Intent and completion cannot lie about each other."""
    database_path = _database(tmp_path)
    _insert_capture(database_path, "cap-1")

    with (
        pytest.raises(sqlite3.IntegrityError),
        database_connection(database_path) as connection,
    ):
            connection.execute(
                "INSERT INTO capture_media_lifecycle VALUES (?, ?, ?, ?, ?)",
                (
                    "cap-1",
                    "pending_delete",
                    NOW.isoformat(),
                    NOW.isoformat(),
                    "age",
                ),
            )


def test_a_deleted_row_requires_a_deletion_timestamp(tmp_path: Path) -> None:
    """Reclaimed media must record when it was reclaimed."""
    database_path = _database(tmp_path)
    _insert_capture(database_path, "cap-1")

    with (
        pytest.raises(sqlite3.IntegrityError),
        database_connection(database_path) as connection,
    ):
            connection.execute(
                "INSERT INTO capture_media_lifecycle VALUES (?, ?, ?, NULL, ?)",
                ("cap-1", "deleted", NOW.isoformat(), "age"),
            )


def test_a_lifecycle_row_for_an_unknown_capture_is_rejected(
    tmp_path: Path,
) -> None:
    """The foreign key refuses a lifecycle row with no capture behind it."""
    database_path = _database(tmp_path)

    with (
        pytest.raises(sqlite3.IntegrityError),
        database_connection(database_path) as connection,
    ):
            connection.execute(
                "INSERT INTO capture_media_lifecycle VALUES (?, ?, ?, NULL, ?)",
                ("no-such-capture", "pending_delete", NOW.isoformat(), "age"),
            )


def test_a_capture_may_hold_at_most_one_lifecycle_row(tmp_path: Path) -> None:
    """The primary key makes a second intent for one capture impossible."""
    database_path = _database(tmp_path)
    _insert_capture(database_path, "cap-1")
    repository = RetentionRepository(database_path, clock=lambda: NOW)
    repository.claim_pending_delete("cap-1", RetentionReason.AGE)

    with (
        pytest.raises(sqlite3.IntegrityError),
        database_connection(database_path) as connection,
    ):
            connection.execute(
                "INSERT INTO capture_media_lifecycle VALUES (?, ?, ?, NULL, ?)",
                ("cap-1", "pending_delete", NOW.isoformat(), "age"),
            )


# --- the repository projection ----------------------------------------------


def test_a_capture_with_no_lifecycle_row_projects_as_present(
    tmp_path: Path,
) -> None:
    """Absence of a row is the ``PRESENT`` state, not a missing record."""
    database_path = _database(tmp_path)
    _insert_capture(database_path, "cap-1")

    [record] = RetentionRepository(database_path).list_lifecycle_records()

    assert record.lifecycle_state is MediaLifecycleState.PRESENT
    assert record.requested_at_utc is None
    assert record.deleted_at_utc is None
    assert record.reason is None
    assert record.origin == "motion"


def test_the_projection_carries_the_planner_and_executor_inputs(
    tmp_path: Path,
) -> None:
    """One projection row supplies everything downstream needs."""
    database_path = _database(tmp_path)
    _insert_capture(database_path, "cap-1", size=4242)

    [record] = RetentionRepository(database_path).list_lifecycle_records()

    assert record.capture_id == "cap-1"
    assert record.filename == "cap-1.jpg"
    assert record.absolute_path == "/captures/cap-1.jpg"
    assert record.filesize_bytes == 4242
    assert record.captured_at_utc.tzinfo is not None
    assert record.created_at_utc.tzinfo is not None


@pytest.mark.parametrize(
    ("metadata", "expected"),
    [
        ('{"origin": "motion"}', "motion"),
        ("{}", None),
        ('{"origin": null}', None),
        ('{"origin": 7}', None),
        ('{"origin": ["motion"]}', None),
        ('{"other": "motion"}', None),
    ],
)
def test_origin_parsing_never_coerces_a_non_string(
    tmp_path: Path, metadata: str, expected: str | None
) -> None:
    """A non-string origin is ``None``, never stringified into a match.

    ``str(["motion"])`` is not the string ``"motion"``; a value that is not the
    exact managed origin must land in the protected set, not near it.
    """
    database_path = _database(tmp_path)
    _insert_capture(database_path, "cap-1", metadata_json=metadata)

    [record] = RetentionRepository(database_path).list_lifecycle_records()

    assert record.origin == expected


@pytest.mark.parametrize("metadata", ["not json", "[1, 2]", '{"origin": ', '"motion"'])
def test_malformed_metadata_fails_closed(tmp_path: Path, metadata: str) -> None:
    """A catalogue row that will not parse stops retention rather than guessing.

    The origin is the single field standing between an automatic capture and a
    manual one. Reading it out of a document that will not parse is how a manual
    capture gets deleted.
    """
    database_path = _database(tmp_path)
    _insert_capture(database_path, "cap-1", metadata_json=metadata)

    with pytest.raises(RetentionCatalogueError):
        RetentionRepository(database_path).list_lifecycle_records()


#: The lifecycle table without any of its CHECK constraints. Used to build the
#: databases below: a hand-repaired or third-party-written database can present
#: a row the migration would have refused, and the repository must refuse it too
#: rather than trusting that the schema already did the checking.
_UNCONSTRAINED_LIFECYCLE_TABLE = """
    CREATE TABLE capture_media_lifecycle (
        capture_id TEXT PRIMARY KEY REFERENCES captures(id),
        state TEXT NOT NULL,
        requested_at_utc TEXT NOT NULL,
        deleted_at_utc TEXT,
        reason TEXT NOT NULL
    )
"""


def _hand_edited_lifecycle_row(
    tmp_path: Path, state: str, reason: str
) -> Path:
    """Return a database holding one lifecycle row the schema would reject."""
    database_path = _database(tmp_path)
    _insert_capture(database_path, "cap-1")
    with database_connection(database_path) as connection:
        connection.execute("DROP TABLE capture_media_lifecycle")
        connection.execute(_UNCONSTRAINED_LIFECYCLE_TABLE)
        connection.execute(
            "INSERT INTO capture_media_lifecycle VALUES (?, ?, ?, NULL, ?)",
            ("cap-1", state, NOW.isoformat(), reason),
        )
    return database_path


@pytest.mark.parametrize("state", ["quarantined", "present", "", "DELETED"])
def test_an_unrecognised_stored_state_fails_closed(
    tmp_path: Path, state: str
) -> None:
    """A lifecycle state outside the vocabulary is refused, not interpreted.

    ``present`` is in the enum but is never stored, so finding it in a row means
    the row's meaning is unknowable -- which is a reason to stop, not to assume.
    """
    database_path = _hand_edited_lifecycle_row(tmp_path, state, "age")

    with pytest.raises(RetentionCatalogueError):
        RetentionRepository(database_path).list_lifecycle_records()


@pytest.mark.parametrize("reason", ["operator asked", "", "AGE", "disk_pressure"])
def test_an_unrecognised_stored_reason_fails_closed(
    tmp_path: Path, reason: str
) -> None:
    """A policy reason outside the vocabulary stops the read."""
    database_path = _hand_edited_lifecycle_row(
        tmp_path, "pending_delete", reason
    )

    with pytest.raises(RetentionCatalogueError):
        RetentionRepository(database_path).list_lifecycle_records()


def test_a_naive_stored_timestamp_fails_closed(tmp_path: Path) -> None:
    """Every persisted retention timestamp must be timezone-aware.

    A naive timestamp cannot be ordered against an aware one without inventing
    an offset, and an invented offset in an age comparison decides whether a
    file is deleted.
    """
    database_path = _database(tmp_path)
    _insert_capture(database_path, "cap-1")
    with database_connection(database_path) as connection:
        connection.execute(
            "UPDATE captures SET captured_at_utc = ?", ("2026-08-18T12:00:00",)
        )

    with pytest.raises(RetentionCatalogueError):
        RetentionRepository(database_path).list_lifecycle_records()


# --- claiming a deletion intent ---------------------------------------------


def test_claiming_creates_exactly_one_pending_row(tmp_path: Path) -> None:
    """Stage A persists intent, its request time and its fixed reason."""
    database_path = _database(tmp_path)
    _insert_capture(database_path, "cap-1")

    repository = RetentionRepository(database_path, clock=lambda: NOW)

    claimed = repository.claim_pending_delete(
        "cap-1", RetentionReason.AGE_AND_MANAGED_BYTES
    )

    assert claimed is True
    assert _lifecycle_rows(database_path) == [
        ("cap-1", "pending_delete", NOW.isoformat(), None, "age_and_managed_bytes")
    ]


def test_a_second_claim_on_the_same_capture_is_refused(tmp_path: Path) -> None:
    """Overlapping execution cannot claim one capture twice.

    The second caller is told ``False`` and leaves the capture entirely alone,
    rather than both proceeding to delete the same file.
    """
    database_path = _database(tmp_path)
    _insert_capture(database_path, "cap-1")
    repository = RetentionRepository(database_path, clock=lambda: NOW)

    first = repository.claim_pending_delete("cap-1", RetentionReason.AGE)
    second = repository.claim_pending_delete("cap-1", RetentionReason.MANAGED_BYTES)

    assert (first, second) == (True, False)
    assert len(_lifecycle_rows(database_path)) == 1
    assert _lifecycle_rows(database_path)[0][4] == "age"


def test_claiming_an_already_deleted_capture_is_refused(tmp_path: Path) -> None:
    """Reclaimed media cannot be claimed again for a second deletion."""
    database_path = _database(tmp_path)
    _insert_capture(database_path, "cap-1")
    repository = RetentionRepository(database_path, clock=lambda: NOW)
    repository.claim_pending_delete("cap-1", RetentionReason.AGE)
    repository.finalize_deletion(
        "cap-1", observation_fields=_observation_fields("cap-1")
    )

    assert repository.claim_pending_delete("cap-1", RetentionReason.AGE) is False


def test_claiming_an_unknown_capture_raises(tmp_path: Path) -> None:
    """The foreign key surfaces as a repository error, not a silent no-op."""
    database_path = _database(tmp_path)

    with pytest.raises(RetentionRepositoryError):
        RetentionRepository(database_path).claim_pending_delete(
            "no-such-capture", RetentionReason.AGE
        )


def test_a_claim_commits_before_the_caller_touches_the_filesystem(
    tmp_path: Path,
) -> None:
    """The intent is durable the moment the claim returns.

    Read back through an independent connection, which is what a later process
    would see after a crash.
    """
    database_path = _database(tmp_path)
    _insert_capture(database_path, "cap-1")

    RetentionRepository(database_path, clock=lambda: NOW).claim_pending_delete(
        "cap-1", RetentionReason.AGE
    )

    connection = sqlite3.connect(database_path)
    try:
        rows = connection.execute(
            "SELECT state FROM capture_media_lifecycle"
        ).fetchall()
    finally:
        connection.close()
    assert [row[0] for row in rows] == ["pending_delete"]


# --- finalising a deletion --------------------------------------------------


def test_finalising_transitions_and_records_together(tmp_path: Path) -> None:
    """The lifecycle transition and its observation commit as one."""
    database_path = _database(tmp_path)
    _insert_capture(database_path, "cap-1")
    repository = RetentionRepository(database_path, clock=lambda: NOW)
    repository.claim_pending_delete("cap-1", RetentionReason.AGE)

    finalized = repository.finalize_deletion(
        "cap-1", observation_fields=_observation_fields("cap-1")
    )

    assert finalized is True
    assert _lifecycle_rows(database_path) == [
        ("cap-1", "deleted", NOW.isoformat(), NOW.isoformat(), "age")
    ]
    observations = list_observations(database_path, kind="capture_retention")
    assert len(observations) == 1
    assert observations[0].correlation_id == "cap-1"


def test_finalising_without_a_pending_intent_writes_no_observation(
    tmp_path: Path,
) -> None:
    """A finalisation that matches nothing must not record a deletion.

    This is what stops one deletion producing two success observations when two
    executions race for the same capture.
    """
    database_path = _database(tmp_path)
    _insert_capture(database_path, "cap-1")

    finalized = RetentionRepository(database_path).finalize_deletion(
        "cap-1", observation_fields=_observation_fields("cap-1")
    )

    assert finalized is False
    assert _lifecycle_rows(database_path) == []
    assert list_observations(database_path, kind="capture_retention") == []


def test_repeated_finalisation_cannot_duplicate_the_success_observation(
    tmp_path: Path,
) -> None:
    """Finalising twice leaves exactly one success observation."""
    database_path = _database(tmp_path)
    _insert_capture(database_path, "cap-1")
    repository = RetentionRepository(database_path, clock=lambda: NOW)
    repository.claim_pending_delete("cap-1", RetentionReason.AGE)

    first = repository.finalize_deletion(
        "cap-1", observation_fields=_observation_fields("cap-1")
    )
    second = repository.finalize_deletion(
        "cap-1", observation_fields=_observation_fields("cap-1")
    )

    assert (first, second) == (True, False)
    assert len(list_observations(database_path, kind="capture_retention")) == 1


def test_an_invalid_observation_rolls_back_the_lifecycle_transition(
    tmp_path: Path,
) -> None:
    """If the observation cannot be written, the media stays pending.

    The two effects share a transaction precisely so the timeline can never
    disagree with the lifecycle table about whether media was reclaimed.
    """
    database_path = _database(tmp_path)
    _insert_capture(database_path, "cap-1")
    repository = RetentionRepository(database_path, clock=lambda: NOW)
    repository.claim_pending_delete("cap-1", RetentionReason.AGE)

    invalid = _observation_fields("cap-1")
    invalid["summary"] = "   "

    with pytest.raises(ValueError):
        repository.finalize_deletion("cap-1", observation_fields=invalid)

    assert _lifecycle_rows(database_path)[0][1] == "pending_delete"
    assert list_observations(database_path, kind="capture_retention") == []


# --- cancelling a deletion intent -------------------------------------------


def test_cancelling_returns_the_media_to_present(tmp_path: Path) -> None:
    """Removing the intent restores the truth that the media exists."""
    database_path = _database(tmp_path)
    _insert_capture(database_path, "cap-1")
    repository = RetentionRepository(database_path, clock=lambda: NOW)
    repository.claim_pending_delete("cap-1", RetentionReason.AGE)

    cancelled = repository.cancel_pending_delete(
        "cap-1", observation_fields=_observation_fields("cap-1", status="failed")
    )

    assert cancelled is True
    assert _lifecycle_rows(database_path) == []
    [observation] = list_observations(database_path, kind="capture_retention")
    assert observation.status == "failed"


def test_cancelling_a_deleted_capture_is_refused(tmp_path: Path) -> None:
    """Reclaimed media may not be quietly restored to ``PRESENT``."""
    database_path = _database(tmp_path)
    _insert_capture(database_path, "cap-1")
    repository = RetentionRepository(database_path, clock=lambda: NOW)
    repository.claim_pending_delete("cap-1", RetentionReason.AGE)
    repository.finalize_deletion(
        "cap-1", observation_fields=_observation_fields("cap-1")
    )

    cancelled = repository.cancel_pending_delete(
        "cap-1", observation_fields=_observation_fields("cap-1", status="failed")
    )

    assert cancelled is False
    assert _lifecycle_rows(database_path)[0][1] == "deleted"


# --- the capture catalogue survives everything ------------------------------


def test_no_lifecycle_operation_ever_deletes_a_capture_row(
    tmp_path: Path,
) -> None:
    """Claim, finalise and cancel all leave the historical catalogue intact.

    A capture record is evidence that a capture happened. Reclaiming the JPEG it
    points at does not un-happen it, and nothing in the retention repository is
    permitted to remove that row.
    """
    database_path = _database(tmp_path)
    for index in range(3):
        _insert_capture(database_path, f"cap-{index}")
    repository = RetentionRepository(database_path, clock=lambda: NOW)

    with database_connection(database_path) as connection:
        before = [tuple(row) for row in connection.execute("SELECT * FROM captures")]

    repository.claim_pending_delete("cap-0", RetentionReason.AGE)
    repository.finalize_deletion(
        "cap-0", observation_fields=_observation_fields("cap-0")
    )
    repository.claim_pending_delete("cap-1", RetentionReason.MANAGED_BYTES)
    repository.cancel_pending_delete(
        "cap-1", observation_fields=_observation_fields("cap-1", status="failed")
    )

    with database_connection(database_path) as connection:
        after = [tuple(row) for row in connection.execute("SELECT * FROM captures")]
    assert after == before


def test_a_reclaimed_capture_is_still_listed_by_the_capture_archive(
    tmp_path: Path,
) -> None:
    """``GET /captures`` compatibility: reclaimed media is not filtered out.

    The catalogue is a history of captures that happened; the lifecycle table is
    the authority on whether their media was later reclaimed.
    """
    from mgo.captures.archive import CaptureArchive

    database_path = _database(tmp_path)
    _insert_capture(database_path, str(uuid.UUID(int=1)))
    repository = RetentionRepository(database_path, clock=lambda: NOW)
    repository.claim_pending_delete(str(uuid.UUID(int=1)), RetentionReason.AGE)
    repository.finalize_deletion(
        str(uuid.UUID(int=1)),
        observation_fields=_observation_fields(str(uuid.UUID(int=1))),
    )

    assert len(CaptureArchive(database_path).list_captures()) == 1


def test_retention_never_mutates_an_existing_observation(tmp_path: Path) -> None:
    """Old timeline entries are immutable; retention only appends."""
    database_path = _database(tmp_path)
    _insert_capture(database_path, "cap-1")
    with database_connection(database_path) as connection:
        connection.execute(
            """
            INSERT INTO observations (
                id, observed_at, kind, source, status, summary, payload_json,
                correlation_id, created_at
            )
            VALUES ('obs-old', ?, 'event_capture', 'mgo-event-capture',
                    'captured', 'earlier', '{}', 'cap-1', ?)
            """,
            (NOW.isoformat(), NOW.isoformat()),
        )
    with database_connection(database_path) as connection:
        before = [
            tuple(row)
            for row in connection.execute(
                "SELECT * FROM observations WHERE id = 'obs-old'"
            )
        ]

    repository = RetentionRepository(database_path, clock=lambda: NOW)
    repository.claim_pending_delete("cap-1", RetentionReason.AGE)
    repository.finalize_deletion(
        "cap-1", observation_fields=_observation_fields("cap-1")
    )

    with database_connection(database_path) as connection:
        after = [
            tuple(row)
            for row in connection.execute(
                "SELECT * FROM observations WHERE id = 'obs-old'"
            )
        ]
    assert after == before


# --- the catalogue decoder fails closed (Task 14.1 correction round) --------
#
# SQLite column affinity is a conversion preference, not a constraint: the
# ``captures`` table is not STRICT, and a hand-repaired or third-party-written
# database can hold a value of any storage class in any column. A decoder that
# coerces one manufactures a plausible-looking value for a destructive
# subsystem; a decoder that lets the conversion raise strands the run with an
# arbitrary Python exception. Both are refused here.

#: The captures table without any of its constraints, so a test can present the
#: storage classes the real schema would reject at insert time.
_UNCONSTRAINED_CAPTURES_TABLE = """
    CREATE TABLE captures (
        id TEXT PRIMARY KEY,
        filename TEXT,
        absolute_path TEXT,
        captured_at_utc TEXT,
        width INTEGER,
        height INTEGER,
        filesize_bytes INTEGER,
        camera_backend TEXT,
        created_at_utc TEXT,
        extra_metadata TEXT
    )
"""


def _hand_edited_capture(tmp_path: Path, column: str, literal: str) -> Path:
    """Return a database whose single capture row has ``column`` set to ``literal``."""
    database_path = _database(tmp_path)
    _insert_capture(database_path, "cap-1")
    with database_connection(database_path) as connection:
        connection.execute("DROP TABLE capture_media_lifecycle")
        connection.execute("ALTER TABLE captures RENAME TO captures_old")
        connection.execute(_UNCONSTRAINED_CAPTURES_TABLE)
        connection.execute("INSERT INTO captures SELECT * FROM captures_old")
        connection.execute("DROP TABLE captures_old")
        connection.execute(f"UPDATE captures SET {column} = {literal}")
        connection.execute(_UNCONSTRAINED_LIFECYCLE_TABLE)
    return database_path


@pytest.mark.parametrize(
    ("label", "literal"),
    [
        ("non-numeric text", "'not-a-number'"),
        ("empty text", "''"),
        ("a real", "12.5"),
        ("null", "NULL"),
        ("a blob", "x'00ff'"),
        ("zero", "0"),
        ("negative", "-1"),
    ],
)
def test_an_unusable_catalogue_filesize_fails_closed(
    tmp_path: Path, label: str, literal: str
) -> None:
    """Every storage class but a positive integer is refused.

    A positive integer is the only thing the capture service ever writes here:
    it catalogues a verified non-empty JPEG. Anything else is a corrupt record,
    and a corrupt record must stop retention rather than be interpreted.
    """
    database_path = _hand_edited_capture(tmp_path, "filesize_bytes", literal)

    with pytest.raises(RetentionCatalogueError):
        RetentionRepository(database_path).list_lifecycle_records()


def test_a_positive_integer_filesize_is_accepted(tmp_path: Path) -> None:
    """The control: an ordinary size decodes exactly as before."""
    database_path = _hand_edited_capture(tmp_path, "filesize_bytes", "4242")

    [record] = RetentionRepository(database_path).list_lifecycle_records()

    assert record.filesize_bytes == 4242


@pytest.mark.parametrize(
    "column", ["id", "filename", "absolute_path", "extra_metadata"]
)
@pytest.mark.parametrize("literal", ["NULL", "''", "x'00ff'"])
def test_a_required_text_column_fails_closed(
    tmp_path: Path, column: str, literal: str
) -> None:
    """A required text column is validated, never coerced with ``str()``.

    An integer literal is deliberately absent: SQLite's TEXT affinity converts
    it to a genuine string on write, so ``7`` really does arrive as ``"7"`` and
    is a valid -- if odd -- value rather than a corruption. What must be refused
    is what affinity does *not* convert: NULL, a blob, and the empty string.

    ``str(None)`` is the four-character filename ``"None"`` -- a value that
    looks real, would be compared against a real path, and was never in the
    catalogue. The filename and the absolute path are the two columns that
    decide which file is about to be removed, so neither may be manufactured.
    """
    database_path = _hand_edited_capture(tmp_path, column, literal)

    with pytest.raises(RetentionCatalogueError):
        RetentionRepository(database_path).list_lifecycle_records()


def test_valid_text_columns_are_accepted(tmp_path: Path) -> None:
    """The control: an ordinary row still decodes into a complete projection."""
    database_path = _hand_edited_capture(tmp_path, "filename", "'cap-1.jpg'")

    [record] = RetentionRepository(database_path).list_lifecycle_records()

    assert record.capture_id == "cap-1"
    assert record.filename == "cap-1.jpg"
    assert record.absolute_path == "/captures/cap-1.jpg"
    assert record.origin == "motion"


def test_no_catalogue_decoding_failure_escapes_as_a_python_error(
    tmp_path: Path,
) -> None:
    """Every corrupt column raises the domain error, never a raw conversion one.

    This is the property the destructive service depends on: it maps
    :class:`RetentionCatalogueError` to the fixed ``catalogue_invalid``
    category, and anything else would escape the run and strand the runtime
    state in ``running``.
    """
    corruptions = [
        ("filesize_bytes", "'x'"),
        ("filesize_bytes", "NULL"),
        ("filename", "NULL"),
        ("absolute_path", "x'00ff'"),
        ("extra_metadata", "NULL"),
        ("extra_metadata", "'{'"),
        ("captured_at_utc", "'not-a-timestamp'"),
        ("created_at_utc", "NULL"),
        ("id", "NULL"),
    ]

    for index, (column, literal) in enumerate(corruptions):
        database_path = _hand_edited_capture(
            tmp_path / f"{index}-{column}", column, literal
        )
        try:
            RetentionRepository(database_path).list_lifecycle_records()
        except RetentionCatalogueError:
            continue
        except Exception as error:
            raise AssertionError(
                f"{column}={literal} escaped as {type(error).__name__}"
            ) from error
        raise AssertionError(f"{column}={literal} was accepted")


# --- lifecycle capture identity through the repository (final correction) ---
#
# The schema is the enforcement point, not the application. Retention is never
# taught to interpret a lifecycle row that belongs to no capture, because the
# database refuses to hold one. These tests prove the repository can only ever
# create bound rows, and that the invalid state fails at the schema boundary.


def test_every_repository_lifecycle_row_carries_the_supplied_capture_id(
    tmp_path: Path,
) -> None:
    """A claim binds the row to exactly the capture id it was given.

    Checked across several ids at once so a repository that dropped, defaulted
    or reused an identity would show up as a mismatch rather than as a row that
    merely exists.
    """
    database_path = _database(tmp_path)
    identifiers = ["cap-a", "cap-b", "cap-c"]
    for identifier in identifiers:
        _insert_capture(database_path, identifier)
    repository = RetentionRepository(database_path, clock=lambda: NOW)

    for identifier, reason in zip(
        identifiers,
        (
            RetentionReason.AGE,
            RetentionReason.MANAGED_BYTES,
            RetentionReason.AGE_AND_MANAGED_BYTES,
        ),
        strict=True,
    ):
        assert repository.claim_pending_delete(identifier, reason) is True

    with database_connection(database_path) as connection:
        stored = {
            str(row[0])
            for row in connection.execute(
                "SELECT capture_id FROM capture_media_lifecycle"
            )
        }
        orphans = connection.execute(
            "SELECT COUNT(*) FROM capture_media_lifecycle "
            "WHERE capture_id IS NULL"
        ).fetchone()[0]
    assert stored == set(identifiers)
    assert orphans == 0


def test_a_finalised_lifecycle_row_keeps_its_capture_identity(
    tmp_path: Path,
) -> None:
    """The identity survives the pending -> deleted transition unchanged."""
    database_path = _database(tmp_path)
    _insert_capture(database_path, "cap-1")
    repository = RetentionRepository(database_path, clock=lambda: NOW)
    repository.claim_pending_delete("cap-1", RetentionReason.AGE)

    repository.finalize_deletion(
        "cap-1", observation_fields=_observation_fields("cap-1")
    )

    assert _lifecycle_rows(database_path) == [
        ("cap-1", "deleted", NOW.isoformat(), NOW.isoformat(), "age")
    ]


def test_every_lifecycle_row_joins_to_a_real_capture(tmp_path: Path) -> None:
    """No lifecycle row can exist without the capture it describes.

    An anti-join is the direct statement of the invariant: the set of lifecycle
    rows with no matching capture must be empty, whatever route created them.
    """
    database_path = _database(tmp_path)
    for identifier in ("cap-1", "cap-2"):
        _insert_capture(database_path, identifier)
    repository = RetentionRepository(database_path, clock=lambda: NOW)
    repository.claim_pending_delete("cap-1", RetentionReason.AGE)
    repository.claim_pending_delete("cap-2", RetentionReason.MANAGED_BYTES)
    repository.finalize_deletion(
        "cap-1", observation_fields=_observation_fields("cap-1")
    )

    with database_connection(database_path) as connection:
        unbound = connection.execute(
            """
            SELECT COUNT(*)
            FROM capture_media_lifecycle AS l
            LEFT JOIN captures AS c ON c.id = l.capture_id
            WHERE c.id IS NULL
            """
        ).fetchone()[0]
    assert unbound == 0


def test_the_schema_refuses_a_null_identity_rather_than_the_application(
    tmp_path: Path,
) -> None:
    """The invalid state is impossible to store, not merely unhandled.

    Retention deliberately has no behaviour for a NULL-identity lifecycle row.
    Teaching it to interpret one would be the wrong fix: the row must not be
    creatable in the first place, so the refusal is asserted at the schema
    boundary through a direct write that bypasses the repository entirely.
    """
    database_path = _database(tmp_path)
    _insert_capture(database_path, "cap-1")

    with (
        pytest.raises(sqlite3.IntegrityError, match="NOT NULL"),
        database_connection(database_path) as connection,
    ):
        connection.execute(
            "INSERT INTO capture_media_lifecycle VALUES "
            "(NULL, 'pending_delete', ?, NULL, 'age')",
            (NOW.isoformat(),),
        )

    # The projection is therefore never asked to decode an unbound row.
    [record] = RetentionRepository(database_path).list_lifecycle_records()
    assert record.capture_id == "cap-1"
    assert record.lifecycle_state is MediaLifecycleState.PRESENT
