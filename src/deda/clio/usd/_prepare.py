"""Option A: make a stage's files present before USD opens it (design doc
§10.2), and get layers ready to be saved (§10.6), in pure Python.

``prepare`` walks the dependency tree in waves. Each wave resolves every
file found so far at once, on threads, through the same C++ core that the
USD plugin uses (``deda.clio._core.AssetResolver``), so the files land where
the plugin would put them and pins mean the same thing. The core collects
the threads' misses into one ``p4 sync`` per wave (or parallel ``p4 print``
for pinned versions), so a shot that is five layers deep costs about five
server round trips, not one per file.

It needs USD's Python bindings (``pxr``) but not the compiled plugin, so it
works in a DCC or USD version the plugin is not built for, for farm
prefetch, and as a test oracle for the plugin.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from deda.clio import _core
from deda.clio.errors import NotInWorkspaceError
from deda.clio.events import CancelToken, ProgressCallback, ProgressEvent
from deda.clio.models import EditReport

if TYPE_CHECKING:
    from deda.clio.session import Session

__all__ = ["PrepareReport", "finish_edit", "prepare", "prepare_for_edit", "resolver_settings"]

#: Files whose contents are scanned for more dependencies.
_LAYER_EXTENSIONS = {".usd", ".usda", ".usdc", ".sdf"}
#: Threads resolving one wave. The core's pool bounds the server connections.
_MAX_THREADS = 16

PayloadFilter = Callable[[str, str], bool]


@dataclass(frozen=True, slots=True)
class PrepareReport:
    """What :func:`prepare` found and made present."""

    #: Open this: the root layer, in the workspace or the version store.
    root_path: Path
    #: Every USD layer reached, root first.
    layers: tuple[Path, ...]
    #: Other files reached (textures, clips, volumes) when ``assets`` is set.
    assets: tuple[Path, ...]
    #: References that could not be provided, as written in the layers.
    missing: tuple[str, ...]
    #: Things to show the user: a local file used instead of the exact
    #: version, a layer that could not be read, an expression not evaluated.
    warnings: tuple[str, ...]
    #: How many rounds of resolving it took.
    waves: int
    #: ``p4 sync`` commands the core ran. Diagnostic: one per wave at most.
    sync_commands: int
    #: The opened layers. Keeping the report alive keeps them in USD's layer
    #: registry, so ``Usd.Stage.Open`` reuses them instead of parsing again.
    keepalive: tuple[Any, ...] = field(default=(), repr=False, compare=False)


def resolver_settings(
    session: Session,
    pin: str | _core.Pin | None = None,
    *,
    policy: str | _core.Policy | None = None,
    store: str | os.PathLike | None = None,
) -> _core.Settings:
    """Core settings for the session's project, the text form of a USD
    resolver context. ``pin`` is ``have`` (default), ``latest``, ``@<change>``
    or ``@<label>``."""
    config = session.config
    paths = session.paths
    pin_text = "have" if pin is None else str(pin)
    values: dict[str, str] = {"depot": paths.depot_root, "root": os.fspath(paths.root), "pin": pin_text}
    if store is not None:
        values["store"] = os.fspath(store)
    if policy is not None:
        values["policy"] = (policy.name.lower() if isinstance(policy, _core.Policy) else str(policy))
    for key, value in (
        ("port", config.port),
        ("user", session.user),
        ("client", session.client),
        ("tickets", config.tickets),
        ("timeout", config.timeout),
        ("connect_timeout", config.connect_timeout),
    ):
        if value not in ("", None):
            values[key] = str(value)
    return _core.Settings.parse(";".join(f"{k}={v}" for k, v in values.items()))


def _key(path: str | os.PathLike) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(path)))


class _Walker:
    def __init__(self, session: Session, resolver: _core.AssetResolver, payloads: str | PayloadFilter,
                 assets: bool) -> None:
        from pxr import Sdf  # imported here: only prepare needs USD

        self._Sdf = Sdf
        self._root = Path(session.paths.root)
        self._resolver = resolver
        self._payloads = payloads
        self._assets = assets
        self.warnings: list[str] = []

    # --- finding references ---------------------------------------------

    def references(self, layer: Any) -> list[tuple[str, bool]]:
        """(authored path, is_layer) for everything ``layer`` points at."""
        Sdf = self._Sdf
        found: list[tuple[str, bool]] = []

        def add(authored: str, is_layer: bool) -> None:
            if authored:
                found.append((authored, is_layer))

        for sublayer in layer.subLayerPaths:
            add(sublayer, True)

        def visit(path: Any) -> None:
            if path.IsPrimPath() or path.IsPrimVariantSelectionPath():
                spec = layer.GetPrimAtPath(path)
                if spec is None:
                    return
                for ref in spec.referenceList.GetAddedOrExplicitItems():
                    add(ref.assetPath, True)
                for payload in spec.payloadList.GetAddedOrExplicitItems():
                    if self._wants_payload(str(path), payload.assetPath):
                        add(payload.assetPath, True)
                if self._assets and spec.HasInfo("clips"):
                    for clip_set in spec.GetInfo("clips").values():
                        for clip in clip_set.get("assetPaths", ()):
                            add(clip.path if isinstance(clip, Sdf.AssetPath) else str(clip), False)
                        manifest = clip_set.get("manifestAssetPath")
                        if manifest:
                            add(manifest.path if isinstance(manifest, Sdf.AssetPath) else str(manifest), False)
                        if clip_set.get("templateAssetPath"):
                            self.warnings.append(f"{path}: clip template {clip_set['templateAssetPath']} "
                                                 "is not expanded; its files are not prepared")
            elif self._assets and path.IsPropertyPath():
                attribute = layer.GetAttributeAtPath(path)
                if attribute is None:
                    return
                values = [attribute.default]
                for time in layer.ListTimeSamplesForPath(path):
                    values.append(layer.QueryTimeSample(path, time))
                for value in values:
                    if isinstance(value, Sdf.AssetPath):
                        add(value.path, False)
                    elif isinstance(value, Sdf.AssetPathArray):
                        for item in value:
                            add(item.path, False)

        layer.Traverse(Sdf.Path.absoluteRootPath, visit)
        return found

    def _wants_payload(self, prim_path: str, asset_path: str) -> bool:
        if self._payloads == "all":
            return True
        if self._payloads == "none":
            return False
        return bool(self._payloads(prim_path, asset_path))  # type: ignore[operator]

    def candidate(self, layer: Any, authored: str) -> Path | None:
        """The local path ``authored`` means from ``layer``, or None if it is
        not something Clio can provide (an expression, or outside the
        project)."""
        if authored.startswith("`"):
            self.warnings.append(f"{authored}: an expression is not evaluated; its files are not prepared")
            return None
        anchored = self._Sdf.ComputeAssetPathRelativeToLayer(layer, authored) or authored
        path = Path(anchored)
        if not path.is_absolute():
            path = self._root / anchored  # the workspace root is the first search path
        return path if self._resolver.manages(path) else None


def prepare(
    session: Session,
    root: str | os.PathLike,
    *,
    pin: str | _core.Pin | None = None,
    payloads: str | PayloadFilter = "all",
    assets: bool = True,
    store: str | os.PathLike | None = None,
    progress: ProgressCallback | None = None,
    cancel: CancelToken | None = None,
) -> PrepareReport:
    """Make a USD stage's files present, then open ``report.root_path``.

    ``root`` is the root layer: an asset path (``"shots/sh010/shot.usda"``)
    or a local path in the workspace. ``pin`` is ``have`` (the default: files
    on disk are used as they are, missing ones are synced), ``latest``,
    ``@<change>`` or ``@<label>``. Layers reached from a pinned layer stay at
    that pin.

    ``payloads`` is ``"all"``, ``"none"`` or ``callable(prim_path, asset_path)
    -> bool``. ``assets`` also prepares textures, clips and volumes. The walk
    works from layer contents, so it covers every variant, not only the
    selected ones; ``payloads`` narrows it.
    """
    settings = resolver_settings(session, pin, store=store)
    resolver = _core.AssetResolver(settings)
    paths = session.paths
    text = os.fspath(root)
    local_root = (Path(text) if Path(text).is_absolute()
                  else paths.local_path(text.replace("\\", "/").lstrip("/")))
    # Raises NotInWorkspaceError for a path outside the project.
    local_root = paths.local_path(paths.asset_path_of_local(local_root))

    walker = _Walker(session, resolver, payloads, assets)  # imports pxr
    seen: set[str] = set()
    layers: list[Path] = []
    other: list[Path] = []
    missing: list[str] = []
    warnings: list[str] = walker.warnings
    keepalive: list[Any] = []
    root_path: Path | None = None
    waves = 0
    found = 1
    done = 0

    from pxr import Sdf

    frontier: list[tuple[Path, str]] = [(local_root, text)]
    seen.add(_key(local_root))
    with ThreadPoolExecutor(max_workers=_MAX_THREADS, thread_name_prefix="clio-prepare") as pool:
        while frontier:
            if cancel:
                cancel.raise_if_cancelled()
            waves += 1
            results = list(pool.map(lambda item: resolver.resolve_path(item[0]), frontier))
            next_frontier: list[tuple[Path, str]] = []
            for (wanted, authored), info in zip(frontier, results):
                done += 1
                if info is None:
                    missing.append(authored)
                    continue
                if info["warning"]:
                    warnings.append(info["warning"])
                local = Path(info["local_path"])
                if root_path is None:
                    root_path = local
                if local.suffix.lower() not in _LAYER_EXTENSIONS:
                    other.append(local)
                    continue
                layers.append(local)
                try:
                    layer = Sdf.Layer.FindOrOpen(os.fspath(local))
                except Exception as e:  # a Tf error for a file that is not valid USD
                    warnings.append(f"{local} could not be read: {e}")
                    continue
                if layer is None:
                    warnings.append(f"{local} could not be read as a USD layer")
                    continue
                keepalive.append(layer)
                for reference, _is_layer in walker.references(layer):
                    candidate = walker.candidate(layer, reference)
                    if candidate is None or _key(candidate) in seen:
                        continue
                    seen.add(_key(candidate))
                    next_frontier.append((candidate, reference))
            found += len(next_frontier)
            if progress:
                progress(ProgressEvent("prepare", done, found, current_path=os.fspath(local_root)))
            frontier = next_frontier

    if root_path is None:
        raise NotInWorkspaceError(f"{text} is not in Perforce and not on disk",
                                  hint="Check the path, or the pin")
    return PrepareReport(root_path, tuple(layers), tuple(other), tuple(missing), tuple(warnings), waves,
                         resolver.sync_commands_run, tuple(keepalive))


def prepare_for_edit(session: Session, layer_paths: Iterable[str | os.PathLike | Sequence[str]]) -> EditReport:
    """Get layers ready to be saved: open them for edit (a ``+l`` file type
    locks them by that), the Python counterpart of what the USD plugin does
    when a layer is saved. DCC integrations call it from their save
    callbacks. One ``fstat`` and one ``edit`` however many layers.

    Raises :class:`~deda.clio.LockedByOtherError` before opening anything if
    someone else holds a lock. Files in ``EditReport.new`` are not in
    Perforce yet: save them, then call :func:`finish_edit`. Never submits.
    """
    return session.workspace.open_for_edit(*layer_paths)  # type: ignore[arg-type]


def finish_edit(session: Session, report: EditReport) -> tuple[str, ...]:
    """After saving: open the report's new files for add. Returns the asset
    paths opened for add."""
    return session.workspace.add_new(*report.new)
