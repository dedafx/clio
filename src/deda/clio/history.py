"""File history and project activity, cached in memory (design doc §9).

* Submitted revisions never change, so they are cached for the process.
* History only grows. Each cached file or folder keeps a **watermark**, the
  newest change known for it. After ``poll_interval`` seconds, one cheap
  ``p4 changes -m1`` asks whether anything newer exists; only then is the
  new part fetched (§9.4).
* One cache per server and Perforce user, shared by every session in the
  process: history belongs to the server, and protections differ per user
  (§9.3).
* The cache is for display. Actions never read it (§9, rule 4).
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from deda.clio.backends.base import Result
from deda.clio.errors import NotInWorkspaceError, WorkflowError
from deda.clio.models import Change, Freshness, HistoryPage, Revision

if TYPE_CHECKING:
    from deda.clio.session import Session

__all__ = ["History", "HistoryCache"]


@dataclass
class _Entry[T]:
    """Newest first. ``complete`` once the oldest item has been fetched."""

    items: list[T] = field(default_factory=list)
    watermark: int = 0
    checked_at: float = 0.0
    complete: bool = False


class HistoryCache:
    """In-memory, LRU-bounded history for one server and user."""

    _registry: dict[tuple[str, str], HistoryCache] = {}
    _registry_lock = threading.Lock()

    def __init__(self, max_entries: int = 2000) -> None:
        self.max_entries = max_entries
        self.lock = threading.RLock()
        self.files: OrderedDict[str, _Entry[Revision]] = OrderedDict()
        self.scopes: OrderedDict[str, _Entry[Change]] = OrderedDict()

    @classmethod
    def for_server(cls, server_id: str, user: str) -> HistoryCache:
        with cls._registry_lock:
            return cls._registry.setdefault((server_id, user), cls())

    @classmethod
    def clear_all(cls) -> None:
        with cls._registry_lock:
            cls._registry.clear()

    def entry[T](self, table: OrderedDict[str, _Entry[T]], key: str) -> _Entry[T]:
        with self.lock:
            entry = table.get(key)
            if entry is None:
                entry = table[key] = _Entry()
                while len(table) > self.max_entries:
                    table.popitem(last=False)
            else:
                table.move_to_end(key)
            return entry

    def mark_stale(self, depot_paths: Iterable[str]) -> None:
        """Files changed by this process: re-check them (and folders above
        them) on next use, without waiting for the polling interval."""
        paths = list(depot_paths)
        with self.lock:
            for path in paths:
                if path in self.files:
                    self.files[path].checked_at = 0.0
            for scope, entry in self.scopes.items():
                prefix = scope.removesuffix("...")
                if any(p.startswith(prefix) for p in paths):
                    entry.checked_at = 0.0


def _time(value: str | None) -> datetime:
    return datetime.fromtimestamp(int(value or 0))


def _revisions(result: Result) -> list[Revision]:
    revisions = []
    for rec in result.records:
        depot = rec.get("depotFile")
        i = 0
        while depot and f"rev{i}" in rec:
            revisions.append(Revision(
                depot_path=depot,
                rev=int(rec[f"rev{i}"]),
                change=int(rec[f"change{i}"]),
                action=rec.get(f"action{i}", ""),
                user=rec.get(f"user{i}", ""),
                workspace=rec.get(f"client{i}", ""),
                time=_time(rec.get(f"time{i}")),
                description=rec.get(f"desc{i}", "").rstrip("\n"),
                file_type=rec.get(f"type{i}", ""),
                size=int(rec[f"fileSize{i}"]) if rec.get(f"fileSize{i}") else None,
                digest=rec.get(f"digest{i}"),
            ))
            i += 1
    return revisions


def _changes(result: Result) -> list[Change]:
    return [
        Change(int(r["change"]), r.get("desc", "").rstrip("\n"), r.get("user", ""), r.get("client", ""),
               _time(r.get("time")))
        for r in result.records
        if "change" in r
    ]


class History:
    """History of files and folders, from the cache when fresh."""

    def __init__(self, session: Session) -> None:
        self._session = session
        self._clock: Callable[[], float] = time.monotonic

    @property
    def cache(self) -> HistoryCache:
        s = self._session
        return HistoryCache.for_server(s.server_id, s.user)

    def _run(self, command: str, args: list[str]) -> Result:
        result = self._session.backend.run(command, args)
        if result.errors:
            raise WorkflowError(f"Could not read history ({command})", p4_messages=result.texts)
        return result

    def _latest_change(self, path: str) -> int:
        records = self._run("changes", ["-m1", "-s", "submitted", path]).records
        return int(records[0]["change"]) if records else 0

    def _file(self, path: str) -> str:
        # History is asked for files: no server call to tell a folder apart,
        # unless the path or the disk says it is one.
        targets = self._session.paths.targets([path], is_depot_folder=lambda paths: set())
        if len(targets) != 1 or targets[0].is_folder:
            raise NotInWorkspaceError(f"{path} is a folder", hint="Use 'activity' for folders, 'history' for files")
        return self._session.paths.depot_path(targets[0].asset_path)

    def _max_age(self, max_age: timedelta | float | None) -> float:
        if max_age is None:
            return self._session.config.poll_interval
        return max_age.total_seconds() if isinstance(max_age, timedelta) else float(max_age)

    def _freshness(self, entry: _Entry, now: float, max_age: timedelta | float | None) -> Freshness:
        return Freshness.FRESH if now - entry.checked_at <= self._max_age(max_age) else Freshness.STALE

    # --- files ----------------------------------------------------------

    def revisions(
        self,
        path: str,
        *,
        limit: int = 20,
        before: int | None = None,
        max_age: timedelta | float | None = None,
    ) -> HistoryPage[Revision]:
        """Revisions of one file, newest first.

        The first page comes from the cache if it was checked within
        ``max_age`` (default: ``poll_interval``). Otherwise one cheap call
        checks for newer revisions, and only those are fetched. ``before``
        (a page's ``next_cursor``) pages to older revisions, fetched once.
        """
        depot = self._file(path)
        cache = self.cache
        now = self._clock()
        with cache.lock:
            entry = cache.entry(cache.files, depot)
            if not entry.items and not entry.complete:
                revisions = _revisions(self._run("filelog", ["-l", "-t", "-m", str(limit), depot]))
                entry.items = revisions
                entry.watermark = max((r.change for r in revisions), default=0)
                entry.complete = not revisions or revisions[-1].rev == 1
                entry.checked_at = now
            elif before is None and now - entry.checked_at > self._max_age(max_age):
                latest = self._latest_change(depot)
                if latest > entry.watermark:
                    newer = _revisions(self._run("filelog", ["-l", "-t", f"{depot}@{entry.watermark + 1},@now"]))
                    known = {r.rev for r in entry.items}
                    entry.items = [r for r in newer if r.rev not in known] + entry.items
                    entry.watermark = latest
                entry.checked_at = now
            if before is not None:
                older = [r for r in entry.items if r.rev < before]
                if len(older) < limit and not entry.complete:
                    oldest = entry.items[-1].rev if entry.items else before
                    if oldest > 1:
                        fetched = _revisions(self._run(
                            "filelog", ["-l", "-t", "-m", str(limit), f"{depot}#1,#{oldest - 1}"]))
                        entry.items.extend(r for r in fetched if r.rev < oldest)
                    entry.complete = not entry.items or entry.items[-1].rev == 1
                    older = [r for r in entry.items if r.rev < before]
                items = older[:limit]
            else:
                items = entry.items[:limit]
            more = bool(items) and items[-1].rev > 1
            return HistoryPage(tuple(items), self._freshness(entry, now, max_age),
                               timedelta(seconds=now - entry.checked_at), items[-1].rev if more else None)

    # --- folders --------------------------------------------------------

    def activity(
        self, path: str = "", *, limit: int = 20, max_age: timedelta | float | None = None
    ) -> HistoryPage[Change]:
        """Changes that touched a folder (or the whole project), newest
        first, with the same caching as :meth:`revisions`."""
        targets = self._session.workspace.targets([path] if path else [])
        scope = targets[0].depot_spec(self._session.paths.depot_root)
        cache = self.cache
        now = self._clock()
        with cache.lock:
            entry = cache.entry(cache.scopes, scope)
            if not entry.items and not entry.complete:
                entry.items = _changes(self._run("changes", ["-l", "-s", "submitted", "-m", str(limit), scope]))
                entry.watermark = entry.items[0].number if entry.items else 0
                entry.complete = len(entry.items) < limit
                entry.checked_at = now
            elif now - entry.checked_at > self._max_age(max_age):
                latest = self._latest_change(scope)
                if latest > entry.watermark:
                    newer = _changes(self._run(
                        "changes", ["-l", "-s", "submitted", f"{scope}@{entry.watermark + 1},@now"]))
                    known = {c.number for c in entry.items}
                    entry.items = [c for c in newer if c.number not in known] + entry.items
                    entry.watermark = latest
                entry.checked_at = now
            if len(entry.items) < limit and not entry.complete:
                entry.items = _changes(self._run("changes", ["-l", "-s", "submitted", "-m", str(limit), scope]))
                entry.complete = len(entry.items) < limit
            items = tuple(entry.items[:limit])
            return HistoryPage(items, self._freshness(entry, now, max_age),
                               timedelta(seconds=now - entry.checked_at), None)

    def note_submitted(self, depot_paths: Iterable[str]) -> None:
        """Called after a save: the next read re-checks affected entries."""
        self.cache.mark_stale(depot_paths)

    def refresh(self) -> None:
        """Forget everything cached for this server and user."""
        cache = self.cache
        with cache.lock:
            cache.files.clear()
            cache.scopes.clear()
