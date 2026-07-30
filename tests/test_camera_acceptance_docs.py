"""Integrity tests for the camera acceptance procedure and record (Task 12).

The acceptance record is a *truth claim about physical hardware*. Nothing in the
repository can verify a claim about focus or feeder coverage, so what these tests
protect is the one property that can be checked mechanically: the record must not
say a gate passed unless a human run actually filled it in.

They therefore fail if the pending record is marked as accepted, if the 48-hour
stability claim is made from the 24-hour gate, if the sign-off is filled in on
Matthew's behalf, or if image evidence, credentials or production filesystem
paths reach the committed record.
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


# --- the pending record -----------------------------------------------------


def test_the_initial_record_is_pending() -> None:
    """The record must announce itself as not yet performed."""
    flat = _flat(_RECORD)

    assert (
        "**Status: PENDING — procedure implemented; physical acceptance not "
        "yet performed.**" in flat
    )


def test_the_final_gate_status_is_pending() -> None:
    """The overall verdict may not be a pass while the run has not happened."""
    flat = _flat(_RECORD)

    assert (
        "**Overall: PENDING — procedure implemented; physical acceptance not "
        "yet performed.**" in flat
    )


def test_neither_time_gate_is_claimed() -> None:
    """Neither the 24-hour nor the 48-hour gate may be recorded as passed."""
    flat = _flat(_RECORD)

    assert "| 24-hour camera bring-up | PENDING |" in flat
    assert "| 48-hour camera pipeline stability | PENDING |" in flat
    # The stability claim must not appear as a recorded outcome anywhere; the
    # only mention is the explicit warning against making it early.
    stability_mentions = re.findall(r"CAMERA PIPELINE STABLE", _text(_RECORD))
    assert len(stability_mentions) == 1, stability_mentions
    assert (
        "`CAMERA PIPELINE STABLE` must not be claimed from the 24-hour result."
        in flat
    )


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


def test_the_record_pre_populates_only_supported_facts() -> None:
    """Known facts are attributed; unknown physical facts stay unrecorded."""
    flat = _flat(_RECORD)

    # Attributed to the completed Task 11 deployment, and labelled as such.
    assert (
        "| Detected sensor | Sony IMX708 / Camera Module 3 Standard (recorded "
        "after Task 11) |" in flat
    )
    # Physical measurements nobody has taken are absent, not guessed.
    for field in (
        "Distance from lens to glass",
        "Distance to feeder 1",
        "Ribbon-cable length",
        "Protective lens film removed",
    ):
        assert f"| {field} | NOT RECORDED |" in flat


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
    """Configuration values are not copied into the record."""
    text = _text(_RECORD)

    # Setting *names* are referenced (they are what was configured); their
    # values are recorded as NOT RECORDED until the run reads them.
    assert "| `preview.auto_start` | NOT RECORDED |" in text
    assert "busy_timeout_seconds" not in text
    assert "changed_pixel_ratio_threshold" not in text
