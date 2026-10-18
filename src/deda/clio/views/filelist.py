"""A file browser's rows (design doc §9.1, §9.5).

``FileListView`` lists the files under a folder with their version, head,
state, lock and size, without a Qt dependency: it maps directly onto a
``QAbstractItemModel`` (``__len__``, ``__getitem__``, ``rows_changed``,
``reset``).

* The whole folder is listed with **one** ``fstat``, and kept for other views
  of the same folder. A view that opens on a cached listing has its rows at
  once.
* The server is asked again only for **visible rows that are stale**, in one
  coalesced ``fstat`` however many rows scroll into view.
* Rows carry a **freshness**, so a UI can show a subtle "updating" mark
  instead of blocking.
* Cached rows are for display. Lock, save and get ask the server themselves.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import Future
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from deda.clio.errors import ClioError
from deda.clio.models import FileState, Freshness
from deda.clio.views._pipeline import PRIORITY_PREFETCH, PRIORITY_SELECTED, PRIORITY_VISIBLE, Signal
from deda.clio.views._polling import Polling
from deda.clio.workspace import _int, _others, file_state, locked_by_other

if TYPE_CHECKING:
    from deda.clio.session import Session

__all__ = ["COLUMNS", "FileListView", "FileRow"]

#: Columns a view can ask for. ``changed_by`` costs a second, cached request
#: (the submitter of the head revision); the others come with the ``fstat``.
COLUMNS = ("version", "head", "state", "locked_by", "changed_by", "changed_at", "size", "type")
DEFAULT_COLUMNS = ("version", "head", "state", "locked_by", "changed_at", "size")


@dataclass(frozen=True, slots=True)
class FileRow:
    """One row of a file list."""

    asset_path: str
    depot_path: str
    local_path: Path
    state: FileState
    have_rev: int | None = None
    head_rev: int | None = None
    head_change: int | None = None
    #: When the head revision was submitted.
    changed_at: datetime | None = None
    #: Who submitted it (``None`` until the ``changed_by`` column is filled).
    changed_by: str | None = None
    size: int | None = None
    file_type: str = ""
    #: Someone else holds a lock (``user@workspace``).
    locked_by: str | None = None
    #: Others who have the file open.
    opened_by: tuple[str, ...] = ()
    #: The head revision is a delete (you may still have the file).
    deleted: bool = False
    freshness: Freshness = Freshness.FRESH

    @property
    def version(self) -> str:
        """``"v12 of 14"`` (or ``"v14"`` when current, ``"-"`` if not in the workspace)."""
        if self.have_rev is None:
            return "-"
        return f"v{self.have_rev}" if self.have_rev == self.head_rev or self.head_rev is None \
            else f"v{self.have_rev} of {self.head_rev}"


class FileListView(Polling):
    """The files under a folder, as rows.

    ``rows_changed(first, last)`` is emitted when rows in that range changed
    (or became fresh), ``reset()`` when the set of rows changed, and
    ``failed(error)`` when the server could not be asked. Slots run on a
    pipeline thread, not the UI thread.
    """

    def __init__(
        self,
        session: Session,
        path: str = "",
        columns: Sequence[str] = DEFAULT_COLUMNS,
        *,
        volatile_ttl: float | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        unknown = [c for c in columns if c not in COLUMNS]
        if unknown:
            raise ValueError(f"Unknown column(s) {', '.join(unknown)}; choose from {', '.join(COLUMNS)}")
        self.session = session
        self.columns = tuple(columns)
        self.rows_changed = Signal()
        self.reset = Signal()
        self.failed = Signal()
        self.error: BaseException | None = None
        self._ttl = session.config.volatile_ttl if volatile_ttl is None else volatile_ttl
        self._clock = clock
        self._lock = threading.RLock()
        self._rows: list[FileRow] = []
        self._index: dict[str, int] = {}
        self._fetched: dict[str, float] = {}
        self._visible = (0, -1)
        self._listing_at = 0.0
        self._closed = False
        self._pipeline = session.pipeline
        self._cache = session.history.cache

        targets = session.workspace.targets([path] if path else [])
        self._scope = targets[0].depot_spec(session.paths.depot_root)
        self.ready: Future = Future()

        with self._cache.lock:
            hit = self._cache.listings.get(self._scope)
        if hit is not None:
            self._set_rows(hit[0], hit[1], emit=False)
            self.ready.set_result(None)
        else:
            self._request_listing()

    # --- the model ----------------------------------------------------------

    def __len__(self) -> int:
        with self._lock:
            return len(self._rows)

    def __getitem__(self, index: int) -> FileRow:
        with self._lock:
            row = self._rows[index]
            return replace(row, freshness=self._freshness_of(row.depot_path))

    def __iter__(self) -> Iterator[FileRow]:
        for i in range(len(self)):
            try:
                yield self[i]
            except IndexError:  # the list shrank while iterating
                return

    def index_of(self, asset_path: str) -> int | None:
        """The row of a file, or None."""
        depot = self.session.paths.depot_path(asset_path)
        with self._lock:
            return self._index.get(depot)

    @property
    def freshness(self) -> Freshness:
        """``LOADING`` before the first answer; ``STALE`` if any visible row is
        older than the time-to-live; otherwise ``FRESH``."""
        with self._lock:
            if not self.ready.done():
                return Freshness.LOADING
            first, last = self._visible
            rows = self._rows[first:last + 1] if last >= first else self._rows
            return Freshness.STALE if any(
                self._freshness_of(r.depot_path) is Freshness.STALE for r in rows) else Freshness.FRESH

    def _freshness_of(self, depot: str) -> Freshness:
        return Freshness.FRESH if self._clock() - self._fetched.get(depot, 0.0) <= self._ttl else Freshness.STALE

    # --- asking for rows ------------------------------------------------------

    def set_visible_range(self, first: int, last: int) -> list[Future]:
        """Say which rows are on screen (inclusive). Stale rows among them are
        asked for in one coalesced request, and the rows just below are
        prefetched. Requests for rows that scrolled away before they started
        are dropped. Returns the futures, for callers that want to wait."""
        with self._lock:
            n = len(self._rows)
            first, last = max(0, first), min(n - 1, last)
            self._visible = (first, last)
            if last < first or self._closed:
                return []
            span = last - first + 1
            wanted = self._stale(first, last)
            ahead = self._stale(last + 1, min(n - 1, last + span))
        keep = set(wanted) | set(ahead)
        self._pipeline.cancel_where("rows", lambda depot: depot in keep)
        keys = [*wanted, *ahead]
        futures = [self._pipeline.request("rows", d, PRIORITY_VISIBLE) for d in wanted]
        futures += [self._pipeline.request("rows", d, PRIORITY_PREFETCH) for d in ahead]
        self._when_all(futures, lambda done: self._on_rows(keys, done))
        self._request_submitters(first, last)
        return futures

    def _stale(self, first: int, last: int) -> list[str]:
        return [self._rows[i].depot_path for i in range(first, last + 1)
                if self._freshness_of(self._rows[i].depot_path) is Freshness.STALE]

    def refresh(self) -> Future:
        """Ask for the whole listing again (one ``fstat``), whatever the
        time-to-live says. Rows that changed are announced."""
        return self._request_listing(PRIORITY_SELECTED)

    def _request_listing(self, priority: int = PRIORITY_VISIBLE) -> Future:
        future = self._pipeline.request("listing", self._scope, priority)
        future.add_done_callback(self._on_listing)
        return future

    # --- answers --------------------------------------------------------------

    def _when_all(self, futures: list[Future], then: Callable[[list[Future]], None]) -> None:
        """Call ``then(futures)`` once, when every future is done."""
        if not futures:
            return
        remaining = len(futures)
        lock = threading.Lock()

        def one_done(_: Future) -> None:
            nonlocal remaining
            with lock:
                remaining -= 1
                last = remaining == 0
            if last:
                then(futures)

        for future in futures:
            future.add_done_callback(one_done)

    def _on_listing(self, future: Future) -> None:
        if future.cancelled() or self._closed:
            return
        try:
            records, _ = future.result()
        except BaseException as e:
            self.error = e
            if not self.ready.done():
                self.ready.set_exception(e)
            self.failed.emit(e)
            return
        # Stamp with this view's clock (the same as the pipeline's in real use).
        self._set_rows(records, self._clock(), emit=True)
        if not self.ready.done():
            self.ready.set_result(None)

    def _make_row(self, depot: str, rec: dict[str, str]) -> FileRow | None:
        state = file_state(rec)
        if state is None:
            return None
        s = self.session
        asset = s.paths.asset_path_of_depot(depot)
        head_time = rec.get("headTime")
        revision = self._cache.revisions.get((depot, _int(rec.get("headRev")) or 0))
        holder = locked_by_other(rec)
        return FileRow(
            asset, depot, s.paths.local_path(asset), state, _int(rec.get("haveRev")), _int(rec.get("headRev")),
            _int(rec.get("headChange")), datetime.fromtimestamp(int(head_time)) if head_time else None,
            revision.user if revision else None, _int(rec.get("fileSize")), rec.get("headType", ""),
            holder, _others(rec, "otherOpen"), rec.get("headAction") in ("delete", "move/delete"),
        )

    def _set_rows(self, records: dict[str, dict[str, str]], fetched: float, *, emit: bool) -> None:
        """Replace the rows with a fresh listing, announcing what changed."""
        built = [r for depot, rec in records.items() if (r := self._make_row(depot, rec)) is not None]
        built.sort(key=lambda r: r.asset_path.casefold())
        with self._lock:
            old = self._rows
            same_files = [r.depot_path for r in old] == [r.depot_path for r in built]
            changed = [i for i, (a, b) in enumerate(zip(old, built)) if a != b] if same_files else []
            self._rows = built
            self._index = {r.depot_path: i for i, r in enumerate(built)}
            self._fetched = dict.fromkeys(self._index, fetched)
            self._listing_at = fetched
            first, last = self._visible
        if not emit:
            return
        if not same_files:
            self.reset.emit()
        else:
            for start, end in _runs(changed):
                self.rows_changed.emit(start, end)
        self._request_submitters(first, last)

    def _on_rows(self, keys: list[str], futures: list[Future]) -> None:
        if self._closed:
            return
        answers: dict[str, tuple[dict[str, str] | None, float]] = {}
        for depot, future in zip(keys, futures):
            if future.cancelled():
                continue
            try:
                answer = future.result()
            except BaseException as e:
                self.error = e
                self.failed.emit(e)
                continue
            if answer is not None:
                answers[depot] = answer  # (record, fetched); a None record: gone from the server
        self._apply(answers)

    def _apply(self, answers: dict[str, tuple[dict[str, str] | None, float]]) -> None:
        changed: list[int] = []
        removed = False
        with self._lock:
            for depot, (record, fetched) in answers.items():
                i = self._index.get(depot)
                if i is None:
                    continue
                row = self._make_row(depot, record) if record else None
                if row is None:
                    removed = True
                    self._rows[i] = None  # type: ignore[assignment]  # dropped below
                    continue
                if row != self._rows[i]:
                    self._rows[i] = row
                    changed.append(i)
                elif self._freshness_of(depot) is Freshness.STALE:
                    changed.append(i)  # unchanged, but no longer stale: let the UI clear its mark
                self._fetched[depot] = self._clock()
                self._cache_record(depot, record)
            if removed:
                self._rows = [r for r in self._rows if r is not None]
                self._index = {r.depot_path: i for i, r in enumerate(self._rows)}
            first, last = self._visible
        if removed:
            self.reset.emit()
        else:
            for start, end in _runs(sorted(changed)):
                self.rows_changed.emit(start, end)
        self._request_submitters(first, last)

    def _cache_record(self, depot: str, record: dict[str, str]) -> None:
        with self._cache.lock:
            hit = self._cache.listings.get(self._scope)
            if hit is not None:
                hit[0][depot] = record

    # --- who submitted the head revision -----------------------------------------

    def _request_submitters(self, first: int, last: int) -> None:
        """Fill the ``changed_by`` column for visible rows: the submitter of a
        revision never changes, so each is asked for once and kept."""
        if "changed_by" not in self.columns or self._closed:
            return
        with self._lock:
            if last < first:
                return
            keys = [(r.depot_path, r.head_rev) for r in self._rows[first:last + 1]
                    if r.head_rev is not None and r.changed_by is None]
        futures = [self._pipeline.request("revision", key, PRIORITY_SELECTED) for key in keys]
        self._when_all(futures, lambda done: self._on_submitters(keys, done))

    def _on_submitters(self, keys: list[tuple[str, int]], futures: list[Future]) -> None:
        changed: list[int] = []
        with self._lock:
            for key, future in zip(keys, futures):
                if future.cancelled() or future.exception() is not None or future.result() is None:
                    continue
                i = self._index.get(key[0])
                if i is not None and self._rows[i].head_rev == key[1]:
                    self._rows[i] = replace(self._rows[i], changed_by=future.result().user)
                    changed.append(i)
        for start, end in _runs(sorted(changed)):
            self.rows_changed.emit(start, end)

    # --- keeping up ----------------------------------------------------------------

    def _tick(self) -> None:
        # The listing is one cheap command, so a stale one is asked for whole
        # (it also finds files that others added); otherwise only stale rows.
        if self._clock() - self._listing_at > self.session.config.poll_interval:
            self.refresh()
        else:
            self.set_visible_range(*self._visible)

    def close(self) -> None:
        """Stop: drop what has not started."""
        self._closed = True
        self.stop_polling()
        self._pipeline.cancel_where("rows", lambda depot: depot not in self._index)

    def __enter__(self) -> FileListView:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _runs(indices: Sequence[int]) -> list[tuple[int, int]]:
    """Consecutive indices as (first, last) pairs: [1,2,3,7] -> [(1,3),(7,7)]."""
    runs: list[tuple[int, int]] = []
    for i in indices:
        if runs and runs[-1][1] == i - 1:
            runs[-1] = (runs[-1][0], i)
        else:
            runs.append((i, i))
    return runs
