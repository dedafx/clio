"""Clio: artist-friendly access to assets stored in Perforce.

The core (Perforce access and asset resolution) is implemented in C++
(``deda.clio._core``) and shared with the USD asset resolver plugin, so
Python and USD resolve every asset path the same way.

``deda`` is a PEP 420 namespace package: there is no ``deda/__init__.py``.
"""

from deda.clio._core import (
    AssetIdentifier,
    AssetResolver,
    ClioError,
    CommandResult,
    ConfigError,
    Connection,
    IdentifierError,
    Message,
    P4Error,
    Pin,
    PinError,
    PinKind,
    Policy,
    Settings,
    __version__,
)

__all__ = [
    "AssetIdentifier",
    "AssetResolver",
    "ClioError",
    "CommandResult",
    "ConfigError",
    "Connection",
    "IdentifierError",
    "Message",
    "P4Error",
    "Pin",
    "PinError",
    "PinKind",
    "Policy",
    "Settings",
    "__version__",
]
