"""Clio: artist-friendly access to assets stored in Perforce.

The core (Perforce access and asset resolution) is implemented in C++
(``deda.clio._core``) and shared with the USD asset resolver plugin, so
Python and USD resolve every asset path the same way. The workflow services
(``connect``, ``Session``, ``Workspace``, ``History``) are Python over that
core.

``deda`` is a PEP 420 namespace package: there is no ``deda/__init__.py``.
"""

from deda.clio._core import (
    AssetIdentifier,
    AssetResolver,
    CommandResult,
    Connection,
    Message,
    Pin,
    PinKind,
    Policy,
    Settings,
    __version__,
)
from deda.clio.config import Config, load_config
from deda.clio.errors import (
    AuthError,
    CancelledError,
    ClioError,
    ConfigError,
    ConfirmationRequiredError,
    IdentifierError,
    LockedByOtherError,
    NothingToSaveError,
    NotInWorkspaceError,
    OutOfDateError,
    P4Error,
    PinError,
    ServerPolicyError,
    ServerUnavailableError,
    WorkflowError,
)
from deda.clio.events import CancelToken, EventBus, ProgressEvent
from deda.clio.models import (
    Change,
    FileState,
    FileStatus,
    Freshness,
    HistoryPage,
    Lock,
    Revision,
    SaveResult,
    SyncedFile,
    SyncPlan,
    SyncReport,
)
from deda.clio.session import Session, connect

__all__ = [
    "AssetIdentifier",
    "AssetResolver",
    "AuthError",
    "CancelToken",
    "CancelledError",
    "Change",
    "ClioError",
    "CommandResult",
    "Config",
    "ConfigError",
    "ConfirmationRequiredError",
    "Connection",
    "EventBus",
    "FileState",
    "FileStatus",
    "Freshness",
    "HistoryPage",
    "IdentifierError",
    "Lock",
    "LockedByOtherError",
    "Message",
    "NotInWorkspaceError",
    "NothingToSaveError",
    "OutOfDateError",
    "P4Error",
    "Pin",
    "PinError",
    "PinKind",
    "Policy",
    "ProgressEvent",
    "Revision",
    "SaveResult",
    "ServerPolicyError",
    "ServerUnavailableError",
    "Session",
    "Settings",
    "SyncPlan",
    "SyncReport",
    "SyncedFile",
    "WorkflowError",
    "__version__",
    "connect",
    "load_config",
]
