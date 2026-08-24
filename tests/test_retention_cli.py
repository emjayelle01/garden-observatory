"""Tests for the ``mgo-retention`` operator command.

The command is the first supported way to invoke the retention engine, so what
matters most is what it *refuses*. Three gates stand between an operator and a
deletion -- the explicit ``--execute`` flag, an explicitly named configuration,
and ``retention.enabled`` -- and a fourth refuses any database that is not
already at the expected schema version, because this command must never migrate.

Every destructive test operates on a temporary database and a temporary capture
directory created by the test itself. No Raspberry Pi, no real capture
directory, no production database.
"""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

import mgo.retention.cli as cli
import mgo.retention.service as service_module
from mgo.core.config import CONFIG_PATH_ENV
from mgo.core.database import (
    MIGRATIONS_DIRECTORY,
    apply_migrations,
    database_connection,
)
from mgo.core.observations import list_observations

NOW = datetime.now(UTC)
PAYLOAD = b"jpeg-bytes-stand-in"

_CONFIG_TEMPLATE = """
[application]
name = "Matt's Garden Observatory"
environment = "test"
host = "127.0.0.1"
port = 8080

[storage]
data_directory = "{root}"
log_directory = "{root}"
database_path = "{database}"

[camera]
enabled = false
backend = "simulator"
detection_interval_seconds = 60
capture_directory = "{captures}"

[retention]
enabled = {enabled}
{bounds}
minimum_keep_count = {keep}
max_deletions_per_run = {per_run}

[health]
enabled = true
collection_interval_seconds = 60
temperature_warning_celsius = 70.0
temperature_critical_celsius = 80.0
disk_warning_percent = 80.0
disk_critical_percent = 90.0
memory_warning_percent = 85.0
memory_critical_percent = 95.0
"""


class _Deployment:
    """A temporary configuration file, database and capture directory."""

    def __init__(
        self,
        tmp_path: Path,
        *,
        enabled: bool = False,
        keep: int = 1,
        per_run: int = 25,
        migrate: bool = True,
    ) -> None:
        self.root = tmp_path
        self.captures = tmp_path / "captures"
        self.captures.mkdir(exist_ok=True)
        self.database_path = tmp_path / "mgo.db"
        if migrate:
            apply_migrations(self.database_path)

        self.config_path = tmp_path / "mgo.toml"
        self.config_path.write_text(
            _CONFIG_TEMPLATE.format(
                root=tmp_path.as_posix(),
                database=self.database_path.as_posix(),
                captures=self.captures.as_posix(),
                enabled=str(enabled).lower(),
                bounds="max_age_days = 7" if enabled else "# no bounds",
                keep=keep,
                per_run=per_run,
            ),
            encoding="utf-8",
        )

    def add(
        self,
        identifier: str,
        *,
        days_old: float = 900.0,
        origin: str | None = "motion",
        write_file: bool = True,
    ) -> Path:
        """Catalogue one capture and (by default) write its media."""
        media = self.captures / f"{identifier}.jpg"
        if write_file:
            media.write_bytes(PAYLOAD)
        stamp = (NOW - timedelta(days=days_old)).isoformat()
        metadata = {} if origin is None else {"origin": origin}
        with database_connection(self.database_path) as connection:
            connection.execute(
                """
                INSERT INTO captures (
                    id, filename, absolute_path, captured_at_utc, width, height,
                    filesize_bytes, camera_backend, created_at_utc, extra_metadata
                )
                VALUES (?, ?, ?, ?, 4608, 2592, ?, 'simulator', ?, ?)
                """,
                (
                    identifier,
                    media.name,
                    str(media),
                    stamp,
                    len(PAYLOAD),
                    stamp,
                    json.dumps(metadata),
                ),
            )
        return media

    def lifecycle(self) -> dict[str, str]:
        """Return ``{capture_id: state}`` for every lifecycle row."""
        with database_connection(self.database_path) as connection:
            return {
                str(row[0]): str(row[1])
                for row in connection.execute(
                    "SELECT capture_id, state FROM capture_media_lifecycle"
                )
            }

    def capture_ids(self) -> set[str]:
        """Return every catalogued capture id."""
        with database_connection(self.database_path) as connection:
            return {
                str(row[0])
                for row in connection.execute("SELECT id FROM captures")
            }


def _run(
    deployment: _Deployment | None,
    argv: list[str],
    monkeypatch: pytest.MonkeyPatch,
    *,
    set_config_env: bool = True,
) -> tuple[int, dict[str, Any], str]:
    """Invoke the command and return ``(exit code, stdout JSON, stderr)``."""
    if deployment is not None and set_config_env:
        monkeypatch.setenv(CONFIG_PATH_ENV, str(deployment.config_path))
    elif not set_config_env:
        monkeypatch.delenv(CONFIG_PATH_ENV, raising=False)

    out, err = io.StringIO(), io.StringIO()
    code = cli.main(argv, stdout=out, stderr=err)
    body = out.getvalue()
    return code, (json.loads(body) if body.strip() else {}), err.getvalue()


# --- argument contract ------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["nonsense"],
        ["plan", "--execute"],
        ["plan", "--max-age-days", "1"],
        ["run-once", "--execute", "--max-age-days", "1"],
        ["run-once", "--yes"],
        ["run-once", "--force"],
        ["plan", "extra"],
    ],
)
def test_an_unsupported_invocation_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, argv: list[str]
) -> None:
    """Unknown arguments and policy overrides are refused, never ignored.

    The policy-override cases matter most. An operator who could pass
    ``--max-age-days`` at the prompt could turn a reviewed retention policy into
    an unreviewed deletion at the moment of deletion, so the flags do not exist
    and are rejected rather than silently dropped.
    """
    deployment = _Deployment(tmp_path)

    code, _, _ = _run(deployment, argv, monkeypatch)

    assert code == cli.EXIT_REFUSED


def test_the_parser_accepts_no_policy_or_target_options() -> None:
    """No option anywhere in the command can redirect or retune the policy."""
    forbidden = {
        "--max-age-days",
        "--max-managed-bytes",
        "--minimum-keep-count",
        "--max-deletions",
        "--origin",
        "--capture-id",
        "--before",
        "--after",
        "--config",
        "--config-file",
        "--database",
        "--capture-root",
        "--yes",
        "-y",
        "--force",
        "--really",
        "--override",
        "--interval",
        "--daemon",
        "--loop",
    }
    text = cli.build_parser().format_help()
    for action in cli.build_parser()._actions:
        for option in action.option_strings:
            assert option not in forbidden, f"{option} must not exist"
    assert not forbidden & set(text.split())


def test_only_two_subcommands_exist() -> None:
    """Exactly ``plan`` and ``run-once``; nothing deletes a named file.

    Read off the parser's own choices rather than the help prose -- the help
    text legitimately contains the word "delete" while describing what
    ``run-once`` can do, and a substring search would either miss a real
    ``delete`` subcommand or trip over that sentence.
    """
    choices: set[str] = set()
    for action in cli.build_parser()._actions:
        if action.dest == "command" and action.choices:
            choices = set(action.choices)

    assert choices == {"plan", "run-once"}
    for forbidden in ("delete", "delete-file", "delete-capture", "purge", "rm"):
        assert forbidden not in choices


def test_help_states_the_safety_contract() -> None:
    """An operator reading ``--help`` learns the gates before running anything."""
    help_text = cli.build_parser().format_help().lower()

    assert "read-only" in help_text
    assert "delete" in help_text
    assert "--execute" in help_text
    assert CONFIG_PATH_ENV.lower() in help_text
    assert "schedule" in help_text


# --- plan -------------------------------------------------------------------


def test_plan_reports_the_configured_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control: a valid deployment produces the expected preview."""
    deployment = _Deployment(tmp_path, enabled=True)
    deployment.add("cap-old")
    deployment.add("cap-new", days_old=0.0)

    code, payload, _ = _run(deployment, ["plan"], monkeypatch)

    assert code == cli.EXIT_SUCCESS
    assert payload["candidate_count"] == 1
    assert payload["candidates"][0]["capture_id"] == "cap-old"
    assert payload["managed_present_count"] == 2
    assert payload["protected_count"] == 1
    assert payload["retention_enabled"] is True


def test_plan_works_while_retention_is_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Previewing is how an operator decides whether to enable retention."""
    deployment = _Deployment(tmp_path, enabled=False)
    deployment.add("cap-old")
    deployment.add("cap-new", days_old=0.0)

    code, payload, _ = _run(deployment, ["plan"], monkeypatch)

    assert code == cli.EXIT_SUCCESS
    assert payload["retention_enabled"] is False
    # With no configured bound, a disabled policy selects nothing.
    assert payload["candidate_count"] == 0


def test_plan_output_has_a_deterministic_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The JSON contract is stable enough to diff between runs."""
    deployment = _Deployment(tmp_path, enabled=True)
    deployment.add("cap-old")
    deployment.add("cap-new", days_old=0.0)

    first = _run(deployment, ["plan"], monkeypatch)
    second = _run(deployment, ["plan"], monkeypatch)

    assert first == second
    assert set(first[1]) == {
        "candidates",
        "candidate_count",
        "managed_present_count",
        "managed_present_bytes",
        "protected_count",
        "projected_bytes_reclaimed",
        "projected_managed_bytes",
        "more_work_remains",
        "byte_target_satisfiable",
        "retention_enabled",
    }
    assert set(first[1]["candidates"][0]) == {
        "capture_id",
        "filename",
        "captured_at",
        "filesize_bytes",
        "policy_reason",
    }


def test_plan_leaks_no_path_of_any_kind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No capture root, database location or configuration path is published."""
    deployment = _Deployment(tmp_path, enabled=True)
    deployment.add("cap-old")
    deployment.add("cap-new", days_old=0.0)

    _, payload, stderr = _run(deployment, ["plan"], monkeypatch)
    rendered = json.dumps(payload) + stderr

    assert "absolute_path" not in rendered
    assert str(deployment.captures) not in rendered
    assert str(deployment.database_path) not in rendered
    assert str(deployment.config_path) not in rendered
    assert deployment.captures.as_posix() not in rendered
    assert deployment.database_path.as_posix() not in rendered


def test_plan_mutates_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No lifecycle row, no observation, no deleted media, no counter."""
    deployment = _Deployment(tmp_path, enabled=True)
    media = deployment.add("cap-old")
    deployment.add("cap-new", days_old=0.0)

    code, _, _ = _run(deployment, ["plan"], monkeypatch)

    assert code == cli.EXIT_SUCCESS
    assert deployment.lifecycle() == {}
    assert list_observations(deployment.database_path) == []
    assert media.exists()
    assert deployment.capture_ids() == {"cap-old", "cap-new"}


def test_plan_never_reaches_the_filesystem_veto(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Planning stats no file: the filesystem is only a veto during execution.

    A capture whose media is missing still appears in the preview, because the
    plan reports what the *policy* selected. Stat-ing during a preview would
    make ``plan`` report a different set from the one the policy chose.
    """

    def _explode(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("plan touched the filesystem")

    for name in ("_path_exists", "_is_regular_file", "_file_size", "_unlink"):
        monkeypatch.setattr(service_module, name, _explode)
    deployment = _Deployment(tmp_path, enabled=True)
    deployment.add("cap-gone", write_file=False)
    deployment.add("cap-new", days_old=0.0)

    code, payload, _ = _run(deployment, ["plan"], monkeypatch)

    assert code == cli.EXIT_SUCCESS
    assert payload["candidates"][0]["capture_id"] == "cap-gone"


def test_plan_touches_no_camera_code(tmp_path: Path) -> None:
    """Asserted structurally: the CLI imports nothing from the camera stack."""
    source = Path(cli.__file__).read_text(encoding="utf-8")

    for forbidden in (
        "mgo.camera",
        "mgo.event_capture",
        "mgo.motion",
        "CameraCoordinator",
        "CaptureService",
        "CaptureWorkflow",
    ):
        assert forbidden not in source


# --- the schema gate --------------------------------------------------------


def _downgrade_to_version_two(deployment: _Deployment) -> None:
    """Rewrite the database as a genuine version-2 deployment."""
    deployment.database_path.unlink()
    for suffix in ("-wal", "-shm"):
        sidecar = deployment.database_path.with_name(
            deployment.database_path.name + suffix
        )
        if sidecar.exists():
            sidecar.unlink()
    with database_connection(deployment.database_path) as connection:
        for name in ("001_initial_observation_engine", "002_capture_archive"):
            connection.executescript(
                (MIGRATIONS_DIRECTORY / f"{name}.sql").read_text(encoding="utf-8")
            )
        # The migration files create tables; the *runner* writes the history
        # rows. Without these inserts this would be an unversioned database and
        # the test would pass for the wrong reason.
        connection.execute("DELETE FROM schema_migrations")
        connection.execute(
            "INSERT INTO schema_migrations (version, name, applied_at) "
            "VALUES (1, '001.sql', ?), (2, '002.sql', ?)",
            (NOW.isoformat(), NOW.isoformat()),
        )


@pytest.mark.parametrize("command", [["plan"], ["run-once", "--execute"]])
def test_a_lower_schema_is_refused_and_not_migrated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: list[str]
) -> None:
    """A version-2 database is refused, and is still version 2 afterwards.

    This is the load-bearing one. Schema migration belongs to application
    startup; an operator asking to inspect or execute retention must never
    silently upgrade a database as a side effect of asking.
    """
    deployment = _Deployment(tmp_path, enabled=True)
    _downgrade_to_version_two(deployment)

    code, _, stderr = _run(deployment, command, monkeypatch)

    assert code == cli.EXIT_SCHEMA
    assert "schema version" in stderr
    with database_connection(deployment.database_path) as connection:
        version = connection.execute(
            "SELECT MAX(version) FROM schema_migrations"
        ).fetchone()[0]
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert version == 2
    assert "capture_media_lifecycle" not in tables


@pytest.mark.parametrize("command", [["plan"], ["run-once", "--execute"]])
def test_a_higher_schema_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: list[str]
) -> None:
    """A database from a newer build is refused rather than acted on."""
    deployment = _Deployment(tmp_path, enabled=True)
    with database_connection(deployment.database_path) as connection:
        connection.execute(
            "INSERT INTO schema_migrations (version, name, applied_at) "
            "VALUES (4, '004_from_the_future.sql', ?)",
            (NOW.isoformat(),),
        )

    code, _, _ = _run(deployment, command, monkeypatch)

    assert code == cli.EXIT_SCHEMA


@pytest.mark.parametrize("command", [["plan"], ["run-once", "--execute"]])
def test_an_unversioned_schema_is_refused_and_not_adopted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: list[str]
) -> None:
    """An unversioned database is refused, and no history is fabricated."""
    deployment = _Deployment(tmp_path, enabled=True)
    with database_connection(deployment.database_path) as connection:
        connection.execute("DROP TABLE schema_migrations")

    code, _, _ = _run(deployment, command, monkeypatch)

    assert code == cli.EXIT_SCHEMA
    with database_connection(deployment.database_path) as connection:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert "schema_migrations" not in tables


@pytest.mark.parametrize("command", [["plan"], ["run-once", "--execute"]])
def test_a_missing_database_is_refused_and_not_created(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: list[str]
) -> None:
    """Pointing the command at the wrong path is a refusal, not a creation."""
    deployment = _Deployment(tmp_path, enabled=True, migrate=False)
    assert not deployment.database_path.exists()

    code, _, _ = _run(deployment, command, monkeypatch)

    assert code == cli.EXIT_SCHEMA
    assert not deployment.database_path.exists()


def test_a_malformed_catalogue_is_refused_safely(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A catalogue that cannot be decoded refuses without leaking why."""
    deployment = _Deployment(tmp_path, enabled=True)
    deployment.add("cap-old")
    with database_connection(deployment.database_path) as connection:
        connection.execute("UPDATE captures SET extra_metadata = 'not json'")

    code, _, stderr = _run(deployment, ["plan"], monkeypatch)

    assert code == cli.EXIT_SCHEMA
    assert "not json" not in stderr
    assert str(deployment.database_path) not in stderr


# --- run-once gates ---------------------------------------------------------


def test_run_once_without_execute_refuses_before_any_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gate A. The flag is required and has no alias."""

    def _explode(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("run-once unlinked without --execute")

    monkeypatch.setattr(service_module, "_unlink", _explode)
    deployment = _Deployment(tmp_path, enabled=True)
    media = deployment.add("cap-old")
    deployment.add("cap-new", days_old=0.0)

    code, _, stderr = _run(deployment, ["run-once"], monkeypatch)

    assert code == cli.EXIT_REFUSED
    assert "--execute" in stderr
    assert media.exists()
    assert deployment.lifecycle() == {}
    assert list_observations(deployment.database_path) == []


def test_run_once_with_retention_disabled_refuses_before_any_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gate B. The flag alone is not enough; configuration must agree."""

    def _explode(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("run-once unlinked while retention was disabled")

    monkeypatch.setattr(service_module, "_unlink", _explode)
    deployment = _Deployment(tmp_path, enabled=False)
    media = deployment.add("cap-old")

    code, _, stderr = _run(deployment, ["run-once", "--execute"], monkeypatch)

    assert code == cli.EXIT_REFUSED
    assert "disabled" in stderr
    assert media.exists()
    assert deployment.lifecycle() == {}


def test_run_once_without_an_explicit_configuration_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gate C. A destructive run must name the deployment it acts on.

    Without this, an operator standing in the repository could run
    ``run-once --execute`` and have it resolve the tracked *development*
    configuration -- and delete against whichever database and capture directory
    that file happens to name.
    """

    def _explode(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("run-once ran without an explicit configuration")

    monkeypatch.setattr(service_module, "_unlink", _explode)
    deployment = _Deployment(tmp_path, enabled=True)
    media = deployment.add("cap-old")

    code, _, stderr = _run(
        deployment, ["run-once", "--execute"], monkeypatch, set_config_env=False
    )

    assert code == cli.EXIT_REFUSED
    assert CONFIG_PATH_ENV in stderr
    assert media.exists()
    assert deployment.lifecycle() == {}


@pytest.mark.parametrize("value", ["", "   "])
def test_an_empty_configuration_variable_is_not_an_explicit_choice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """A set-but-blank variable is not a deliberate selection.

    The exit code alone does not prove this: the configuration loader also
    refuses a blank variable, and both refusals exit the same way. What
    distinguishes them is the message, and the difference matters to whoever is
    reading it -- this gate names the variable to set, whereas the loader can
    only say the configuration would not load. So the message is asserted, which
    is also what proves the gate fired *before* the loader was reached.
    """
    deployment = _Deployment(tmp_path, enabled=True)
    deployment.add("cap-old")
    monkeypatch.setenv(CONFIG_PATH_ENV, value)

    out, err = io.StringIO(), io.StringIO()
    code = cli.main(["run-once", "--execute"], stdout=out, stderr=err)

    assert code == cli.EXIT_REFUSED
    assert err.getvalue().strip() == cli.REFUSAL_NO_EXPLICIT_CONFIG
    assert CONFIG_PATH_ENV in err.getvalue()
    assert deployment.lifecycle() == {}


# --- run-once execution -----------------------------------------------------


def test_a_gated_run_deletes_exactly_one_eligible_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """All three gates satisfied: one run, one deletion, one observation."""
    deployment = _Deployment(tmp_path, enabled=True)
    old = deployment.add("cap-old")
    recent = deployment.add("cap-new", days_old=0.0)

    code, payload, _ = _run(deployment, ["run-once", "--execute"], monkeypatch)

    assert code == cli.EXIT_SUCCESS
    assert payload["executed"] is True
    assert payload["deleted_count"] == 1
    assert payload["bytes_reclaimed"] == len(PAYLOAD)
    assert payload["error_category"] is None
    assert not old.exists()
    assert recent.exists()


def test_the_capture_row_survives_a_successful_reclamation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reclaiming media never erases the history that a capture happened."""
    deployment = _Deployment(tmp_path, enabled=True)
    deployment.add("cap-old")
    deployment.add("cap-new", days_old=0.0)

    _run(deployment, ["run-once", "--execute"], monkeypatch)

    assert deployment.capture_ids() == {"cap-old", "cap-new"}


def test_a_successful_run_reaches_the_deleted_lifecycle_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lifecycle row records the reclamation durably."""
    deployment = _Deployment(tmp_path, enabled=True)
    deployment.add("cap-old")
    deployment.add("cap-new", days_old=0.0)

    _run(deployment, ["run-once", "--execute"], monkeypatch)

    assert deployment.lifecycle() == {"cap-old": "deleted"}


def test_a_successful_run_persists_exactly_one_success_observation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One deletion, one immutable timeline entry."""
    deployment = _Deployment(tmp_path, enabled=True)
    deployment.add("cap-old")
    deployment.add("cap-new", days_old=0.0)

    _run(deployment, ["run-once", "--execute"], monkeypatch)

    observations = list_observations(
        deployment.database_path, kind="capture_retention"
    )
    assert len(observations) == 1
    assert observations[0].status == "reclaimed"
    assert observations[0].correlation_id == "cap-old"


@pytest.mark.parametrize("origin", [None, "timelapse", "Motion"])
def test_a_protected_capture_is_never_deleted_through_the_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, origin: str | None
) -> None:
    """Manual and unknown-origin captures are protected here too."""
    deployment = _Deployment(tmp_path, enabled=True)
    protected = deployment.add("cap-protected", origin=origin)
    deployment.add("cap-new", days_old=0.0)

    code, payload, _ = _run(deployment, ["run-once", "--execute"], monkeypatch)

    assert code == cli.EXIT_SUCCESS
    assert payload["deleted_count"] == 0
    assert protected.exists()
    assert deployment.lifecycle() == {}


def test_more_work_remaining_does_not_trigger_a_second_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One invocation is one run, even when the result says more is eligible.

    Repeating here because ``more_work_remains`` is true would be a scheduler
    with extra steps. The count of ``run_once`` calls is asserted directly.
    """
    deployment = _Deployment(tmp_path, enabled=True, per_run=1)
    for index in range(4):
        deployment.add(f"cap-{index}", days_old=900 - index)
    deployment.add("cap-new", days_old=0.0)

    calls: list[int] = []
    original = service_module.RetentionService.run_once

    def _counting(self: Any) -> Any:
        calls.append(1)
        return original(self)

    monkeypatch.setattr(service_module.RetentionService, "run_once", _counting)

    code, payload, _ = _run(deployment, ["run-once", "--execute"], monkeypatch)

    assert code == cli.EXIT_SUCCESS
    assert payload["more_work_remains"] is True
    assert payload["deleted_count"] == 1
    assert len(calls) == 1
    assert len(list(deployment.captures.iterdir())) == 4


def test_the_cli_delegates_to_the_retention_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The command runs the reviewed engine rather than reimplementing it."""
    deployment = _Deployment(tmp_path, enabled=True)
    deployment.add("cap-old")
    deployment.add("cap-new", days_old=0.0)

    def _explode(self: Any) -> Any:
        raise AssertionError("the CLI bypassed RetentionService.run_once")

    monkeypatch.setattr(service_module.RetentionService, "run_once", _explode)

    code, _, _ = _run(deployment, ["run-once", "--execute"], monkeypatch)

    assert code == cli.EXIT_UNEXPECTED


# --- failure reporting ------------------------------------------------------


def test_a_bounded_retention_failure_exits_non_zero_with_a_fixed_category(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A safety refusal during the run is reported, not hidden."""
    deployment = _Deployment(tmp_path, enabled=True)
    deployment.add("cap-gone", write_file=False)
    deployment.add("cap-new", days_old=0.0)

    code, payload, _ = _run(deployment, ["run-once", "--execute"], monkeypatch)

    assert code == cli.EXIT_RETENTION_ERROR
    assert payload["error_category"] == "media_missing"
    assert payload["error_message"] == (
        "A capture's media is missing without a recorded deletion intent."
    )
    assert payload["deleted_count"] == 0


def test_failure_output_carries_no_path_or_raw_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The published failure is the fixed vocabulary and nothing else."""
    deployment = _Deployment(tmp_path, enabled=True)
    secret = "C:/secret/location/mgo.db"

    def _fail(path: Path) -> None:
        raise PermissionError(f"cannot remove {secret}")

    monkeypatch.setattr(service_module, "_unlink", _fail)
    deployment.add("cap-old")
    deployment.add("cap-new", days_old=0.0)

    code, payload, stderr = _run(
        deployment, ["run-once", "--execute"], monkeypatch
    )
    rendered = json.dumps(payload) + stderr

    assert code == cli.EXIT_RETENTION_ERROR
    assert payload["error_category"] == "filesystem_delete_failed"
    assert secret not in rendered
    assert "PermissionError" not in rendered
    assert "Traceback" not in rendered
    assert str(deployment.captures) not in rendered
    assert str(deployment.database_path) not in rendered
    assert "filename" not in payload


def test_an_unexpected_error_becomes_a_fixed_operator_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No traceback reaches the operator; a fixed sentence does."""
    deployment = _Deployment(tmp_path, enabled=True)
    deployment.add("cap-old")
    secret = "C:/secret/location/mgo.db"

    def _explode(self: Any) -> Any:
        raise RuntimeError(f"internal failure touching {secret}")

    monkeypatch.setattr(service_module.RetentionService, "run_once", _explode)

    code, _, stderr = _run(deployment, ["run-once", "--execute"], monkeypatch)

    assert code == cli.EXIT_UNEXPECTED
    assert stderr.strip() == cli.UNEXPECTED_FAILURE
    assert secret not in stderr
    assert "RuntimeError" not in stderr
    assert "Traceback" not in stderr


def test_the_result_payload_declares_exactly_the_safe_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The destructive result contract is bounded and stable."""
    deployment = _Deployment(tmp_path, enabled=True)
    deployment.add("cap-old")
    deployment.add("cap-new", days_old=0.0)

    _, payload, _ = _run(deployment, ["run-once", "--execute"], monkeypatch)

    assert set(payload) == {
        "executed",
        "enabled",
        "candidate_count",
        "deleted_count",
        "bytes_reclaimed",
        "recovered_count",
        "more_work_remains",
        "error_category",
        "error_message",
    }


# --- pending recovery -------------------------------------------------------


def test_pending_recovery_semantics_survive_the_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A durable intent whose file is already gone is finalised, not re-decided.

    The keep count here would protect the capture if the intent were re-decided
    now; recovery deliberately ignores it, because the destructive decision was
    already made and committed.
    """
    deployment = _Deployment(tmp_path, enabled=True, keep=100)
    media = deployment.add("cap-pending", days_old=0.5)
    with database_connection(deployment.database_path) as connection:
        connection.execute(
            "INSERT INTO capture_media_lifecycle VALUES "
            "(?, 'pending_delete', ?, NULL, 'age')",
            ("cap-pending", NOW.isoformat()),
        )
    media.unlink()

    code, payload, _ = _run(deployment, ["run-once", "--execute"], monkeypatch)

    assert code == cli.EXIT_SUCCESS
    assert payload["recovered_count"] == 1
    assert deployment.lifecycle() == {"cap-pending": "deleted"}
    observations = list_observations(
        deployment.database_path, kind="capture_retention"
    )
    assert len(observations) == 1
    assert observations[0].payload["recovered_pending"] is True


def test_a_disabled_configuration_recovers_nothing_through_the_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Disabled still means no destructive mutation, recovery included."""
    deployment = _Deployment(tmp_path, enabled=False)
    media = deployment.add("cap-pending")
    with database_connection(deployment.database_path) as connection:
        connection.execute(
            "INSERT INTO capture_media_lifecycle VALUES "
            "(?, 'pending_delete', ?, NULL, 'age')",
            ("cap-pending", NOW.isoformat()),
        )

    code, _, _ = _run(deployment, ["run-once", "--execute"], monkeypatch)

    assert code == cli.EXIT_REFUSED
    assert media.exists()
    assert deployment.lifecycle() == {"cap-pending": "pending_delete"}


# --- packaging --------------------------------------------------------------


def test_the_project_declares_the_console_entry_point() -> None:
    """The packaged command targets this module's ``main``."""
    import tomllib

    from mgo.core.config import PROJECT_ROOT

    manifest = tomllib.loads(
        (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    scripts = manifest["project"]["scripts"]

    assert scripts[cli.COMMAND_NAME] == "mgo.retention.cli:main"
    assert callable(cli.main)


def test_the_entry_point_adds_no_dependency() -> None:
    """Two subcommands do not justify a CLI framework."""
    import tomllib

    from mgo.core.config import PROJECT_ROOT

    manifest = tomllib.loads(
        (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    declared = " ".join(manifest["project"].get("dependencies", [])).lower()

    for framework in ("click", "typer", "docopt", "fire"):
        assert framework not in declared


def test_help_exits_zero(tmp_path: Path) -> None:
    """``--help`` is a successful invocation, not a refusal."""
    out, err = io.StringIO(), io.StringIO()

    assert cli.main(["--help"], stdout=out, stderr=err) == cli.EXIT_SUCCESS


def test_no_http_or_scheduling_machinery_exists_in_the_command() -> None:
    """The command calls the domain directly and schedules nothing."""
    source = Path(cli.__file__).read_text(encoding="utf-8")

    # Import statements and real constructs, not prose: the module docstring
    # legitimately discusses scheduling in order to say there is none, and a
    # bare substring search would flag its own explanation.
    imported = {
        line.strip()
        for line in source.splitlines()
        if line.startswith(("import ", "from "))
    }
    for module in ("httpx", "requests", "urllib", "asyncio", "sched", "time"):
        # Exact import forms, not substrings: "RetentionRuntimeState" contains
        # "time", and matching loosely would flag a value object as a scheduler.
        forbidden = (f"import {module}", f"from {module} import", f"from {module}.")
        assert not any(
            line.startswith(forbidden) for line in imported
        ), module

    for construct in (
        "while True",
        "threading.Timer(",
        "apply_migrations(",
        "sleep(",
    ):
        assert construct not in source


def test_the_database_is_never_migrated_by_the_command() -> None:
    """Structural proof that no migration entry point is reachable."""
    source = Path(cli.__file__).read_text(encoding="utf-8")

    assert "apply_migrations" not in source
    assert "read_schema_version" in source
