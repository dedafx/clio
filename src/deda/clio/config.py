"""Layered configuration (design doc §6).

Later layers win:

1. Built-in defaults.
2. Site config: ``$CLIO_SITE_CONFIG``.
3. User config: ``$CLIO_USER_CONFIG``, else ``%APPDATA%\\clio\\config.toml``
   on Windows and ``$XDG_CONFIG_HOME/clio/config.toml`` (or
   ``~/.config/clio/config.toml``) elsewhere.
4. Project config: ``.clio.toml`` in the current folder or a parent.
5. The standard Perforce environment (``P4PORT``, ``P4USER``, ``P4CLIENT``,
   ``P4CONFIG``, ...), which the Perforce API reads for any value left empty.
6. Keyword arguments to :func:`load_config` (or ``clio`` command options).

A config file holds top-level keys and, in site and user files, one table
per project, chosen by ``project``::

    port = "ssl:perforce:1666"

    [projects.imagine]
    depot = "//imagine/main"
    root = "D:/work/imagine"
"""

from __future__ import annotations

import os
import sys
import tomllib
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Any

from deda.clio.errors import ConfigError

__all__ = ["Config", "load_config", "user_config_path"]

PROJECT_FILE = ".clio.toml"


@dataclass(frozen=True, slots=True)
class Config:
    """Everything Clio needs to reach a project. Empty strings mean "use the
    Perforce environment"."""

    #: Project name, used to pick a ``[projects.<name>]`` table and to name
    #: new workspaces.
    project: str = ""
    #: Perforce server (P4PORT).
    port: str = ""
    #: Perforce user (P4USER).
    user: str = ""
    #: Client workspace (P4CLIENT).
    client: str = ""
    #: Ticket file (P4TICKETS).
    tickets: str = ""
    #: Depot path of the project root, for example ``//imagine/main``. When
    #: empty, it is taken from the workspace's stream or view.
    depot: str = ""
    #: Stream for new workspaces (``clio setup``); empty for a classic view.
    stream: str = ""
    #: Local folder of the workspace. Taken from the workspace when empty.
    root: str = ""
    #: Parallel transfer threads for get and save (1 disables it).
    parallel_threads: int = 4
    #: Minimum number of files before transfers go parallel.
    parallel_min_files: int = 8
    #: How long history and activity stay fresh before a cheap server check.
    poll_interval: float = 30.0
    #: Where ``discard`` copies files before reverting them.
    backup_dir: str = ""
    #: Seconds before an unreachable server is given up on.
    connect_timeout: int = 10
    #: Seconds a single Perforce command may run.
    timeout: int = 120
    #: Where each setting came from, for ``clio doctor`` style reports.
    sources: dict[str, str] = field(default_factory=dict, compare=False)

    def with_overrides(self, source: str = "arguments", **values: Any) -> Config:
        """A copy with ``values`` applied (``None`` values are ignored)."""
        values = {k: v for k, v in values.items() if v is not None}
        _check_keys(values, source)
        coerced = {k: _coerce(k, v, source) for k, v in values.items()}
        sources = dict(self.sources)
        sources.update(dict.fromkeys(coerced, source))
        return replace(self, **coerced, sources=sources)

    @property
    def backup_path(self) -> Path:
        """Where ``discard`` keeps copies: ``backup_dir``, else
        ``%LOCALAPPDATA%\\clio\\backups`` on Windows and
        ``$XDG_STATE_HOME/clio/backups`` (or ``~/.local/state/...``) elsewhere."""
        if self.backup_dir:
            return Path(self.backup_dir)
        if sys.platform == "win32":
            base = os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local"
        else:
            base = os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state"
        return Path(base) / "clio" / "backups"


_KEYS = {f.name: f.type for f in fields(Config) if f.name != "sources"}


def _check_keys(values: dict[str, Any], source: str) -> None:
    unknown = sorted(set(values) - set(_KEYS))
    if unknown:
        raise ConfigError(f"Unknown setting(s) {', '.join(unknown)} in {source}")


def _coerce(key: str, value: Any, source: str) -> Any:
    kind = _KEYS[key]
    try:
        if kind == "int":
            if isinstance(value, bool):
                raise TypeError
            return int(value)
        if kind == "float":
            return float(value)
        if isinstance(value, os.PathLike):
            return os.fspath(value)
        if not isinstance(value, str):
            raise TypeError
        return value
    except (TypeError, ValueError):
        raise ConfigError(f"Setting '{key}' in {source} must be {kind}, got {value!r}") from None


def _config_home() -> Path:
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        return Path(appdata) / "clio" if appdata else Path.home() / "AppData" / "Roaming" / "clio"
    xdg = os.environ.get("XDG_CONFIG_HOME")
    return (Path(xdg) if xdg else Path.home() / ".config") / "clio"


def user_config_path() -> Path:
    """The user config file (it may not exist)."""
    explicit = os.environ.get("CLIO_USER_CONFIG")
    return Path(explicit) if explicit else _config_home() / "config.toml"


def _read(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ConfigError(f"Could not read {path}: {e}") from None


def _find_project_file(start: Path) -> Path | None:
    for folder in (start, *start.parents):
        candidate = folder / PROJECT_FILE
        if candidate.is_file():
            return candidate
    return None


def _layer(config: Config, data: dict[str, Any], source: str, project: str) -> Config:
    projects = data.pop("projects", {})
    if not isinstance(projects, dict):
        raise ConfigError(f"'projects' in {source} must be a table")
    config = config.with_overrides(source, **data)
    name = project or config.project
    if name and name in projects:
        config = config.with_overrides(f"{source} [projects.{name}]", **projects[name])
    return config


def load_config(*, cwd: str | os.PathLike | None = None, **overrides: Any) -> Config:
    """Read every config layer and apply ``overrides`` last.

    ``cwd`` is where the search for ``.clio.toml`` starts (default: the
    current folder).
    """
    project = overrides.get("project") or ""
    config = Config()
    site = os.environ.get("CLIO_SITE_CONFIG")
    if site:
        config = _layer(config, _read(Path(site)), site, project)
    user_file = user_config_path()
    config = _layer(config, _read(user_file), os.fspath(user_file), project)
    project_file = _find_project_file(Path(cwd) if cwd else Path.cwd())
    if project_file:
        config = _layer(config, _read(project_file), os.fspath(project_file), project)
    return config.with_overrides("arguments", **overrides)
