"""Camera-operation coordination for Matt's Garden Observatory.

The camera is a single, exclusively owned device: at any instant either the
preview process or a still capture may use it, never both. Before this module
that rule was enforced *per call site* -- the capture endpoint released preview,
the preview service made start and stop idempotent -- with nothing serialising
the operations against each other. Two requests arriving together, or a lifespan
auto-start racing an early request, could interleave.

:class:`CameraCoordinator` is the one place that answers "who may touch the
camera now". Every camera-*mutating* operation goes through it:

* preview start;
* preview stop;
* still capture (including the optional preview restoration around it);
* application shutdown.

Read-only operations deliberately do **not**: preview status, ``/health`` and
the MJPEG frame stream go straight to :class:`~mgo.camera.preview.PreviewService`
so an operator can always see what the camera is doing, even mid-capture.

The coordinator depends only on :class:`~mgo.camera.capture.CaptureService` and
:class:`~mgo.camera.preview.PreviewService`. It knows nothing about FastAPI, HTTP
status codes, the database, the capture archive, systemd, concrete physical
backends or simulator internals, which keeps it fully testable without hardware.
"""

from __future__ import annotations

import logging
import threading

from mgo.camera.capture import CaptureService
from mgo.camera.exceptions import PreviewError
from mgo.camera.models import CaptureResult
from mgo.camera.preview import PreviewService, PreviewState, PreviewStatus

LOGGER = logging.getLogger(__name__)


class CameraCoordinator:
    """Serialises every camera-mutating operation behind one operation lock.

    ``restore_after_capture`` selects the managed policy described in
    :class:`~mgo.core.config.PreviewConfig`: when it is true and preview was
    *running* as a capture transaction began, a restart is attempted once the
    capture attempt finishes. When it is false the Task 11 behaviour is kept
    exactly -- a capture releases preview and leaves it stopped.
    """

    def __init__(
        self,
        capture_service: CaptureService,
        preview_service: PreviewService,
        *,
        restore_after_capture: bool = False,
    ) -> None:
        self._capture_service = capture_service
        self._preview_service = preview_service
        self._restore_after_capture = restore_after_capture
        # A plain (non-reentrant) mutex, on purpose: nothing inside a held
        # transaction may re-enter a public coordinator method, and a plain lock
        # turns an accidental nesting into an immediate, visible failure rather
        # than a silently unserialised operation. Internal steps therefore call
        # the underlying services directly.
        self._operation_lock = threading.Lock()

    # -- public API --------------------------------------------------------

    def start_preview(self) -> PreviewStatus:
        """Start preview, serialised against every other camera mutation.

        Delegates to :meth:`PreviewService.start`, so idempotence, first-frame
        startup validation and the preview failure model are unchanged; the
        coordinator only guarantees that no capture, stop or shutdown can
        interleave with the start.
        """
        with self._operation_lock:
            return self._preview_service.start()

    def stop_preview(self) -> PreviewStatus:
        """Stop preview, serialised against every other camera mutation.

        Delegates to :meth:`PreviewService.stop` and is therefore idempotent.
        """
        with self._operation_lock:
            return self._preview_service.stop()

    def capture_image(self) -> CaptureResult:
        """Run one still-capture transaction and return the capture's result.

        The transaction records whether preview was genuinely ``RUNNING``,
        releases it so the capture owns the camera exclusively, performs the real
        capture, and -- only when configured *and* only when preview was running
        at entry -- attempts to restore it.

        The capture's own outcome is always the answer: a successful capture is
        returned even if restoration then failed, and a failed capture raises its
        original exception even if restoration also failed. Preview truth is
        reported separately through preview status, never merged into the capture
        result.
        """
        with self._operation_lock:
            # status() reconciles an unexpected process exit first, so this is
            # the *actual* state of the camera rather than a remembered one.
            was_running = (
                self._preview_service.status().state is PreviewState.RUNNING
            )
            # A no-op when preview is not active; keeps the existing exclusive
            # ownership handoff exactly as it was.
            self._preview_service.release_for_capture()

            try:
                # The capture's outcome -- result or exception -- is decided
                # here and is never rewritten below.
                return self._capture_service.capture_image()
            finally:
                # Restoration is attempted after a successful *and* after a
                # failed capture: the camera is free either way. It never
                # raises, so a successful capture is still returned and a failed
                # capture still propagates its own original exception.
                self._restore_preview_if_requested(was_running)

    def shutdown(self) -> None:
        """Stop preview during application shutdown; never leaves an orphan.

        Taking the operation lock makes shutdown wait for an active capture
        transaction (including any restoration) to finish, so shutdown can never
        race a restart back into existence. Idempotent.
        """
        with self._operation_lock:
            self._preview_service.shutdown()

    # -- internal helpers (call with the operation lock held) --------------

    def _restore_preview_if_requested(self, was_running: bool) -> None:
        """Restart preview after a capture, when policy and prior state allow.

        Never raises. A restoration failure leaves preview truthfully in
        ``FAILED`` with its own ``last_error`` and is logged here; it must not
        reach the caller, because the caller asked for a *capture* and a caller
        told "capture failed" would retry a capture that actually succeeded,
        duplicating evidence in the archive.
        """
        if not (self._restore_after_capture and was_running):
            # Either the policy is off, or preview was not running when the
            # transaction began. A capture never starts a preview that the
            # operator had not started.
            return

        try:
            status = self._preview_service.start()
        except PreviewError as exc:
            # Expected preview failure (camera busy, tool absent, no first
            # frame, ...). The service has already settled into FAILED.
            LOGGER.error(
                "Preview restoration after capture failed: %s", exc
            )
            return
        except Exception:
            # An unexpected fault must still not rewrite the capture's outcome,
            # but it is not ordinary hardware absence either: log the full
            # traceback rather than swallowing it silently.
            LOGGER.exception(
                "Preview restoration after capture failed unexpectedly"
            )
            return

        LOGGER.info(
            "Preview restored after capture (state=%s)", status.state.value
        )


__all__ = ["CameraCoordinator"]
