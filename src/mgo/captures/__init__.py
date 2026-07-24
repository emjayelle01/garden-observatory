"""Capture-archive package for Matt's Garden Observatory.

This package owns the persistent *catalogue* of captures: the metadata-only
record created for every successful capture. It is deliberately separate from
``mgo.camera`` (which produces the image) so the hardware-facing capture
pipeline stays unchanged and free of persistence concerns.

* :mod:`mgo.captures.models` -- the :class:`Capture` SQLModel record and its
  JSON serialisers;
* :mod:`mgo.captures.archive` -- the :class:`CaptureArchive` that records and
  retrieves capture metadata.
"""

from __future__ import annotations

from mgo.captures.archive import CaptureArchive, CaptureArchiveError
from mgo.captures.models import Capture, capture_detail, capture_summary

__all__ = [
    "Capture",
    "CaptureArchive",
    "CaptureArchiveError",
    "capture_detail",
    "capture_summary",
]
