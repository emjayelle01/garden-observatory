"""Integrity tests for the camera acceptance procedure and record (Task 12).

The acceptance record is a *truth claim about physical hardware*. Nothing in the
repository can verify a claim about focus or feeder coverage, so what these tests
protect is the one property that can be checked mechanically: the record must not
say a gate passed unless a human run actually filled it in.

The record is no longer wholly pending. An authorised acceptance run began on
2026-08-05, closed the objective lifecycle, restart and reboot-recovery gates,
started the continuous soak and recorded the zero-hour checkpoint. That makes the
failure mode sharper rather than softer: a record with real passes in it reads as
authoritative, so the gates that are still open have to be defended individually.

These tests therefore fail if the in-progress record claims either time gate, if
a checkpoint that has not happened is filled in, if the 48-hour stability claim
is made from the 24-hour gate, if the sign-off is filled in on Matthew's behalf,
if a populated hardware fact carries no attribution, or if image evidence,
credentials, configuration contents or production filesystem paths reach the
committed record.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_DOCS = Path(__file__).resolve().parents[1] / "docs"
_GUIDE = _DOCS / "Camera-Acceptance.md"
_RECORD = _DOCS / "acceptance" / "Initial-Camera-Acceptance.md"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _flat(path: Path) -> str:
    """Return the document with all runs of whitespace collapsed to one space.

    Markdown wraps prose across lines, so assertions about a *sentence* must not
    depend on where the wrap happens to fall.
    """
    return " ".join(_text(path).split())


# --- the procedure ----------------------------------------------------------


def test_the_acceptance_guide_exists_and_is_referenced() -> None:
    """The task record and the README both point at one authoritative guide."""
    assert _GUIDE.is_file()

    readme = (_DOCS.parent / "README.md").read_text(encoding="utf-8")
    assert "docs/Camera-Acceptance.md" in readme


def test_the_guide_separates_software_checks_from_human_acceptance() -> None:
    """A green test suite must never be presented as physical acceptance."""
    flat = _flat(_GUIDE)

    assert "A pass in one category never implies a pass in another." in flat
    assert "Matthew's visual acceptance" in flat
    assert "Privacy cannot be auto-approved" in flat


def test_the_guide_distinguishes_the_24_and_48_hour_gates() -> None:
    """The two time gates are different claims and are documented as such."""
    flat = _flat(_GUIDE)

    assert "CAMERA BRING-UP PASSED" in flat
    assert "CAMERA PIPELINE STABLE" in flat
    assert (
        "A 24-hour result must never be extrapolated into this claim." in flat
    )


def test_the_guide_forbids_hot_plugging_and_impact_testing() -> None:
    """Hardware-safety rules are stated, not left to the operator's judgement."""
    flat = _flat(_GUIDE)

    assert "not** hot-pluggable" in flat or "Do not hot-plug." in flat
    assert (
        "Do not deliberately strike the camera, the glass, the Pi or the "
        "ribbon cable." in flat
    )


def test_the_guide_adds_no_camera_tuning_flag_to_production() -> None:
    """The procedure assesses current defaults before any flag is considered."""
    flat = _flat(_GUIDE)

    assert (
        "MGO adds none of these to its production commands in Task 12." in flat
    )


# --- evidence commands must fail closed -------------------------------------
#
# An acceptance gate is only as trustworthy as the command that evidences it.
# ``curl -s localhost:8080/...`` prints a 404 body and exits 0, and honours a
# proxy variable that could send the check to another host entirely -- so it can
# report a pass for an endpoint that answered nothing useful, or for a machine
# that is not the Pi.


def _shell_lines() -> list[str]:
    """Return every line inside a ```bash fence in the guide.

    Prose that merely *mentions* a command (including the warning against a
    bare ``curl -s localhost``) is deliberately excluded: these checks are about
    what an operator would actually run.
    """
    lines: list[str] = []
    in_shell = False
    for raw in _text(_GUIDE).splitlines():
        if raw.startswith("```"):
            in_shell = raw.startswith("```bash")
            continue
        if in_shell and raw.strip():
            lines.append(raw.strip())
    return lines


def _curl_commands() -> list[str]:
    """Return every runnable line of the guide that invokes curl."""
    return [line for line in _shell_lines() if "curl" in line]


def test_the_guide_contains_curl_evidence_commands() -> None:
    """Guard the guard: the checks below must have something to check."""
    assert _curl_commands()


@pytest.mark.parametrize(
    "requirement",
    ["--noproxy '*'", "-fsS", "http://127.0.0.1:"],
)
def test_every_curl_command_fails_closed(requirement: str) -> None:
    """Proxy-disabled, HTTP-failing, literal-loopback -- on every invocation."""
    for command in _curl_commands():
        assert requirement in command, command


def test_no_curl_command_uses_a_resolvable_hostname() -> None:
    """`localhost` needs a lookup; the loopback address does not."""
    for command in _curl_commands():
        assert "localhost" not in command, command


def test_the_guide_explains_why_a_bare_curl_is_unsafe() -> None:
    """The reasoning is written down, so a later edit cannot lose it."""
    flat = _flat(_GUIDE)

    assert "A response body is not a passing endpoint check" in flat
    assert "never a bare `curl -s localhost:...`" in flat


def test_the_preview_process_count_gate_requires_exactly_one() -> None:
    """"At least one" is not the gate: zero and two are both failures."""
    flat = _flat(_GUIDE)

    assert "mgo_preview_count()" in flat
    assert 'pgrep -c -x rpicam-vid' in flat
    assert 'if [ "$n" -eq 1 ]' in flat
    assert "FAIL expected exactly 1 rpicam-vid" in flat
    assert (
        '"at least one" is not the gate' in flat.lower()
        or "at least one\" is not the gate" in flat
    )


def test_the_guide_keeps_its_checks_read_only_by_default() -> None:
    """The only authorised write is the capture the gate deliberately performs."""
    writes = [
        command
        for command in _curl_commands()
        if "-X POST" in command or "--request POST" in command
    ]

    assert writes, "the capture gate must actually issue a capture"
    for command in writes:
        assert "/camera/capture" in command, command


# --- the in-progress record -------------------------------------------------


def test_the_initial_record_is_in_progress() -> None:
    """The record must announce itself as started and unfinished."""
    flat = _flat(_RECORD)

    assert (
        "**Status: IN PROGRESS — immediate physical and lifecycle gates "
        "performed; continuous soak started; 24-hour and 48-hour gates "
        "pending.**" in flat
    )


def test_the_final_gate_status_is_in_progress() -> None:
    """The overall verdict may not be a pass while the soak is still running."""
    flat = _flat(_RECORD)

    assert (
        "**Overall: IN PROGRESS — zero-hour checkpoint recorded; awaiting "
        "timed checkpoints and final Matthew sign-off.**" in flat
    )

    # The two phrasings that would announce a finished run.
    for banned in ("Status: PASSED", "Overall: PASSED", "Overall: COMPLETE"):
        assert banned not in flat, banned


def test_neither_time_gate_is_claimed() -> None:
    """Running is not passing: both time gates stay open while the soak runs."""
    flat = _flat(_RECORD)

    assert "| 24-hour camera bring-up | IN PROGRESS |" in flat
    assert "| 48-hour camera pipeline stability | IN PROGRESS |" in flat

    for banned in (
        "| 24-hour camera bring-up | PASS |",
        "| 24-hour camera bring-up | PASSED |",
        "| 48-hour camera pipeline stability | PASS |",
        "| 48-hour camera pipeline stability | PASSED |",
    ):
        assert banned not in flat, banned

    # Neither claim may appear as a recorded outcome; the only mention of each
    # is the explicit statement of the condition under which it could be made.
    bring_up_mentions = re.findall(r"CAMERA BRING-UP PASSED", _text(_RECORD))
    assert len(bring_up_mentions) == 1, bring_up_mentions
    stability_mentions = re.findall(r"CAMERA PIPELINE STABLE", _text(_RECORD))
    assert len(stability_mentions) == 1, stability_mentions
    assert (
        "`CAMERA PIPELINE STABLE` must not be claimed from the 24-hour result."
        in flat
    )
    assert (
        "`CAMERA BRING-UP PASSED` may be recorded only when this gate actually "
        "passes." in flat
    )


def test_only_the_zero_hour_checkpoint_is_populated() -> None:
    """A checkpoint that has not come round yet cannot have a value.

    The soak table is the easiest place in the record to write a plausible
    number, because every column looks the same and only the clock knows which
    ones are allowed to be filled in.
    """
    text = _text(_RECORD)
    flat = _flat(_RECORD)

    # The zero-hour column carries the values the run actually observed.
    assert "| SOAK_START" not in text  # raw evidence variables are not pasted in
    assert "| UTC time | 2026-08-06T06:35:36+00:00 |" in flat
    assert "| SAST time | 2026-08-06T08:35:36+02:00 |" in flat
    assert "| Service state | active |" in flat
    assert "| `NRestarts` | 0 |" in flat
    assert "| Preview state | running |" in flat

    # Every later checkpoint in the 24-hour table is still unrecorded: four
    # trailing NOT RECORDED cells on each row that has a start value.
    section = flat.split("## 21. Twenty-four-hour checkpoints", 1)[1].split(
        "**Checkpoint times due:**", 1
    )[0]
    rows = [row for row in section.split("|") if row.strip()]
    assert rows, "the 24-hour checkpoint table is missing"
    for item in (
        "UTC time",
        "SAST time",
        "Service state",
        "`MainPID`",
        "Preview state",
        "Temperature",
        "Journal errors",
        "Capture count",
    ):
        pattern = (
            rf"\| {re.escape(item)} \| [^|]+ \| NOT RECORDED \| NOT RECORDED "
            r"\| NOT RECORDED \| NOT RECORDED \|"
        )
        assert re.search(pattern, flat), item

    # The 48-hour table has nothing in it at all.
    forty_eight = flat.split("## 22. Forty-eight-hour checkpoint", 1)[1].split(
        "## 23.", 1
    )[0]
    for item in ("Service state", "`MainPID`", "Database health", "Temperature"):
        assert f"| {item} | NOT RECORDED |" in forty_eight, item

    # Continuity is a claim about a series, and one point is not a series.
    assert "one point is not continuity" in flat


def test_the_future_checkpoint_times_are_recorded_but_not_their_results() -> None:
    """Knowing when a checkpoint falls due is not knowing what it found."""
    flat = _flat(_RECORD)

    for due in (
        "| 1 h | 2026-08-06T07:35:36+00:00 | 2026-08-06T09:35:36+02:00 |",
        "| 24 h | 2026-08-07T06:35:36+00:00 | 2026-08-07T08:35:36+02:00 |",
        "| 48 h | 2026-08-08T06:35:36+00:00 | 2026-08-08T08:35:36+02:00 |",
    ):
        assert due in flat, due


def test_the_managed_preview_values_are_recorded_truthfully() -> None:
    """All three keys are now explicitly true, and how they got there matters.

    Two of them did not exist in the external configuration at all. A record
    saying they were "changed from false" would describe an edit that never
    happened and would hide why the first enablement attempt could not run.
    """
    flat = _flat(_RECORD)

    assert "| `preview.enabled` | `true` |" in flat
    assert "| `preview.auto_start` | `true` — inserted by this run |" in flat
    assert (
        "| `preview.restore_after_capture` | `true` — inserted by this run |"
        in flat
    )
    assert "Both keys were **absent**" in flat
    assert (
        "must not be read as though the keys had previously been present and "
        "set to `false`" in flat
    )


def test_the_runtime_and_evidence_shas_are_distinguished() -> None:
    """The Pi runs one commit; GitHub `main` is two evidence commits ahead.

    Recording only "main" would let a reader conclude the newer commits were
    deployed. They were not, and the difference is the whole reason the record
    has to name both.
    """
    flat = _flat(_RECORD)

    assert (
        "| Production runtime SHA | `938134d4f4963256cd74b5bbf59123abe49e1d5d` |"
        in flat
    )
    assert (
        "| GitHub documentation/evidence `main` | "
        "`4340dff1efa4bd81147bf9bb2eb187d01d3b78c1` |" in flat
    )
    assert "The two-commit difference is evidence-only and was not deployed." in flat
    assert "was **not** installed on the Pi" in flat


def test_the_human_sign_off_is_not_filled_in() -> None:
    """Matthew's decision may never be signed on his behalf."""
    flat = _flat(_RECORD)

    assert "Accepted by: NOT RECORDED" in flat
    assert "Decision: PENDING" in flat
    assert "| Matthew's sign-off | NOT GIVEN |" in flat
    assert (
        "It must not be filled in on his behalf." in flat
    )


def test_optional_gates_are_recorded_as_not_performed() -> None:
    """A deferred test is recorded as not performed, never as passed."""
    flat = _flat(_RECORD)

    assert "## 20. Camera-disconnect test **Status: NOT PERFORMED.**" in flat
    assert (
        "**No partial file after a controlled failure: NOT PERFORMED.**" in flat
    )
    assert "It is not described as passed." in flat

    # The mechanical-stability gate was skipped by an explicit human decision,
    # which is a reason to record it, not a reason to treat it as satisfied.
    assert "**Matthew's mechanical-stability decision:** PENDING" in flat
    assert "It is not recorded as passed" in flat


def test_every_populated_hardware_fact_carries_attribution() -> None:
    """A value with no source cannot be audited, and cannot be trusted.

    This replaces an earlier rule that allowed exactly two pre-populated rows.
    That rule was correct while nobody had been to the window; once Matthew
    supplied real measurements it would have forced true facts back out of the
    record. The durable property is not *how many* values exist but that each
    one says where it came from.
    """
    flat = _flat(_RECORD)

    section = flat.split("## 5. Hardware identity", 1)[1].split("## 6.", 1)[0]
    rows = re.findall(r"\| ([^|]+?) \| ([^|]+?) \| ([^|]+?) \|", section)
    assert rows, "the hardware-identity table is missing"

    unattributed = [
        name.strip()
        for name, value, source in rows
        if "NOT RECORDED" not in value
        and name.strip() != "Field"
        and set(name.strip()) != {"-"}
        and (source.strip() in {"", "—"} or set(source.strip()) == {"-"})
    ]
    assert unattributed == [], unattributed

    # An unknown field stays unknown rather than being guessed from the brief's
    # expectation, and the expectation is written as an expectation.
    assert (
        "| Field of view variant | NOT RECORDED (expected: Standard, not Wide) |"
        in flat
    )
    assert "| Supported sensor modes | NOT RECORDED |" in flat

    # The measurements Matthew supplied are his, and are labelled as his.
    for field in (
        "Ribbon-cable length",
        "Distance from lens to glass",
        "Camera mounting method",
        "Protective lens film removed",
    ):
        pattern = rf"\| {re.escape(field)} \| [^|]+ \| Matthew \|"
        assert re.search(pattern, flat), field

    # A tool that could not answer is recorded as a permission fact, not as a
    # camera result -- the two would look identical to a later reader.
    assert "that is a permission fact, not a camera result" in flat


# --- evidence handling ------------------------------------------------------


def test_no_image_or_binary_evidence_is_committed() -> None:
    """The acceptance directory carries records, never captured imagery."""
    committed = sorted(
        path for path in (_DOCS / "acceptance").iterdir() if path.is_file()
    )

    assert committed, "the acceptance directory is empty"
    for path in committed:
        assert path.suffix == ".md", path.name


@pytest.mark.parametrize(
    "forbidden",
    ["password", "secret", "token", "BEGIN PRIVATE KEY", "Authorization:"],
)
def test_the_record_carries_no_credential_material(forbidden: str) -> None:
    """An acceptance record is shared evidence; it holds no secrets."""
    assert forbidden.lower() not in _text(_RECORD).lower()


def test_the_record_carries_no_absolute_filesystem_paths() -> None:
    """Captures are referenced by archive ID and filename, not by path.

    A committed record that named production paths would disclose deployment
    layout for no acceptance benefit -- the archive ID already identifies the
    evidence.
    """
    text = _text(_RECORD)

    for pattern in (r"/var/lib/", r"/var/log/", r"/etc/[a-z]", r"[A-Z]:\\\\"):
        assert not re.search(pattern, text), pattern


def test_the_record_contains_no_configuration_contents() -> None:
    """The settings the gate turns on are recorded; the file is not copied in.

    The run read the whole production configuration. Only the handful of values
    the acceptance gates actually depend on belong in a shared record -- an
    unrelated tuning value discloses deployment detail for no acceptance
    benefit.
    """
    text = _text(_RECORD)

    # The gate-relevant settings are named with their values.
    assert "| `preview.auto_start` | `true` — inserted by this run |" in text
    assert "| `motion.enabled` | `false` |" in text

    # Nothing else from the file comes with it.
    for unrelated in (
        "busy_timeout_seconds",
        "changed_pixel_ratio_threshold",
        "pixel_difference_threshold",
        "analysis_interval_seconds",
        "startup_timeout_seconds",
        "shutdown_timeout_seconds",
        "data_directory",
        "database_path",
        "log_directory",
        "capture_directory",
    ):
        assert unrelated not in text, unrelated


def test_captures_are_referenced_by_identity_and_never_by_path() -> None:
    """Three real captures are now recorded, which is where paths creep in.

    Each capture the run took has an archive ID, a filename and a digest. The
    absolute path it was written to identifies nothing the ID does not, and is
    the one field that would disclose production layout.
    """
    text = _text(_RECORD)
    flat = _flat(_RECORD)

    for capture_id, filename, digest in (
        (
            "0e2d9a3e-81ba-49a9-a9bc-923f0d6b2b0f",
            "2026-08-05T17-54-30.483486Z.jpg",
            "d349190f73124b2f04fcb06cdd8b80a6a93457d2df7088542121b01f0512e816",
        ),
        (
            "54cd5d9e-29c0-444b-883a-20462b41934b",
            "2026-08-05T17-55-16.391721Z.jpg",
            "37965424aee79dc64ddae6fc27238f78e07fd87ccb81f582dc6244b9635b9fee",
        ),
        (
            "35a1ddd9-b718-4f88-bc55-2df4bbfab32f",
            "2026-08-06T06-34-34.755758Z.jpg",
            "57a9db921fd6f18f826885359e82c876b03f0207aa00e00f44b6ebfb34e8146c",
        ),
    ):
        assert capture_id in flat, capture_id
        assert filename in flat, filename
        assert digest in flat, digest
        # The filename appears on its own, never with a directory in front.
        assert f"/{filename}" not in text, filename

    assert "no filesystem path is recorded" in flat

    # A capture taken for one purpose is not silently reused as evidence for
    # another: the reference rows nobody reviewed stay unrecorded.
    assert (
        "| Privacy / framing reference | NOT RECORDED | NOT RECORDED |" in flat
    )
    assert "| Post-soak capture | NOT RECORDED | NOT RECORDED |" in flat
