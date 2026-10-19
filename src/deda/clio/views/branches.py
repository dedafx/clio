"""Branch views (design doc §9.1, "Branch view"; §7).

* ``BranchListView``: the project's branches with how each relates to the one
  you are on (parent, child, sibling), and how far behind its parent your
  branch is.
* ``BranchDiffView``: which files differ between a branch and another (by
  default its parent): changed in both, only on this branch, only on the other.

Both are display only: switching, updating and publishing ask the server
themselves (``session.branches``).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import Future
from dataclasses import dataclass
from enum import Enum

from deda.clio._paths import unescape
from deda.clio.models import Branch, Freshness
from deda.clio.views._pipeline import PRIORITY_VISIBLE, Signal
from deda.clio.views._polling import Polling

__all__ = ["BranchDiffRow", "BranchDiffView", "BranchListView", "BranchRow", "DiffKind", "Relation"]


class Relation(Enum):
    """How a branch relates to the one the workspace is on."""

    CURRENT = "current"
    PARENT = "parent"
    CHILD = "child"
    SIBLING = "sibling"
    OTHER = "other"


@dataclass(frozen=True, slots=True)
class BranchRow:
    branch: Branch
    relation: Relation


def _relation(branch: Branch, current: Branch | None) -> Relation:
    if current is None:
        return Relation.OTHER
    if branch.path == current.path:
        return Relation.CURRENT
    if branch.path == current.parent:
        return Relation.PARENT
    if branch.parent == current.path:
        return Relation.CHILD
    if current.parent is not None and branch.parent == current.parent:
        return Relation.SIBLING
    return Relation.OTHER


class BranchListView(Polling):
    """The branches of the project.

    ``reset()`` when the set of branches changes, ``rows_changed(first,
    last)`` when rows changed (a branch was renamed, or the one you are on
    changed), ``behind_changed()`` when the number of files your branch is
    behind its parent is known or changes, ``failed(error)``.
    """

    def __init__(self, session, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.session = session
        self.reset = Signal()
        self.rows_changed = Signal()
        self.behind_changed = Signal()
        self.failed = Signal()
        self.error: BaseException | None = None
        self._clock = clock
        self._lock = threading.RLock()
        self._rows: list[BranchRow] = []
        self._behind: tuple[str, ...] | None = None
        self._fetched = 0.0
        self._closed = False
        self._pipeline = session.pipeline
        self.ready: Future = Future()
        self.refresh()

    # --- the model ------------------------------------------------------------

    def __len__(self) -> int:
        with self._lock:
            return len(self._rows)

    def __getitem__(self, index: int) -> BranchRow:
        with self._lock:
            return self._rows[index]

    def __iter__(self) -> Iterator[BranchRow]:
        for i in range(len(self)):
            try:
                yield self[i]
            except IndexError:
                return

    @property
    def current(self) -> BranchRow | None:
        with self._lock:
            return next((r for r in self._rows if r.relation is Relation.CURRENT), None)

    @property
    def behind(self) -> tuple[str, ...] | None:
        """Depot paths the parent changed since your branch was last updated;
        empty when up to date, ``None`` before it is known (and for a
        mainline, which has no parent)."""
        with self._lock:
            return self._behind

    @property
    def freshness(self) -> Freshness:
        with self._lock:
            if not self.ready.done():
                return Freshness.LOADING
            age = self._clock() - self._fetched
            return Freshness.FRESH if age <= self.session.config.poll_interval else Freshness.STALE

    # --- asking -----------------------------------------------------------------

    def refresh(self) -> Future:
        """Ask the server for the branches (one ``streams``) and, for a
        branch that has a parent, how far behind it is (one ``merge -n``)."""
        future = self._pipeline.request("branches", "all", PRIORITY_VISIBLE)
        future.add_done_callback(self._on_branches)
        return future

    def _tick(self) -> None:
        self.refresh()

    def _on_branches(self, future: Future) -> None:
        if future.cancelled() or self._closed:
            return
        try:
            branches: list[Branch] = future.result()
        except BaseException as e:
            self.error = e
            if not self.ready.done():
                self.ready.set_exception(e)
            self.failed.emit(e)
            return
        current = next((b for b in branches if b.is_current), None)
        rows = [BranchRow(b, _relation(b, current)) for b in branches]
        with self._lock:
            old = self._rows
            same = [r.branch.path for r in old] == [r.branch.path for r in rows]
            changed = [i for i, (a, b) in enumerate(zip(old, rows)) if a != b] if same else []
            self._rows, self._fetched = rows, self._clock()
        first = not self.ready.done()
        if first:
            self.ready.set_result(None)
        if first or not same:
            self.reset.emit()
        else:
            for i in changed:
                self.rows_changed.emit(i, i)
        if current is not None and current.parent is not None:
            behind = self._pipeline.request("behind", current.path, PRIORITY_VISIBLE)
            behind.add_done_callback(self._on_behind)
        elif self._behind is not None:
            with self._lock:
                self._behind = None
            self.behind_changed.emit()

    def _on_behind(self, future: Future) -> None:
        if future.cancelled() or self._closed:
            return
        try:
            paths = tuple(future.result() or ())
        except BaseException as e:
            self.error = e
            self.failed.emit(e)
            return
        with self._lock:
            changed = paths != self._behind
            self._behind = paths
        if changed:
            self.behind_changed.emit()

    def close(self) -> None:
        self._closed = True
        self.stop_polling()

    def __enter__(self) -> BranchListView:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


# --- what differs between two branches ---------------------------------------------


class DiffKind(Enum):
    DIFFERENT = "different"  # in both, with other content
    ONLY_HERE = "only_here"  # only on the branch being looked at
    ONLY_THERE = "only_there"  # only on the other branch


@dataclass(frozen=True, slots=True)
class BranchDiffRow:
    """One file that differs between two branches."""

    asset_path: str
    kind: DiffKind
    #: Revisions on the branch being looked at ("here") and on the other
    #: ("there"); None where the file does not exist.
    here_rev: int | None = None
    there_rev: int | None = None
    here_path: str = ""
    there_path: str = ""


class BranchDiffView(Polling):
    """The files that differ between ``branch`` and ``against`` (one
    ``diff2``). Defaults: the branch you are on, against its parent.

    ``reset()`` when the answer arrives or changes, ``failed(error)``.
    """

    def __init__(self, session, branch: str | None = None, against: str | None = None, *,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.session = session
        branches = session.branches
        self.here = branches.get(branch) if branch else branches.current()
        parent = against or self.here.parent
        if parent is None:
            from deda.clio.errors import WorkflowError

            raise WorkflowError(f"{self.here.name} is a mainline: it has no parent to compare with",
                                hint="Name the other branch to compare with")
        self.there = branches.get(parent)
        self.reset = Signal()
        self.failed = Signal()
        self.error: BaseException | None = None
        self._clock = clock
        self._lock = threading.RLock()
        self._rows: list[BranchDiffRow] = []
        self._fetched = 0.0
        self._closed = False
        self._pipeline = session.pipeline
        self.ready: Future = Future()
        self.refresh()

    def __len__(self) -> int:
        with self._lock:
            return len(self._rows)

    def __getitem__(self, index: int) -> BranchDiffRow:
        with self._lock:
            return self._rows[index]

    def __iter__(self) -> Iterator[BranchDiffRow]:
        for i in range(len(self)):
            try:
                yield self[i]
            except IndexError:
                return

    @property
    def freshness(self) -> Freshness:
        with self._lock:
            if not self.ready.done():
                return Freshness.LOADING
            age = self._clock() - self._fetched
            return Freshness.FRESH if age <= self.session.config.volatile_ttl else Freshness.STALE

    def refresh(self) -> Future:
        """Compare again (one ``diff2``)."""
        future = self._pipeline.request("branchdiff", (self.here.path, self.there.path), PRIORITY_VISIBLE)
        future.add_done_callback(self._on_diff)
        return future

    def _tick(self) -> None:
        self.refresh()

    def _on_diff(self, future: Future) -> None:
        if future.cancelled() or self._closed:
            return
        try:
            records: list[dict[str, str]] = future.result()
        except BaseException as e:
            self.error = e
            if not self.ready.done():
                self.ready.set_exception(e)
            self.failed.emit(e)
            return
        rows = []
        for rec in records:
            kind = {"left only": DiffKind.ONLY_HERE, "right only": DiffKind.ONLY_THERE,
                    "content": DiffKind.DIFFERENT, "types": DiffKind.DIFFERENT}.get(rec.get("status", ""))
            if kind is None:
                continue
            here, there = rec.get("depotFile", ""), rec.get("depotFile2", "")
            relative = (here[len(self.here.path) + 1:] if here else there[len(self.there.path) + 1:])
            rows.append(BranchDiffRow(
                unescape(relative),
                kind, int(rec["rev"]) if rec.get("rev") else None, int(rec["rev2"]) if rec.get("rev2") else None,
                here, there))
        rows.sort(key=lambda r: r.asset_path.casefold())
        with self._lock:
            changed = rows != self._rows or not self.ready.done()
            self._rows, self._fetched = rows, self._clock()
        first = not self.ready.done()
        if first:
            self.ready.set_result(None)
        if changed:
            self.reset.emit()

    def close(self) -> None:
        self._closed = True
        self.stop_polling()

    def __enter__(self) -> BranchDiffView:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
