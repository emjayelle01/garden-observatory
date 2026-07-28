"""Tests for structured operational events and the operations lock.

Two small foundations that everything else in :mod:`mgo.operations` relies on:

* an event stream where **every** line is valid JSON and carries the six
  required fields, because a half-parsed log line during an incident is worse
  than no log line;
* a lock that actually excludes a second job, reclaims only genuinely abandoned
  locks, and never deletes someone else's.

Nothing here touches the Raspberry Pi, ``systemd``, the journal or a production
path.
"""

from __future__ import annotations

import io
import json
import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from mgo.operations.errors import ErrorCode, OperationError
from mgo.operations.events import (
    MAX_MESSAGE_LENGTH,
    REDACTED,
    EventEmitter,
    OperationEvent,
    Severity,
    is_sensitive_key,
    redact_mapping,
)
from mgo.operations.locking import (
    LOCK_FILE_MODE,
    OperationLock,
    operation_lock,
)

REQUIRED_FIELDS = (
    "timestamp",
    "service",
    "severity",
    "event_id",
    "message",
    "error_code",
)


def _emit(**kwargs: object) -> dict[str, object]:
    """Emit one event into a buffer and return the parsed record."""
    stream = io.StringIO()
    emitter = EventEmitter("mgo-test", stream=stream)
    emitter.emit(
        Severity(kwargs.pop("severity", Severity.INFO)),  # type: ignore[arg-type]
        str(kwargs.pop("event_id", "test.event")),
        str(kwargs.pop("message", "A test event.")),
        error_code=kwargs.pop("error_code", None),  # type: ignore[arg-type]
        **kwargs,
    )
    lines = stream.getvalue().splitlines()
    assert len(lines) == 1, "one event must produce exactly one line"
    parsed = json.loads(lines[0])
    assert isinstance(parsed, dict)
    return parsed


# --- required fields --------------------------------------------------------


def test_every_required_field_is_present() -> None:
    """The six required fields appear on every event, success or failure."""
    record = _emit()

    for name in REQUIRED_FIELDS:
        assert name in record, name


def test_required_fields_come_first_in_declaration_order() -> None:
    """A human scanning the journal sees identity before extra detail."""
    record = _emit(backup_filename="mgo-20260101T000000Z.db")

    assert list(record)[: len(REQUIRED_FIELDS)] == list(REQUIRED_FIELDS)


def test_timestamp_is_utc_and_iso_8601() -> None:
    """Local time would be ambiguous across a DST change; UTC never is."""
    record = _emit()

    parsed = datetime.fromisoformat(str(record["timestamp"]))
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == timedelta(0)


def test_a_non_utc_timestamp_is_converted_rather_than_recorded_as_given() -> None:
    """An event built with a local-time moment is still reported in UTC."""
    moment = datetime(2026, 7, 28, 12, 0, tzinfo=UTC).astimezone(
        # A fixed +02:00 zone, matching the Pi's Africa/Johannesburg offset.
        timezone := __import__("datetime").timezone(timedelta(hours=2))
    )
    event = OperationEvent(
        service="mgo-test",
        severity=Severity.INFO,
        event_id="test.event",
        message="x",
        timestamp=moment,
    )

    assert str(event.as_dict()["timestamp"]).endswith("+00:00")
    assert timezone.utcoffset(None) == timedelta(hours=2)


def test_error_code_is_null_for_success_and_stable_for_failure() -> None:
    """Success carries no code; a known failure carries its exact identifier."""
    assert _emit()["error_code"] is None

    record = _emit(
        severity=Severity.ERROR, error_code=ErrorCode.BACKUP_INTEGRITY_FAILED
    )
    assert record["error_code"] == "BACKUP_INTEGRITY_FAILED"


def test_severity_uses_the_closed_vocabulary() -> None:
    """A consumer filtering on severity must not miss an invented value."""
    assert {member.value for member in Severity} == {
        "DEBUG",
        "INFO",
        "WARNING",
        "ERROR",
        "CRITICAL",
    }

    for severity in Severity:
        assert _emit(severity=severity)["severity"] == severity.value


def test_service_identifies_the_component() -> None:
    """Events from different tools must be attributable."""
    assert _emit()["service"] == "mgo-test"


def test_event_id_is_recorded_verbatim() -> None:
    """Event IDs are matched exactly by operator searches."""
    assert _emit(event_id="backup.completed")["event_id"] == "backup.completed"


# --- JSON validity ----------------------------------------------------------


@pytest.mark.parametrize(
    "message",
    [
        "plain",
        'contains "double quotes"',
        "contains 'single quotes'",
        "contains\nnewline",
        "contains\r\ncarriage return",
        "contains\ttab",
        "contains \\ backslash",
        "contains unicode: naïve café — 日本語 🐦",
        "contains a null-ish sequence \\u0000",
    ],
)
def test_awkward_messages_still_produce_one_valid_json_line(message: str) -> None:
    """A message can never split, escape or corrupt its own record."""
    stream = io.StringIO()
    EventEmitter("mgo-test", stream=stream).info("test.event", message)

    lines = stream.getvalue().splitlines()
    assert len(lines) == 1
    assert isinstance(json.loads(lines[0]), dict)


def test_embedded_newlines_do_not_survive_into_the_message() -> None:
    """Whitespace is collapsed, so the rendered message stays on one line."""
    record = _emit(message="first\nsecond\n\nthird")

    assert record["message"] == "first second third"


def test_a_long_message_is_bounded() -> None:
    """An unbounded message would write a large record on every failed run."""
    record = _emit(message="x" * (MAX_MESSAGE_LENGTH * 3))

    assert len(str(record["message"])) <= MAX_MESSAGE_LENGTH


def test_unicode_is_preserved_rather_than_escaped() -> None:
    """Readability in ``journalctl`` matters; the stream is UTF-8."""
    record = _emit(message="café 🐦")

    assert record["message"] == "café 🐦"


def test_an_unserialisable_field_does_not_break_the_record() -> None:
    """Emission is a report about work; it must not fail the work."""

    class Awkward:
        def __repr__(self) -> str:
            return "<awkward>"

    record = _emit(subject=Awkward())

    assert isinstance(record, dict)
    assert record["event_id"] == "test.event"


def test_a_field_that_raises_on_str_still_yields_valid_json() -> None:
    """Even a pathological object cannot corrupt the stream."""

    class Hostile:
        def __str__(self) -> str:
            raise RuntimeError("no")

        def __repr__(self) -> str:
            raise RuntimeError("no")

    stream = io.StringIO()
    EventEmitter("mgo-test", stream=stream).info("test.event", "x", value=Hostile())

    lines = stream.getvalue().splitlines()
    assert len(lines) == 1
    assert isinstance(json.loads(lines[0]), dict)


def test_a_broken_stream_does_not_fail_the_caller() -> None:
    """Losing a log line must not fail an otherwise successful backup."""
    stream = io.StringIO()
    stream.close()

    event = EventEmitter("mgo-test", stream=stream).info("test.event", "x")

    assert event.event_id == "test.event"


# --- redaction --------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "secret",
        "token",
        "bot_token",
        "password",
        "smtp_passwd",
        "credential",
        "credentials",
        "private_key",
        "api_key",
        "apikey",
        "API_KEY",
        "Authorization",
        "signature",
        "cookie",
        "session_id",
        "key",
    ],
)
def test_sensitive_field_names_are_recognised(name: str) -> None:
    """Detection is case-insensitive and matches on substrings."""
    assert is_sensitive_key(name)


@pytest.mark.parametrize(
    "name",
    [
        "backup_filename",
        "schema_version",
        "duration_ms",
        "status",
        "result",
        "files_included",
    ],
)
def test_ordinary_field_names_are_not_redacted(name: str) -> None:
    """Over-redaction would make diagnostics useless."""
    assert not is_sensitive_key(name)


@pytest.mark.parametrize("name", ["monkey", "keyword", "turnkey"])
def test_the_broad_key_marker_over_matches_by_design(name: str) -> None:
    """A documented, accepted false positive rather than an unnoticed one.

    ``key`` is matched as a substring so ``signing_key``, ``bot_key`` and a bare
    ``key`` are all caught. The cost is that an unrelated field whose name
    merely contains those three letters is redacted too. That trade is
    deliberate: a redacted diagnostic field is an inconvenience, while a leaked
    credential in a support bundle sent to another person is not. No field in
    this codebase is affected; this test exists so the behaviour is a recorded
    decision rather than a surprise to the next author.
    """
    assert is_sensitive_key(name)


def test_sensitive_values_never_reach_the_stream() -> None:
    """The whole point: a credential must not be printable from an event."""
    record = _emit(api_key="sk-live-abcdef", provider="log")

    assert record["api_key"] == REDACTED
    assert "sk-live-abcdef" not in json.dumps(record)
    assert record["provider"] == "log"


def test_nested_sensitive_values_are_redacted() -> None:
    """A secret one level down is exactly as sensitive."""
    redacted = redact_mapping({"outer": {"token": "abc", "safe": 1}})

    assert redacted == {"outer": {"token": REDACTED, "safe": 1}}


def test_redaction_is_applied_to_the_key_regardless_of_the_value() -> None:
    """Whether a credential is *set* must not be inferable from the output."""
    for value in (None, "", 0, False):
        assert redact_mapping({"password": value}) == {"password": REDACTED}


def test_an_extra_field_cannot_displace_a_required_field() -> None:
    """A caller must not be able to rewrite the record's own severity."""
    record = _emit(severity=Severity.ERROR, error_code=ErrorCode.UNEXPECTED_ERROR)

    assert record["severity"] == "ERROR"
    assert record["error_code"] == "UNEXPECTED_ERROR"


def test_extra_fields_named_like_required_ones_are_dropped() -> None:
    """``service`` in extras must not overwrite the emitter's own identity."""
    stream = io.StringIO()
    emitter = EventEmitter("mgo-real", stream=stream)
    emitter.emit(Severity.INFO, "test.event", "x", service="mgo-impostor")

    record = json.loads(stream.getvalue())
    assert record["service"] == "mgo-real"


# --- the lock ---------------------------------------------------------------


def test_a_second_backup_cannot_run_concurrently(tmp_path: Path) -> None:
    """Overlapping backups would compete for the same destination."""
    lock_path = tmp_path / "op.lock"

    with operation_lock(lock_path, operation="backup"):
        second = OperationLock(lock_path, operation="backup")
        with pytest.raises(OperationError) as caught:
            second.acquire()

    assert caught.value.code is ErrorCode.BACKUP_LOCKED
    assert "already running" in caught.value.message


def test_the_lock_is_released_after_the_block(tmp_path: Path) -> None:
    """A finished job must not leave the next one locked out."""
    lock_path = tmp_path / "op.lock"

    with operation_lock(lock_path, operation="backup"):
        assert lock_path.exists()

    assert not lock_path.exists()
    with operation_lock(lock_path, operation="backup"):
        pass


def test_the_lock_is_released_even_when_the_block_raises(tmp_path: Path) -> None:
    """A crashed backup must not lock the directory until the stale timeout."""
    lock_path = tmp_path / "op.lock"

    with pytest.raises(RuntimeError), operation_lock(lock_path, operation="backup"):
        raise RuntimeError("boom")

    assert not lock_path.exists()


def test_acquisition_does_not_wait(tmp_path: Path) -> None:
    """A blocked run must fail immediately, never queue behind its predecessor."""
    lock_path = tmp_path / "op.lock"

    with operation_lock(lock_path, operation="backup"):
        started = time.monotonic()
        with pytest.raises(OperationError):
            OperationLock(lock_path, operation="backup").acquire()
        elapsed = time.monotonic() - started

    assert elapsed < 1.0


def test_a_fresh_lock_is_never_treated_as_stale(tmp_path: Path) -> None:
    """Stale reclamation must not race a healthy running job."""
    lock_path = tmp_path / "op.lock"

    with operation_lock(lock_path, operation="backup"):
        second = OperationLock(
            lock_path, operation="backup", stale_after_seconds=3600
        )
        with pytest.raises(OperationError):
            second.acquire()
        assert not second.reclaimed_stale_lock


def test_an_abandoned_lock_is_reclaimed_after_the_threshold(tmp_path: Path) -> None:
    """A power cut must not disable backups permanently."""
    lock_path = tmp_path / "op.lock"
    lock_path.write_text('{"token": "abandoned"}', encoding="utf-8")
    old = time.time() - 7200
    os.utime(lock_path, (old, old))

    lock = OperationLock(lock_path, operation="backup", stale_after_seconds=60)
    lock.acquire()
    try:
        assert lock.reclaimed_stale_lock
        assert lock.held
    finally:
        lock.release()


def test_stale_reclamation_never_consults_a_process_id(tmp_path: Path) -> None:
    """A PID is not portable and its absence is not proof of death.

    A lock recording a certainly-dead PID but a *recent* timestamp must still be
    respected: only age reclaims a lock.
    """
    lock_path = tmp_path / "op.lock"
    lock_path.write_text(
        json.dumps({"token": "held", "pid": 999999999}), encoding="utf-8"
    )

    with pytest.raises(OperationError) as caught:
        OperationLock(lock_path, operation="backup").acquire()

    assert caught.value.code is ErrorCode.BACKUP_LOCKED


def test_release_leaves_another_owners_lock_alone(tmp_path: Path) -> None:
    """A reclaimed process must not delete the new owner's lock on its way out."""
    lock_path = tmp_path / "op.lock"

    first = OperationLock(lock_path, operation="backup")
    first.acquire()

    # Simulate the lock having been reclaimed and retaken by someone else.
    lock_path.write_text('{"token": "someone-else"}', encoding="utf-8")
    first.release()

    assert lock_path.exists()
    assert json.loads(lock_path.read_text(encoding="utf-8"))["token"] == (
        "someone-else"
    )


def test_release_is_a_no_op_when_the_lock_was_never_held(tmp_path: Path) -> None:
    """Release runs in a ``finally``; it must never raise."""
    OperationLock(tmp_path / "op.lock", operation="backup").release()


def test_the_lock_records_a_diagnosable_payload(tmp_path: Path) -> None:
    """An operator reading the file during an incident needs context."""
    lock_path = tmp_path / "op.lock"

    with operation_lock(lock_path, operation="backup"):
        payload = json.loads(lock_path.read_text(encoding="utf-8"))

    assert payload["operation"] == "backup"
    assert payload["pid"] == os.getpid()
    assert datetime.fromisoformat(payload["acquired_at"]).tzinfo is not None


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode bits")
def test_the_lock_file_is_not_world_readable(tmp_path: Path) -> None:
    """Nothing this tooling creates is world-accessible."""
    lock_path = tmp_path / "op.lock"

    with operation_lock(lock_path, operation="backup"):
        assert lock_path.stat().st_mode & 0o777 == LOCK_FILE_MODE
        assert not lock_path.stat().st_mode & 0o007


def test_a_non_positive_stale_threshold_is_rejected(tmp_path: Path) -> None:
    """A zero threshold would make every lock instantly reclaimable."""
    with pytest.raises(OperationError) as caught:
        OperationLock(tmp_path / "op.lock", operation="backup", stale_after_seconds=0)

    assert caught.value.code is ErrorCode.INVALID_ARGUMENT
