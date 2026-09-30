"""The backend protocol: the only layer that knows how Perforce is reached
(design doc §4.1)."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Protocol

__all__ = ["Backend", "P4Message", "Result", "Severity"]


class Severity(IntEnum):
    INFO = 1
    WARNING = 2
    FAILED = 3
    FATAL = 4


@dataclass(frozen=True, slots=True)
class P4Message:
    severity: Severity
    text: str


@dataclass(frozen=True, slots=True)
class Result:
    """What one command reported: tagged records and untagged messages."""

    records: list[dict[str, str]] = field(default_factory=list)
    messages: list[P4Message] = field(default_factory=list)

    @property
    def errors(self) -> list[str]:
        return [m.text for m in self.messages if m.severity >= Severity.FAILED]

    @property
    def warnings(self) -> list[str]:
        return [m.text for m in self.messages if m.severity == Severity.WARNING]

    @property
    def infos(self) -> list[str]:
        return [m.text for m in self.messages if m.severity == Severity.INFO]

    @property
    def texts(self) -> list[str]:
        return [m.text for m in self.messages]

    def find(self, pattern: str) -> re.Match[str] | None:
        """The first message matching ``pattern`` (a regular expression)."""
        regex = re.compile(pattern)
        for message in self.messages:
            if match := regex.search(message.text):
                return match
        return None


class Backend(Protocol):
    """Runs Perforce commands. Implementations must be safe to call from
    several threads."""

    def run(self, command: str, args: Sequence[str] = (), input: str | None = None) -> Result:
        """Run ``p4 <command> <args>`` with tagged output. Raises
        :class:`~deda.clio.errors.AuthError` or
        :class:`~deda.clio.errors.ServerUnavailableError` when the command
        could not run at all; command errors are returned in the result."""
        ...

    def close(self) -> None: ...
