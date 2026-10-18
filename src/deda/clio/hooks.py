"""Validation hooks: checks that run before a save is submitted (design doc
§14).

A hook looks at the files about to be submitted and reports problems, so the
artist hears about them before a server trigger rejects the change, with a
message and a hint written for them.

A hook is a function ``hook(context) -> issues``, where ``context`` is a
:class:`HookContext` and ``issues`` is an iterable of :class:`HookIssue` (or
``None``). Hooks come from three places:

* **Entry points** in the group ``deda.clio.hooks``, so that a studio package
  (or Dedaverse) adds its own checks without Clio knowing about it.
* ``deda.clio.hooks.register(name, fn)`` for the whole process, and
  ``session.hooks.register(name, fn)`` for one session.
* Built-in hooks that a config turns on by name: ``hooks = ["no-absolute-paths",
  "naming"]`` (see :data:`BUILTIN_HOOKS`).

An ``error`` issue stops the save: nothing is submitted, no file on disk
changes, and :class:`~deda.clio.ValidationError` lists the problems. A
``warning`` is reported in ``SaveResult.warnings``. A hook that raises is
reported as a warning and does not stop the save: Clio is an enhancement, not
a barrier. ``save(..., validate=False)`` (``clio save --no-validate``) skips
the hooks.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from enum import Enum
from importlib import metadata
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import TYPE_CHECKING

from deda.clio.errors import ConfigError, ValidationError

if TYPE_CHECKING:
    from deda.clio.session import Session

__all__ = [
    "BUILTIN_HOOKS",
    "ENTRY_POINT_GROUP",
    "Hook",
    "HookContext",
    "HookFile",
    "HookIssue",
    "HookRegistry",
    "HookReport",
    "IssueSeverity",
    "register",
    "run_hooks",
]

ENTRY_POINT_GROUP = "deda.clio.hooks"


class IssueSeverity(Enum):
    ERROR = "error"
    WARNING = "warning"


@dataclass(frozen=True, slots=True)
class HookIssue:
    """One problem a hook found."""

    #: Asset path of the file, or "" for a problem with the whole change.
    path: str
    message: str
    severity: IssueSeverity = IssueSeverity.ERROR
    #: What to do about it.
    hint: str = ""

    def __str__(self) -> str:
        where = f"{self.path}: " if self.path else ""
        return f"{where}{self.message}" + (f" ({self.hint})" if self.hint else "")


@dataclass(frozen=True, slots=True)
class HookFile:
    """A file in the change that is about to be submitted."""

    asset_path: str
    depot_path: str
    local_path: Path
    #: ``add``, ``edit``, ``delete``, ``branch``, ``integrate``, ...
    action: str
    file_type: str = ""

    @property
    def is_delete(self) -> bool:
        return self.action in ("delete", "move/delete")


@dataclass(frozen=True, slots=True)
class HookContext:
    session: Session
    files: tuple[HookFile, ...]
    #: The description the artist gave the change.
    message: str = ""


Hook = Callable[[HookContext], "Iterable[HookIssue] | None"]


@dataclass(frozen=True, slots=True)
class HookReport:
    issues: tuple[HookIssue, ...] = ()
    #: Names of the hooks that ran.
    ran: tuple[str, ...] = ()

    @property
    def errors(self) -> tuple[HookIssue, ...]:
        return tuple(i for i in self.issues if i.severity is IssueSeverity.ERROR)

    @property
    def warnings(self) -> tuple[HookIssue, ...]:
        return tuple(i for i in self.issues if i.severity is IssueSeverity.WARNING)


_GLOBAL: dict[str, Hook] = {}
_GLOBAL_LOCK = threading.Lock()


def register(name: str, hook: Hook) -> Callable[[], None]:
    """Add a hook for every session in this process. Returns a function that
    removes it again."""
    with _GLOBAL_LOCK:
        _GLOBAL[name] = hook

    def unregister() -> None:
        with _GLOBAL_LOCK:
            if _GLOBAL.get(name) is hook:
                del _GLOBAL[name]

    return unregister


def _entry_point_hooks() -> tuple[dict[str, Hook], list[HookIssue]]:
    found: dict[str, Hook] = {}
    problems: list[HookIssue] = []
    for entry in metadata.entry_points(group=ENTRY_POINT_GROUP):
        try:
            found[entry.name] = entry.load()
        except Exception as e:  # a broken plugin must not stop saving
            problems.append(HookIssue("", f"The hook '{entry.name}' could not be loaded: {type(e).__name__}: {e}",
                                      IssueSeverity.WARNING))
    return found, problems


@dataclass
class HookRegistry:
    """The hooks of one session: its own, the process-wide ones, the entry
    points, and the built-ins its config enables."""

    _own: dict[str, Hook] = field(default_factory=dict)

    def register(self, name: str, hook: Hook) -> Callable[[], None]:
        """Add a hook for this session only. Returns a function that removes it."""
        self._own[name] = hook

        def unregister() -> None:
            if self._own.get(name) is hook:
                del self._own[name]

        return unregister

    def collect(self, session: Session) -> tuple[dict[str, Hook], list[HookIssue]]:
        """Every hook that applies, by name (a session hook replaces a
        process-wide one, which replaces an entry point of the same name)."""
        hooks, problems = _entry_point_hooks()
        with _GLOBAL_LOCK:
            hooks.update(_GLOBAL)
        for name in session.config.hooks:
            if name not in BUILTIN_HOOKS:
                raise ConfigError(f"Unknown hook '{name}' in the 'hooks' setting "
                                  f"(built in: {', '.join(sorted(BUILTIN_HOOKS))})")
            hooks.setdefault(name, BUILTIN_HOOKS[name])
        hooks.update(self._own)
        return hooks, problems

    def names(self, session: Session) -> tuple[str, ...]:
        return tuple(self.collect(session)[0])

    def run(self, context: HookContext) -> HookReport:
        hooks, issues = self.collect(context.session)
        ran: list[str] = []
        for name, hook in hooks.items():
            ran.append(name)
            try:
                issues += list(hook(context) or ())
            except Exception as e:  # fail open: a buggy hook must not block saving
                issues.append(HookIssue("", f"The hook '{name}' could not run: {type(e).__name__}: {e}",
                                        IssueSeverity.WARNING, "Tell whoever maintains it; the save was not checked by it"))
        return HookReport(tuple(issues), tuple(ran))


def run_hooks(context: HookContext) -> HookReport:
    """Run the context's session hooks; raise :class:`ValidationError` on errors."""
    report = context.session.hooks.run(context)
    if report.errors:
        raise ValidationError(
            f"The save was stopped by validation: {len(report.errors)} problem(s)",
            issues=report.issues,
            hint="Fix these and save again. Nothing was saved and your files are unchanged. "
                 "('--no-validate' skips the checks.)",
        )
    return report


# --- built-in hooks ---------------------------------------------------------

_USD_SUFFIXES = {".usd", ".usda", ".usdc"}


def _is_absolute(path: str) -> bool:
    """An absolute path on any platform: it will not be the same on another
    artist's machine. (`./x`, `../x` and `assets/x` are relative.)"""
    return (PurePosixPath(path).is_absolute() or PureWindowsPath(path).is_absolute()
            or path.startswith(("\\\\", "//")))


def no_absolute_paths(context: HookContext) -> list[HookIssue]:
    """USD layers must not point at absolute paths (``D:/work/x.exr``,
    ``/mnt/x.usd``): they only exist on one machine. Needs USD's Python
    bindings; without them the layers are not checked."""
    try:
        from pxr import Sdf
    except ImportError:
        return []
    issues: list[HookIssue] = []
    for file in context.files:
        if file.is_delete or file.local_path.suffix.lower() not in _USD_SUFFIXES:
            continue
        layer = Sdf.Layer.FindOrOpen(str(file.local_path))
        if layer is None:
            continue
        found: set[str] = set(layer.subLayerPaths)

        def visit(path, layer=layer, found=found) -> None:
            if path.IsPrimPath() or path.IsPrimVariantSelectionPath():
                spec = layer.GetPrimAtPath(path)
                if spec is not None:
                    found.update(r.assetPath for r in spec.referenceList.GetAddedOrExplicitItems())
                    found.update(p.assetPath for p in spec.payloadList.GetAddedOrExplicitItems())
            elif path.IsPropertyPath():
                attribute = layer.GetAttributeAtPath(path)
                values = [attribute.default] if attribute is not None else []
                for value in values:
                    if isinstance(value, Sdf.AssetPath):
                        found.add(value.path)
                    elif isinstance(value, Sdf.AssetPathArray):
                        found.update(v.path for v in value)

        layer.Traverse(Sdf.Path.absoluteRootPath, visit)
        issues += [
            HookIssue(file.asset_path, f"refers to the absolute path {p}",
                      hint="Use a path relative to this file (./tex/a.png) so it works on every machine")
            for p in sorted(found) if p and not p.startswith("`") and _is_absolute(p)
        ]
    return issues


def naming(context: HookContext) -> list[HookIssue]:
    """New files must match the ``naming_pattern`` setting (a regular
    expression for the whole file name, for example ``[a-z0-9_]+\\.[a-z0-9]+``).
    Only added files are checked, so old files do not block a save."""
    pattern = context.session.config.naming_pattern
    if not pattern:
        return []
    try:
        regex = re.compile(pattern)
    except re.error as e:
        raise ConfigError(f"The 'naming_pattern' setting is not a valid regular expression: {e}") from None
    return [
        HookIssue(f.asset_path, f"'{PurePosixPath(f.asset_path).name}' does not match the naming rule {pattern}",
                  hint="Rename the file before saving")
        for f in context.files
        if f.action in ("add", "move/add", "branch") and not regex.fullmatch(PurePosixPath(f.asset_path).name)
    ]


#: Built-in hooks, turned on by name in the ``hooks`` setting.
BUILTIN_HOOKS: dict[str, Hook] = {
    "no-absolute-paths": no_absolute_paths,
    "naming": naming,
}
