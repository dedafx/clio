"""USD integration for Clio.

Clio's USD plugin (``clioUsd``) replaces USD's default asset resolver with a
subclass of it (design doc §10.3). Layers keep ordinary paths, so they open
in any USD without Clio. With the plugin installed and a Clio context
bound, files inside the project are fetched from Perforce, at the context's
version, before USD reads them. Without a Clio context the plugin behaves
exactly like USD's default resolver.

This module helps Python code find and configure the plugin. It does not
import ``pxr`` until needed.
"""

from __future__ import annotations

import os
from pathlib import Path

_RESOURCES = Path(__file__).parent / "plugin" / "clioUsd" / "resources"


def plugin_path() -> Path | None:
    """Directory to add to ``PXR_PLUGINPATH_NAME`` for the bundled plugin.

    Returns ``None`` if this installation was built without the USD plugin.
    """
    return _RESOURCES if (_RESOURCES / "plugInfo.json").is_file() else None


def register_plugin() -> None:
    """Register the bundled plugin with USD's plugin registry.

    Must run before USD creates its asset resolver, which happens the first
    time anything resolves a path (for example opening a stage). Prefer
    setting ``PXR_PLUGINPATH_NAME`` before USD starts, which also covers
    processes that do not import this module.
    """
    path = plugin_path()
    if path is None:
        raise RuntimeError("This deda-clio build does not include the USD plugin")
    from pxr import Plug

    Plug.Registry().RegisterPlugins([os.fspath(path)])


def create_context(settings: str):
    """Create an ``Ar.ResolverContext`` that turns Clio on for a stage.

    ``settings`` uses the same text form as :class:`deda.clio.Settings`, for
    example ``"depot=//imagine/main;root=/work/imagine;pin=@18234"``. Pass the
    result to ``Usd.Stage.Open(path, context)``.
    """
    from pxr import Ar

    return Ar.GetResolver().CreateContextFromString(settings)
