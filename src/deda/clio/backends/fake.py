"""A scripted backend for unit tests: no server, canned answers.

It does not emulate Perforce. Tests that depend on Perforce behaviour run
against a real, throwaway ``p4d`` (design doc §13); this backend covers what
a server makes hard to trigger, such as a trigger rejecting a submit.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from deda.clio.backends.base import P4Message, Result, Severity

__all__ = ["Call", "FakeBackend"]


@dataclass(frozen=True, slots=True)
class Call:
    command: str
    args: tuple[str, ...]
    input: str | None


@dataclass
class _Rule:
    command: str
    match: Callable[[Sequence[str]], bool]
    result: Result | Callable[[Call], Result]
    times: int | None


@dataclass
class FakeBackend:
    """Answers commands from rules added with :meth:`on`. An unmatched
    command gets an empty result. Every call is recorded in ``calls``."""

    calls: list[Call] = field(default_factory=list)
    _rules: list[_Rule] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def on(
        self,
        command: str,
        *,
        records: Sequence[dict[str, str]] = (),
        info: Sequence[str] = (),
        warnings: Sequence[str] = (),
        errors: Sequence[str] = (),
        match: Callable[[Sequence[str]], bool] | None = None,
        result: Callable[[Call], Result] | None = None,
        times: int | None = None,
    ) -> FakeBackend:
        """Answer ``command`` (when ``match(args)`` is true) with the given
        records and messages, or with ``result(call)``. ``times`` limits how
        often the rule applies. Later rules win."""
        if result is None:
            messages = (
                [P4Message(Severity.INFO, t) for t in info]
                + [P4Message(Severity.WARNING, t) for t in warnings]
                + [P4Message(Severity.FAILED, t) for t in errors]
            )
            answer: Result | Callable[[Call], Result] = Result([dict(r) for r in records], messages)
        else:
            answer = result
        self._rules.append(_Rule(command, match or (lambda args: True), answer, times))
        return self

    def run(self, command: str, args: Sequence[str] = (), input: str | None = None) -> Result:
        call = Call(command, tuple(args), input)
        with self._lock:
            self.calls.append(call)
            for rule in reversed(self._rules):
                if rule.command == command and rule.times != 0 and rule.match(call.args):
                    if rule.times is not None:
                        rule.times -= 1
                    answer = rule.result
                    break
            else:
                return Result()
        return answer(call) if callable(answer) else Result(
            [dict(r) for r in answer.records], list(answer.messages)
        )

    def commands(self) -> list[str]:
        return [c.command for c in self.calls]

    def close(self) -> None:
        pass
