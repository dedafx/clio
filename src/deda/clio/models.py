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
    "Branch",
    "Change",
    "Conflict",
    "Draft",
    "EditReport",
    "FileState",
    "FileStatus",
    "Freshness",
    "HistoryPage",
    "Lock",
    "PublishResult",
    "Revision",
    "SaveResult",
    "SwitchResult",
    "SyncPlan",
    "SyncReport",
    "SyncedFile",
    "UpdateResult",
]


@dataclass(frozen=True, slots=True)
class EditReport:
    """Files made ready to be written (design doc §10.6)."""

    #: Asset paths open for edit now (including files that already were).
    opened: tuple[str, ...] = ()
    #: Asset paths that are not in Perforce yet. Add them with
    #: ``Workspace.add_new`` once they have been written.
    new: tuple[str, ...] = ()
    #: Things the user should know, for example that a copy is out of date.
    warnings: tuple[str, ...] = ()


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
    #: Who has the same asset open in a related branch, as
    #: "user@workspace (//depot/branch)" (design doc §7.3). Empty when
    #: nobody does, or when the check is off.
    elsewhere: tuple[str, ...] = ()


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
    #: Warnings from validation hooks (errors stop the save).
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Integration:
    """Where a revision's content came from, or went to (a Perforce
    integration record). ``how`` is Perforce's wording: ``branch from``,
    ``copy from``, ``merge from``, ``edit from``, ``add from``, ``delete from``,
    ``ignored``, or the forward form (``copy into``, ``merge into`` ...) on the
    revision that was the source."""

    how: str
    #: The other file, its revisions (0 when Perforce says ``#none``).
    depot_path: str
    start_rev: int
    end_rev: int

    @property
    def into(self) -> bool:
        """The other file received this revision (the forward direction)."""
        return self.how.endswith(" into") or self.how in ("ignored by",)

    @property
    def branch(self) -> str:
        """Name of the branch (stream) the other file is on: the first folder
        under its depot, e.g. ``crate-damage`` for ``//imagine/crate-damage/a.ma``."""
        parts = self.depot_path.split("/")
        return parts[3] if len(parts) > 3 and parts[:2] == ["", ""] else ""


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
    #: Which branch this revision came from (and where it went): empty for an
    #: ordinary edit. Comes with the history, at no extra server call.
    integrations: tuple[Integration, ...] = ()

    @property
    def came_from(self) -> Integration | None:
        """The first record saying this revision's content came from another file."""
        return next((i for i in self.integrations if i.how.endswith(" from") and i.depot_path), None)


@dataclass(frozen=True, slots=True)
class Branch:
    """A Perforce stream."""

    path: str  # //imagine/crate-damage
    name: str  # crate-damage
    parent: str | None  # //imagine/main, None for a mainline
    kind: str  # mainline, development, release, task, virtual
    owner: str = ""
    description: str = ""
    #: This workspace is on this branch.
    is_current: bool = False


@dataclass(frozen=True, slots=True)
class Conflict:
    """A file changed in both the branch and its parent (design doc §7.2)."""

    asset_path: str
    depot_path: str
    #: The parent's version, as "//imagine/main/crate.ma#3".
    theirs: str


@dataclass(frozen=True, slots=True)
class SwitchResult:
    branch: Branch
    report: SyncReport
    #: The draft that holds files that were open before the switch, if any.
    draft: Draft | None = None


@dataclass(frozen=True, slots=True)
class UpdateResult:
    """What ``update`` brought down from the parent. ``change`` is None when
    the branch was already up to date."""

    change: Change | None
    #: Files that changed only in the parent, taken as they are there.
    taken: tuple[str, ...] = ()
    #: Conflicts settled by keeping this branch's version.
    kept_mine: tuple[str, ...] = ()
    #: Conflicts settled by taking the parent's version.
    took_theirs: tuple[str, ...] = ()
    #: Copies of the parent's version written next to the kept file
    #: ("keep both"). They are not in Perforce.
    theirs_copies: tuple[Path, ...] = ()


@dataclass(frozen=True, slots=True)
class PublishResult:
    change: Change
    #: Branch the files went to.
    parent: str
    #: Depot paths in the parent.
    files: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Draft:
    """A shelved change: work shared without saving it."""

    number: int
    description: str
    user: str
    workspace: str
    time: datetime | None = None
    #: Depot paths of the shelved files, when known.
    files: tuple[str, ...] = ()


class Freshness(Enum):
    FRESH = "fresh"  # checked against the server within the polling interval
    STALE = "stale"  # from the cache, not re-checked (the server was not asked)
    LOADING = "loading"  # nothing yet: the first answer is on its way


@dataclass(frozen=True, slots=True)
class HistoryPage[T]:
    items: tuple[T, ...]
    freshness: Freshness
    #: Time since the data was last checked against the server.
    age: timedelta = field(default_factory=timedelta)
    #: Pass as ``before`` to get the next (older) page; None at the end.
    next_cursor: int | None = None
