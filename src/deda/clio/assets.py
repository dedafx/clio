"""Assets: named groups of files (design doc §14, open question 6).

Clio works on **files**. USD calls a file an *asset*; Dedaverse calls it an
*element*. In Dedaverse, an **asset** is a higher-level collection of related
elements: a character "Bob" is a mesh, a look, textures, a skeleton and
animation data, work files and published files. In Imagine, an asset is a
photograph being prepared for print, and its variants are parts of the same
asset.

Clio does not need to know what an asset *is*, only which files make it up.
An **asset resolver plugin** answers that: given an asset id, it lists the
project paths (files or folders) of its parts. Then ``session.assets`` runs
the everyday actions on the whole asset::

    s.assets.get("dedaverse:Bob")
    s.assets.lock("dedaverse:Bob")
    s.assets.save("dedaverse:Bob", message="Bob: new look")

Plugins are Python objects with a ``name`` and ``files(session, asset_id)``,
registered in the entry point group ``deda.clio.resolvers`` (or with
``session.assets.register``). The built-in ``folder`` plugin treats an asset
as the folder at that path, and is the default.

This is separate from the USD asset resolver (``clioUsd``), which resolves one
file path at a time: whether it is on disk, and if not, which revision to
fetch (design doc §10).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from deda.clio.errors import AssetNotFoundError, ConfigError, WorkflowError

if TYPE_CHECKING:
    from deda.clio.models import FileStatus, Lock, SaveResult, SyncPlan, SyncReport
    from deda.clio.session import Session

__all__ = ["AssetFiles", "AssetResolverPlugin", "Assets", "ENTRY_POINT_GROUP", "FolderResolver"]

ENTRY_POINT_GROUP = "deda.clio.resolvers"
DEFAULT_KIND = "folder"


class AssetResolverPlugin(Protocol):
    """Says which files make up an asset."""

    #: The kind in ``kind:id`` (``dedaverse``, ``imagine``, ...).
    name: str

    def files(self, session: Session, asset_id: str) -> Iterable[str]:
        """Project paths (asset paths, depot paths or absolute local paths;
        files or folders) of the parts of the asset ``asset_id``. Raise
        :class:`~deda.clio.AssetNotFoundError` if there is no such asset."""
        ...


@dataclass(frozen=True, slots=True)
class AssetFiles:
    """The parts of one asset."""

    kind: str
    asset_id: str
    #: Files and folders, as the plugin gave them, without duplicates.
    paths: tuple[str, ...]

    @property
    def spec(self) -> str:
        return f"{self.kind}:{self.asset_id}"


class FolderResolver:
    """An asset is the folder at its path: every file under it."""

    name = DEFAULT_KIND

    def files(self, session: Session, asset_id: str) -> Iterable[str]:
        folder = asset_id.strip().replace("\\", "/").rstrip("/")
        if not folder:
            raise AssetNotFoundError("An asset folder needs a path", hint="Say which folder, for example props/crate")
        return [folder + "/"]


class Assets:
    """Asset-level actions for one session (``session.assets``)."""

    def __init__(self, session: Session) -> None:
        self._session = session
        self._own: dict[str, AssetResolverPlugin] = {DEFAULT_KIND: FolderResolver()}

    # --- plugins -------------------------------------------------------------

    def register(self, plugin: AssetResolverPlugin) -> None:
        """Add a plugin for this session (replacing one of the same name)."""
        self._own[plugin.name] = plugin

    def kinds(self) -> tuple[str, ...]:
        return tuple(sorted(self._plugins()))

    def _plugins(self) -> dict[str, AssetResolverPlugin]:
        plugins: dict[str, AssetResolverPlugin] = {}
        for entry in metadata.entry_points(group=ENTRY_POINT_GROUP):
            try:
                loaded = entry.load()
                plugin = loaded() if isinstance(loaded, type) else loaded
                plugins[getattr(plugin, "name", entry.name)] = plugin
            except Exception as e:  # a broken plugin must not hide the others
                raise ConfigError(f"The asset resolver '{entry.name}' could not be loaded: "
                                  f"{type(e).__name__}: {e}") from None
        plugins.update(self._own)
        return plugins

    # --- from an id to files ---------------------------------------------------

    def parse(self, spec: str) -> tuple[str, str]:
        """``"dedaverse:Bob"`` -> ``("dedaverse", "Bob")``. Without a known kind
        in front, the whole text is an id for the default kind (the
        ``asset_resolver`` setting, else ``folder``), so a folder such as
        ``props/crate`` or ``C:/work/crate`` needs no prefix."""
        plugins = self._plugins()
        kind, sep, asset_id = spec.partition(":")
        if sep and kind in plugins:
            return kind, asset_id
        default = self._session.config.asset_resolver or DEFAULT_KIND
        if default not in plugins:
            raise ConfigError(f"Unknown asset resolver '{default}' in the 'asset_resolver' setting "
                              f"(known: {', '.join(sorted(plugins))})")
        return default, spec

    def files(self, spec: str) -> AssetFiles:
        """The parts of an asset. Raises :class:`~deda.clio.AssetNotFoundError`
        if the plugin knows no such asset or lists nothing for it."""
        kind, asset_id = self.parse(spec)
        plugin = self._plugins()[kind]
        try:
            paths = tuple(dict.fromkeys(str(p) if not isinstance(p, Path) else p.as_posix()
                                        for p in plugin.files(self._session, asset_id)))
        except (AssetNotFoundError, ConfigError):
            raise
        except Exception as e:  # a plugin bug: say which plugin
            raise WorkflowError(f"The asset resolver '{kind}' failed for '{asset_id}': {type(e).__name__}: {e}",
                                hint="Tell whoever maintains the plugin") from None
        if not paths:
            raise AssetNotFoundError(f"The asset '{kind}:{asset_id}' has no files",
                                     hint="Check the asset's name, or that its files are in this project")
        return AssetFiles(kind, asset_id, paths)

    # --- everyday actions on a whole asset -----------------------------------------

    def get(self, spec: str, **options) -> SyncReport | SyncPlan:
        """``Workspace.get`` for every part of the asset."""
        return self._session.workspace.get(*self.files(spec).paths, **options)

    def status(self, spec: str, **options) -> list[FileStatus]:
        """``Workspace.status`` for every part of the asset."""
        return self._session.workspace.status(*self.files(spec).paths, **options)

    def lock(self, spec: str, **options) -> tuple[Lock, ...]:
        """``Workspace.lock`` for every part of the asset."""
        return self._session.workspace.lock(*self.files(spec).paths, **options)

    def unlock(self, spec: str) -> tuple[str, ...]:
        """``Workspace.unlock`` for every part of the asset."""
        return self._session.workspace.unlock(*self.files(spec).paths)

    def save(self, spec: str, *, message: str, **options) -> SaveResult:
        """``Workspace.save`` for every part of the asset, in one change."""
        return self._session.workspace.save(*self.files(spec).paths, message=message, **options)

    def activity(self, spec: str, *, limit: int = 20):
        """Recent changes that touched the asset, newest first, merged from
        all its parts."""
        changes = {}
        for path in self.files(spec).paths:
            for change in self._session.history.activity(path.rstrip("/"), limit=limit).items:
                changes[change.number] = change
        return tuple(sorted(changes.values(), key=lambda c: c.number, reverse=True)[:limit])

