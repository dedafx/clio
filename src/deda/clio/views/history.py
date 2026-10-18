"""The version history of one file, as rows (design doc §9.1, §9.5).

``HistoryView`` shows the first page from the cache at once, then asks for
newer revisions (one cheap check, and only the new part if there is any).
``fetch_more()`` loads the next, older page when the user scrolls. File
content for a revision (to compare, or to show) comes from the content cache
through :meth:`HistoryView.content`.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import Future
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

from deda.clio.content import ContentRequest
from deda.clio.errors import WorkflowError
from deda.clio.models import Freshness, HistoryPage, Revision
from deda.clio.views._pipeline import PRIORITY_SELECTED, PRIORITY_VISIBLE, Signal
from deda.clio.views._polling import Polling

if TYPE_CHECKING:
    from deda.clio.session import Session

__all__ = ["HistoryRow", "HistoryView"]


@dataclass(frozen=True, slots=True)
class HistoryRow:
    """One revision, with how fresh the list it is in was."""

    revision: Revision
    freshness: Freshness = Freshness.FRESH

    # The fields a history panel shows, without going through ``revision``.
    @property
    def rev(self) -> int:
        return self.revision.rev

    @property
    def change(self) -> int:
        return self.revision.change

    @property
    def user(self) -> str:
        return self.revision.user

    @property
    def description(self) -> str:
        return self.revision.description


class HistoryView(Polling):
    """The revisions of one file, newest first.

    Signals (slots run on a pipeline thread, not the UI thread):
    ``reset()`` when the first page arrives, ``rows_inserted(first, last)``
    for newer revisions at the top and older pages at the end, and
    ``failed(error)``.
    """

    def __init__(
        self,
        session: Session,
        path: str,
        page_size: int = 20,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.session = session
        self.path = path
        self.page_size = page_size
        self.reset = Signal()
        self.rows_inserted = Signal()
        self.failed = Signal()
        self.error: BaseException | None = None
        self._clock = clock
        self._lock = threading.RLock()
        self._items: list[Revision] = []
        self._freshness = Freshness.LOADING
        self._more = False
        self._closed = False
        self._loading_more: Future | None = None
        self._pipeline = session.pipeline
        self.ready: Future = Future()

        depot = session.history._file(path)
        with session.history.cache.lock:
            entry = session.history.cache.files.get(depot)
            cached = list(entry.items[:page_size]) if entry is not None else []
        if cached:
            # Show the cache at once, marked stale; the check below refreshes it.
            self._items, self._freshness = cached, Freshness.STALE
            self._more = cached[-1].rev > 1
            self.ready.set_result(None)
        self._request_first_page(max_age=None)

    # --- the model ------------------------------------------------------------

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)

    def __getitem__(self, index: int) -> HistoryRow:
        with self._lock:
            return HistoryRow(self._items[index], self._freshness)

    def __iter__(self) -> Iterator[HistoryRow]:
        for i in range(len(self)):
            try:
                yield self[i]
            except IndexError:
                return

    @property
    def freshness(self) -> Freshness:
        with self._lock:
            return self._freshness

    @property
    def has_more(self) -> bool:
        """Older revisions exist and have not been loaded."""
        with self._lock:
            return self._more

    # --- asking -----------------------------------------------------------------

    def _request_first_page(self, max_age: float | None) -> Future:
        future = self._pipeline.request("history", (self.path, self.page_size, None, max_age), PRIORITY_VISIBLE)
        future.add_done_callback(self._on_first_page)
        return future

    def refresh(self) -> Future:
        """Check the server now for newer revisions, whatever the age of the
        cache."""
        return self._request_first_page(max_age=0.0)

    def fetch_more(self) -> Future:
        """Load the next, older page. The future resolves when it is added
        (to ``None`` if there was nothing more). Asking again while a page is
        on its way returns the same future."""
        with self._lock:
            if not self._more or self._closed:
                done: Future = Future()
                done.set_result(None)
                return done
            if self._loading_more is not None and not self._loading_more.done():
                return self._loading_more
            cursor = self._items[-1].rev
            future = self._pipeline.request("history", (self.path, self.page_size, cursor, None), PRIORITY_VISIBLE)
            outcome: Future = Future()
            self._loading_more = outcome
        future.add_done_callback(lambda f: self._on_older_page(f, outcome))
        return outcome

    def _tick(self) -> None:
        self._request_first_page(max_age=None)

    # --- answers ------------------------------------------------------------------

    def _fail(self, error: BaseException) -> None:
        self.error = error
        if not self.ready.done():
            self.ready.set_exception(error)
        self.failed.emit(error)

    def _on_first_page(self, future: Future) -> None:
        if future.cancelled() or self._closed:
            return
        try:
            page: HistoryPage[Revision] = future.result()
        except BaseException as e:
            self._fail(e)
            return
        with self._lock:
            first_load = not self.ready.done() and not self._items
            known = {r.rev for r in self._items}
            newest = max(known, default=0)
            if first_load or not self._items:
                self._items = list(page.items)
                self._more = page.next_cursor is not None
                inserted = None
            else:
                fresh = [r for r in page.items if r.rev > newest and r.rev not in known]
                self._items = fresh + self._items
                inserted = (0, len(fresh) - 1) if fresh else None
            self._freshness = page.freshness
        if not self.ready.done():
            self.ready.set_result(None)
            self.reset.emit()
        elif inserted:
            self.rows_inserted.emit(*inserted)

    def _on_older_page(self, future: Future, outcome: Future) -> None:
        if future.cancelled() or self._closed:
            outcome.set_result(None)
            return
        try:
            page: HistoryPage[Revision] = future.result()
        except BaseException as e:
            self._fail(e)
            outcome.set_exception(e)
            return
        with self._lock:
            known = {r.rev for r in self._items}
            older = [r for r in page.items if r.rev not in known and (not self._items or r.rev < self._items[-1].rev)]
            first = len(self._items)
            self._items.extend(older)
            self._more = bool(older) and older[-1].rev > 1
        if older:
            self.rows_inserted.emit(first, first + len(older) - 1)
        outcome.set_result(None)

    # --- file content -----------------------------------------------------------------

    def content(self, index: int) -> Future:
        """The file's bytes at that revision, as a path in the content cache
        (fetched once per machine, never into the workspace). The future
        raises :class:`~deda.clio.WorkflowError` if it cannot be fetched."""
        with self._lock:
            revision = self._items[index]
        result: Future = Future()
        if revision.digest is None:
            result.set_exception(WorkflowError(f"{revision.depot_path}#{revision.rev} has no digest to fetch by"))
            return result
        request = ContentRequest(revision.depot_path, revision.rev, revision.digest, revision.size or 0,
                                 revision.file_type)
        future = self._pipeline.request("content", request, PRIORITY_SELECTED)

        def done(f: Future) -> None:
            if f.cancelled():
                result.cancel()
                return
            try:
                value = f.result()
            except BaseException as e:
                result.set_exception(e)
                return
            if isinstance(value, BaseException):
                result.set_exception(value)
            else:
                result.set_result(value)

        future.add_done_callback(done)
        return result

    def close(self) -> None:
        self._closed = True
        self.stop_polling()

    def __enter__(self) -> HistoryView:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
