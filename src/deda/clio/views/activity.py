"""The activity feed: "what changed on my branch today" (design doc §9.1).

``ActivityView`` lists the changes that touched a folder, the whole project
or any group of paths (an asset's parts), newest first, and merges them when
there are several paths. It shows the cached feed at once, checks for news
with the cheap watermark call (§9.4), pages back in time with
``fetch_more()``, and can stop at a date (``since``) for a "today" feed.
The files of a change are asked for only when a row needs them, in one
``describe`` for as many changes as are on screen, and kept for good (a
submitted change never changes).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import Future
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from deda.clio.models import Change, Freshness, HistoryPage
from deda.clio.views._pipeline import PRIORITY_SELECTED, PRIORITY_VISIBLE, Signal
from deda.clio.views._polling import Polling

if TYPE_CHECKING:
    from deda.clio.session import Session

__all__ = ["ActivityRow", "ActivityView"]


@dataclass(frozen=True, slots=True)
class ActivityRow:
    """One change in the feed."""

    change: Change
    freshness: Freshness = Freshness.FRESH

    @property
    def number(self) -> int:
        return self.change.number

    @property
    def user(self) -> str:
        return self.change.user

    @property
    def time(self) -> datetime | None:
        return self.change.time

    @property
    def summary(self) -> str:
        """The first line of the description."""
        return self.change.description.strip().splitlines()[0] if self.change.description.strip() else ""


class ActivityView(Polling):
    """Changes under one or more paths, newest first.

    Signals (slots run on a pipeline thread): ``reset()`` when the first
    answer arrives or the rows are rearranged, ``rows_inserted(first, last)``
    for newer changes at the top and older pages at the end, ``failed(error)``.
    """

    def __init__(
        self,
        session: Session,
        paths: str | Sequence[str] = "",
        page_size: int = 20,
        *,
        since: datetime | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.session = session
        self.paths = (paths,) if isinstance(paths, str) else tuple(paths) or ("",)
        self.page_size = page_size
        self.since = since
        self.reset = Signal()
        self.rows_inserted = Signal()
        self.failed = Signal()
        self.error: BaseException | None = None
        self._clock = clock
        self._lock = threading.RLock()
        self._items: list[Change] = []
        self._freshness = Freshness.LOADING
        self._limit = page_size
        self._more = False
        self._closed = False
        self._pipeline = session.pipeline
        self.ready: Future = Future()

        cached = self._cached()
        if cached:
            self._items, self._freshness = self._clip(cached), Freshness.STALE
            self._more = len(cached) >= self._limit
            self.ready.set_result(None)
        self._load(max_age=None)

    # --- the model ------------------------------------------------------------

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)

    def __getitem__(self, index: int) -> ActivityRow:
        with self._lock:
            return ActivityRow(self._items[index], self._freshness)

    def __iter__(self) -> Iterator[ActivityRow]:
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
        """Older changes may exist (and, with ``since``, are not older than it)."""
        with self._lock:
            return self._more

    # --- loading --------------------------------------------------------------------

    def _cached(self) -> list[Change]:
        """Whatever the history cache already holds for these paths."""
        cache = self.session.history.cache
        merged: dict[int, Change] = {}
        with cache.lock:
            for path in self.paths:
                scope = self.session.workspace.targets([path] if path else [])[0].depot_spec(
                    self.session.paths.depot_root)
                entry = cache.scopes.get(scope)
                if entry is None:
                    return []
                merged.update((c.number, c) for c in entry.items)
        return sorted(merged.values(), key=lambda c: c.number, reverse=True)[:self._limit]

    def _clip(self, changes: list[Change]) -> list[Change]:
        if self.since is None:
            return changes
        return [c for c in changes if c.time is None or c.time >= self.since]

    def _load(self, max_age: float | None) -> Future:
        """Ask every path for its newest ``limit`` changes and merge them."""
        limit = self._limit
        futures = [self._pipeline.request("activity", (path, limit, max_age), PRIORITY_VISIBLE)
                   for path in self.paths]
        outcome: Future = Future()
        remaining = len(futures)
        lock = threading.Lock()

        def one_done(_: Future) -> None:
            nonlocal remaining
            with lock:
                remaining -= 1
                last = remaining == 0
            if last:
                self._on_pages(futures, limit, outcome)

        for future in futures:
            future.add_done_callback(one_done)
        return outcome

    def _on_pages(self, futures: list[Future], limit: int, outcome: Future) -> None:
        if self._closed or any(f.cancelled() for f in futures):
            outcome.set_result(None)
            return
        pages: list[HistoryPage[Change]] = []
        for future in futures:
            try:
                pages.append(future.result())
            except BaseException as e:
                self.error = e
                if not self.ready.done():
                    self.ready.set_exception(e)
                self.failed.emit(e)
                outcome.set_exception(e)
                return
        merged = {c.number: c for page in pages for c in page.items}
        newest_first = sorted(merged.values(), key=lambda c: c.number, reverse=True)[:limit]
        shown = self._clip(newest_first)
        more = any(len(p.items) >= limit for p in pages) and len(shown) == len(newest_first)
        freshness = Freshness.FRESH if all(p.freshness is Freshness.FRESH for p in pages) else Freshness.STALE
        with self._lock:
            old = self._items
            old_numbers = {c.number for c in old}
            added = [c for c in shown if c.number not in old_numbers]
            first_load = not self.ready.done()
            self._items, self._more, self._freshness = shown, more, freshness
            top = bool(added) and old and all(c.number > old[0].number for c in added)
            tail = bool(added) and old and all(c.number < old[-1].number for c in added)
            count = len(added)
        if first_load:
            self.ready.set_result(None)
            self.reset.emit()
        elif added and top:
            self.rows_inserted.emit(0, count - 1)
        elif added and tail:
            self.rows_inserted.emit(len(shown) - count, len(shown) - 1)
        elif added or [c.number for c in old] != [c.number for c in shown]:
            self.reset.emit()
        outcome.set_result(None)

    def refresh(self) -> Future:
        """Check the server now for news, whatever the age of the cache."""
        return self._load(max_age=0.0)

    def fetch_more(self) -> Future:
        """Show a page more, going back in time. Resolves when it is added."""
        with self._lock:
            if not self._more or self._closed:
                done: Future = Future()
                done.set_result(None)
                return done
            self._limit += self.page_size
        return self._load(max_age=None)

    def _tick(self) -> None:
        self._load(max_age=None)

    # --- what a change touched ---------------------------------------------------------

    def files(self, index: int) -> Future:
        """The depot paths in a change. Changes on screen asked for together
        are one ``describe``; the answer is kept, as a submitted change never
        changes."""
        with self._lock:
            number = self._items[index].number
        return self._pipeline.request("describe", number, PRIORITY_SELECTED)

    def close(self) -> None:
        self._closed = True
        self.stop_polling()

    def __enter__(self) -> ActivityView:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
