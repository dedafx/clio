"""Sessions: connect, log in, set up a workspace (design doc §5.1, §6)."""

from __future__ import annotations

import os
import re
import socket
import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from functools import cached_property
from pathlib import Path
from typing import Any

from deda.clio._paths import ProjectPaths
from deda.clio.backends.base import Backend
from deda.clio.backends.core import CoreBackend
from deda.clio.config import Config, load_config
from deda.clio.errors import AuthError, ConfigError, NotInWorkspaceError, WorkflowError
from deda.clio.events import EventBus

__all__ = ["Session", "connect"]

PasswordPrompt = Callable[[str], str | None]

#: Options for workspaces Clio creates: files are read-only until locked,
#: and the modification time is the sync time (so USD reloads see changes).
WORKSPACE_OPTIONS = "noallwrite noclobber nocompress unlocked nomodtime normdir"


def connect(project: str | None = None, *, backend: Backend | None = None, **settings: Any) -> Session:
    """Open a :class:`Session`.

    Reads the layered config (§6) for ``project``; keyword ``settings``
    (``port``, ``user``, ``client``, ``depot``, ``root``, ...) override it.
    Reuses the existing Perforce ticket and never prompts. Nothing is sent
    to the server until the first command.
    """
    config = load_config(project=project, **settings)
    return Session(config, backend=backend)


class Session:
    """A connection to one project. Thread-safe: each thread uses its own
    Perforce connection (design doc §8.1)."""

    def __init__(self, config: Config, *, backend: Backend | None = None) -> None:
        self.config = config
        self.backend: Backend = backend or CoreBackend(config)
        self.events = EventBus()
        self._lock = threading.Lock()
        self._executor: ThreadPoolExecutor | None = None
        self._info: dict[str, str] | None = None

    # --- lifecycle ------------------------------------------------------

    def __enter__(self) -> Session:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        with self._lock:
            executor, self._executor = self._executor, None
        if executor:
            executor.shutdown(wait=True)
        self.backend.close()

    def submit_bg(self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Future:
        """Run ``fn(*args, **kwargs)`` on a worker thread (design doc §5.6)."""
        with self._lock:
            if self._executor is None:
                self._executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="clio")
            return self._executor.submit(fn, *args, **kwargs)

    # --- who and where --------------------------------------------------

    def info(self, *, refresh: bool = False) -> dict[str, str]:
        """``p4 info``, asked once per session."""
        if self._info is None or refresh:
            result = self.backend.run("info")
            if result.errors or not result.records:
                raise WorkflowError("Could not get server information", p4_messages=result.texts)
            self._info = result.records[0]
        return self._info

    @property
    def user(self) -> str:
        return self.config.user or self.info()["userName"]

    @property
    def client(self) -> str:
        name = self.config.client or self.info().get("clientName", "")
        if not name or name == "*unknown*":
            raise NotInWorkspaceError("There is no Perforce workspace", hint="Run 'clio setup'")
        return name

    @property
    def server_id(self) -> str:
        """Identifies the server for caches: its server ID where set,
        otherwise its address and root (design doc §9.3)."""
        info = self.info()
        return info.get("serverID") or f"{info.get('serverAddress', '')}|{info.get('serverRoot', '')}|{self.config.port}"

    @cached_property
    def paths(self) -> ProjectPaths:
        """The project's depot root and workspace root."""
        depot = self.config.depot.rstrip("/")
        root = self.config.root
        if not depot or not root:
            spec = self._client_spec(self.client)
            if not spec.get("Access") and not spec.get("Update"):
                raise NotInWorkspaceError(f"Workspace {self.client} does not exist", hint="Run 'clio setup'")
            root = root or spec.get("Root", "")
            depot = depot or self._depot_from_spec(spec)
        if not depot.startswith("//"):
            raise ConfigError(f"The project depot path must start with //, got '{depot}'")
        return ProjectPaths(depot.removesuffix("/..."), Path(root))

    @cached_property
    def workspace(self):  # -> deda.clio.workspace.Workspace
        from deda.clio.workspace import Workspace

        return Workspace(self)

    @cached_property
    def history(self):  # -> deda.clio.history.History
        from deda.clio.history import History

        return History(self)

    def _client_spec(self, name: str) -> dict[str, str]:
        result = self.backend.run("client", ["-o", name])
        if result.errors or not result.records:
            raise WorkflowError(f"Could not read workspace {name}", p4_messages=result.texts)
        return result.records[0]

    def _depot_from_spec(self, spec: dict[str, str]) -> str:
        if spec.get("Stream"):
            return spec["Stream"]
        views = [v for k, v in sorted(spec.items()) if re.fullmatch(r"View\d+", k)]
        mapped = [v.split()[0] for v in views if v and not v.startswith("-")]
        if len(mapped) != 1 or not mapped[0].endswith("/..."):
            raise ConfigError(
                "Cannot tell the project root from the workspace view; set 'depot' in the config"
            )
        return mapped[0].removesuffix("/...")

    # --- authentication (§6) --------------------------------------------

    def login(self, password: str | None = None, *, prompt: PasswordPrompt | None = None) -> None:
        """Log in with ``password``, or ask ``prompt(text)`` for it. Clio
        never stores passwords; Perforce keeps a ticket."""

        def answer(text: str, no_echo: bool) -> str | None:
            if password is not None:
                return password
            return prompt(text) if prompt else None

        backend = CoreBackend(self.config, prompt=answer)
        try:
            result = backend.run("login")
        finally:
            backend.close()
        if result.errors:
            raise AuthError("Login failed", hint="Check your user name and password", p4_messages=result.texts)
        self._info = None

    def logout(self) -> None:
        result = self.backend.run("logout")
        if result.errors:
            raise WorkflowError("Logout failed", p4_messages=result.texts)

    def login_status(self) -> str:
        """Perforce's description of the current ticket. Raises
        :class:`AuthError` when not logged in."""
        result = self.backend.run("login", ["-s"])
        if result.errors:
            raise AuthError("You are not logged in to Perforce", hint="Run 'clio login'", p4_messages=result.texts)
        record = result.records[0] if result.records else {}
        seconds = int(record.get("TicketExpiration", 0) or 0)
        return f"{record.get('User', self.user)}, ticket expires in {seconds // 3600} h {seconds % 3600 // 60} min"

    # --- first-time setup (§6) ------------------------------------------

    def default_workspace_name(self) -> str:
        project = self.config.project or self.config.depot.strip("/").replace("/", "_") or "clio"
        host = socket.gethostname().split(".")[0]
        return re.sub(r"[^A-Za-z0-9_.-]", "_", f"{self.user}_{host}_{project}")

    def setup(self, *, root: str | os.PathLike | None = None, name: str | None = None) -> str:
        """Create the workspace if it does not exist and return its name.

        The workspace maps the project depot root (or ``stream``) to
        ``root`` one to one, with read-only files until they are locked.
        """
        config = self.config
        name = name or config.client or self.default_workspace_name()
        root_path = Path(root or config.root or "")
        if not root_path.parts:
            raise ConfigError("Set 'root' (the local folder for the project) before running setup")
        if not config.depot and not config.stream:
            raise ConfigError("Set 'depot' (for example //imagine/main) or 'stream' before running setup")
        spec = self._client_spec(name)
        if spec.get("Access") or spec.get("Update"):
            existing = Path(spec.get("Root", ""))
            if os.path.normcase(os.path.abspath(existing)) != os.path.normcase(os.path.abspath(root_path)):
                raise ConfigError(f"Workspace {name} already exists with root {existing}")
        else:
            lines = [
                f"Client: {name}",
                f"Owner: {self.user}",
                f"Root: {root_path.resolve()}",
                f"Options: {WORKSPACE_OPTIONS}",
                "SubmitOptions: submitunchanged",
                "LineEnd: local",
                "Description:\n\tCreated by Clio.",
            ]
            if config.stream:
                lines.append(f"Stream: {config.stream}")
            else:
                depot = config.depot.rstrip("/").removesuffix("/...")
                lines.append(f"View:\n\t{depot}/... //{name}/...")
            result = self.backend.run("client", ["-i"], "\n".join(lines) + "\n")
            if result.errors:
                raise WorkflowError(f"Could not create workspace {name}", p4_messages=result.texts)
            root_path.mkdir(parents=True, exist_ok=True)
        self._switch_client(name, root_path)
        return name

    def _switch_client(self, name: str, root: Path) -> None:
        self.config = self.config.with_overrides("setup", client=name, root=os.fspath(root.resolve()))
        self.backend.close()
        if isinstance(self.backend, CoreBackend):
            self.backend = CoreBackend(self.config)
        self._info = None
        for attr in ("paths", "workspace", "history"):
            self.__dict__.pop(attr, None)

