"""Tests for the diagnostic support bundle.

A support bundle is a file that leaves the Raspberry Pi and is handed to another
person, so the tests that matter most are the ones asserting what is *not* in
it. Those tests inspect the generated archive's real members and their real
bytes rather than trusting the collection code's intent.

The second theme is bounded degradation. On this machine there is no
``systemctl``, no ``journalctl``, no running API and no production path, which
is exactly the environment the bundle has to survive: every one of those is an
expected absence that must produce a truthful record, not an exception.
"""

from __future__ import annotations

import io
import json
import os
import stat
import tarfile
import urllib.error
import urllib.request
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from mgo.core.config import (
    SYSTEM_CAPTURE_DIRECTORY,
    SYSTEM_DATABASE_PATH,
    SYSTEM_LOG_DIRECTORY,
    SYSTEM_STATE_DIRECTORY,
    MGOConfig,
    load_config,
)
from mgo.operations import support_bundle
from mgo.operations.errors import ErrorCode, OperationError
from mgo.operations.events import REDACTED, EventEmitter
from mgo.operations.support_bundle import (
    ALLOWED_LOOPBACK_HOSTS,
    BUNDLE_FILE_MODE,
    BUNDLE_NAME_PREFIX,
    BUNDLE_SUFFIX,
    COLLECTED_ENDPOINTS,
    MAX_ARCHIVE_BYTES,
    MAX_COMMAND_ERROR_DETAIL,
    MAX_ENDPOINT_RESPONSE_BYTES,
    MAX_JOURNAL_LINES,
    MEMBER_MODE,
    NON_CANONICAL_PATH,
    SERVICE_PROPERTIES,
    BundleOutcome,
    CommandResult,
    _directory_totals,
    collect_configuration_summary,
    collect_journal,
    collect_journal_disk_usage,
    collect_service_status,
    create_support_bundle,
)

POSIX_ONLY = pytest.mark.skipif(os.name != "posix", reason="POSIX mode bits")

CONFIG_TEMPLATE = """\
[application]
name = "Matt's Garden Observatory"
environment = "production"
host = "0.0.0.0"
port = 8080

[storage]
data_directory = "{root}/data"
log_directory = "{root}/logs"
database_path = "{root}/data/mgo.db"

[camera]
enabled = false
backend = "null"
detection_interval_seconds = 60
capture_directory = "{root}/data/captures"

[preview]
enabled = false
width = 1280
height = 720
fps = 15
startup_timeout_seconds = 5.0
shutdown_timeout_seconds = 5.0

[motion]
enabled = false
analysis_interval_seconds = 1.0
analysis_width = 160
analysis_height = 90
pixel_difference_threshold = 20
changed_pixel_ratio_threshold = 0.08
cooldown_seconds = 5.0

[notifications]
enabled = false
provider = "log"

[database]
health_check_interval_seconds = 60
busy_timeout_seconds = 5.0

[health]
enabled = true
collection_interval_seconds = 60
temperature_warning_celsius = 70.0
temperature_critical_celsius = 80.0
disk_warning_percent = 80.0
disk_critical_percent = 90.0
memory_warning_percent = 85.0
memory_critical_percent = 95.0
{extra}
"""


# --- fixtures and doubles ---------------------------------------------------


def _write_config(tmp_path: Path, extra: str = "") -> Path:
    """Write a valid configuration whose paths live inside ``tmp_path``."""
    path = tmp_path / "mgo.toml"
    path.write_text(
        CONFIG_TEMPLATE.format(root=tmp_path.as_posix(), extra=extra),
        encoding="utf-8",
    )
    return path


@pytest.fixture
def config(tmp_path: Path) -> MGOConfig:
    """A loaded configuration pointing entirely inside the temporary tree."""
    return load_config(_write_config(tmp_path))


#: Minimal, contract-shaped payloads for each collected endpoint.
HEALTHY_RESPONSES: dict[str, dict[str, Any]] = {
    "/": {"name": "MGO", "version": "0.1.0", "status": "operational"},
    "/version": {
        "application": "MGO",
        "version": "0.1.0",
        "commit": None,
        "python_version": "3.13.0",
        "architecture": "aarch64",
    },
    "/health": {"status": "healthy", "hostname": "mgo-core"},
    "/database/status": {"status": "healthy", "schema_version": 2},
    "/camera/status": {"enabled": False, "status": "disabled"},
    "/camera/preview/status": {"enabled": False, "state": "stopped"},
    "/motion/status": {"enabled": False, "status": "disabled"},
    "/notifications/status": {"enabled": False, "providers": []},
}


def healthy_fetch(url: str) -> bytes:
    """A fetcher that answers every collected endpoint successfully."""
    for _, path in COLLECTED_ENDPOINTS:
        if url.endswith(path) and (path != "/" or url.endswith("8080/")):
            return json.dumps(HEALTHY_RESPONSES[path]).encode("utf-8")
    raise AssertionError(f"unexpected URL requested: {url}")


def dead_fetch(url: str) -> bytes:
    """A fetcher standing in for an API that is not running."""
    raise urllib.error.URLError("connection refused")


def healthy_run(command: list[str]) -> CommandResult:
    """A runner standing in for a working ``systemctl``/``journalctl``."""
    if command[0] == "systemctl":
        return CommandResult(
            available=True,
            returncode=0,
            stdout="\n".join(f"{name}=value-{name}" for name in SERVICE_PROPERTIES),
        )
    if command[:2] == ["journalctl", "--disk-usage"]:
        return CommandResult(
            available=True,
            returncode=0,
            stdout="Archived and active journals take 96.0M.\n",
        )
    return CommandResult(
        available=True, returncode=0, stdout="Jul 28 02:30:00 mgo-core mgo[1]: ok\n"
    )


def absent_run(command: list[str]) -> CommandResult:
    """A runner standing in for a host with no systemd tooling."""
    return CommandResult(available=False, error=f"{command[0]} is not installed.")


def _generate(
    config: MGOConfig,
    destination: Path,
    **kwargs: Any,
) -> Any:
    """Generate a bundle with working doubles unless overridden."""
    kwargs.setdefault("fetch", healthy_fetch)
    kwargs.setdefault("run", healthy_run)
    kwargs.setdefault("backup_directory", destination / "backups")
    return create_support_bundle(
        config=config, destination=destination, **kwargs
    )


def _members(bundle: Path) -> dict[str, bytes]:
    """Return every archive member's name and bytes."""
    with tarfile.open(bundle) as archive:
        result = {}
        for info in archive.getmembers():
            handle = archive.extractfile(info)
            result[info.name] = handle.read() if handle is not None else b""
        return result


def _archive_text(bundle: Path) -> str:
    """Return every member's content concatenated, for exclusion assertions."""
    return "\n".join(
        payload.decode("utf-8", "replace") for payload in _members(bundle).values()
    )


# --- outcomes ---------------------------------------------------------------


def test_a_complete_bundle_is_produced_when_every_source_answers(
    config: MGOConfig, tmp_path: Path
) -> None:
    """The happy path: outcome ``complete`` and exit code 0."""
    result = _generate(config, tmp_path / "out")

    assert result.outcome is BundleOutcome.COMPLETE
    assert result.exit_code == 0
    assert result.errors == ()
    assert result.bundle_path is not None
    assert result.bundle_path.is_file()


def test_a_partial_bundle_is_still_produced_when_the_api_is_down(
    config: MGOConfig, tmp_path: Path
) -> None:
    """The bundle describing a dead API is exactly the one worth sending."""
    result = _generate(config, tmp_path / "out", fetch=dead_fetch)

    assert result.outcome is BundleOutcome.PARTIAL
    assert result.exit_code == 1
    assert result.bundle_path is not None
    assert result.bundle_path.is_file()
    assert len(result.errors) == len(COLLECTED_ENDPOINTS)


def test_a_partial_bundle_still_contains_every_member(
    config: MGOConfig, tmp_path: Path
) -> None:
    """A failed source leaves a truthful error member, not a missing one."""
    result = _generate(config, tmp_path / "out", fetch=dead_fetch, run=absent_run)

    assert result.bundle_path is not None
    members = _members(result.bundle_path)
    for name, _ in COLLECTED_ENDPOINTS:
        assert name in members
        assert "error" in json.loads(members[name])


def test_no_bundle_is_created_when_the_destination_cannot_be_made(
    config: MGOConfig, tmp_path: Path
) -> None:
    """The only genuine failure mode: nowhere to put the file."""
    blocker = tmp_path / "blocked"
    blocker.write_text("a file, not a directory", encoding="utf-8")

    with pytest.raises(OperationError) as caught:
        _generate(config, blocker)

    assert caught.value.code is ErrorCode.DIAGNOSTIC_OUTPUT_UNWRITABLE


def test_a_failed_generation_leaves_no_temporary_file(
    config: MGOConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A partial archive must never be left where a bundle is expected."""
    destination = tmp_path / "out"

    def explode(*args: object, **kwargs: object) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(
        "mgo.operations.support_bundle._write_archive", explode
    )

    with pytest.raises(OperationError) as caught:
        _generate(config, destination)

    assert caught.value.code is ErrorCode.DIAGNOSTIC_ARCHIVE_FAILED
    assert list(destination.iterdir()) == []


def test_the_bundle_is_published_atomically(
    config: MGOConfig, tmp_path: Path
) -> None:
    """Only the final name ever appears; no ``.tmp`` survives."""
    destination = tmp_path / "out"
    result = _generate(config, destination)

    assert result.bundle_path is not None
    names = sorted(item.name for item in destination.iterdir())
    assert names == [result.bundle_path.name]
    assert names[0].startswith(BUNDLE_NAME_PREFIX)
    assert names[0].endswith(BUNDLE_SUFFIX)


def test_the_exit_code_contract_is_stable() -> None:
    """0 complete, 1 partial, 2 failed -- documented and depended upon."""
    from mgo.operations.support_bundle import BundleResult

    assert BundleResult(BundleOutcome.COMPLETE, None).exit_code == 0
    assert BundleResult(BundleOutcome.PARTIAL, None).exit_code == 1
    assert BundleResult(BundleOutcome.FAILED, None).exit_code == 2


# --- privacy exclusions -----------------------------------------------------


def test_the_bundle_contains_no_database_file(
    config: MGOConfig, tmp_path: Path
) -> None:
    """The database holds every observation ever recorded."""
    config.storage.database_path.parent.mkdir(parents=True, exist_ok=True)
    config.storage.database_path.write_bytes(b"SQLite format 3\x00SECRETDATA")

    result = _generate(config, tmp_path / "out")

    assert result.bundle_path is not None
    members = _members(result.bundle_path)
    assert "mgo.db" not in members
    assert not any(name.endswith(".db") for name in members)
    assert b"SECRETDATA" not in b"".join(members.values())


def test_the_bundle_contains_no_wal_or_shm_sidecar(
    config: MGOConfig, tmp_path: Path
) -> None:
    """The sidecars carry committed data that has not yet been checkpointed."""
    database = config.storage.database_path
    database.parent.mkdir(parents=True, exist_ok=True)
    database.write_bytes(b"db")
    database.with_name(database.name + "-wal").write_bytes(b"WALCONTENT")
    database.with_name(database.name + "-shm").write_bytes(b"SHMCONTENT")

    result = _generate(config, tmp_path / "out")

    assert result.bundle_path is not None
    payload = b"".join(_members(result.bundle_path).values())
    assert b"WALCONTENT" not in payload
    assert b"SHMCONTENT" not in payload
    assert not any(
        name.endswith(("-wal", "-shm")) for name in _members(result.bundle_path)
    )


def test_the_bundle_contains_no_media_or_media_filenames(
    config: MGOConfig, tmp_path: Path
) -> None:
    """Imagery of a private garden must never leave with a diagnostic file."""
    captures = config.camera.capture_directory
    captures.mkdir(parents=True, exist_ok=True)
    (captures / "capture-20260728-blackbird.jpg").write_bytes(b"JPEGBYTES")
    (captures / "capture-20260728-robin.jpg").write_bytes(b"JPEGBYTES")

    result = _generate(config, tmp_path / "out")

    assert result.bundle_path is not None
    text = _archive_text(result.bundle_path)
    assert "blackbird" not in text
    assert "robin" not in text
    assert ".jpg" not in text
    assert "JPEGBYTES" not in text

    # ...while the diagnostic aggregate is still present and correct.
    storage = json.loads(_members(result.bundle_path)["storage-summary.json"])
    assert storage["captures"]["entries"] == 2


def test_the_bundle_contains_no_raw_configuration(
    tmp_path: Path
) -> None:
    """A summary is included; the file itself never is."""
    config_path = _write_config(
        tmp_path, extra='\n[telegram]\nbot_token = "SUPER-SECRET-TOKEN"\n'
    )
    loaded = load_config(config_path)
    import tomllib

    with config_path.open("rb") as handle:
        raw = tomllib.load(handle)

    result = _generate(
        loaded, tmp_path / "out", raw_configuration=raw
    )

    assert result.bundle_path is not None
    members = _members(result.bundle_path)
    assert "mgo.toml" not in members
    text = _archive_text(result.bundle_path)
    assert "SUPER-SECRET-TOKEN" not in text
    assert "[application]" not in text


def test_a_secret_in_an_unrecognised_section_is_never_exposed(
    tmp_path: Path
) -> None:
    """The default for a future setting is withheld, not published."""
    config_path = _write_config(
        tmp_path, extra='\n[telegram]\nbot_token = "SUPER-SECRET-TOKEN"\n'
    )
    import tomllib

    with config_path.open("rb") as handle:
        raw = tomllib.load(handle)

    summary = collect_configuration_summary(load_config(config_path), raw)

    assert summary["unrecognised_settings"]["telegram"] == REDACTED
    assert "SUPER-SECRET-TOKEN" not in json.dumps(summary)


def test_an_unrecognised_key_in_a_known_section_is_named_but_withheld(
    tmp_path: Path
) -> None:
    """"Present but not reviewed" is more useful than silence, and still safe."""
    config_path = _write_config(tmp_path)
    text = config_path.read_text(encoding="utf-8").replace(
        '[notifications]\nenabled = false',
        '[notifications]\napi_key = "LEAKME"\nenabled = false',
    )
    config_path.write_text(text, encoding="utf-8")
    import tomllib

    with config_path.open("rb") as handle:
        raw = tomllib.load(handle)

    summary = collect_configuration_summary(load_config(config_path), raw)

    assert summary["unrecognised_settings"]["notifications"] == {
        "api_key": REDACTED
    }
    assert "LEAKME" not in json.dumps(summary)


def test_the_bundle_contains_no_environment_dump(
    config: MGOConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An environment block is the classic way a token escapes into a log."""
    monkeypatch.setenv("MGO_TEST_CANARY", "ENVIRONMENT-CANARY-VALUE")

    result = _generate(config, tmp_path / "out")

    assert result.bundle_path is not None
    text = _archive_text(result.bundle_path)
    assert "ENVIRONMENT-CANARY-VALUE" not in text
    assert "MGO_TEST_CANARY" not in text


def test_systemctl_output_is_restricted_to_the_reviewed_properties() -> None:
    """``systemctl show`` would otherwise dump the unit's whole environment."""

    def leaky_run(command: list[str]) -> CommandResult:
        return CommandResult(
            available=True,
            returncode=0,
            stdout=(
                "ActiveState=active\n"
                "Environment=TELEGRAM_TOKEN=leaked SECRET=alsoleaked\n"
                "EnvironmentFiles=/etc/secrets\n"
                "MainPID=1234\n"
            ),
        )

    status, failure = collect_service_status("mgo.service", leaky_run)

    assert failure is None
    assert status["properties"] == {"ActiveState": "active", "MainPID": "1234"}
    assert "leaked" not in json.dumps(status)


def test_the_bundle_contains_no_ssh_or_git_material(
    config: MGOConfig, tmp_path: Path
) -> None:
    """Nothing walks the filesystem, so none of these can be picked up."""
    result = _generate(config, tmp_path / "out")

    assert result.bundle_path is not None
    members = _members(result.bundle_path)
    text = _archive_text(result.bundle_path)

    for forbidden in (
        "authorized_keys",
        "known_hosts",
        "id_rsa",
        "id_ed25519",
        "BEGIN OPENSSH PRIVATE KEY",
        "BEGIN RSA PRIVATE KEY",
        ".gitconfig",
        ".git/config",
        "bash_history",
        "https://github.com/",
    ):
        assert forbidden not in text, forbidden
        assert not any(forbidden in name for name in members), forbidden


def test_a_non_canonical_path_is_reported_by_role_not_by_location(
    config: MGOConfig, tmp_path: Path
) -> None:
    """A developer checkout or relocated data directory is not disclosed."""
    summary = collect_configuration_summary(config)
    locations = summary["storage_locations"]

    assert {entry["role"] for entry in locations} == {
        "data_directory",
        "log_directory",
        "database_path",
        "capture_directory",
    }
    for entry in locations:
        assert entry["path"] == NON_CANONICAL_PATH
        assert entry["is_canonical"] is False
    assert str(tmp_path) not in json.dumps(summary)


def test_a_canonical_deployment_path_is_reported_verbatim(
    config: MGOConfig,
) -> None:
    """Public, documented locations help an operator diagnose the layout.

    The configuration is built directly rather than loaded from TOML: on
    Windows ``load_config`` resolves ``/var/lib/...`` against the project root
    (a rooted path with no drive letter is not absolute there), so a
    file-driven version of this test would exercise a path the Pi never sees.
    Constructing the production values makes the assertion mean the same thing
    on both platforms.
    """
    production = replace(
        config,
        storage=replace(
            config.storage,
            data_directory=Path(SYSTEM_STATE_DIRECTORY.as_posix()),
            log_directory=Path(SYSTEM_LOG_DIRECTORY.as_posix()),
            database_path=Path(SYSTEM_DATABASE_PATH.as_posix()),
        ),
        camera=replace(
            config.camera,
            capture_directory=Path(SYSTEM_CAPTURE_DIRECTORY.as_posix()),
        ),
    )

    summary = collect_configuration_summary(production)
    by_role = {entry["role"]: entry for entry in summary["storage_locations"]}

    assert by_role["database_path"]["path"] == "/var/lib/garden-observatory/db/mgo.db"
    assert by_role["database_path"]["is_canonical"] is True
    assert by_role["log_directory"]["path"] == "/var/log/garden-observatory"
    assert by_role["capture_directory"]["is_canonical"] is True


# --- archive safety ---------------------------------------------------------


def test_no_archive_member_uses_an_absolute_path(
    config: MGOConfig, tmp_path: Path
) -> None:
    """An absolute member could write outside the extraction directory."""
    result = _generate(config, tmp_path / "out")

    assert result.bundle_path is not None
    for name in _members(result.bundle_path):
        assert not name.startswith("/")
        assert not name.startswith("\\")
        assert ":" not in name


def test_no_archive_member_contains_a_parent_reference(
    config: MGOConfig, tmp_path: Path
) -> None:
    """``..`` is the other half of the path-traversal problem."""
    result = _generate(config, tmp_path / "out")

    assert result.bundle_path is not None
    for name in _members(result.bundle_path):
        assert ".." not in Path(name).parts


def test_every_archive_member_is_a_regular_file(
    config: MGOConfig, tmp_path: Path
) -> None:
    """No symlink, hard link, device node or directory entry can be present."""
    result = _generate(config, tmp_path / "out")
    assert result.bundle_path is not None

    with tarfile.open(result.bundle_path) as archive:
        for info in archive.getmembers():
            assert info.isreg(), info.name
            assert not info.issym()
            assert not info.islnk()
            assert not info.isdev()


def test_archive_members_carry_no_real_ownership(
    config: MGOConfig, tmp_path: Path
) -> None:
    """A username in an archive discloses the account the Pi runs under."""
    result = _generate(config, tmp_path / "out")
    assert result.bundle_path is not None

    with tarfile.open(result.bundle_path) as archive:
        for info in archive.getmembers():
            assert info.uid == 0
            assert info.gid == 0
            assert info.uname == ""
            assert info.gname == ""
            assert info.mode == MEMBER_MODE


@POSIX_ONLY
def test_the_bundle_file_is_owner_only(config: MGOConfig, tmp_path: Path) -> None:
    """A bundle is for the operator to hand over deliberately."""
    result = _generate(config, tmp_path / "out")

    assert result.bundle_path is not None
    mode = stat.S_IMODE(result.bundle_path.stat().st_mode)
    assert mode == BUNDLE_FILE_MODE
    assert not mode & 0o077


# --- bounds -----------------------------------------------------------------


def test_an_over_long_endpoint_response_is_discarded(
    config: MGOConfig, tmp_path: Path
) -> None:
    """A misbehaving endpoint must not be able to fill the SD card."""

    def flood(url: str) -> bytes:
        return b"x" * (MAX_ENDPOINT_RESPONSE_BYTES + 1024)

    result = _generate(config, tmp_path / "out", fetch=flood)

    assert result.outcome is BundleOutcome.PARTIAL
    assert all(
        error.error_code is ErrorCode.DIAGNOSTIC_LIMIT_EXCEEDED
        for error in result.errors
    )
    assert result.bundle_path is not None
    assert result.bundle_path.stat().st_size < MAX_ARCHIVE_BYTES


def test_the_journal_is_bounded_by_line_count() -> None:
    """A busy day must not produce an unbounded journal member."""

    def flood(command: list[str]) -> CommandResult:
        return CommandResult(
            available=True,
            returncode=0,
            stdout="\n".join(f"line {index}" for index in range(MAX_JOURNAL_LINES * 3)),
        )

    text, failure = collect_journal("mgo.service", flood)

    assert failure is None
    assert len(text.splitlines()) <= MAX_JOURNAL_LINES


def test_the_journal_is_scoped_to_the_mgo_unit_only() -> None:
    """The whole system journal would carry other services' logs off the Pi."""
    seen: list[list[str]] = []

    def record(command: list[str]) -> CommandResult:
        seen.append(list(command))
        return CommandResult(available=True, returncode=0, stdout="")

    collect_journal("mgo.service", record)

    assert seen[0][0] == "journalctl"
    assert "--unit" in seen[0]
    assert seen[0][seen[0].index("--unit") + 1] == "mgo.service"
    assert "--since" in seen[0]


def test_the_archive_size_limit_is_enforced(
    config: MGOConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A backstop for a source that misbehaves in an unforeseen way."""
    monkeypatch.setattr(
        "mgo.operations.support_bundle.MAX_ARCHIVE_BYTES", 128
    )

    with pytest.raises(OperationError) as caught:
        _generate(config, tmp_path / "out")

    assert caught.value.code is ErrorCode.DIAGNOSTIC_LIMIT_EXCEEDED


def test_the_member_count_limit_is_enforced(
    config: MGOConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other archive backstop."""
    monkeypatch.setattr("mgo.operations.support_bundle.MAX_ARCHIVE_MEMBERS", 3)

    with pytest.raises(OperationError) as caught:
        _generate(config, tmp_path / "out")

    assert caught.value.code is ErrorCode.DIAGNOSTIC_LIMIT_EXCEEDED


# --- degradation ------------------------------------------------------------


def test_a_missing_systemctl_is_recorded_not_raised() -> None:
    """The normal case on the development machine."""
    status, failure = collect_service_status("mgo.service", absent_run)

    assert status["available"] is False
    assert failure is not None
    assert failure.error_code is ErrorCode.DIAGNOSTIC_SOURCE_UNAVAILABLE


def test_a_missing_journalctl_is_recorded_not_raised() -> None:
    """Likewise, and the member still exists with an explanation."""
    text, failure = collect_journal("mgo.service", absent_run)

    assert "journal unavailable" in text
    assert failure is not None
    assert failure.error_code is ErrorCode.DIAGNOSTIC_SOURCE_UNAVAILABLE


def test_a_subprocess_timeout_is_recorded_not_raised() -> None:
    """A hung command must bound the run, not end it."""

    def timed_out(command: list[str]) -> CommandResult:
        return CommandResult(available=True, error="journalctl did not finish.")

    text, failure = collect_journal("mgo.service", timed_out)

    assert failure is not None
    assert failure.error_code is ErrorCode.DIAGNOSTIC_TIMEOUT
    assert "unavailable" in text


def test_a_denied_journal_is_recorded_and_collection_continues() -> None:
    """An unprivileged account may not read the unit's journal."""

    def denied(command: list[str]) -> CommandResult:
        return CommandResult(available=True, returncode=1, stdout="", stderr="denied")

    text, failure = collect_journal("mgo.service", denied)

    assert failure is not None
    assert "Access may be denied" in failure.detail
    assert "journal unavailable" in text


def test_malformed_api_json_is_recorded_not_raised(
    config: MGOConfig, tmp_path: Path
) -> None:
    """A 200 response is not proof the body is the expected contract."""

    def garbage(url: str) -> bytes:
        return b"<html>proxy error</html>"

    result = _generate(config, tmp_path / "out", fetch=garbage)

    assert result.outcome is BundleOutcome.PARTIAL
    assert result.bundle_path is not None
    health = json.loads(_members(result.bundle_path)["health.json"])
    assert "did not return valid JSON" in health["error"]


def test_an_http_error_is_recorded_with_its_status(
    config: MGOConfig, tmp_path: Path
) -> None:
    """A 500 from a struggling API is diagnostic information in itself."""

    def failing(url: str) -> bytes:
        raise urllib.error.HTTPError(url, 503, "Service Unavailable", {}, None)  # type: ignore[arg-type]

    result = _generate(config, tmp_path / "out", fetch=failing)

    assert result.bundle_path is not None
    health = json.loads(_members(result.bundle_path)["health.json"])
    assert "503" in health["error"]


# --- network boundary -------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com:8080",
        "http://192.168.1.50:8080",
        "https://127.0.0.1:8080",
        "http://mgo-core:8080",
    ],
)
def test_a_non_loopback_api_url_is_refused(
    config: MGOConfig, tmp_path: Path, url: str
) -> None:
    """A support bundle must never reach the network."""
    with pytest.raises(OperationError) as caught:
        _generate(config, tmp_path / "out", api_base_url=url)

    assert caught.value.code is ErrorCode.INVALID_ARGUMENT


def test_only_read_only_endpoints_are_collected() -> None:
    """No capture, no preview control, no stream, no notification publication."""
    paths = [path for _, path in COLLECTED_ENDPOINTS]

    for forbidden in (
        "/camera/capture",
        "/camera/preview/start",
        "/camera/preview/stop",
        "/camera/preview/stream",
    ):
        assert forbidden not in paths


def test_every_collected_endpoint_is_requested_once(
    config: MGOConfig, tmp_path: Path
) -> None:
    """No retries: a retry loop is how a bounded tool becomes unbounded."""
    requested: list[str] = []

    def counting(url: str) -> bytes:
        requested.append(url)
        return healthy_fetch(url)

    _generate(config, tmp_path / "out", fetch=counting)

    assert len(requested) == len(COLLECTED_ENDPOINTS)
    assert len(set(requested)) == len(requested)


def test_a_dead_api_is_requested_once_per_endpoint(
    config: MGOConfig, tmp_path: Path
) -> None:
    """A failure must not be retried either."""
    attempts: list[str] = []

    def counting(url: str) -> bytes:
        attempts.append(url)
        raise urllib.error.URLError("refused")

    _generate(config, tmp_path / "out", fetch=counting)

    assert len(attempts) == len(COLLECTED_ENDPOINTS)


# --- manifest and reporting -------------------------------------------------


def test_the_manifest_checksums_every_other_member(
    config: MGOConfig, tmp_path: Path
) -> None:
    """A recipient can prove the bundle arrived intact."""
    import hashlib

    result = _generate(config, tmp_path / "out")
    assert result.bundle_path is not None
    members = _members(result.bundle_path)
    manifest = json.loads(members["manifest.json"])

    described = {entry["name"]: entry for entry in manifest["members"]}
    for name, payload in members.items():
        if name in {"manifest.json", "generation-summary.json"}:
            continue
        assert described[name]["sha256"] == hashlib.sha256(payload).hexdigest()
        assert described[name]["size_bytes"] == len(payload)


def test_the_generation_summary_reports_the_outcome_truthfully(
    config: MGOConfig, tmp_path: Path
) -> None:
    """Which sources answered, which did not, and the overall verdict."""
    result = _generate(config, tmp_path / "out", fetch=dead_fetch)
    assert result.bundle_path is not None

    summary = json.loads(_members(result.bundle_path)["generation-summary.json"])
    assert summary["outcome"] == "partial"
    assert summary["error_count"] == len(COLLECTED_ENDPOINTS)
    assert "/health" in summary["files_skipped"]


def test_errors_are_reported_deterministically(
    config: MGOConfig, tmp_path: Path
) -> None:
    """Two identical runs must describe the same failures the same way."""
    first = _generate(config, tmp_path / "a", fetch=dead_fetch, run=absent_run)
    second = _generate(config, tmp_path / "b", fetch=dead_fetch, run=absent_run)

    assert first.bundle_path is not None
    assert second.bundle_path is not None
    assert _members(first.bundle_path)["errors.json"] == (
        _members(second.bundle_path)["errors.json"]
    )


def test_every_documented_content_category_is_present(
    config: MGOConfig, tmp_path: Path
) -> None:
    """The bundle's contract with the operator reading docs/Operations.md."""
    result = _generate(config, tmp_path / "out")
    assert result.bundle_path is not None
    members = _members(result.bundle_path)

    for required in (
        "manifest.json",
        "generation-summary.json",
        "application-identity.json",
        "application-version.json",
        "health.json",
        "database-status.json",
        "camera-status.json",
        "preview-status.json",
        "motion-status.json",
        "notifications-status.json",
        "service-status.json",
        "journal.log",
        "journal-disk-usage.txt",
        "configuration-summary.json",
        "storage-summary.json",
        "errors.json",
    ):
        assert required in members, required


def test_the_bundle_name_uses_a_utc_timestamp(
    config: MGOConfig, tmp_path: Path
) -> None:
    """Consistent with backup naming, and unambiguous across a DST change."""
    moment = datetime(2026, 7, 28, 2, 30, 0, tzinfo=UTC)

    result = _generate(config, tmp_path / "out", now=moment)

    assert result.bundle_path is not None
    assert result.bundle_path.name == "mgo-support-20260728T023000Z.tar.gz"


def test_a_bundle_cannot_be_mistaken_for_a_backup_by_retention(
    config: MGOConfig, tmp_path: Path
) -> None:
    """Bundles and backups may legitimately share a directory."""
    from mgo.operations.backup import BACKUP_NAME_PATTERN

    result = _generate(config, tmp_path / "out")

    assert result.bundle_path is not None
    assert not BACKUP_NAME_PATTERN.match(result.bundle_path.name)


# --- regression: no external network access ---------------------------------
#
# Validating only the initial URL was insufficient. urllib's default opener
# honours HTTP_PROXY/ALL_PROXY and follows redirects, so a "loopback only"
# request could have been sent to a proxy or redirected to an external host --
# and the initial validation would have passed either way.


def test_the_opener_installs_no_proxy_handler() -> None:
    """No proxy handler at all is the strongest form of "no proxy".

    ``build_opener(ProxyHandler({}))`` works by *exclusion*: passing an instance
    makes ``build_opener`` drop the default ``ProxyHandler`` class, and an empty
    proxy map registers no ``*_open`` methods so nothing is installed in its
    place. The observable result — and the thing worth asserting — is that the
    opener carries no proxy handler, so there is nothing left to read
    ``HTTP_PROXY``.
    """
    for handler in support_bundle._OPENER.handlers:
        assert not isinstance(handler, urllib.request.ProxyHandler), handler


@pytest.mark.parametrize(
    "variable",
    ["HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "ALL_PROXY", "all_proxy"],
)
def test_a_configured_proxy_is_never_consulted(
    monkeypatch: pytest.MonkeyPatch, variable: str
) -> None:
    """The environment must not be able to redirect a diagnostic request.

    A default opener built while these are set would carry a proxy handler
    populated from them; ours never does, whatever the environment says.
    """
    monkeypatch.setenv(variable, "http://proxy.example.com:3128")

    # The shipped opener is unaffected...
    for handler in support_bundle._OPENER.handlers:
        assert not isinstance(handler, urllib.request.ProxyHandler)

    # ...and rebuilding it the same way under this environment stays clean,
    # proving the exclusion is structural rather than an artefact of import
    # order.
    rebuilt = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), support_bundle._NoRedirects
    )
    for handler in rebuilt.handlers:
        assert not isinstance(handler, urllib.request.ProxyHandler)

    # For contrast: the default opener *would* have picked the proxy up.
    default = urllib.request.build_opener()
    proxied = [
        handler
        for handler in default.handlers
        if isinstance(handler, urllib.request.ProxyHandler)
    ]
    assert proxied and proxied[0].proxies, (
        "the default opener should read the environment — that is the risk "
        "this correction removes"
    )


def test_redirects_are_never_followed() -> None:
    """A local service must not be able to redirect a bundle off the host."""
    from mgo.operations.support_bundle import _NoRedirects

    handler = _NoRedirects()
    assert handler.redirect_request(None, None, 302, "Found", {}, "http://x/") is None


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_a_redirect_response_is_recorded_as_a_source_failure(
    config: MGOConfig, tmp_path: Path, status: int
) -> None:
    """A 3xx is a failed source, not a hop to follow."""

    def redirecting(url: str) -> bytes:
        raise urllib.error.HTTPError(
            url,
            status,
            "Redirect",
            {"Location": "http://evil.example.com/"},  # type: ignore[arg-type]
            None,
        )

    result = _generate(config, tmp_path / "out", fetch=redirecting)

    assert result.outcome is BundleOutcome.PARTIAL
    assert result.bundle_path is not None
    text = _archive_text(result.bundle_path)
    assert "evil.example.com" not in text
    assert str(status) in text


def test_the_opener_is_used_rather_than_the_global_urlopen() -> None:
    """``urlopen`` would reintroduce the default proxy and redirect handlers."""
    source = Path(
        "src/mgo/operations/support_bundle.py"
    ).read_text(encoding="utf-8")

    assert "_OPENER.open(" in source
    assert "urllib.request.urlopen(" not in source


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com:8080",
        "http://192.168.1.50:8080",
        "http://10.0.0.1:8080",
        "https://127.0.0.1:8080",
        "ftp://127.0.0.1:8080",
        "file:///etc/passwd",
        "http://user:password@127.0.0.1:8080",
        "http://user@127.0.0.1:8080",
        "http://127.0.0.1:8080?debug=1",
        "http://127.0.0.1:8080#fragment",
        "http://mgo-core:8080",
        "http://localhost:8080",
        "http://127.0.0.1.evil.com:8080",
    ],
)
def test_every_unsafe_base_url_is_refused(
    config: MGOConfig, tmp_path: Path, url: str
) -> None:
    """Only a literal loopback address with no credentials, query or fragment."""
    with pytest.raises(OperationError) as caught:
        _generate(config, tmp_path / "out", api_base_url=url)

    assert caught.value.code is ErrorCode.INVALID_ARGUMENT


def test_localhost_is_refused_because_it_needs_resolution() -> None:
    """A name resolves through /etc/hosts and the resolver; a literal does not."""
    assert "localhost" not in ALLOWED_LOOPBACK_HOSTS
    assert {"127.0.0.1", "::1"} == ALLOWED_LOOPBACK_HOSTS


def test_literal_loopback_addresses_are_accepted(
    config: MGOConfig, tmp_path: Path
) -> None:
    """The permitted forms must actually work."""
    result = _generate(config, tmp_path / "out", api_base_url="http://127.0.0.1:8080")

    assert result.outcome is BundleOutcome.COMPLETE


# --- regression: command return codes ----------------------------------------
#
# A non-zero systemctl was previously reported as ``available: true`` with an
# empty property set, which reads as "the service exists and told us nothing".


def test_a_non_zero_systemctl_is_not_reported_as_available() -> None:
    """The defect: an empty property set presented as a successful reading."""

    def failing(command: list[str]) -> CommandResult:
        return CommandResult(
            available=True,
            returncode=1,
            stdout="",
            stderr="Unit mgo.service could not be found.",
        )

    status, failure = collect_service_status("mgo.service", failing)

    assert status["available"] is False
    assert failure is not None
    assert failure.error_code is ErrorCode.DIAGNOSTIC_SOURCE_UNAVAILABLE
    assert "could not be found" in status["error"]


def test_a_non_zero_systemctl_without_stderr_still_fails() -> None:
    """The exit code alone is enough to know the answer was not obtained."""

    def failing(command: list[str]) -> CommandResult:
        return CommandResult(available=True, returncode=1, stdout="", stderr="")

    status, failure = collect_service_status("mgo.service", failing)

    assert status["available"] is False
    assert failure is not None
    assert "exited 1" in status["error"]


def test_systemctl_returning_no_recognised_property_fails() -> None:
    """Exit 0 with nothing usable is still not a service status."""

    def empty(command: list[str]) -> CommandResult:
        return CommandResult(available=True, returncode=0, stdout="")

    status, failure = collect_service_status("mgo.service", empty)

    assert status["available"] is False
    assert failure is not None


def test_stderr_in_a_command_failure_is_bounded() -> None:
    """Unbounded stderr must not flow into a file that leaves the machine."""

    def noisy(command: list[str]) -> CommandResult:
        return CommandResult(
            available=True, returncode=1, stdout="", stderr="x" * 10_000
        )

    status, _ = collect_service_status("mgo.service", noisy)

    assert len(status["error"]) < MAX_COMMAND_ERROR_DETAIL + 100


def test_a_non_zero_journal_disk_usage_is_a_failure() -> None:
    """An empty line must not be published as a disk-usage reading."""

    def failing(command: list[str]) -> CommandResult:
        return CommandResult(
            available=True, returncode=1, stdout="", stderr="No journal files."
        )

    text, failure = collect_journal_disk_usage(failing)

    assert failure is not None
    assert failure.error_code is ErrorCode.DIAGNOSTIC_SOURCE_UNAVAILABLE
    assert "unavailable" in text


def test_a_successful_journal_disk_usage_is_reported() -> None:
    """The success path still works."""
    text, failure = collect_journal_disk_usage(healthy_run)

    assert failure is None
    assert "96.0M" in text


def test_a_command_timeout_is_distinguished_from_a_failure() -> None:
    """A hung command and a refused one are different diagnoses."""

    def timed_out(command: list[str]) -> CommandResult:
        return CommandResult(available=True, error="systemctl did not finish.")

    _, failure = collect_service_status("mgo.service", timed_out)

    assert failure is not None
    assert failure.error_code is ErrorCode.DIAGNOSTIC_TIMEOUT


def test_a_failing_command_makes_the_bundle_partial(
    config: MGOConfig, tmp_path: Path
) -> None:
    """A non-zero command must degrade the overall outcome."""

    def failing(command: list[str]) -> CommandResult:
        return CommandResult(available=True, returncode=1, stdout="", stderr="no")

    result = _generate(config, tmp_path / "out", run=failing)

    assert result.outcome is BundleOutcome.PARTIAL


# --- regression: bounded storage aggregation ---------------------------------
#
# Path.rglob("*") walked an entire tree with no limit. The captures directory is
# flat today; the implementation must not depend on that staying true.


def test_directory_totals_stop_at_the_entry_limit(tmp_path: Path) -> None:
    """A media archive must not be walked without bound."""
    directory = tmp_path / "many"
    directory.mkdir()
    for index in range(25):
        (directory / f"file-{index}.bin").write_bytes(b"x")

    totals = _directory_totals(directory, max_entries=10)

    assert totals["entries"] == 10
    assert totals["truncated"] is True
    assert totals["truncated_by"] == "max_entries"
    assert totals["limit"] == 10


def test_directory_totals_stop_at_the_depth_limit(tmp_path: Path) -> None:
    """A deep tree must not be descended without bound."""
    directory = tmp_path / "deep"
    current = directory
    for level in range(6):
        current = current / f"level-{level}"
    current.mkdir(parents=True)
    (current / "buried.bin").write_bytes(b"x")

    totals = _directory_totals(directory, max_depth=2)

    assert totals["truncated"] is True
    assert totals["truncated_by"] == "max_depth"
    assert totals["entries"] == 0


def test_directory_totals_count_nested_files_within_the_bounds(
    tmp_path: Path
) -> None:
    """Bounded does not mean shallow: nesting inside the limits is counted."""
    directory = tmp_path / "nested"
    (directory / "a" / "b").mkdir(parents=True)
    (directory / "top.bin").write_bytes(b"xx")
    (directory / "a" / "mid.bin").write_bytes(b"xxx")
    (directory / "a" / "b" / "deep.bin").write_bytes(b"x")

    totals = _directory_totals(directory)

    assert totals["entries"] == 3
    assert totals["total_bytes"] == 6
    assert totals["truncated"] is False


def test_directory_totals_never_descend_a_symlinked_directory(
    tmp_path: Path
) -> None:
    """A link into / would turn a capture scan into a filesystem walk."""
    directory = tmp_path / "root"
    directory.mkdir()
    (directory / "real.bin").write_bytes(b"x")

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    for index in range(5):
        (elsewhere / f"other-{index}.bin").write_bytes(b"yyyy")

    try:
        (directory / "link").symlink_to(elsewhere, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not creatable in this environment")

    totals = _directory_totals(directory)

    assert totals["entries"] == 1
    assert totals["total_bytes"] == 1


def test_directory_totals_survive_a_symlink_loop(tmp_path: Path) -> None:
    """A self-referential link must not make aggregation run forever."""
    directory = tmp_path / "loop"
    directory.mkdir()
    (directory / "real.bin").write_bytes(b"x")
    try:
        (directory / "self").symlink_to(directory, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not creatable in this environment")

    totals = _directory_totals(directory)

    assert totals["entries"] == 1
    assert totals["truncated"] is False


def test_directory_totals_do_not_measure_a_symlinked_files_target(
    tmp_path: Path
) -> None:
    """The size of a file outside the approved directory is never read."""
    directory = tmp_path / "root"
    directory.mkdir()
    outside = tmp_path / "secret.bin"
    outside.write_bytes(b"y" * 5000)

    try:
        (directory / "link.bin").symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not creatable in this environment")

    totals = _directory_totals(directory)

    assert totals["entries"] == 1
    assert totals["total_bytes"] == 0


def test_directory_totals_handle_an_empty_directory(tmp_path: Path) -> None:
    """Zero is a valid answer, not a missing one."""
    directory = tmp_path / "empty"
    directory.mkdir()

    totals = _directory_totals(directory)

    assert totals == {
        "exists": True,
        "entries": 0,
        "total_bytes": 0,
        "truncated": False,
    }


def test_directory_totals_handle_a_missing_directory(tmp_path: Path) -> None:
    """An absent directory is reported, not an error."""
    totals = _directory_totals(tmp_path / "absent")

    assert totals["exists"] is False
    assert totals["truncated"] is False


def test_directory_totals_skip_a_file_that_disappears(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A live system deletes files mid-walk; that is not a bundle failure."""
    directory = tmp_path / "racing"
    directory.mkdir()
    (directory / "vanishing.bin").write_bytes(b"xxx")
    (directory / "stable.bin").write_bytes(b"xx")

    real_stat = Path.stat

    def vanishing(self: Path, *args: object, **kwargs: object) -> object:
        if self.name == "vanishing.bin":
            raise OSError(2, "No such file or directory")
        return real_stat(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "stat", vanishing)
    totals = _directory_totals(directory)

    assert totals["entries"] == 2
    assert totals["total_bytes"] == 2


def test_an_unreadable_subdirectory_does_not_fail_aggregation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Permission problems are skipped, keeping the totals gathered so far."""
    directory = tmp_path / "root"
    (directory / "blocked").mkdir(parents=True)
    (directory / "top.bin").write_bytes(b"x")

    real_iterdir = Path.iterdir

    def refusing(self: Path) -> object:
        if self.name == "blocked":
            raise OSError(13, "Permission denied")
        return real_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", refusing)
    totals = _directory_totals(directory)

    assert totals["entries"] == 1


def test_storage_truncation_makes_the_bundle_partial(
    config: MGOConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Truncation is reported, never silent -- and never fatal."""
    captures = config.camera.capture_directory
    captures.mkdir(parents=True, exist_ok=True)
    for index in range(5):
        (captures / f"capture-{index}.jpg").write_bytes(b"x")

    monkeypatch.setattr(
        "mgo.operations.support_bundle.MAX_STORAGE_ENTRIES", 2
    )

    result = _generate(config, tmp_path / "out")

    assert result.outcome is BundleOutcome.PARTIAL
    assert any(
        error.error_code is ErrorCode.DIAGNOSTIC_LIMIT_EXCEEDED
        for error in result.errors
    )
    assert result.bundle_path is not None
    storage = json.loads(_members(result.bundle_path)["storage-summary.json"])
    assert storage["captures"]["truncated"] is True
    # Still no filename, even when truncated.
    assert "capture-0.jpg" not in _archive_text(result.bundle_path)


# --- regression: bundle publication hardening --------------------------------


def test_an_existing_bundle_is_never_overwritten(
    config: MGOConfig, tmp_path: Path
) -> None:
    """A bundle is generated when something is wrong; evidence must survive."""
    destination = tmp_path / "out"
    moment = datetime(2026, 7, 28, 2, 30, 0, tzinfo=UTC)

    first = _generate(config, destination, now=moment)
    assert first.bundle_path is not None
    original = first.bundle_path.read_bytes()

    with pytest.raises(OperationError) as caught:
        _generate(config, destination, now=moment)

    assert caught.value.code is ErrorCode.DIAGNOSTIC_OUTPUT_UNWRITABLE
    assert first.bundle_path.read_bytes() == original


def test_a_bundle_over_the_size_limit_is_discarded(
    config: MGOConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The compressed artefact is what lands on the SD card, so it is checked."""
    destination = tmp_path / "out"
    real_write = support_bundle._write_archive

    def oversized(members: object, target: Path) -> None:
        real_write(members, target)  # type: ignore[arg-type]
        # Simulate an archive that compressed far worse than expected. The
        # limit is set high enough that the pre-compression check passes, so
        # this test exercises the *post-write* size check specifically.
        target.write_bytes(b"x" * 30_000)

    monkeypatch.setattr(support_bundle, "_write_archive", oversized)
    monkeypatch.setattr(support_bundle, "MAX_ARCHIVE_BYTES", 20_000)

    with pytest.raises(OperationError) as caught:
        _generate(config, destination)

    assert caught.value.code is ErrorCode.DIAGNOSTIC_LIMIT_EXCEEDED
    assert "completed bundle" in caught.value.message
    # Neither the temporary file nor a published bundle survives.
    assert list(destination.iterdir()) == []


def test_the_temporary_archive_is_removed_after_a_size_failure(
    config: MGOConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No half-published bundle may be left in the output directory."""
    destination = tmp_path / "out"
    monkeypatch.setattr(support_bundle, "MAX_ARCHIVE_BYTES", 64)

    with pytest.raises(OperationError):
        _generate(config, destination)

    assert not destination.exists() or list(destination.iterdir()) == []


def test_generation_emits_a_structured_completion_event(
    config: MGOConfig, tmp_path: Path
) -> None:
    """The journal record a timer-driven or operator run leaves behind."""
    stream = io.StringIO()

    _generate(
        config,
        tmp_path / "out",
        emitter=EventEmitter("mgo-support-bundle", stream=stream),
    )

    events = [json.loads(line) for line in stream.getvalue().splitlines()]
    completion = [item for item in events if item["event_id"] == "bundle.completed"]
    assert len(completion) == 1
    assert completion[0]["result"] == "complete"
    assert completion[0]["error_code"] is None
