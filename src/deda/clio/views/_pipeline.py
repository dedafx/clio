"""The request pipeline shared by every view in a process (design doc §9.5).

A UI scrolls, selects and refreshes far faster than a server should be
asked. The pipeline sits between the views and the backend and does five
things:

1. **Coalesce.** Requests that arrive within a short window (30 ms) are
   merged, so scrolling past 40 rows is one batch (one ``fstat``), not 40.
2. **Deduplicate.** Two views asking for the same key share one request and
   its result.
3. **Prioritize.** Visible rows first, then the selected file's history, then
   prefetch.
4. **Cancel.** A request that has not started can be dropped (rows scrolled
   out of view). One that has started finishes and is cached.
5. **Run off the UI thread**, on a few worker threads (and so on the
   backend's own connections), reporting through futures and signals.
"""

from __future__ import annotations

import itertools
import logging
import threading
from collections.abc import Callable, Hashable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

__all__ = ["PRIORITY_PREFETCH", "PRIORITY_SELECTED", "PRIORITY_VISIBLE", "RequestPipeline", "Signal"]

_log = logging.getLogger(__name__)

#: Lower runs first.
PRIORITY_VISIBLE = 0
PRIORITY_SELECTED = 1
PRIORITY_PREFETCH = 2

#: Gets the keys of one batch and returns ``{key: result}``. A key that is
#: missing from the result resolves to ``None``.
BatchHandler = Callable[[Sequence[Any]], dict[Any, Any]]


class Signal:
    """A minimal signal: ``connect(fn)`` and ``emit(*args)``. Slots run on
    the thread that emits, which is a pipeline worker, never the UI thread; a
    UI marshals to its own thread (in Qt, a queued signal). A slot that raises
    is logged and does not stop the others."""

    def __init__(self) -> None:
        self._slots: list[Callable[..., Any]] = []
        self._lock = threading.Lock()

    def connect(self, slot: Callable[..., Any]) -> Callable[[], None]:
        """Call ``slot`` on each emit. Returns a function that disconnects it."""
        with self._lock:
            self._slots.append(slot)

        def disconnect() -> None:
            with self._lock:
                if slot in self._slots:
                    self._slots.remove(slot)

        return disconnect

    def emit(self, *args: Any) -> None:
        with self._lock:
            slots = list(self._slots)
        for slot in slots:
            try:
                slot(*args)
            except Exception:  # a broken slot must not stop the pipeline
                _log.exception("A signal slot raised")


@dataclass
class _Pending:
    future: Future
    priority: int
    sequence: int


@dataclass
class _Kind:
    handler: BatchHandler
    max_batch: int


@dataclass
class PipelineStats:
    """What the pipeline has done: for tests and diagnostics."""

    requested: int = 0
    deduplicated: int = 0
    cancelled: int = 0
    batches: int = 0
    #: Keys per batch, in the order batches ran.
    batch_sizes: list[int] = field(default_factory=list)


class RequestPipeline:
    """Coalescing, deduplicating, prioritized, cancellable requests.

    Register a handler per kind of request with :meth:`register`, then
    :meth:`request` keys. Thread-safe.
    """

    def __init__(self, window: float = 0.03, workers: int = 4) -> None:
        self.window = window
        self.stats = PipelineStats()
        self._kinds: dict[str, _Kind] = {}
        self._pending: dict[tuple[str, Hashable], _Pending] = {}
        self._inflight: dict[tuple[str, Hashable], Future] = {}
        self._sequence = itertools.count()
        self._condition = threading.Condition()
        self._closed = False
        self._executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="clio-pipeline")
        self._dispatcher = threading.Thread(target=self._run, name="clio-pipeline-dispatch", daemon=True)
        self._dispatcher.start()

    def register(self, kind: str, handler: BatchHandler, *, max_batch: int = 500) -> None:
        """Set how requests of ``kind`` are answered, ``max_batch`` keys at a time."""
        with self._condition:
            self._kinds[kind] = _Kind(handler, max_batch)

    def request(self, kind: str, key: Hashable, priority: int = PRIORITY_VISIBLE) -> Future:
        """Ask for ``key``. The future resolves to the handler's answer for it.
        A request that is already waiting or running is shared, and raised to
        the higher of the two priorities."""
        with self._condition:
            if self._closed:
                future: Future = Future()
                future.cancel()
                return future
            if kind not in self._kinds:
                raise KeyError(f"No handler registered for '{kind}'")
            self.stats.requested += 1
            entry = (kind, key)
            if (pending := self._pending.get(entry)) is not None:
                self.stats.deduplicated += 1
                pending.priority = min(pending.priority, priority)
                return pending.future
            if (running := self._inflight.get(entry)) is not None:
                self.stats.deduplicated += 1
                return running
            future = Future()
            self._pending[entry] = _Pending(future, priority, next(self._sequence))
            self._condition.notify_all()
            return future

    def cancel(self, kind: str, key: Hashable) -> bool:
        """Drop a request that has not started. False if it already started
        (it will finish) or was never made."""
        with self._condition:
            pending = self._pending.pop((kind, key), None)
        if pending is None:
            return False
        pending.future.cancel()
        self.stats.cancelled += 1
        return True

    def cancel_where(self, kind: str, keep: Callable[[Hashable], bool]) -> int:
        """Drop every waiting request of ``kind`` whose key ``keep`` rejects."""
        with self._condition:
            dropped = [e for e in self._pending if e[0] == kind and not keep(e[1])]
            pendings = [self._pending.pop(e) for e in dropped]
        for pending in pendings:
            pending.future.cancel()
        self.stats.cancelled += len(pendings)
        return len(pendings)

    def idle(self) -> bool:
        """True when nothing is waiting or running."""
        with self._condition:
            return not self._pending and not self._inflight

    def wait_idle(self, timeout: float = 10.0) -> bool:
        """Block until :meth:`idle`. Returns False on timeout."""
        with self._condition:
            return self._condition.wait_for(lambda: not self._pending and not self._inflight, timeout)

    def close(self) -> None:
        """Cancel what is waiting and stop. Running batches finish."""
        with self._condition:
            self._closed = True
            pendings = list(self._pending.values())
            self._pending.clear()
            self._condition.notify_all()
        for pending in pendings:
            pending.future.cancel()
        self._executor.shutdown(wait=False)

    # --- the dispatcher -----------------------------------------------------

    def _run(self) -> None:
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._pending or self._closed)
                if self._closed:
                    return
            # Let requests that arrive together join this round.
            self._sleep(self.window)
            with self._condition:
                if self._closed:
                    return
                taken = sorted(self._pending.items(), key=lambda item: (item[1].priority, item[1].sequence))
                self._pending.clear()
                batches: dict[str, list[tuple[Hashable, _Pending]]] = {}
                order: list[str] = []
                for (kind, key), pending in taken:
                    if not pending.future.set_running_or_notify_cancel():
                        continue
                    if kind not in batches:
                        batches[kind] = []
                        order.append(kind)
                    batches[kind].append((key, pending))
                    self._inflight[(kind, key)] = pending.future
            for kind in order:
                handler = self._kinds[kind]
                items = batches[kind]
                for start in range(0, len(items), handler.max_batch):
                    self._executor.submit(self._execute, kind, handler.handler, items[start:start + handler.max_batch])

    def _sleep(self, seconds: float) -> None:
        with self._condition:
            self._condition.wait_for(lambda: self._closed, seconds)

    def _execute(self, kind: str, handler: BatchHandler, items: list[tuple[Hashable, _Pending]]) -> None:
        keys = [key for key, _ in items]
        self.stats.batches += 1
        self.stats.batch_sizes.append(len(keys))
        try:
            results = handler(keys)
        except BaseException as e:  # the whole batch failed
            for _, pending in items:
                pending.future.set_exception(e)
        else:
            for key, pending in items:
                pending.future.set_result(results.get(key))
        finally:
            with self._condition:
                for key in keys:
                    self._inflight.pop((kind, key), None)
                self._condition.notify_all()
