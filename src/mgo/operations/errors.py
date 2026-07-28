"""Stable error codes for MGO operations tooling.

An operator diagnosing a failed backup at 02:30 needs a value they can search
for, compare between runs and match against documentation. An exception message
is none of those things: it is prose, it changes when the code changes, and it
may embed a path or an SQLite detail that differs between machines.

Every *expected* operational failure therefore carries a code from
:class:`ErrorCode` **and** a human-readable message. The code is the contract;
the message is the explanation. Neither replaces the other, and the code is
never derived from the exception text.

``error_code`` is ``None`` for success. That is deliberate: an event stream in
which success carries a code named ``OK`` invites filtering on the presence of
the field rather than its value, and makes "did anything fail?" a string
comparison instead of a null check.
"""

from __future__ import annotations

from enum import StrEnum


class ErrorCode(StrEnum):
    """Stable, machine-readable identifiers for expected failures.

    Values are the identifier text itself, so a code survives JSON
    serialisation, a log grep and a documentation table unchanged. Codes are
    only ever *added*: removing or renaming one would break an operator's
    saved search and any troubleshooting note that references it.
    """

    # --- backup ------------------------------------------------------------
    BACKUP_SOURCE_UNAVAILABLE = "BACKUP_SOURCE_UNAVAILABLE"
    BACKUP_LOCKED = "BACKUP_LOCKED"
    BACKUP_DESTINATION_UNWRITABLE = "BACKUP_DESTINATION_UNWRITABLE"
    BACKUP_ALREADY_EXISTS = "BACKUP_ALREADY_EXISTS"
    BACKUP_SQLITE_FAILED = "BACKUP_SQLITE_FAILED"
    BACKUP_INTEGRITY_FAILED = "BACKUP_INTEGRITY_FAILED"
    BACKUP_SCHEMA_INCOMPATIBLE = "BACKUP_SCHEMA_INCOMPATIBLE"
    BACKUP_MANIFEST_FAILED = "BACKUP_MANIFEST_FAILED"
    BACKUP_RETENTION_FAILED = "BACKUP_RETENTION_FAILED"
    BACKUP_CHECKSUM_MISMATCH = "BACKUP_CHECKSUM_MISMATCH"
    BACKUP_NOT_FOUND = "BACKUP_NOT_FOUND"

    #: The production configuration could not be captured: missing, not a
    #: regular file, a symlink, unreadable, larger than the permitted bound, or
    #: modified while it was being copied. A recovery set without the
    #: configuration is incomplete, so this fails the whole backup.
    BACKUP_CONFIGURATION_UNAVAILABLE = "BACKUP_CONFIGURATION_UNAVAILABLE"

    #: The three files of a recovery set are not all present, or do not
    #: describe each other. Distinct from a corrupt individual artefact: the
    #: files may each be intact while the set as a whole cannot be restored.
    BACKUP_SET_INCOMPLETE = "BACKUP_SET_INCOMPLETE"

    # --- restore verification ----------------------------------------------
    RESTORE_TEST_FAILED = "RESTORE_TEST_FAILED"
    RESTORE_TARGET_REJECTED = "RESTORE_TARGET_REJECTED"

    #: A restore-test target file already exists. Overwriting it could destroy
    #: whatever the operator put there, so the test refuses before writing.
    RESTORE_TARGET_EXISTS = "RESTORE_TARGET_EXISTS"

    # --- diagnostics --------------------------------------------------------
    DIAGNOSTIC_OUTPUT_UNWRITABLE = "DIAGNOSTIC_OUTPUT_UNWRITABLE"
    DIAGNOSTIC_SOURCE_UNAVAILABLE = "DIAGNOSTIC_SOURCE_UNAVAILABLE"
    DIAGNOSTIC_TIMEOUT = "DIAGNOSTIC_TIMEOUT"
    DIAGNOSTIC_REDACTION_FAILED = "DIAGNOSTIC_REDACTION_FAILED"
    DIAGNOSTIC_ARCHIVE_FAILED = "DIAGNOSTIC_ARCHIVE_FAILED"
    DIAGNOSTIC_LIMIT_EXCEEDED = "DIAGNOSTIC_LIMIT_EXCEEDED"

    # --- shared -------------------------------------------------------------
    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    CONFIGURATION_UNAVAILABLE = "CONFIGURATION_UNAVAILABLE"
    UNEXPECTED_ERROR = "UNEXPECTED_ERROR"


class OperationError(RuntimeError):
    """An expected operational failure carrying a stable :class:`ErrorCode`.

    Raised only for failures the tooling *anticipates* -- a missing source
    database, a held lock, an unwritable destination, a corrupt backup. An
    unanticipated exception is deliberately **not** wrapped at the point it
    occurs: the CLI boundary maps it to
    :attr:`ErrorCode.UNEXPECTED_ERROR` so that the distinction between "we knew
    this could happen" and "we did not" is never lost.

    The message is safe to show an operator: callers construct it from values
    they already deemed reportable, never from raw configuration, environment or
    database contents.
    """

    def __init__(self, code: ErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


__all__ = ["ErrorCode", "OperationError"]
