"""The one capture-and-catalogue workflow shared by every capture producer.

Before this module the two halves of "take a picture and record it" lived only
inside the manual ``POST /camera/capture`` route: the route asked the
:class:`~mgo.camera.coordinator.CameraCoordinator` for an image and then asked
the :class:`~mgo.captures.archive.CaptureArchive` to catalogue it. That was
fine while there was exactly one caller. Task 13.1 adds a second one -- the
motion-triggered event-capture worker -- and two independent copies of a
two-step transaction drift: one gains a retry the other does not, one deletes a
JPEG on an archive failure, one archives a capture the other would not have.

:class:`CaptureWorkflow` is therefore the single place that composition lives.
It knows only the coordinator and the archive. It knows nothing about FastAPI,
HTTP status codes, motion monitoring, notification providers, systemd or any
concrete camera backend, so both callers get identical behaviour and the
workflow is fully testable without hardware.

Two properties are deliberate and load-bearing:

* **The camera-operation lock is never held across database work.** The
  coordinator's capture transaction completes -- and releases the camera,
  including any preview restoration -- *before* the archive is touched. SQLite
  work must never be able to stall the camera.
* **A successful JPEG is never deleted because cataloguing failed.** An archive
  failure propagates as the archive's own domain error and the file stays on
  disk for a later reconciliation. Only the capture service removes a file, and
  only when the capture itself failed.
"""

from __future__ import annotations

import logging
from typing import Any

from mgo.camera.coordinator import CameraCoordinator
from mgo.captures.archive import CaptureArchive
from mgo.captures.models import Capture

LOGGER = logging.getLogger(__name__)


class CaptureWorkflow:
    """Captures one still image and catalogues it, exactly once each."""

    def __init__(
        self,
        coordinator: CameraCoordinator,
        archive: CaptureArchive,
    ) -> None:
        self._coordinator = coordinator
        self._archive = archive

    def capture(
        self,
        *,
        extra_metadata: dict[str, Any] | None = None,
    ) -> Capture:
        """Capture one still image, catalogue it, and return the record.

        Blocking: it runs a capture subprocess and a SQLite transaction, so
        callers on an event loop must run it in a worker thread.

        ``extra_metadata`` is forward-compatible structured attribution (for
        example the motion facts behind an automatic capture). It is copied on
        the way in so a caller that reuses or mutates its dictionary afterwards
        cannot change what was -- or is about to be -- persisted.

        The coordinator is invoked exactly once and the archive exactly once.
        There is no retry: a failed attempt raises and the caller decides what
        that means. A capture failure raises the camera domain's own exception
        and *nothing* is archived; an archive failure raises
        :class:`~mgo.captures.archive.CaptureArchiveError` and the captured
        JPEG remains on disk.
        """
        # Copied here, not at the call site: this is the boundary the metadata
        # crosses, so the defensive copy belongs where the guarantee is made.
        metadata = None if extra_metadata is None else dict(extra_metadata)

        # Exactly one camera transaction. Its outcome -- result or exception --
        # is never rewritten below, and the camera is free again the moment it
        # returns.
        result = self._coordinator.capture_image()

        # The capture is complete and verified on disk and the camera-operation
        # lock has been released, so the database work below can neither hold
        # nor contend for the camera.
        record = self._archive.record_capture(result, extra_metadata=metadata)
        LOGGER.info(
            "Capture %s catalogued as %s", record.filename, record.id
        )
        return record


__all__ = ["CaptureWorkflow"]
