"""Operations tooling for Matt's Garden Observatory.

This package holds the *operator-facing* functionality delivered by Task 10:
consistent database backups, backup verification, isolated restore testing,
bounded retention and a privacy-safe diagnostic support bundle.

Everything here is deliberately importable and testable on a machine that is not
the Raspberry Pi. There is no ``systemd``, no ``journalctl``, no ``logrotate``,
no camera, no running service and no production filesystem on the Windows
development machine, so every one of those is treated as an *expected absence*
that degrades into a recorded ``unavailable`` result rather than an exception.
Importing this package touches nothing: no directory is created, no database is
opened, no subprocess runs and no network socket is used.

The package is not imported by the API. Nothing in :mod:`mgo.api` depends on it,
and it registers no route -- backup and support-bundle generation are privileged
operator actions and must not be reachable through the unauthenticated LAN API.
"""

from __future__ import annotations

from mgo.operations.errors import ErrorCode, OperationError
from mgo.operations.events import (
    EventEmitter,
    Severity,
    redact_mapping,
)

__all__ = [
    "ErrorCode",
    "EventEmitter",
    "OperationError",
    "Severity",
    "redact_mapping",
]
