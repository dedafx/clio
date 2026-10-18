"""Clio's exception hierarchy (design doc §11).

Everything Clio raises is a :class:`ClioError`. The core's errors
(``IdentifierError``, ``PinError``, ``ConfigError``, ``P4Error``) come from
the C++ extension; the workflow errors below add an artist-readable
``message``, a ``hint`` (what to do next) and the raw Perforce messages in
``p4_messages`` for debugging.
"""

from __future__ import annotations

from collections.abc import Sequence

from deda.clio._core import ClioError, ConfigError, IdentifierError, P4Error, PinError

__all__ = [
    "AuthError",
    "CancelledError",
    "ClioError",
    "ConfigError",
    "ConfirmationRequiredError",
    "ConflictError",
    "AssetNotFoundError",
    "IdentifierError",
    "LockedByOtherError",
    "ManifestError",
    "NotInWorkspaceError",
    "NothingToSaveError",
    "OutOfDateError",
    "P4Error",
    "PinError",
    "ServerPolicyError",
    "ServerUnavailableError",
    "ValidationError",
    "WorkflowError",
]


class WorkflowError(ClioError):
    """A Clio action could not be done. Carries a message, a hint and the
    Perforce messages that explain it."""

    #: Process exit code used by the ``clio`` command line for this error.
    exit_code = 1

    def __init__(self, message: str, *, hint: str = "", p4_messages: Sequence[str] = ()) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.p4_messages = tuple(p4_messages)

    def __str__(self) -> str:
        return f"{self.message} ({self.hint})" if self.hint else self.message


class AuthError(WorkflowError):
    """Not logged in, or the Perforce ticket expired."""

    exit_code = 3


class ServerUnavailableError(WorkflowError):
    """The Perforce server could not be reached."""

    exit_code = 4


class LockedByOtherError(WorkflowError):
    """Someone else has a file locked (or exclusively open)."""

    exit_code = 5

    def __init__(self, message: str, *, path: str, user: str, workspace: str, branch: str = "", **kwargs) -> None:
        super().__init__(message, **kwargs)
        self.path = path
        self.user = user
        self.workspace = workspace
        #: The branch (stream) where the file is locked, when it is not the
        #: current one.
        self.branch = branch


class OutOfDateError(WorkflowError):
    """The local copy is older than the latest version on the server."""

    exit_code = 6

    def __init__(self, message: str, *, paths: Sequence[str] = (), **kwargs) -> None:
        super().__init__(message, **kwargs)
        self.paths = tuple(paths)


class NothingToSaveError(WorkflowError):
    """``save`` found no added, changed or deleted files."""

    exit_code = 7


class ServerPolicyError(WorkflowError):
    """The server refused the action, for example a submit trigger."""

    exit_code = 8

    def __init__(self, message: str, *, change: int | None = None, **kwargs) -> None:
        super().__init__(message, **kwargs)
        self.change = change


class CancelledError(WorkflowError):
    """The action was cancelled through a :class:`~deda.clio.CancelToken`."""

    exit_code = 9


class ConfirmationRequiredError(WorkflowError):
    """A destructive action was called without ``confirm=True``."""

    exit_code = 9


class ConflictError(WorkflowError):
    """The branch and its parent both changed the same files; a choice is
    needed for each (design doc §7.2). Nothing was changed."""

    exit_code = 12

    def __init__(self, message: str, *, conflicts: Sequence = (), **kwargs) -> None:
        super().__init__(message, **kwargs)
        #: The :class:`~deda.clio.models.Conflict` for each file.
        self.conflicts = tuple(conflicts)


class AssetNotFoundError(WorkflowError):
    """An asset resolver plugin knows no asset of that name, or lists no
    files for it (design doc §14)."""

    exit_code = 15


class ManifestError(WorkflowError):
    """A version manifest cannot be made or used: a file a farm machine
    could not reproduce, or a manifest for another project."""

    exit_code = 13

    def __init__(self, message: str, *, problems: Sequence[str] = (), **kwargs) -> None:
        super().__init__(message, **kwargs)
        #: One sentence per file or reference that stops the manifest.
        self.problems = tuple(problems)


class ValidationError(WorkflowError):
    """A validation hook (``deda.clio.hooks``) stopped a save. Nothing was
    submitted and no file on disk changed."""

    exit_code = 14

    def __init__(self, message: str, *, issues: Sequence = (), **kwargs) -> None:
        issues = tuple(issues)
        super().__init__(message, **kwargs)
        #: The :class:`~deda.clio.hooks.HookIssue` found (errors and warnings).
        self.issues = issues
        #: One sentence per error, for display.
        self.problems = tuple(str(i) for i in issues if getattr(i.severity, "value", "") == "error")


class NotInWorkspaceError(WorkflowError):
    """A path is outside the project, or there is no workspace yet."""

    exit_code = 10
