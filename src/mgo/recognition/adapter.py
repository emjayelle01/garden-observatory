"""The boundary between the job queue and whatever does the recognising.

The queue knows nothing about models, and a model pipeline knows nothing about
leases, retries or SQLite. :class:`RecognitionAdapter` is the whole contract
between them: given one :class:`~mgo.recognition.models.RecognitionRequest`,
return one :class:`~mgo.recognition.models.RecognitionResult` or raise
:class:`~mgo.recognition.models.RecognitionAdapterError` naming a bounded
category. A later detector/classifier pipeline replaces the development adapter
without any change to queue semantics.

An adapter is always called with no database transaction open. It may take as
long as it needs, calling ``request.renew_lease`` to keep its claim.
"""

from __future__ import annotations

import errno
import os
import stat
from typing import BinaryIO, Protocol

from mgo.recognition.models import (
    RecognitionAdapterError,
    RecognitionErrorCategory,
    RecognitionRequest,
    RecognitionResult,
)

#: ``O_NOFOLLOW`` where the platform has it (every POSIX host, including the
#: Raspberry Pi); ``0`` on Windows, which has no equivalent flag. The catalogue
#: safety boundary has already refused a symlinked target on every platform;
#: the flag closes the window between that check and the open.
_NOFOLLOW: int = getattr(os, "O_NOFOLLOW", 0)

#: Windows opens in text mode unless told otherwise; POSIX has no such flag.
_BINARY: int = getattr(os, "O_BINARY", 0)

#: ``O_NONBLOCK`` where available, so a FIFO swapped in after validation cannot
#: block the open forever waiting for a writer; it then fails the regular-file
#: check. The flag has no effect on reading a regular file.
_NONBLOCK: int = getattr(os, "O_NONBLOCK", 0)


class RecognitionAdapter(Protocol):
    """What the single-job runner needs from a recognition pipeline."""

    @property
    def pipeline_version(self) -> str:
        """The pipeline version this adapter implements.

        The runner claims only jobs recorded for this version, so a job is
        never answered by a pipeline it was not queued for.
        """
        ...

    def recognise(self, request: RecognitionRequest) -> RecognitionResult:
        """Recognise one capture, or raise ``RecognitionAdapterError``."""
        ...


def open_media(request: RecognitionRequest) -> BinaryIO:
    """Open a request's media for reading, refusing anything but the real file.

    The final path component is opened with ``O_NOFOLLOW`` where available, so
    a symlink swapped in after validation fails the open rather than being
    followed. Type and size are then re-checked on the *open descriptor* --
    the file actually being read, not whatever the path names a moment later.

    Raises :class:`RecognitionAdapterError` with ``media_missing``,
    ``unsafe_path``, ``size_mismatch`` or ``unexpected``. Pixel decoding, and
    ``decode_error``, belong to the adapter that reads the stream.
    """
    try:
        descriptor = os.open(
            request.media_path, os.O_RDONLY | _NOFOLLOW | _NONBLOCK | _BINARY
        )
    except FileNotFoundError as exc:
        raise RecognitionAdapterError(RecognitionErrorCategory.MEDIA_MISSING) from exc
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise RecognitionAdapterError(RecognitionErrorCategory.UNSAFE_PATH) from exc
        raise RecognitionAdapterError(RecognitionErrorCategory.UNEXPECTED) from exc

    try:
        status = os.fstat(descriptor)
        if not stat.S_ISREG(status.st_mode):
            raise RecognitionAdapterError(RecognitionErrorCategory.UNSAFE_PATH)
        if status.st_size != request.expected_size_bytes:
            raise RecognitionAdapterError(RecognitionErrorCategory.SIZE_MISMATCH)
    except BaseException:
        os.close(descriptor)
        raise

    return os.fdopen(descriptor, "rb")


__all__ = ["RecognitionAdapter", "open_media"]
