"""Immutable result types (design doc §5.5).

Paths come in three forms, so callers never translate between them:
``asset_path`` (relative to the project root, with '/'), ``depot_path``
(Perforce syntax) and ``local_path``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path

__all__ = [
    "Change",
    "FileState",
    "FileStatus",
    "Freshness",
    "HistoryPage",
    "Lock",
    "Revision",
    "SaveResult",
    "SyncPlan",
    "SyncReport",
    "SyncedFile",
]


class FileState(Enum):
    """How a local file relates to the server, in artist terms."""

    SYNCED = "synced"  # the latest version is on disk
    OUT_OF_DATE = "out_of_date"  # an older version is on disk
    MISSING = "missing"  # in the project, not on disk
    MODIFIED = "modified"  # changed on disk without being locked/opened
    NEW = "new"  # on disk, not in Perforce yet
    DELETED = "deleted"  # removed from disk without Perforce knowing
    EDITING = "editing"  # opened for edit (locked or not)
    ADDING = "adding"  # opened for add
    DELETING = "deleting"  # opened for delete


@dataclass(frozen=True, slots=True)
class FileStatus:
    asset_path: str
    depot_path: str
    local_path: Path
    state: FileState
    have_rev: int | None = None
    head_rev: int | None = None
    head_change: int | None = None
    #: This workspace holds the lock.
    locked_by_me: bool = False
    #: "user@workspace" of whoever else holds a lock, if anyone.
    locked_by: str | None = None
    #: "user@workspace" of everyone else who has the file open.
    opened_by: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SyncedFile:
    asset_path: str
    depot_path: str
    rev: int | None
    action: str
    size: int | None


@dataclass(frozen=True, slots=True)
class SyncPlan:
    """What ``get(preview=True)`` would transfer. Nothing was changed."""

    file_count: int
    bytes_to_transfer: int
    files: tuple[SyncedFile, ...] = ()


@dataclass(frozen=True, slots=True)
class SyncReport:
    """What ``get`` did."""

    file_count: int
    bytes_transferred: int
    #: Perforce warnings that are worth showing, such as paths with no files.
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Lock:
    asset_path: str
    depot_path: str
    user: str
    workspace: str


@dataclass(frozen=True, slots=True)
class Change:
    number: int
    description: str
    user: str
    workspace: str
    time: datetime | None = None
    #: Depot paths of the files in the change, when known.
    files: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SaveResult:
    change: Change
    added: tuple[str, ...] = ()
    edited: tuple[str, ...] = ()
    deleted: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Revision:
    depot_path: str
    rev: int
    change: int
    action: str
    user: str
    workspace: str
    time: datetime
    description: str
    file_type: str
    size: int | None = None
    digest: str | None = None


class Freshness(Enum):
    FRESH = "fresh"  # checked against the server within the polling interval
    STALE = "stale"  # from the cache, not re-checked (the server was not asked)


@dataclass(frozen=True, slots=True)
class HistoryPage[T]:
    items: tuple[T, ...]
    freshness: Freshness
    #: Time since the data was last checked against the server.
    age: timedelta = field(default_factory=timedelta)
    #: Pass as ``before`` to get the next (older) page; None at the end.
    next_cursor: int | None = None
