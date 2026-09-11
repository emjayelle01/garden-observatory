"""Which catalogued captures recognition may ever look at.

**The catalogue nominates; the filesystem only vetoes.** This is retention's
rule, and recognition inherits it for the same reason. Work is discovered from
committed ``captures`` rows and nowhere else. Nothing here lists the capture
directory: a file with no catalogue row is not a capture MGO published, and a
directory listing is exactly the thing that must never be able to put a file in
front of a model.

A capture is eligible only when every rule below holds, checked in this order:

1. its ``extra_metadata`` decodes to a JSON object;
2. its ``origin`` is exactly :data:`~mgo.retention.policy.MANAGED_ORIGIN` --
   the same constant retention manages and event capture writes;
3. no ``capture_media_lifecycle`` row exists for it;
4. it was captured at or after the operator-supplied enrolment watermark;
5. its media passes **retention's own** safety boundary --
   :func:`~mgo.retention.service.validate_media_path` and
   :func:`~mgo.retention.service.validate_media_file` are called directly, not
   re-implemented, so recognition can never be weaker than the check that
   governs deletion, and a change to that boundary changes both at once.

Every refusal is per row. Retention fails a whole run closed on one undecodable
row because it deletes files; recognition only declines to look at one, so a
single malformed legacy row must not stop enrolment of every capture after it.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType

from mgo.recognition.models import RecognitionErrorCategory
from mgo.retention.models import RetentionErrorCategory
from mgo.retention.policy import MANAGED_ORIGIN
from mgo.retention.service import validate_media_file, validate_media_path

LOGGER = logging.getLogger(__name__)


class IneligibilityReason(StrEnum):
    """Why reconciliation declined to queue a capture. Reported, never stored."""

    INVALID_RECORD = "invalid_record"
    MALFORMED_METADATA = "malformed_metadata"
    NOT_MOTION_ORIGIN = "not_motion_origin"
    LIFECYCLE_RECORDED = "lifecycle_recorded"
    BEFORE_WATERMARK = "before_watermark"
    UNSAFE_PATH = "unsafe_path"
    MEDIA_MISSING = "media_missing"
    SIZE_MISMATCH = "size_mismatch"
    MEDIA_UNREADABLE = "media_unreadable"


@dataclass(frozen=True)
class CatalogueCapture:
    """One committed catalogue row, as recognition reads it.

    The value columns are typed ``object`` because that is what they are: the
    ``captures`` table is not ``STRICT``, so a damaged or hand-edited row can
    hold ``NULL``, a number or a blob in any of them. Decoding happens in the
    rules below, where a value of the wrong type is a refusal rather than a
    coercion -- ``str(None)`` is a perfectly plausible filename.
    """

    capture_id: str
    filename: object
    absolute_path: object
    captured_at_utc: object
    filesize_bytes: object
    extra_metadata: object
    lifecycle_recorded: bool


@dataclass(frozen=True)
class MediaReference:
    """The three catalogue facts media validation needs, decoded and typed."""

    absolute_path: str
    filename: str
    filesize_bytes: int


#: Retention's refusals, in recognition's vocabulary. A directory, device or
#: socket at a catalogued path is not a different *kind* of problem from a
#: symlink there -- the path does not name the file that was published -- so
#: ``not_regular_file`` folds into ``unsafe_path`` rather than widening the
#: stored vocabulary.
_CATEGORY_FOR_RETENTION_REFUSAL: Mapping[
    RetentionErrorCategory, RecognitionErrorCategory
] = MappingProxyType(
    {
        RetentionErrorCategory.UNSAFE_PATH: RecognitionErrorCategory.UNSAFE_PATH,
        RetentionErrorCategory.NOT_REGULAR_FILE: RecognitionErrorCategory.UNSAFE_PATH,
        RetentionErrorCategory.MEDIA_MISSING: RecognitionErrorCategory.MEDIA_MISSING,
        RetentionErrorCategory.SIZE_MISMATCH: RecognitionErrorCategory.SIZE_MISMATCH,
    }
)

_REASON_FOR_CATEGORY: Mapping[RecognitionErrorCategory, IneligibilityReason] = (
    MappingProxyType(
        {
            RecognitionErrorCategory.UNSAFE_PATH: IneligibilityReason.UNSAFE_PATH,
            RecognitionErrorCategory.MEDIA_MISSING: IneligibilityReason.MEDIA_MISSING,
            RecognitionErrorCategory.SIZE_MISMATCH: IneligibilityReason.SIZE_MISMATCH,
        }
    )
)


def resolve_capture_root(capture_directory: Path) -> Path | None:
    """Return the fully resolved capture root, or ``None`` if it is unusable.

    Retention's containment check compares against a resolved root, so this
    applies the same rule retention applies before a run: the configured
    directory must be absolute and must resolve to an existing directory. A
    relative root would make "inside the capture directory" depend on the
    process's working directory.
    """
    if not capture_directory.is_absolute():
        return None
    resolved = Path(os.path.realpath(capture_directory))
    if not resolved.is_dir():
        return None
    return resolved


def _decode_metadata(raw: object) -> dict[str, object] | None:
    """Return the metadata object, or ``None`` if it is not a JSON object.

    ``ValueError`` covers malformed JSON and also Python's integer-digit limit
    (a damaged row holding a 5000-digit number); ``RecursionError`` covers
    pathological nesting. Either must refuse one row, not end reconciliation.
    """
    if not isinstance(raw, str):
        return None
    try:
        decoded = json.loads(raw)
    except (ValueError, RecursionError):
        return None
    return decoded if isinstance(decoded, dict) else None


def _decode_utc(raw: object) -> datetime | None:
    """Return a stored ISO-8601 timestamp as an aware UTC instant, or ``None``."""
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def decode_media_reference(capture: CatalogueCapture) -> MediaReference | None:
    """Return the capture's media facts, or ``None`` if any is unusable.

    The rules match retention's catalogue decoder: non-empty text for the two
    path columns, and a positive integer that is not a ``bool`` for the size.
    """
    path = capture.absolute_path
    name = capture.filename
    size = capture.filesize_bytes
    if not isinstance(path, str) or not path:
        return None
    if not isinstance(name, str) or not name:
        return None
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
        return None
    return MediaReference(absolute_path=path, filename=name, filesize_bytes=size)


def media_refusal(
    media: MediaReference, *, capture_root: Path
) -> RecognitionErrorCategory | None:
    """Apply retention's media safety boundary. ``None`` means the media is safe.

    ``capture_root`` must come from :func:`resolve_capture_root`.

    The retention validators can raise where the host refuses to answer -- a
    ``stat`` on a file the account cannot reach, a path holding a NUL byte.
    That is not evidence the media is safe, so it is a refusal, reported as
    ``unexpected`` because it is not one of the recognised conditions.
    """
    try:
        refusal = validate_media_path(
            absolute_path=media.absolute_path,
            filename=media.filename,
            capture_root=capture_root,
        )
        if refusal is None:
            refusal = validate_media_file(
                absolute_path=media.absolute_path,
                filesize_bytes=media.filesize_bytes,
            )
    except (OSError, ValueError) as exc:
        LOGGER.warning(
            "Recognition could not validate catalogued media (%s)",
            type(exc).__name__,
        )
        return RecognitionErrorCategory.UNEXPECTED

    if refusal is None:
        return None
    return _CATEGORY_FOR_RETENTION_REFUSAL.get(
        refusal, RecognitionErrorCategory.UNEXPECTED
    )


def evaluate_eligibility(
    capture: CatalogueCapture,
    *,
    capture_root: Path,
    enrolment_watermark: datetime,
) -> IneligibilityReason | None:
    """Return why ``capture`` may not be queued, or ``None`` if it may.

    ``capture_root`` must come from :func:`resolve_capture_root` and
    ``enrolment_watermark`` must be timezone-aware.

    The watermark boundary is inclusive: a capture taken exactly at the
    watermark is enrolled. Captures from before it -- including the protected
    Task 13.2 evidence -- are not backfilled by accident; enrolling them is a
    later, explicit operator decision made by supplying an earlier watermark.
    """
    metadata = _decode_metadata(capture.extra_metadata)
    if metadata is None:
        return IneligibilityReason.MALFORMED_METADATA

    if metadata.get("origin") != MANAGED_ORIGIN:
        return IneligibilityReason.NOT_MOTION_ORIGIN

    if capture.lifecycle_recorded:
        return IneligibilityReason.LIFECYCLE_RECORDED

    captured_at = _decode_utc(capture.captured_at_utc)
    if captured_at is None:
        return IneligibilityReason.INVALID_RECORD
    if captured_at < enrolment_watermark:
        return IneligibilityReason.BEFORE_WATERMARK

    media = decode_media_reference(capture)
    if media is None:
        return IneligibilityReason.INVALID_RECORD

    refusal = media_refusal(media, capture_root=capture_root)
    if refusal is None:
        return None
    return _REASON_FOR_CATEGORY.get(refusal, IneligibilityReason.MEDIA_UNREADABLE)


__all__ = [
    "CatalogueCapture",
    "IneligibilityReason",
    "MediaReference",
    "decode_media_reference",
    "evaluate_eligibility",
    "media_refusal",
    "resolve_capture_root",
]
