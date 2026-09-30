"""Backends: how Clio reaches Perforce (design doc §4.1)."""

from deda.clio.backends.base import Backend, P4Message, Result, Severity

__all__ = ["Backend", "P4Message", "Result", "Severity"]
