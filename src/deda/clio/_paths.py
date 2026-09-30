"""Mapping between asset paths, depot paths and local paths.

Clio's workspaces map the project's depot root to the workspace root one to
one (``//imagine/main/...`` → ``<root>/...``), which ``clio setup``
guarantees. Paths are therefore translated without asking the server,
except to tell a folder from a file when neither the path nor the disk
says (one batched ``p4 dirs``).
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from deda.clio.errors import NotInWorkspaceError

__all__ = ["ProjectPaths", "Target", "escape", "unescape"]

_ESCAPES = {"%": "%25", "@": "%40", "#": "%23", "*": "%2A"}


def escape(name: str) -> str:
    """Perforce's escaping of reserved characters in file names."""
    return "".join(_ESCAPES.get(c, c) for c in name)


def unescape(name: str) -> str:
    for plain, escaped in (("@", "%40"), ("#", "%23"), ("*", "%2A"), ("%", "%25")):
        name = name.replace(escaped, plain).replace(escaped.lower(), plain)
    return name


def _same(a: str, b: str) -> bool:
    return a.casefold() == b.casefold() if sys.platform == "win32" else a == b


@dataclass(frozen=True, slots=True)
class Target:
    """One path argument, resolved: a file or a whole folder."""

    asset_path: str  # "" for the project root
    is_folder: bool

    def depot_spec(self, depot_root: str) -> str:
        base = depot_root + ("/" + escape(self.asset_path) if self.asset_path else "")
        return base + "/..." if self.is_folder else base


@dataclass(frozen=True, slots=True)
class ProjectPaths:
    depot_root: str  # //imagine/main, no trailing slash
    root: Path  # workspace root

    # --- one path -------------------------------------------------------

    def asset_path_of_depot(self, depot_path: str) -> str:
        prefix = self.depot_root + "/"
        if not depot_path.startswith(prefix):
            raise NotInWorkspaceError(
                f"{depot_path} is outside the project {self.depot_root}",
                hint="Use a path inside the project",
            )
        return unescape(depot_path[len(prefix):])

    def asset_path_of_local(self, local: Path) -> str:
        parts = Path(os.path.normpath(os.path.abspath(local))).parts
        root_parts = Path(os.path.normpath(os.path.abspath(self.root))).parts
        if len(parts) < len(root_parts) or not all(_same(a, b) for a, b in zip(parts, root_parts)):
            raise NotInWorkspaceError(
                f"{local} is outside the workspace {self.root}",
                hint="Use a path inside the workspace, or run 'clio setup'",
            )
        return "/".join(parts[len(root_parts):])

    def depot_path(self, asset_path: str) -> str:
        return f"{self.depot_root}/{escape(asset_path)}" if asset_path else self.depot_root

    def local_path(self, asset_path: str) -> Path:
        return self.root.joinpath(*asset_path.split("/")) if asset_path else self.root

    def local_of_depot(self, depot_path: str) -> Path:
        return self.local_path(self.asset_path_of_depot(depot_path))

    # --- path arguments -------------------------------------------------

    def targets(
        self,
        paths: Sequence[str | os.PathLike],
        *,
        is_depot_folder: Callable[[Iterable[str]], set[str]],
        local_base: Path | None = None,
    ) -> list[Target]:
        """Resolve API or command-line path arguments.

        * ``//...`` is a depot path.
        * An absolute path, or any path when ``local_base`` is given (the
          command line's current folder), is a local path.
        * Anything else is an asset path, relative to the project root.

        A trailing '/' or '/...' marks a folder, as does an existing local
        folder; an existing local file is a file. For the rest,
        ``is_depot_folder(depot_paths)`` is asked once for all of them.
        No arguments means the whole project.
        """
        if not paths:
            return [Target("", True)]
        resolved: list[tuple[str, bool | None]] = []
        for raw in paths:
            text = os.fspath(raw)
            folder_hint = text.endswith(("/", "\\", "/...", "\\..."))
            text = text.removesuffix("...").rstrip("/\\") if folder_hint else text
            if text.startswith("//"):
                asset = self.asset_path_of_depot(text) if text != self.depot_root else ""
            elif Path(text).is_absolute() or local_base is not None:
                local = Path(text) if Path(text).is_absolute() else (local_base or Path.cwd()) / text
                asset = self.asset_path_of_local(local)
            else:
                asset = str(PurePosixPath(text.replace("\\", "/"))).lstrip("/")
                asset = "" if asset == "." else asset
                if ".." in asset.split("/"):
                    raise NotInWorkspaceError(f"{text} leaves the project", hint="Use a path inside the project")
            if folder_hint or asset == "":
                resolved.append((asset, True))
                continue
            local = self.local_path(asset)
            if local.is_dir():
                resolved.append((asset, True))
            elif local.is_file():
                resolved.append((asset, False))
            else:
                resolved.append((asset, None))
        unknown = [self.depot_path(a) for a, folder in resolved if folder is None]
        folders = is_depot_folder(unknown) if unknown else set()
        return [
            Target(a, folder if folder is not None else self.depot_path(a) in folders) for a, folder in resolved
        ]
