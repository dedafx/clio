"""Keeping a view fresh while it is on screen (design doc §9.4): a freshness
check every ``poll_interval`` seconds, paused when the view is hidden and
done at once when it becomes visible."""

from __future__ import annotations

import threading


class Polling:
    """Mixin for views. A subclass implements :meth:`_tick`."""

    _poll_timer: threading.Timer | None = None
    _poll_interval = 30.0
    _poll_running = False
    _poll_visible = True

    def _tick(self) -> None:  # pragma: no cover - implemented by the view
        raise NotImplementedError

    def start_polling(self, interval: float | None = None) -> None:
        """Check for news every ``interval`` seconds (default: the
        ``poll_interval`` setting) while the view is visible."""
        self.stop_polling()
        self._poll_interval = interval if interval is not None else self.session.config.poll_interval  # type: ignore[attr-defined]
        self._poll_running = True
        self._schedule()

    def stop_polling(self) -> None:
        self._poll_running = False
        timer, self._poll_timer = self._poll_timer, None
        if timer is not None:
            timer.cancel()

    def set_visible(self, visible: bool) -> None:
        """Tell the view whether it is on screen. Polling pauses while hidden;
        becoming visible again checks for news at once."""
        was, self._poll_visible = self._poll_visible, visible
        if visible and not was:
            self._tick()

    def _schedule(self) -> None:
        if not self._poll_running:
            return
        timer = threading.Timer(self._poll_interval, self._fire)
        timer.daemon = True
        self._poll_timer = timer
        timer.start()

    def _fire(self) -> None:
        if not self._poll_running:
            return
        try:
            if self._poll_visible:
                self._tick()
        finally:
            self._schedule()
