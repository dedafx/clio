"""The default backend, over the C++ core (``deda.clio._core``)."""

from __future__ import annotations

import threading
from collections import Counter
from collections.abc import Callable, Sequence

from deda.clio import _core
from deda.clio.backends.base import P4Message, Result, Severity
from deda.clio.config import Config
from deda.clio.errors import AuthError, ServerUnavailableError

__all__ = ["CoreBackend", "translate_p4_error"]

PromptCallback = Callable[[str, bool], str | None]

_AUTH_MARKERS = (
    "P4PASSWD) invalid or unset",
    "session has expired",
    "Password invalid",
    "password has expired",
    "Password must be set",
)
_UNAVAILABLE_MARKERS = (
    "Connect to server failed",
    "cannot reach",
    "no answer from",
    "cannot resolve",
    "TCP connect to",
    "Partner exited unexpectedly",
)


def translate_p4_error(error: _core.P4Error) -> Exception:
    """An artist-facing error for a core P4Error, or the error itself."""
    text = str(error)
    if any(marker in text for marker in _AUTH_MARKERS):
        return AuthError("You are not logged in to Perforce", hint="Run 'clio login'", p4_messages=[text])
    if any(marker in text for marker in _UNAVAILABLE_MARKERS):
        return ServerUnavailableError(
            "The Perforce server is not reachable",
            hint="Check the network and the server address (port)",
            p4_messages=[text],
        )
    return error


class CoreBackend:
    """Runs commands through ``_core.Connection``.

    A connection must not be used by two threads at once (design doc §8.1),
    so each thread gets its own, opened on first use and kept for later
    commands. ``calls`` counts commands by name, which tests use to check
    the server-call budgets of design doc §9.6.
    """

    def __init__(self, config: Config, *, cwd: str = "", prompt: PromptCallback | None = None) -> None:
        self._config = config
        self._cwd = cwd
        self._prompt = prompt
        self._local = threading.local()
        self._lock = threading.Lock()
        self._connections: list[_core.Connection] = []
        self.calls: Counter[str] = Counter()

    def _connection(self) -> _core.Connection:
        conn = getattr(self._local, "connection", None)
        if conn is None:
            c = self._config
            conn = _core.Connection(
                port=c.port,
                user=c.user,
                client=c.client,
                cwd=self._cwd,
                tickets=c.tickets,
                timeout=c.timeout,
                connect_timeout=c.connect_timeout,
                prompt=self._prompt,
            )
            self._local.connection = conn
            with self._lock:
                self._connections.append(conn)
        return conn

    def run(self, command: str, args: Sequence[str] = (), input: str | None = None) -> Result:
        with self._lock:
            self.calls[command] += 1
        try:
            raw = self._connection().run(command, list(args), input)
        except _core.P4Error as e:
            raise translate_p4_error(e) from e
        messages = [P4Message(Severity(m.severity), m.text) for m in raw.messages]
        result = Result(list(raw.records), messages)
        # A server that drops the connection or rejects the ticket reports it
        # as a command error; treat those like connection failures.
        for text in result.errors:
            if any(marker in text for marker in _AUTH_MARKERS):
                raise AuthError("You are not logged in to Perforce", hint="Run 'clio login'", p4_messages=[text])
        return result

    @property
    def total_calls(self) -> int:
        with self._lock:
            return sum(self.calls.values())

    def close(self) -> None:
        with self._lock:
            connections, self._connections = self._connections, []
        for conn in connections:
            conn.disconnect()
        self._local = threading.local()
