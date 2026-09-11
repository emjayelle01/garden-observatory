"""Tests for migration 004: the recognition job and result schema (Task 15.1).

Every database here is a temporary SQLite file under ``tmp_path``. A genuine
schema-3 database is built by pointing the migration runner at copies of
migrations 001-003 with the build's expected version set back to 3 -- the
exact state a database written by the previous release is in -- so the upgrade
is proven against the real predecessor rather than a hand-written imitation.
"""

from __future__ import annotations

import shutil
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import pytest

import mgo.core.database as database_module
from mgo.core.config import StorageConfig, load_config
from mgo.core.database import (
    CURRENT_SCHEMA_VERSION,
    MIGRATIONS_DIRECTORY,
    DatabaseError,
    IncompatibleSchemaError,
    apply_migrations,
    database_connection,
    read_schema_version,
    utc_now_iso,
)
from mgo.core.database_health import MigrationStatus, check_database_health
from mgo.core.observations import list_observations, record_observation
from mgo.retention.repository import RetentionRepository

STAMP = "2026-09-11T12:00:00.000000+00:00"
LATER = "2026-09-11T12:10:00.000000+00:00"
SHA = "a" * 64

_V3_MIGRATIONS = (
    "001_initial_observation_engine.sql",
    "002_capture_archive.sql",
    "003_capture_media_lifecycle.sql",
)


@contextmanager
def _previous_release(tmp_path: Path) -> Iterator[None]:
    """Run the migration runner exactly as the schema-3 build shipped it."""
    directory = tmp_path / "v3-migrations"
    directory.mkdir(exist_ok=True)
    for name in _V3_MIGRATIONS:
        shutil.copyfile(MIGRATIONS_DIRECTORY / name, directory / name)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(database_module, "MIGRATIONS_DIRECTORY", directory)
        patch.setattr(database_module, "CURRENT_SCHEMA_VERSION", 3)
        yield


def _schema_objects(database_path: Path) -> dict[tuple[str, str], str | None]:
    """Return ``{(type, name): sql}`` for every stored schema object."""
    connection = sqlite3.connect(database_path)
    try:
        rows = connection.execute(
            "SELECT type, name, sql FROM sqlite_master"
        ).fetchall()
    finally:
        connection.close()
    return {(str(row[0]), str(row[1])): row[2] for row in rows}


def _all_rows(database_path: Path, table: str) -> list[tuple[object, ...]]:
    connection = sqlite3.connect(database_path)
    try:
        return [
            tuple(row)
            for row in connection.execute(f"SELECT * FROM {table} ORDER BY 1")
        ]
    finally:
        connection.close()


def _insert_capture(connection: sqlite3.Connection, identifier: str) -> None:
    connection.execute(
        """
        INSERT INTO captures (
            id, filename, absolute_path, captured_at_utc, width, height,
            filesize_bytes, camera_backend, created_at_utc, extra_metadata
        )
        VALUES (?, ?, ?, ?, 4608, 2592, 1000, 'simulator', ?, ?)
        """,
        (
            identifier,
            f"{identifier}.jpg",
            f"/var/lib/garden-observatory/media/captures/{identifier}.jpg",
            STAMP,
            STAMP,
            '{"origin": "motion"}',
        ),
    )


def _job_values(**overrides: object) -> dict[str, object]:
    """A valid pending job row, with any column overridden."""
    values: dict[str, object] = {
        "id": str(uuid.uuid4()),
        "capture_id": "cap-1",
        "pipeline_version": "fake-0",
        "camera_id": None,
        "state": "pending",
        "attempt_count": 0,
        "max_attempts": 3,
        "next_attempt_at": STAMP,
        "lease_owner": None,
        "lease_expires_at": None,
        "created_at": STAMP,
        "started_at": None,
        "finished_at": None,
        "error_category": None,
    }
    values.update(overrides)
    return values


def _insert_job(connection: sqlite3.Connection, **overrides: object) -> str:
    values = _job_values(**overrides)
    columns = ", ".join(values)
    placeholders = ", ".join(f":{name}" for name in values)
    connection.execute(
        f"INSERT INTO recognition_jobs ({columns}) VALUES ({placeholders})",
        values,
    )
    return str(values["id"])


def _result_values(job_id: str, **overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "id": str(uuid.uuid4()),
        "job_id": job_id,
        "outcome": "no_bird",
        "detector_model_id": None,
        "detector_model_sha256": None,
        "classifier_model_id": None,
        "classifier_model_sha256": None,
        "label_set_id": None,
        "label_set_sha256": None,
        "taxonomy_id": None,
        "taxonomy_version": None,
        "preprocessing_version": None,
        "thresholds_version": None,
        "inference_duration_ms": None,
        "peak_rss_bytes": None,
        "cpu_time_ms": None,
        "image_width": None,
        "image_height": None,
        "created_at": STAMP,
    }
    values.update(overrides)
    return values


def _insert_result(
    connection: sqlite3.Connection, job_id: str, **overrides: object
) -> None:
    values = _result_values(job_id, **overrides)
    columns = ", ".join(values)
    placeholders = ", ".join(f":{name}" for name in values)
    connection.execute(
        f"INSERT INTO recognition_results ({columns}) VALUES ({placeholders})",
        values,
    )


@pytest.fixture
def database(tmp_path: Path) -> Path:
    """A fresh schema-4 database holding one capture, ``cap-1``."""
    database_path = tmp_path / "mgo.db"
    apply_migrations(database_path)
    with database_connection(database_path) as connection:
        _insert_capture(connection, "cap-1")
    return database_path


# --- migration ----------------------------------------------------------------


def test_the_current_schema_version_is_four() -> None:
    """Migration 004 moves the build's notion of current with it."""
    assert CURRENT_SCHEMA_VERSION == 4
    assert (MIGRATIONS_DIRECTORY / "004_recognition_jobs.sql").is_file()


def test_a_fresh_database_migrates_through_every_migration(tmp_path: Path) -> None:
    """A new database receives 001-004 and records each by name."""
    database_path = tmp_path / "fresh.db"

    assert apply_migrations(database_path) == [1, 2, 3, 4]

    assert read_schema_version(database_path) == 4
    with database_connection(database_path) as connection:
        names = [
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM schema_migrations ORDER BY version"
            )
        ]
    assert names == [*_V3_MIGRATIONS, "004_recognition_jobs.sql"]
    objects = _schema_objects(database_path)
    assert ("table", "recognition_jobs") in objects
    assert ("table", "recognition_results") in objects
    assert ("index", "idx_recognition_jobs_due") in objects
    assert ("index", "idx_recognition_jobs_lease") in objects


def test_migration_004_adds_only_the_two_recognition_tables(tmp_path: Path) -> None:
    """No detection, review, sighting, encounter, outbox or notification table."""
    with _previous_release(tmp_path):
        v3_path = tmp_path / "v3.db"
        apply_migrations(v3_path)
    v4_path = tmp_path / "v4.db"
    apply_migrations(v4_path)

    v3_tables = {name for kind, name in _schema_objects(v3_path) if kind == "table"}
    v4_tables = {name for kind, name in _schema_objects(v4_path) if kind == "table"}

    assert v4_tables - v3_tables == {"recognition_jobs", "recognition_results"}


def _populated_schema_three(tmp_path: Path) -> Path:
    """A schema-3 database, written by the schema-3 runner, holding real rows."""
    database_path = tmp_path / "upgrade.db"
    with _previous_release(tmp_path):
        assert apply_migrations(database_path) == [1, 2, 3]
    record_observation(
        database_path,
        kind="application_start",
        source="mgo-api",
        status="success",
        summary="Row written by the schema-3 build",
    )
    with database_connection(database_path) as connection:
        _insert_capture(connection, "cap-1")
        _insert_capture(connection, "cap-2")
        connection.execute(
            "INSERT INTO capture_media_lifecycle VALUES "
            "('cap-2', 'deleted', ?, ?, 'age')",
            (utc_now_iso(), utc_now_iso()),
        )
    return database_path


def test_a_schema_three_database_upgrades_to_four_with_every_row_intact(
    tmp_path: Path,
) -> None:
    """The upgrade applies exactly 004 and changes no existing row."""
    database_path = _populated_schema_three(tmp_path)
    before = {
        table: _all_rows(database_path, table)
        for table in (
            "observations",
            "captures",
            "capture_media_lifecycle",
        )
    }
    projection_before = RetentionRepository(database_path).list_lifecycle_records()

    assert apply_migrations(database_path) == [4]

    assert read_schema_version(database_path) == 4
    for table, rows in before.items():
        assert _all_rows(database_path, table) == rows
    assert RetentionRepository(database_path).list_lifecycle_records() == (
        projection_before
    )
    assert len(list_observations(database_path)) == 1


def test_the_upgrade_is_additive_and_rebuilds_no_existing_object(
    tmp_path: Path,
) -> None:
    """Every schema-3 table and index survives with byte-identical DDL."""
    database_path = _populated_schema_three(tmp_path)
    before = _schema_objects(database_path)

    apply_migrations(database_path)

    after = _schema_objects(database_path)
    for key, sql in before.items():
        assert after[key] == sql, key
    assert set(after) - set(before) == {
        ("table", "recognition_jobs"),
        ("table", "recognition_results"),
        ("index", "sqlite_autoindex_recognition_jobs_1"),
        ("index", "sqlite_autoindex_recognition_jobs_2"),
        ("index", "sqlite_autoindex_recognition_results_1"),
        ("index", "sqlite_autoindex_recognition_results_2"),
        ("index", "idx_recognition_jobs_due"),
        ("index", "idx_recognition_jobs_lease"),
    }


def test_foreign_key_integrity_holds_after_the_upgrade(tmp_path: Path) -> None:
    """No row anywhere references something that does not exist."""
    database_path = _populated_schema_three(tmp_path)
    apply_migrations(database_path)

    with database_connection(database_path) as connection:
        _insert_job(connection, capture_id="cap-1")
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_the_previous_release_refuses_a_schema_four_database(
    tmp_path: Path,
) -> None:
    """The rollback implication, proven: a schema-3 build will not open it.

    The refusal leaves the database exactly as it was, which is why rolling a
    deployment back past this migration needs a schema-3 recovery set rather
    than the previous commit alone.
    """
    database_path = _populated_schema_three(tmp_path)
    apply_migrations(database_path)
    before = _schema_objects(database_path)

    with _previous_release(tmp_path), pytest.raises(IncompatibleSchemaError) as excinfo:
        apply_migrations(database_path)

    assert "newer than the version this application supports" in str(excinfo.value)
    assert read_schema_version(database_path) == 4
    assert _schema_objects(database_path) == before


def test_the_previous_release_health_check_reports_the_database_ahead(
    tmp_path: Path,
) -> None:
    """The schema-3 build's health check names the problem instead of guessing."""
    database_path = _populated_schema_three(tmp_path)
    apply_migrations(database_path)
    config = replace(
        load_config(),
        storage=StorageConfig(
            data_directory=database_path.parent,
            log_directory=database_path.parent / "logs",
            database_path=database_path,
        ),
    )

    with _previous_release(tmp_path), pytest.MonkeyPatch.context() as patch:
        patch.setattr("mgo.core.database_health.CURRENT_SCHEMA_VERSION", 3)
        health = check_database_health(config)

    assert health.migration_status is MigrationStatus.AHEAD
    assert health.schema_version == 4


def test_a_pre_existing_recognition_table_fails_migration_004(
    tmp_path: Path,
) -> None:
    """A shadow table cannot satisfy migration 004, which is not IF NOT EXISTS."""
    database_path = _populated_schema_three(tmp_path)
    with database_connection(database_path) as connection:
        connection.execute("CREATE TABLE recognition_jobs (id TEXT, state TEXT)")

    with pytest.raises(DatabaseError):
        apply_migrations(database_path)

    assert read_schema_version(database_path) == 3
    assert "recognition_results" not in {
        name for _, name in _schema_objects(database_path)
    }


# --- recognition_jobs constraints ---------------------------------------------


def test_a_valid_pending_job_is_accepted(database: Path) -> None:
    """The control for every refusal below."""
    with database_connection(database) as connection:
        _insert_job(connection)
        count = connection.execute("SELECT COUNT(*) FROM recognition_jobs").fetchone()
    assert count[0] == 1


@pytest.mark.parametrize(
    "state", ["pending", "running", "succeeded", "failed", "skipped", "superseded"]
)
def test_every_job_state_in_the_vocabulary_is_storable(
    database: Path, state: str
) -> None:
    overrides: dict[str, object] = {"state": state}
    if state == "running":
        overrides |= {
            "attempt_count": 1,
            "lease_owner": "worker:1",
            "lease_expires_at": LATER,
            "started_at": STAMP,
        }
    elif state != "pending":
        overrides |= {"finished_at": LATER, "next_attempt_at": None}
    if state in {"failed", "skipped"}:
        overrides["error_category"] = "media_missing"
    with database_connection(database) as connection:
        _insert_job(connection, **overrides)


@pytest.mark.parametrize("state", ["queued", "done", "PENDING", ""])
def test_a_job_state_outside_the_vocabulary_is_refused(
    database: Path, state: str
) -> None:
    with (
        pytest.raises(sqlite3.IntegrityError, match="CHECK"),
        database_connection(database) as connection,
    ):
        _insert_job(connection, state=state)


@pytest.mark.parametrize(
    "category",
    [
        "media_missing",
        "unsafe_path",
        "size_mismatch",
        "decode_error",
        "model_unavailable",
        "timeout",
        "resource_limit",
        "unexpected",
    ],
)
def test_every_error_category_in_the_vocabulary_is_storable(
    database: Path, category: str
) -> None:
    with database_connection(database) as connection:
        _insert_job(
            connection,
            state="failed",
            finished_at=LATER,
            error_category=category,
        )


@pytest.mark.parametrize(
    "category",
    ["not_regular_file", "Traceback (most recent call last)", "/var/lib/x.jpg"],
)
def test_an_error_category_outside_the_vocabulary_is_refused(
    database: Path, category: str
) -> None:
    """Free text -- an exception message, a path -- cannot be stored."""
    with (
        pytest.raises(sqlite3.IntegrityError, match="CHECK"),
        database_connection(database) as connection,
    ):
        _insert_job(
            connection, state="failed", finished_at=LATER, error_category=category
        )


def test_second_job_for_the_same_capture_and_pipeline_is_refused(
    database: Path,
) -> None:
    """``UNIQUE (capture_id, pipeline_version)`` is the idempotency guarantee."""
    with database_connection(database) as connection:
        _insert_job(connection)

    with (
        pytest.raises(sqlite3.IntegrityError, match="UNIQUE"),
        database_connection(database) as connection,
    ):
        _insert_job(connection)


def test_a_different_pipeline_version_is_a_distinct_job(database: Path) -> None:
    with database_connection(database) as connection:
        _insert_job(connection, pipeline_version="fake-0")
        _insert_job(connection, pipeline_version="fake-1")
        count = connection.execute("SELECT COUNT(*) FROM recognition_jobs").fetchone()
    assert count[0] == 2


def test_a_job_for_an_unknown_capture_is_refused(database: Path) -> None:
    with (
        pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"),
        database_connection(database) as connection,
    ):
        _insert_job(connection, capture_id="no-such-capture")


def test_a_job_bound_to_no_capture_is_refused(database: Path) -> None:
    with (
        pytest.raises(sqlite3.IntegrityError, match="NOT NULL"),
        database_connection(database) as connection,
    ):
        _insert_job(connection, capture_id=None)


def test_a_job_cannot_be_removed_from_under_its_capture(database: Path) -> None:
    """The catalogue stays append-only in practice: a queued capture is pinned."""
    with database_connection(database) as connection:
        _insert_job(connection)

    with (
        pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"),
        database_connection(database) as connection,
    ):
        connection.execute("DELETE FROM captures WHERE id = 'cap-1'")


@pytest.mark.parametrize(
    ("label", "overrides"),
    [
        (
            "running without a lease owner",
            {
                "state": "running",
                "attempt_count": 1,
                "lease_expires_at": LATER,
                "started_at": STAMP,
            },
        ),
        (
            "running without an expiry",
            {
                "state": "running",
                "attempt_count": 1,
                "lease_owner": "w:1",
                "started_at": STAMP,
            },
        ),
        (
            "running never started",
            {
                "state": "running",
                "attempt_count": 1,
                "lease_owner": "w:1",
                "lease_expires_at": LATER,
            },
        ),
        ("pending holding a lease", {"lease_owner": "w:1", "lease_expires_at": LATER}),
        (
            "terminal holding a lease",
            {
                "state": "failed",
                "finished_at": LATER,
                "error_category": "timeout",
                "lease_owner": "w:1",
                "lease_expires_at": LATER,
            },
        ),
        ("pending with a finish time", {"finished_at": LATER}),
        ("terminal without a finish time", {"state": "succeeded"}),
        ("pending with no retry time", {"next_attempt_at": None}),
        ("pending with no attempt left", {"attempt_count": 3, "max_attempts": 3}),
        (
            "more attempts than permitted",
            {
                "state": "failed",
                "finished_at": LATER,
                "error_category": "timeout",
                "attempt_count": 4,
                "max_attempts": 3,
            },
        ),
        ("negative attempts", {"attempt_count": -1}),
        # Terminal, so the pending rule (attempt_count < max_attempts) cannot
        # be what refuses it: only the max_attempts bound can.
        (
            "zero max attempts",
            {
                "state": "failed",
                "finished_at": LATER,
                "error_category": "timeout",
                "next_attempt_at": None,
                "attempt_count": 0,
                "max_attempts": 0,
            },
        ),
        # Within every numeric bound, so only the typeof check can refuse it.
        ("fractional attempt count", {"attempt_count": 0.5}),
        (
            "succeeded carrying an error",
            {"state": "succeeded", "finished_at": LATER, "error_category": "timeout"},
        ),
        ("failed without a reason", {"state": "failed", "finished_at": LATER}),
        ("skipped without a reason", {"state": "skipped", "finished_at": LATER}),
        ("empty pipeline version", {"pipeline_version": ""}),
        ("overlong pipeline version", {"pipeline_version": "v" * 65}),
        ("naive timestamp", {"next_attempt_at": "2026-09-11T12:00:00"}),
        ("zulu timestamp", {"next_attempt_at": "2026-09-11T12:00:00.000000Z"}),
        ("second-precision timestamp", {"created_at": "2026-09-11T12:00:00+00:00"}),
        ("non-UTC offset", {"next_attempt_at": "2026-09-11T14:00:00.000000+02:00"}),
    ],
)
def test_an_incoherent_job_row_is_refused(
    database: Path, label: str, overrides: dict[str, object]
) -> None:
    """Each variant breaks exactly one invariant the schema enforces itself."""
    with (
        pytest.raises(sqlite3.IntegrityError),
        database_connection(database) as connection,
    ):
        _insert_job(connection, **overrides)


# --- recognition_results constraints ------------------------------------------


def _succeeded_job(connection: sqlite3.Connection) -> str:
    return _insert_job(
        connection,
        state="succeeded",
        attempt_count=1,
        started_at=STAMP,
        finished_at=LATER,
    )


def test_a_result_with_full_provenance_is_accepted(database: Path) -> None:
    with database_connection(database) as connection:
        job_id = _succeeded_job(connection)
        _insert_result(
            connection,
            job_id,
            outcome="species",
            detector_model_id="detector",
            detector_model_sha256=SHA,
            classifier_model_id="classifier",
            classifier_model_sha256="b" * 64,
            label_set_id="labels",
            label_set_sha256="c" * 64,
            taxonomy_id="ebird",
            taxonomy_version="2025",
            preprocessing_version="pre-1",
            thresholds_version="thr-1",
            inference_duration_ms=1234,
            peak_rss_bytes=512_000_000,
            cpu_time_ms=900,
            image_width=4608,
            image_height=2592,
        )


@pytest.mark.parametrize(
    "outcome",
    ["species", "uncertain", "unknown_species", "no_bird", "person_present_only"],
)
def test_every_outcome_in_the_vocabulary_is_storable(
    database: Path, outcome: str
) -> None:
    with database_connection(database) as connection:
        _insert_result(connection, _succeeded_job(connection), outcome=outcome)


@pytest.mark.parametrize("outcome", ["bird", "robin", "failed", ""])
def test_an_outcome_outside_the_vocabulary_is_refused(
    database: Path, outcome: str
) -> None:
    with (
        pytest.raises(sqlite3.IntegrityError, match="CHECK"),
        database_connection(database) as connection,
    ):
        _insert_result(connection, _succeeded_job(connection), outcome=outcome)


def test_second_result_for_one_job_is_refused(database: Path) -> None:
    """One result per job, enforced by the database."""
    with database_connection(database) as connection:
        job_id = _succeeded_job(connection)
        _insert_result(connection, job_id)

    with (
        pytest.raises(sqlite3.IntegrityError, match="UNIQUE"),
        database_connection(database) as connection,
    ):
        _insert_result(connection, job_id, outcome="species")


def test_a_result_for_an_unknown_job_is_refused(database: Path) -> None:
    with (
        pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"),
        database_connection(database) as connection,
    ):
        _insert_result(connection, str(uuid.uuid4()))


@pytest.mark.parametrize(
    ("label", "overrides"),
    [
        ("identity without digest", {"detector_model_id": "detector"}),
        ("digest without identity", {"classifier_model_sha256": SHA}),
        ("uppercase digest", {"label_set_id": "labels", "label_set_sha256": "A" * 64}),
        ("short digest", {"detector_model_id": "d", "detector_model_sha256": "a" * 63}),
        ("taxonomy without version", {"taxonomy_id": "ebird"}),
        ("width without height", {"image_width": 4608}),
        ("zero width", {"image_width": 0, "image_height": 10}),
        ("negative duration", {"inference_duration_ms": -1}),
        ("textual rss", {"peak_rss_bytes": "lots"}),
        ("naive creation time", {"created_at": "2026-09-11T12:00:00"}),
    ],
)
def test_an_incoherent_result_row_is_refused(
    database: Path, label: str, overrides: dict[str, object]
) -> None:
    with (
        pytest.raises(sqlite3.IntegrityError),
        database_connection(database) as connection,
    ):
        _insert_result(connection, _succeeded_job(connection), **overrides)


# --- legacy adoption ----------------------------------------------------------


def _unversioned_v4(tmp_path: Path, name: str, *, jobs_ddl: str | None = None) -> Path:
    """An unversioned database carrying the full schema-4 table set."""
    database_path = tmp_path / f"{name}.db"
    with database_connection(database_path) as connection:
        for migration in (*_V3_MIGRATIONS, "004_recognition_jobs.sql"):
            sql = (MIGRATIONS_DIRECTORY / migration).read_text(encoding="utf-8")
            if jobs_ddl is not None and migration.startswith("004"):
                connection.executescript(jobs_ddl)
                continue
            connection.executescript(sql)
        connection.execute("DROP TABLE schema_migrations")
    return database_path


def test_a_canonical_unversioned_schema_four_database_is_adopted(
    tmp_path: Path,
) -> None:
    """The control: the real schema, with no history, adopts at version 4."""
    database_path = _unversioned_v4(tmp_path, "canonical")

    assert apply_migrations(database_path) == []
    assert read_schema_version(database_path) == 4


def _canonical_004() -> str:
    return (MIGRATIONS_DIRECTORY / "004_recognition_jobs.sql").read_text(
        encoding="utf-8"
    )


@pytest.mark.parametrize(
    ("label", "old", "new"),
    [
        pytest.param(
            "no uniqueness",
            "    UNIQUE (capture_id, pipeline_version),\n",
            "",
            id="no-uniqueness",
        ),
        pytest.param(
            "widened state vocabulary",
            "'skipped', 'superseded')),",
            "'skipped', 'superseded', 'cancelled')),",
            id="widened-states",
        ),
        pytest.param(
            "no foreign key",
            "    capture_id TEXT NOT NULL\n        REFERENCES captures(id),",
            "    capture_id TEXT NOT NULL,",
            id="no-foreign-key",
        ),
        pytest.param(
            "nullable capture id",
            "    capture_id TEXT NOT NULL\n",
            "    capture_id TEXT\n",
            id="nullable-capture-id",
        ),
        pytest.param(
            "no one-result-per-job",
            "    job_id TEXT NOT NULL UNIQUE\n",
            "    job_id TEXT NOT NULL\n",
            id="no-one-result-per-job",
        ),
        pytest.param(
            "widened outcome vocabulary",
            "'person_present_only')",
            "'person_present_only', 'robin')",
            id="widened-outcomes",
        ),
        pytest.param(
            "no failure-reason rule",
            "CHECK (state NOT IN ('failed', 'skipped') "
            "OR error_category IS NOT NULL)",
            "CHECK (1)",
            id="no-failure-reason-rule",
        ),
        pytest.param(
            "fractional attempts admitted",
            "CHECK (typeof(attempt_count) = 'integer' AND attempt_count >= 0)",
            "CHECK (attempt_count >= 0)",
            id="fractional-attempts",
        ),
        pytest.param(
            "unpaired image dimensions",
            "    CHECK ((image_width IS NULL) = (image_height IS NULL))",
            "    CHECK (1)",
            id="unpaired-dimensions",
        ),
        pytest.param(
            "no lease coherence",
            "(state <> 'running' AND lease_owner IS NULL AND lease_expires_at IS NULL)",
            "(state <> 'running')",
            id="no-lease-coherence",
        ),
    ],
)
def test_an_unversioned_schema_four_table_without_its_guarantees_is_refused(
    tmp_path: Path, label: str, old: str, new: str
) -> None:
    """Adoption verifies the recognition tables' constraints, not only names."""
    ddl = _canonical_004().replace("\r\n", "\n")
    assert ddl.count(old) == 1, label
    database_path = _unversioned_v4(
        tmp_path, label.replace(" ", "-"), jobs_ddl=ddl.replace(old, new)
    )

    with pytest.raises(IncompatibleSchemaError):
        apply_migrations(database_path)

    assert read_schema_version(database_path) is None


def test_recognition_tables_without_the_schema_beneath_them_are_refused(
    tmp_path: Path,
) -> None:
    """Version-4 tables over a version-2 database are not adopted as anything."""
    database_path = tmp_path / "stray.db"
    with database_connection(database_path) as connection:
        for migration in _V3_MIGRATIONS[:2]:
            connection.executescript(
                (MIGRATIONS_DIRECTORY / migration).read_text(encoding="utf-8")
            )
        connection.execute("DROP TABLE schema_migrations")
        connection.executescript(_canonical_004())

    with pytest.raises(IncompatibleSchemaError):
        apply_migrations(database_path)

    assert read_schema_version(database_path) is None
