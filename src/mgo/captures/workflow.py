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
import os
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Any

from mgo.camera.coordinator import CameraCoordinator
from mgo.camera.models import CaptureResult
from mgo.captures.archive import CaptureArchive
from mgo.captures.models import Capture

LOGGER = logging.getLogger(__name__)


class CapturePublicationRefused(Exception):
    """A publication guard declined to catalogue a captured file.

    Raised only by a caller-supplied guard (Task 14.5: the automatic-capture
    size ceiling). The workflow answers it by removing the file the *same
    attempt* just created -- never any pre-existing media -- and re-raising, so
    the refused capture leaves no image and no catalogue row behind.
    ``discarded`` records whether that removal succeeded; ``None`` until the
    workflow has tried.
    """

    discarded: bool | None = None


#: Inspects a verified capture *before* it is catalogued. It may raise
#: :class:`CapturePublicationRefused`; anything else it raises is a defect and
#: propagates unchanged.
PublicationGuard = Callable[[CaptureResult], None]


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
        publication_guard: PublicationGuard | None = None,
    ) -> Capture:
        """Capture one still image, catalogue it, and return the record.

        ``publication_guard`` (Task 14.5) runs between the camera transaction
        and the archive write. If it raises
        :class:`CapturePublicationRefused`, the freshly captured file is
        removed and the refusal propagates; nothing is catalogued. It is the
        one place a size ceiling can be enforced exactly, because it is the
        only moment the real file size is known and nothing durable has yet
        been written about it.

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

        if publication_guard is not None:
            try:
                publication_guard(result)
            except CapturePublicationRefused as refusal:
                refusal.discarded = self._discard_refused(result.absolute_path)
                raise

        # The capture is complete and verified on disk and the camera-operation
        # lock has been released, so the database work below can neither hold
        # nor contend for the camera.
        record = self._archive.record_capture(result, extra_metadata=metadata)
        LOGGER.info(
            "Capture %s catalogued as %s", record.filename, record.id
        )
        return record

    @staticmethod
    def _discard_refused(path: Path) -> bool:
        """Remove the file a refused capture just produced. Never raises.

        This is the single exception to "a successful JPEG is never deleted",
        and it is narrow on purpose: the file was created by *this* attempt,
        milliseconds ago, has no catalogue row and was refused publication.
        The path is the one the capture service built from the configured
        capture directory and a fixed-format timestamp name, so it is beneath
        the capture root by construction; what is checked here (Task 14.5A)
        is that the object at that path is still a plain regular file --
        never a symlink, never a directory -- because ``unlink`` is the one
        destructive call in this module and it must not be aimed at anything
        this attempt did not make.

        Returns whether the file is gone. A failure is logged at error level;
        the refusal still propagates, the orphan stays counted by the durable
        reservation and by the free-space probe, and it is visible on disk
        for reconciliation.
        """
        try:
            details = os.lstat(path)
        except FileNotFoundError:
            return True
        except OSError:
            LOGGER.error(
                "Could not inspect refused capture %s; leaving it in place",
                path,
                exc_info=True,
            )
            return False
        if not stat.S_ISREG(details.st_mode):
            LOGGER.error(
                "Refused capture %s is not a regular file; refusing to remove it",
                path,
            )
            return False
        try:
            path.unlink()
        except OSError:
            LOGGER.error(
                "Could not remove refused capture %s; it remains counted",
                path,
                exc_info=True,
            )
            return False
        LOGGER.warning("Removed refused capture %s before cataloguing", path)
        return True


__all__ = ["CapturePublicationRefused", "CaptureWorkflow", "PublicationGuard"]
