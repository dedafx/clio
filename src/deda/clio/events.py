"""Progress events, cancellation and a small event bus (design doc §5.6)."""

from __future__ import annotations

import threading
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from deda.clio.errors import CancelledError

__all__ = ["CancelToken", "EventBus", "ProgressCallback", "ProgressEvent"]


@dataclass(frozen=True, slots=True)
class ProgressEvent:
    """Where a long-running action is. ``total`` and ``bytes_total`` are 0
    when unknown."""

    phase: str
    done: int = 0
    total: int = 0
    bytes_done: int = 0
    bytes_total: int = 0
    current_path: str = ""


ProgressCallback = Callable[[ProgressEvent], None]


class CancelToken:
    """Cancels an action from another thread (for example a UI's Cancel
    button). Clio checks it between Perforce commands; a command that has
    started runs to completion."""

    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def raise_if_cancelled(self) -> None:
        if self._event.is_set():
            raise CancelledError("Cancelled", hint="Nothing after this point was done")


class EventBus:
    """Publish/subscribe for workflow events, so tools such as Dedaverse can
    update lock icons or file lists without polling.

    Event names: ``files_synced``, ``file_locked``, ``file_unlocked``,
    ``change_submitted``, ``files_discarded``. Callbacks run on the thread
    that did the action; a UI marshals them to its own thread.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: dict[str, list[Callable[..., Any]]] = defaultdict(list)

    def subscribe(self, name: str, callback: Callable[..., Any]) -> Callable[[], None]:
        """Call ``callback(**payload)`` for each ``name`` event. Returns a
        function that unsubscribes."""
        with self._lock:
            self._subscribers[name].append(callback)

        def unsubscribe() -> None:
            with self._lock:
                if callback in self._subscribers[name]:
                    self._subscribers[name].remove(callback)

        return unsubscribe

    def emit(self, name: str, **payload: Any) -> None:
        with self._lock:
            callbacks = list(self._subscribers.get(name, ()))
        for callback in callbacks:
            callback(**payload)
