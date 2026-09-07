"""Tests for the ``mgo-retention`` operator command.

The command is the first supported way to invoke the retention engine, so what
matters most is what it *refuses*. Three gates stand between an operator and a
deletion, and they are named here in the order the code checks them:

* **Gate A** -- the exact ``--execute`` flag, before any file is read;
* **Gate B** -- ``MGO_CONFIG_PATH`` set and absolute as supplied, also before
  any file is read;
* **Gate C** -- ``retention.enabled``, necessarily after the configuration has
  been read, because that is where the value lives.

A fourth gate refuses any database that is not already at the expected schema
version, because this command must never migrate.

Every destructive test operates on a temporary database and a temporary capture
directory created by the test itself. No Raspberry Pi, no real capture
directory, no production database.
"""

from __future__ import annotations

import io
import json
import sys
from collections.abc import Iterator
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
from mgo.retention.models import RetentionPlan

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

    def rewrite_config(self, body: str) -> None:
        """Replace the configuration file wholesale, for shape-error tests."""
        self.config_path.write_text(body, encoding="utf-8")

    def add(
        self,
        identifier: str,
        *,
        days_old: float = 900.0,
        origin: str | None = "motion",
        write_file: bool = True,
        filename: str | None = None,
    ) -> Path:
        """Catalogue one capture and (by default) write its media.

        ``filename`` overrides the catalogue's filename column *without* moving
        the media, which is how a damaged or hand-edited catalogue presents: the
        column holds something the capture pipeline would never have written.
        """
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
                    filename if filename is not None else media.name,
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


def test_only_three_subcommands_exist() -> None:
    """Exactly ``plan``, ``run-once`` and ``scheduled-run``; nothing deletes a
    named file.

    ``scheduled-run`` (Task 14.5) is the timer's entry point: the same single
    run as ``run-once`` behind the same ``--execute`` gate, differing only in
    how a run that correctly declined to start is reported.

    Read off the parser's own choices rather than the help prose -- the help
    text legitimately contains the word "delete" while describing what
    ``run-once`` can do, and a substring search would either miss a real
    ``delete`` subcommand or trip over that sentence.
    """
    choices: set[str] = set()
    for action in cli.build_parser()._actions:
        if action.dest == "command" and action.choices:
            choices = set(action.choices)

    assert choices == {"plan", "run-once", "scheduled-run"}
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
    # No ``filename``: the catalogue value has only been validated as a
    # non-empty string, so it is omitted from operator output rather than
    # published unverified. See the hostile-filename tests below.
    assert set(first[1]["candidates"][0]) == {
        "capture_id",
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
    """Gate A. The flag is required, and has no alias and no abbreviation."""

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
    """Gate C. The flag alone is not enough; the configuration must agree.

    Checked after the configuration is read, because ``retention.enabled`` is a
    value inside it. Nothing destructive happens in between.
    """

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
    """Gate B. A destructive run must name the deployment it acts on.

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


# --- operator plan output carries no filename (correction round 1) ----------
#
# ``RetentionPlan.as_dict()`` includes each candidate's catalogue ``filename``,
# and that value has been validated only as a non-empty string. It has not
# passed the path/filename safety boundary -- that boundary belongs to
# destructive execution, and running it during a preview would make ``plan``
# disagree with the pure policy selection it exists to report. So a damaged
# catalogue can hold a "filename" that is really a path, and publishing it would
# put that string into operator output the contract says carries none.

#: Filenames the capture pipeline would never write, which a damaged or
#: hand-edited catalogue can nonetheless contain. The Windows and UNC forms are
#: the ones that matter most to the *checking*: their backslashes are doubled in
#: rendered JSON, so a substring search of the document misses them entirely.
HOSTILE_FILENAMES = [
    "/var/lib/garden-observatory/db/mgo.db",
    "../../secret.jpg",
    "C:\\sensitive\\secret.jpg",
    "\\\\server\\share\\private\\secret.jpg",
    "~/private/secret.jpg",
    "/etc/garden-observatory/mgo.toml",
    "../../../captures/private.jpg",
]


def _decoded_strings(value: Any) -> Iterator[str]:
    """Yield every mapping key and every string scalar in a *decoded* value.

    Recursive, and deliberately over-inclusive: mapping keys, mapping values and
    list elements at any depth. The point is to search the decoded structure,
    where a Windows or UNC path is an ordinary string, rather than the rendered
    document, where its backslashes are doubled and a substring search silently
    finds nothing.
    """
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from _decoded_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _decoded_strings(item)
    elif isinstance(value, str):
        yield value


def _assert_absent_from_decoded(payload: Any, hostile: str) -> None:
    """Assert ``hostile`` occurs nowhere in the decoded structure.

    As an exact value *and* as a substring, in keys as well as values, because a
    leak does not have to arrive whole or under the key it came from.
    """
    strings = list(_decoded_strings(payload))
    assert strings, "the walk found nothing to inspect, so it proves nothing"
    for text in strings:
        assert hostile not in text, (hostile, text)


def test_ordinary_plan_output_has_no_filename_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even an entirely benign filename is omitted, not merely a hostile one."""
    deployment = _Deployment(tmp_path, enabled=True)
    deployment.add("cap-old")
    deployment.add("cap-new", days_old=0.0)

    code, payload, _ = _run(deployment, ["plan"], monkeypatch)

    assert code == cli.EXIT_SUCCESS
    assert payload["candidate_count"] == 1
    assert "filename" not in payload["candidates"][0]
    assert set(payload["candidates"][0]) == {
        "capture_id",
        "captured_at",
        "filesize_bytes",
        "policy_reason",
    }


@pytest.mark.parametrize("hostile", HOSTILE_FILENAMES)
def test_a_hostile_catalogue_filename_never_reaches_operator_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, hostile: str
) -> None:
    """The raw string appears nowhere in the decoded output, or in stderr.

    The decoded structure is what is searched, recursively, through
    :func:`_assert_absent_from_decoded`. That distinction is the whole test: a
    Windows or UNC filename is backslash-escaped on the way out, so a substring
    search of the *rendered* document misses a leak that is plainly there once
    decoded. An earlier version of this test tried to close that gap with
    ``json.dumps(json.loads(rendered))``, which re-encodes to the identical
    string and therefore repeated the same blind spot -- see
    ``test_the_decoded_walk_catches_what_a_rendered_search_misses``.

    The raw text is still checked as well, for the values where it does work.
    """
    deployment = _Deployment(tmp_path, enabled=True)
    deployment.add("cap-hostile", filename=hostile)
    deployment.add("cap-new", days_old=0.0)

    code, payload, stderr = _run(deployment, ["plan"], monkeypatch)

    assert code == cli.EXIT_SUCCESS
    assert hostile not in json.dumps(payload)
    assert hostile not in stderr
    _assert_absent_from_decoded(payload, hostile)


@pytest.mark.parametrize("hostile", HOSTILE_FILENAMES)
def test_a_hostile_filename_is_still_selected_by_the_pure_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, hostile: str
) -> None:
    """Privacy is achieved by omission, not by dropping the candidate.

    This is the control that keeps the previous test honest: if the hostile
    capture simply vanished from the plan, "the string is absent" would prove
    nothing about output privacy. The candidate is still selected, still
    reported, and still carries every planning fact.
    """
    deployment = _Deployment(tmp_path, enabled=True)
    deployment.add("cap-hostile", filename=hostile)
    deployment.add("cap-new", days_old=0.0)

    _, payload, _ = _run(deployment, ["plan"], monkeypatch)

    assert payload["candidate_count"] == 1
    candidate = payload["candidates"][0]
    assert candidate["capture_id"] == "cap-hostile"
    assert candidate["policy_reason"] == "age"
    assert candidate["filesize_bytes"] == len(PAYLOAD)
    assert candidate["captured_at"]


def test_plan_output_stays_deterministic_without_the_filename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dropping a field did not make the projection unstable."""
    deployment = _Deployment(tmp_path, enabled=True)
    deployment.add("cap-a", days_old=900)
    deployment.add("cap-b", days_old=800)
    deployment.add("cap-new", days_old=0.0)

    assert _run(deployment, ["plan"], monkeypatch) == _run(
        deployment, ["plan"], monkeypatch
    )


def test_the_operator_projection_adds_no_filesystem_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The correction did not smuggle validation into ``plan``.

    Omitting the filename is an output decision. It must not have become an
    excuse to start stat-ing candidates, which would change what ``plan``
    reports.
    """

    def _explode(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("plan touched the filesystem")

    for name in ("_path_exists", "_is_regular_file", "_file_size", "_unlink"):
        monkeypatch.setattr(service_module, name, _explode)
    deployment = _Deployment(tmp_path, enabled=True)
    deployment.add("cap-hostile", filename="../../secret.jpg", write_file=False)
    deployment.add("cap-new", days_old=0.0)

    code, payload, _ = _run(deployment, ["plan"], monkeypatch)

    assert code == cli.EXIT_SUCCESS
    assert payload["candidates"][0]["capture_id"] == "cap-hostile"


def test_destructive_execution_still_validates_the_filename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Omitting it from *output* did not remove it from the safety boundary.

    The catalogue filename disagrees with the path's final component, which is
    exactly what the Task 14.1 boundary refuses -- and it must still refuse,
    because the destructive path is where that value actually matters.
    """
    deployment = _Deployment(tmp_path, enabled=True)
    media = deployment.add("cap-hostile", filename="../../secret.jpg")
    deployment.add("cap-new", days_old=0.0)

    code, payload, _ = _run(deployment, ["run-once", "--execute"], monkeypatch)

    assert code == cli.EXIT_RETENTION_ERROR
    assert payload["error_category"] == "unsafe_path"
    assert payload["deleted_count"] == 0
    assert media.exists()
    assert deployment.lifecycle() == {}


# --- configuration-shape errors are refusals (correction round 1) -----------
#
# The loader can legitimately raise TypeError and AttributeError for
# syntactically valid TOML whose values or sections have the wrong shape. Those
# are operator mistakes, and reporting them as unexpected internal failures told
# the operator to look at MGO instead of at their file.

_VALID_PREFIX = """
[application]
name = "t"
environment = "test"
host = "127.0.0.1"
port = 8080

[storage]
data_directory = "d"
log_directory = "l"
database_path = "d/mgo.db"

[camera]
enabled = false
backend = "simulator"
detection_interval_seconds = 60
capture_directory = "d/captures"
"""

_VALID_HEALTH = """
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

_MALFORMED_CONFIGS = {
    # tomllib raises TOMLDecodeError, a ValueError subclass.
    "invalid TOML": "this is not = = valid toml [[[",
    # KeyError: a whole required section is absent.
    "missing section": "[application]\nname = \"t\"\n",
    # KeyError: a required key inside a present section is absent.
    "missing key": _VALID_PREFIX + "\n[health]\nenabled = true\n",
    # TypeError: float() is handed a list.
    "wrong value type": _VALID_PREFIX
    + _VALID_HEALTH.replace(
        "temperature_warning_celsius = 70.0",
        "temperature_warning_celsius = [1, 2]",
    ),
    # AttributeError: a section is a scalar, so .get() does not exist on it.
    "wrong section shape": 'health = "not-a-table"\n' + _VALID_PREFIX,
    # OverflowError: int() is handed a floating-point infinity. TOML has a
    # literal inf, so this file is *syntactically valid* -- and OverflowError is
    # not a ValueError subclass, so it escaped the boundary entirely.
    "overflowing value": _VALID_PREFIX
    + _VALID_HEALTH.replace(
        "collection_interval_seconds = 60",
        "collection_interval_seconds = inf",
    ),
    # ValueError: a validator rejects an otherwise well-typed value.
    "rejected value": _VALID_PREFIX
    + _VALID_HEALTH.replace(
        "disk_warning_percent = 80.0", "disk_warning_percent = 95.0"
    ),
}


@pytest.mark.parametrize("label", sorted(_MALFORMED_CONFIGS))
@pytest.mark.parametrize("command", [["plan"], ["run-once", "--execute"]])
def test_a_malformed_configuration_is_an_operator_refusal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    label: str,
    command: list[str],
) -> None:
    """Every configuration-shape mistake exits 2, never 5.

    Exit 5 is reserved for genuine defects. Spending it on an ordinary mistake
    in the operator's own file points them at the wrong thing entirely.
    """
    deployment = _Deployment(tmp_path, enabled=True)
    deployment.rewrite_config(_MALFORMED_CONFIGS[label])

    code, _, stderr = _run(deployment, command, monkeypatch)

    assert code == cli.EXIT_REFUSED
    assert stderr.strip() == cli.REFUSAL_CONFIGURATION_INVALID


@pytest.mark.parametrize("label", sorted(_MALFORMED_CONFIGS))
def test_a_malformed_configuration_refusal_leaks_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, label: str
) -> None:
    """The refusal is one fixed sentence: no exception, path or TOML value."""
    deployment = _Deployment(tmp_path, enabled=True)
    deployment.rewrite_config(_MALFORMED_CONFIGS[label])

    _, _, stderr = _run(deployment, ["plan"], monkeypatch)

    for leak in (
        "TypeError",
        "AttributeError",
        "KeyError",
        "ValueError",
        "OverflowError",
        "TOMLDecodeError",
        "Traceback",
        "not-a-table",
        "temperature_warning",
        str(deployment.config_path),
        str(deployment.database_path),
        str(deployment.captures),
    ):
        assert leak not in stderr


@pytest.mark.parametrize("label", sorted(_MALFORMED_CONFIGS))
def test_a_malformed_configuration_mutates_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, label: str
) -> None:
    """A destructive invocation against a broken config touches nothing."""

    def _explode(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a malformed configuration reached the filesystem")

    monkeypatch.setattr(service_module, "_unlink", _explode)
    deployment = _Deployment(tmp_path, enabled=True)
    media = deployment.add("cap-old")
    deployment.add("cap-new", days_old=0.0)
    before = deployment.capture_ids()
    deployment.rewrite_config(_MALFORMED_CONFIGS[label])

    code, _, _ = _run(deployment, ["run-once", "--execute"], monkeypatch)

    assert code == cli.EXIT_REFUSED
    assert media.exists()
    assert deployment.lifecycle() == {}
    assert list_observations(deployment.database_path) == []
    assert deployment.capture_ids() == before


def test_a_genuine_internal_defect_is_still_unexpected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control: widening the refusal boundary did not swallow real bugs.

    An injected ``RuntimeError`` is not a configuration problem and must not be
    reported as one -- otherwise every defect inside the command would be
    blamed on the operator's file.
    """
    deployment = _Deployment(tmp_path, enabled=True)
    deployment.add("cap-old")

    def _explode(self: Any) -> Any:
        raise RuntimeError("a genuine internal defect")

    monkeypatch.setattr(service_module.RetentionService, "run_once", _explode)

    code, _, stderr = _run(deployment, ["run-once", "--execute"], monkeypatch)

    assert code == cli.EXIT_UNEXPECTED
    assert stderr.strip() == cli.UNEXPECTED_FAILURE


# --- the destructive gate needs a stable identity (correction round 1) ------
#
# The application's own rules resolve a relative MGO_CONFIG_PATH against the
# current working directory. That is fine for configuration generally and far
# too weak for a gate whose entire purpose is that the operator has identified
# one deployment: "config/mgo.toml" names a different file after a cd, and would
# then point a deletion at a different database and capture directory.


def test_a_relative_configuration_path_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A relative value does not identify a deployment, so it is refused."""
    deployment = _Deployment(tmp_path, enabled=True)
    deployment.add("cap-old")
    monkeypatch.setenv(CONFIG_PATH_ENV, "mgo.toml")

    out, err = io.StringIO(), io.StringIO()
    code = cli.main(["run-once", "--execute"], stdout=out, stderr=err)

    assert code == cli.EXIT_REFUSED
    assert err.getvalue().strip() == cli.REFUSAL_RELATIVE_CONFIG


def test_a_relative_path_is_refused_even_when_it_would_resolve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The headline case: it resolves, it is valid, and it is still refused.

    Standing in the deployment directory, ``mgo.toml`` names a perfectly good
    retention-enabled configuration and the run would succeed. It is refused
    anyway, because a value whose meaning depends on where the operator happens
    to be standing is not the stable identity this gate asks for.
    """

    def _explode(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a relative configuration path reached the unlink")

    monkeypatch.setattr(service_module, "_unlink", _explode)
    deployment = _Deployment(tmp_path, enabled=True)
    media = deployment.add("cap-old")
    deployment.add("cap-new", days_old=0.0)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(CONFIG_PATH_ENV, "mgo.toml")

    out, err = io.StringIO(), io.StringIO()
    code = cli.main(["run-once", "--execute"], stdout=out, stderr=err)

    assert code == cli.EXIT_REFUSED
    assert err.getvalue().strip() == cli.REFUSAL_RELATIVE_CONFIG
    assert media.exists()
    assert deployment.lifecycle() == {}
    assert list_observations(deployment.database_path) == []


@pytest.mark.parametrize(
    "value", ["mgo.toml", "./mgo.toml", "config/mgo.toml", "../mgo.toml"]
)
def test_every_relative_spelling_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """No relative spelling slips through."""
    deployment = _Deployment(tmp_path, enabled=True)
    deployment.add("cap-old")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(CONFIG_PATH_ENV, value)

    out, err = io.StringIO(), io.StringIO()

    assert (
        cli.main(["run-once", "--execute"], stdout=out, stderr=err)
        == cli.EXIT_REFUSED
    )
    assert deployment.lifecycle() == {}


def test_the_relative_refusal_does_not_echo_the_supplied_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The supplied value is operator text and is not repeated back."""
    _Deployment(tmp_path, enabled=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(CONFIG_PATH_ENV, "secret/location/mgo.toml")

    out, err = io.StringIO(), io.StringIO()
    cli.main(["run-once", "--execute"], stdout=out, stderr=err)

    assert "secret/location" not in err.getvalue()
    assert str(tmp_path) not in err.getvalue()


def test_the_relative_gate_refuses_before_reading_any_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing is opened: no schema read, no repository read, no unlink."""

    def _explode(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the relative gate ran after opening the database")

    monkeypatch.setattr(cli, "read_schema_version", _explode)
    monkeypatch.setattr(
        cli.RetentionRepository, "read_lifecycle_records", _explode
    )
    monkeypatch.setattr(
        cli.RetentionRepository, "list_lifecycle_records", _explode
    )
    monkeypatch.setattr(service_module, "_unlink", _explode)
    deployment = _Deployment(tmp_path, enabled=True)
    deployment.add("cap-old")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(CONFIG_PATH_ENV, "mgo.toml")

    out, err = io.StringIO(), io.StringIO()

    assert (
        cli.main(["run-once", "--execute"], stdout=out, stderr=err)
        == cli.EXIT_REFUSED
    )


def test_an_absolute_configuration_path_still_executes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control: a stable absolute identity runs exactly as before."""
    deployment = _Deployment(tmp_path, enabled=True)
    old = deployment.add("cap-old")
    deployment.add("cap-new", days_old=0.0)
    assert deployment.config_path.is_absolute()

    code, payload, _ = _run(deployment, ["run-once", "--execute"], monkeypatch)

    assert code == cli.EXIT_SUCCESS
    assert payload["deleted_count"] == 1
    assert not old.exists()


def test_plan_still_accepts_a_relative_configuration_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stricter rule is destructive-only and did not spread to ``plan``.

    ``plan`` is read-only, so the ordinary application resolution rules remain
    appropriate for it. Tightening it too would have been scope creep dressed as
    caution.
    """
    deployment = _Deployment(tmp_path, enabled=True)
    deployment.add("cap-old")
    deployment.add("cap-new", days_old=0.0)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(CONFIG_PATH_ENV, "mgo.toml")

    out, err = io.StringIO(), io.StringIO()
    code = cli.main(["plan"], stdout=out, stderr=err)

    assert code == cli.EXIT_SUCCESS
    assert json.loads(out.getvalue())["candidate_count"] == 1


def test_the_general_configuration_resolver_is_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``resolve_config_path`` keeps its documented relative-path behaviour.

    The destructive gate is a CLI rule layered on top; it must not have altered
    the application-wide contract that every other component depends on.
    """
    from mgo.core.config import resolve_config_path

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(CONFIG_PATH_ENV, "relative/mgo.toml")

    resolved = resolve_config_path()

    assert resolved.is_absolute()
    assert resolved == tmp_path / "relative" / "mgo.toml"


# --- the execute flag is exact (correction round 2) -------------------------
#
# argparse accepts unambiguous prefixes of long options by default, so the one
# deliberate destructive authorisation spelling was really a family of them:
# --exe, --exec, --execut and even --e all satisfied Gate A and reached
# deletion. A flag that a typo can produce is not an authorisation.


_ABBREVIATIONS = ["--exe", "--exec", "--execut", "--e", "--ex"]


def _forbid_everything(monkeypatch: pytest.MonkeyPatch, why: str) -> None:
    """Make configuration, schema, catalogue and unlink all fatal to touch."""

    def _explode(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError(why)

    monkeypatch.setattr(cli, "load_config", _explode)
    monkeypatch.setattr(cli, "read_schema_version", _explode)
    monkeypatch.setattr(
        cli.RetentionRepository, "read_lifecycle_records", _explode
    )
    monkeypatch.setattr(
        cli.RetentionRepository, "list_lifecycle_records", _explode
    )
    monkeypatch.setattr(service_module, "_unlink", _explode)


def test_the_exact_execute_flag_is_accepted_by_the_parser() -> None:
    """The control, read off real parse behaviour rather than a declaration."""
    arguments = cli.build_parser().parse_args(["run-once", "--execute"])

    assert arguments.command == "run-once"
    assert arguments.execute is True


@pytest.mark.parametrize("spelling", _ABBREVIATIONS)
def test_an_abbreviated_execute_flag_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spelling: str
) -> None:
    """No prefix of ``--execute`` authorises anything.

    Exercised through a real invocation, not by inspecting
    ``parser.option_strings``: the defect was never in what the parser declared,
    it was in what the parser *accepted*.
    """
    deployment = _Deployment(tmp_path, enabled=True)
    media = deployment.add("cap-old")
    deployment.add("cap-new", days_old=0.0)

    code, payload, stderr = _run(
        deployment, ["run-once", spelling], monkeypatch
    )

    assert code == cli.EXIT_REFUSED
    assert payload == {}
    assert stderr.strip() == cli.REFUSAL_INVALID_ARGUMENTS
    assert media.exists()
    assert deployment.lifecycle() == {}
    assert list_observations(deployment.database_path) == []


@pytest.mark.parametrize("spelling", _ABBREVIATIONS)
def test_an_abbreviated_flag_is_refused_before_anything_is_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spelling: str
) -> None:
    """Gate A holds at the parser, ahead of every other moving part."""
    _forbid_everything(monkeypatch, f"{spelling} reached past the parser")
    deployment = _Deployment(tmp_path, enabled=True)
    deployment.add("cap-old")

    code, _, _ = _run(deployment, ["run-once", spelling], monkeypatch)

    assert code == cli.EXIT_REFUSED


def test_an_enabled_policy_still_deletes_nothing_for_an_abbreviation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Everything else is satisfied, so only the spelling can be doing this.

    A refusal proves nothing if the run would have been a no-op anyway. This
    deployment has retention enabled, an absolute configuration path and an
    eligible capture -- the exact conditions under which the control below
    deletes -- and the abbreviation alone stops it.
    """
    deployment = _Deployment(tmp_path, enabled=True)
    media = deployment.add("cap-old")
    deployment.add("cap-new", days_old=0.0)

    code, _, _ = _run(deployment, ["run-once", "--exec"], monkeypatch)

    assert code == cli.EXIT_REFUSED
    assert media.exists()
    assert media.read_bytes() == PAYLOAD
    assert deployment.lifecycle() == {}
    assert deployment.capture_ids() == {"cap-old", "cap-new"}


def test_the_exact_flag_still_performs_one_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The paired control: the same deployment, the full spelling, one delete."""
    deployment = _Deployment(tmp_path, enabled=True)
    media = deployment.add("cap-old")
    deployment.add("cap-new", days_old=0.0)

    code, payload, _ = _run(deployment, ["run-once", "--execute"], monkeypatch)

    assert code == cli.EXIT_SUCCESS
    assert payload["deleted_count"] == 1
    assert not media.exists()


def test_abbreviation_is_disabled_on_every_parser_in_the_tree() -> None:
    """Including the subparsers, which argparse would otherwise default back on.

    ``allow_abbrev=False`` on the parent alone is not enough: ``add_subparsers``
    constructs each subparser with argparse's own defaults, so a subparser could
    happily accept ``--exe`` under a parent that would not.
    """
    parser = cli.build_parser()

    assert parser.allow_abbrev is False

    subparsers = [
        action
        for action in parser._subparsers._group_actions
        if hasattr(action, "choices")
    ]
    children = [
        child
        for action in subparsers
        for child in action.choices.values()  # type: ignore[union-attr]
    ]

    assert children
    for child in children:
        assert child.allow_abbrev is False


# --- an invalid invocation is refused without echoing it (round 2) ----------
#
# ArgumentParser.error() writes a usage dump plus the offending argument text
# straight to sys.stderr and only then raises SystemExit, so catching SystemExit
# afterwards was too late -- "--config /etc/garden-observatory/mgo.toml" had
# already been published, and to a stream the caller never chose.


_HOSTILE_ARGUMENTS = {
    "posix absolute": "/var/lib/garden-observatory/db/mgo.db",
    "posix configuration": "/etc/garden-observatory/mgo.toml",
    "traversal": "../../secret",
    "windows absolute": "C:\\sensitive\\secret.db",
}


def _invalid_invocations(secret: str) -> list[list[str]]:
    """Every shape of invalid invocation that can carry operator text."""
    return [
        ["run-once", "--execute", "--config", secret],
        ["run-once", "--execute", "--database", secret],
        ["run-once", "--execute", "--capture-root", secret],
        ["plan", secret],
        [secret],
    ]


@pytest.mark.parametrize("label", sorted(_HOSTILE_ARGUMENTS))
def test_an_invalid_invocation_never_echoes_the_operators_argument(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, label: str
) -> None:
    """One fixed sentence, no usage dump, and nothing that was typed."""
    secret = _HOSTILE_ARGUMENTS[label]
    deployment = _Deployment(tmp_path, enabled=True)

    for argv in _invalid_invocations(secret):
        out, err = io.StringIO(), io.StringIO()
        code = cli.main(argv, stdout=out, stderr=err)
        stderr = err.getvalue()

        assert code == cli.EXIT_REFUSED, argv
        assert out.getvalue() == "", argv
        assert stderr.strip() == cli.REFUSAL_INVALID_ARGUMENTS, argv
        assert secret not in stderr, argv
        assert secret not in out.getvalue(), argv
        for leak in (
            "usage:",
            "unrecognized arguments",
            "invalid choice",
            "Traceback",
            "--config",
            "--database",
            "--capture-root",
            str(deployment.database_path),
        ):
            assert leak not in stderr, (argv, leak)


@pytest.mark.parametrize("label", sorted(_HOSTILE_ARGUMENTS))
def test_a_parser_refusal_reaches_the_injected_stream_and_no_other(
    monkeypatch: pytest.MonkeyPatch, label: str
) -> None:
    """The refusal goes where the caller asked, not to the process's stderr.

    This is the second half of the defect and the easier half to miss. Argparse
    writes to ``sys.stderr`` directly, so a test that captured only the injected
    stream would have seen an empty, innocent-looking refusal while the real
    stderr carried the operator's path. Both streams are asserted.
    """
    secret = _HOSTILE_ARGUMENTS[label]
    process_stderr = io.StringIO()
    process_stdout = io.StringIO()
    monkeypatch.setattr(sys, "stderr", process_stderr)
    monkeypatch.setattr(sys, "stdout", process_stdout)

    out, err = io.StringIO(), io.StringIO()
    code = cli.main(
        ["run-once", "--execute", "--config", secret], stdout=out, stderr=err
    )

    assert code == cli.EXIT_REFUSED
    assert err.getvalue().strip() == cli.REFUSAL_INVALID_ARGUMENTS
    assert process_stderr.getvalue() == ""
    assert process_stdout.getvalue() == ""
    assert secret not in process_stderr.getvalue()


def test_an_invalid_invocation_deletes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refusal is at the parser, so nothing downstream is even reached."""
    _forbid_everything(monkeypatch, "an invalid invocation reached the engine")
    deployment = _Deployment(tmp_path, enabled=True)
    media = deployment.add("cap-old")

    code, _, _ = _run(
        deployment,
        ["run-once", "--execute", "--config", "/etc/garden-observatory/mgo.toml"],
        monkeypatch,
    )

    assert code == cli.EXIT_REFUSED
    assert media.exists()
    assert deployment.lifecycle() == {}
    assert list_observations(deployment.database_path) == []


def test_help_still_succeeds_and_prints_the_static_help(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The bounded parser narrows ``error()`` only; ``--help`` is untouched.

    Help exits through ``parser.exit()`` rather than ``error()``, and its text
    is static -- it contains no operator input, so there is nothing to bound.
    """
    out, err = io.StringIO(), io.StringIO()

    code = cli.main(["--help"], stdout=out, stderr=err)
    printed = capsys.readouterr().out

    assert code == cli.EXIT_SUCCESS
    assert err.getvalue() == ""
    assert cli.COMMAND_NAME in printed
    assert "plan" in printed
    assert "run-once" in printed


# --- valid TOML can still fail conversion (correction round 2) --------------
#
# TOML has a literal inf, so `collection_interval_seconds = inf` parses fine and
# then reaches int(float("inf")), which raises OverflowError -- not a ValueError
# subclass, and so not caught by the round-1 boundary. An ordinary mistake in
# the operator's own file was being reported as an MGO defect.


_OVERFLOWING_CONFIG_FIELD = "collection_interval_seconds"


def test_an_infinite_integer_field_is_an_overflow_not_a_value_error() -> None:
    """Why ``OverflowError`` belongs at the boundary, stated as a fact.

    If this ever becomes a ``ValueError`` the extra entry is redundant rather
    than wrong -- but the reasoning behind it should fail loudly, not quietly
    stop being true.
    """
    from mgo.core.config import parse_config_text

    text = _MALFORMED_CONFIGS["overflowing value"]
    assert f"{_OVERFLOWING_CONFIG_FIELD} = inf" in text

    with pytest.raises(OverflowError) as caught:
        parse_config_text(text)

    assert not isinstance(caught.value, ValueError)


@pytest.mark.parametrize("command", [["plan"], ["run-once", "--execute"]])
def test_an_overflowing_configuration_value_is_an_operator_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: list[str]
) -> None:
    """Exit 2 with the fixed sentence, and nothing of the file in it."""
    deployment = _Deployment(tmp_path, enabled=True)
    deployment.rewrite_config(_MALFORMED_CONFIGS["overflowing value"])

    code, _, stderr = _run(deployment, command, monkeypatch)

    assert code == cli.EXIT_REFUSED
    assert stderr.strip() == cli.REFUSAL_CONFIGURATION_INVALID
    for leak in (
        "OverflowError",
        "inf",
        "Traceback",
        _OVERFLOWING_CONFIG_FIELD,
        str(deployment.config_path),
        str(deployment.database_path),
        str(deployment.captures),
    ):
        assert leak not in stderr


def test_an_overflowing_configuration_value_mutates_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A destructive invocation against it touches no media and no row."""

    def _explode(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("an overflowing configuration reached the filesystem")

    monkeypatch.setattr(service_module, "_unlink", _explode)
    deployment = _Deployment(tmp_path, enabled=True)
    media = deployment.add("cap-old")
    deployment.add("cap-new", days_old=0.0)
    before = deployment.capture_ids()
    deployment.rewrite_config(_MALFORMED_CONFIGS["overflowing value"])

    code, _, _ = _run(deployment, ["run-once", "--execute"], monkeypatch)

    assert code == cli.EXIT_REFUSED
    assert media.exists()
    assert deployment.lifecycle() == {}
    assert list_observations(deployment.database_path) == []
    assert deployment.capture_ids() == before


# --- the destructive gate wants a literal absolute path (round 2) ----------
#
# "~/mgo.toml" is not an absolute path. It is an instruction to look in the
# executing account's home directory, so the same string names a different
# configuration under a different account -- the same context-dependence the
# gate exists to remove, merely a different context from the working directory.
# Expanding it before the absolute test let it through.


_TILDE_PATHS = ["~/mgo.toml", "~someone/mgo.toml", "~/deploy/mgo.toml"]


@pytest.mark.parametrize("value", _TILDE_PATHS)
def test_a_tilde_configuration_path_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """A home-relative value does not identify one deployment either."""
    deployment = _Deployment(tmp_path, enabled=True)
    media = deployment.add("cap-old")
    monkeypatch.setenv(CONFIG_PATH_ENV, value)

    out, err = io.StringIO(), io.StringIO()
    code = cli.main(["run-once", "--execute"], stdout=out, stderr=err)

    assert code == cli.EXIT_REFUSED
    assert err.getvalue().strip() == cli.REFUSAL_RELATIVE_CONFIG
    assert media.exists()
    assert deployment.lifecycle() == {}
    assert list_observations(deployment.database_path) == []


@pytest.mark.parametrize("value", _TILDE_PATHS)
def test_a_tilde_path_would_otherwise_have_expanded_to_an_absolute_one(
    value: str,
) -> None:
    """The defect restated as a fact, so the refusal above cannot be vacuous.

    If ``expanduser()`` stopped producing an absolute path on this platform the
    old gate would have refused anyway, and the regression tests would be
    proving nothing.
    """
    assert not Path(value).is_absolute()
    assert Path(value).expanduser().is_absolute()


@pytest.mark.parametrize("value", _TILDE_PATHS)
def test_the_tilde_refusal_happens_before_anything_is_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """No configuration read, no schema read, no catalogue read, no unlink."""
    _forbid_everything(monkeypatch, f"{value} reached past the gate")
    deployment = _Deployment(tmp_path, enabled=True)
    deployment.add("cap-old")
    monkeypatch.setenv(CONFIG_PATH_ENV, value)

    out, err = io.StringIO(), io.StringIO()

    assert (
        cli.main(["run-once", "--execute"], stdout=out, stderr=err)
        == cli.EXIT_REFUSED
    )


@pytest.mark.parametrize("value", _TILDE_PATHS)
def test_the_tilde_refusal_does_not_echo_the_supplied_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """Operator-supplied text, including a home directory, is not repeated."""
    _Deployment(tmp_path, enabled=True)
    monkeypatch.setenv(CONFIG_PATH_ENV, value)

    out, err = io.StringIO(), io.StringIO()
    cli.main(["run-once", "--execute"], stdout=out, stderr=err)

    assert value not in err.getvalue()
    assert str(Path(value).expanduser()) not in err.getvalue()
    assert str(Path.home()) not in err.getvalue()


def test_a_literal_absolute_path_still_executes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control: a real absolute path is what the gate wants, and it runs.

    ``tmp_path`` is absolute on whichever platform this runs on, which is the
    point -- a genuinely already-expanded path passes, so the correction refuses
    deferred interpretation rather than home directories.
    """
    deployment = _Deployment(tmp_path, enabled=True)
    old = deployment.add("cap-old")
    deployment.add("cap-new", days_old=0.0)
    assert deployment.config_path.is_absolute()
    assert "~" not in str(deployment.config_path)

    code, payload, _ = _run(deployment, ["run-once", "--execute"], monkeypatch)

    assert code == cli.EXIT_SUCCESS
    assert payload["deleted_count"] == 1
    assert not old.exists()


def test_the_general_resolver_still_expands_a_tilde(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``resolve_config_path`` is untouched: the stricter rule is CLI-only.

    The application at large may keep expanding ``~``; it is only the
    destructive gate that needs an identity it cannot reinterpret.
    """
    from mgo.core.config import resolve_config_path

    monkeypatch.setenv(CONFIG_PATH_ENV, "~/mgo.toml")

    resolved = resolve_config_path()

    assert resolved.is_absolute()
    assert "~" not in str(resolved)
    assert resolved == (Path.home() / "mgo.toml").resolve()


def test_plan_still_accepts_a_tilde_configuration_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``plan`` is read-only and keeps the ordinary resolution rules.

    Proven by pointing ``~`` at a real deployment: the home directory is moved
    to ``tmp_path`` for the duration, so the expansion resolves to a temporary
    configuration this test created and to nothing of the user's own.
    """
    home = tmp_path / "home"
    home.mkdir()
    deployment = _Deployment(home, enabled=True)
    deployment.add("cap-old")
    deployment.add("cap-new", days_old=0.0)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv(CONFIG_PATH_ENV, "~/mgo.toml")
    assert Path("~/mgo.toml").expanduser() == deployment.config_path

    out, err = io.StringIO(), io.StringIO()
    code = cli.main(["plan"], stdout=out, stderr=err)

    assert code == cli.EXIT_SUCCESS
    assert json.loads(out.getvalue())["candidate_count"] == 1


# --- the two review findings, closed (final micro-correction) ---------------


@pytest.mark.parametrize("hostile", HOSTILE_FILENAMES)
def test_the_decoded_walk_catches_what_a_rendered_search_misses(
    hostile: str,
) -> None:
    """The privacy check is effective, proven against a deliberately leaky payload.

    A check that never fires proves nothing, and this one very nearly did not.
    The superseded assertion pair was::

        assert hostile not in rendered
        assert hostile not in json.dumps(json.loads(rendered))

    The second is byte-identical to the first -- decoding and re-encoding
    reproduces the same escaping -- so both miss a Windows or UNC path
    completely. Here a payload that *does* leak is built and fed to both, so the
    gap is a fact in the suite rather than a claim in a docstring.
    """
    leaky = {
        "candidate_count": 1,
        "candidates": [
            {
                "capture_id": "cap-hostile",
                "captured_at": "2024-01-01T00:00:00+00:00",
                "filesize_bytes": 19,
                "policy_reason": "age",
                "filename": hostile,
            }
        ],
        "nested": {"deeper": [{"anywhere": hostile}]},
    }
    rendered = json.dumps(leaky)

    # The walk finds it, wherever it is and however it renders.
    with pytest.raises(AssertionError):
        _assert_absent_from_decoded(leaky, hostile)

    # And it finds it under a *key* as well as under a value.
    with pytest.raises(AssertionError):
        _assert_absent_from_decoded({hostile: "value"}, hostile)

    # A rendered-text search is only reliable when nothing needed escaping;
    # for the backslash forms it silently reports the payload as clean.
    escapes = "\\" in hostile
    assert (hostile not in rendered) is escapes
    assert (hostile not in json.dumps(json.loads(rendered))) is escapes
    # The old round-trip really was the same string, not a stronger check.
    assert json.dumps(json.loads(rendered)) == rendered


def test_an_unexpected_path_bearing_field_is_omitted_by_the_allow_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A field this build has never heard of is dropped, not published.

    This is what an allow-list buys over the filename deny-list it replaced.
    ``as_dict()`` is widened here to carry both ``filename`` and a
    ``source_path`` that no current candidate has -- the shape a future domain
    change would produce -- and the operator output still contains exactly the
    four safe fields.

    The deny-list is checked against the same candidate at the end: it would
    have withheld ``filename`` and published ``source_path``, which is the whole
    argument for the change.
    """
    hostile_filename = "C:\\sensitive\\secret.jpg"
    hostile_path = "\\\\server\\share\\private\\secret.jpg"
    original = RetentionPlan.as_dict
    widened: list[dict[str, Any]] = []

    def _as_dict_with_a_future_field(self: RetentionPlan) -> dict[str, Any]:
        payload = original(self)
        for entry in payload["candidates"]:
            entry["filename"] = hostile_filename
            entry["source_path"] = hostile_path
            widened.append(dict(entry))
        return payload

    monkeypatch.setattr(RetentionPlan, "as_dict", _as_dict_with_a_future_field)
    deployment = _Deployment(tmp_path, enabled=True)
    deployment.add("cap-old")
    deployment.add("cap-new", days_old=0.0)

    code, payload, stderr = _run(deployment, ["plan"], monkeypatch)

    assert code == cli.EXIT_SUCCESS
    assert widened, "the widened projection was never exercised"

    candidate = payload["candidates"][0]
    assert set(candidate) == {
        "capture_id",
        "captured_at",
        "filesize_bytes",
        "policy_reason",
    }
    for hostile in (hostile_filename, hostile_path):
        _assert_absent_from_decoded(payload, hostile)
        assert hostile not in stderr

    # The candidate is still selected, and its safe values are still right:
    # omission, not exclusion, and not corruption either.
    assert payload["candidate_count"] == 1
    assert candidate["capture_id"] == "cap-old"
    assert candidate["policy_reason"] == "age"
    assert candidate["filesize_bytes"] == len(PAYLOAD)
    assert candidate["captured_at"]

    # Why the allow-list is stronger: the deny-list would have let the new
    # field straight through.
    deny_listed = {
        key: value for key, value in widened[0].items() if key != "filename"
    }
    assert "filename" not in deny_listed
    assert deny_listed["source_path"] == hostile_path


def test_the_operator_field_list_is_exactly_the_four_safe_fields() -> None:
    """The allow-list itself is pinned, so widening it is a visible decision."""
    assert cli._OPERATOR_CANDIDATE_FIELDS == (
        "capture_id",
        "captured_at",
        "filesize_bytes",
        "policy_reason",
    )
    assert "filename" not in cli._OPERATOR_CANDIDATE_FIELDS
    assert "absolute_path" not in cli._OPERATOR_CANDIDATE_FIELDS


def test_a_missing_safe_field_is_an_internal_defect_not_a_silent_omission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dropping a *required* field must fail loudly, not shrink the candidate.

    The allow-list indexes rather than ``.get``s, so a candidate that has lost a
    safe field is a bug in this build and exits 5. Emitting a short candidate
    would hide it behind output that still looked plausible.
    """
    original = RetentionPlan.as_dict

    def _as_dict_missing_a_field(self: RetentionPlan) -> dict[str, Any]:
        payload = original(self)
        for entry in payload["candidates"]:
            entry.pop("policy_reason")
        return payload

    monkeypatch.setattr(RetentionPlan, "as_dict", _as_dict_missing_a_field)
    deployment = _Deployment(tmp_path, enabled=True)
    deployment.add("cap-old")
    deployment.add("cap-new", days_old=0.0)

    code, payload, stderr = _run(deployment, ["plan"], monkeypatch)

    assert code == cli.EXIT_UNEXPECTED
    assert payload == {}
    assert stderr.strip() == cli.UNEXPECTED_FAILURE
    assert "KeyError" not in stderr
    assert "Traceback" not in stderr
