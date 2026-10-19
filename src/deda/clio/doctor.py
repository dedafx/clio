"""``clio doctor``: is this machine set up to work well with Clio?

Each check reports ``ok``, ``warn`` (works, but slower or riskier than it
needs to be), ``fail`` (does not work) or ``skip`` (could not be checked, for
example because the server is unreachable). A check never raises: a problem
in one check does not hide the others.

Clio does not change the server. The checks about server settings tell the
admin what to set; ``recommended_typemap()`` is the typemap from design doc
§8.5, ready to paste into ``p4 typemap``.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

from deda.clio.errors import ClioError

if TYPE_CHECKING:
    from deda.clio.session import Session

__all__ = ["Check", "CheckStatus", "DoctorReport", "RECOMMENDED_TYPEMAP", "recommended_typemap", "run_doctor"]


class CheckStatus(Enum):
    OK = "ok"
    WARN = "warn"
    FAIL = "fail"
    SKIP = "skip"


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    status: CheckStatus
    message: str
    #: What to do about it (empty when there is nothing to do).
    hint: str = ""


@dataclass(frozen=True, slots=True)
class DoctorReport:
    checks: tuple[Check, ...]

    @property
    def ok(self) -> bool:
        """No check failed. Warnings do not count."""
        return all(c.status is not CheckStatus.FAIL for c in self.checks)

    def by_status(self, status: CheckStatus) -> tuple[Check, ...]:
        return tuple(c for c in self.checks if c.status is status)


#: (file type, extensions): the typemap recommended in design doc §8.5.
RECOMMENDED_TYPEMAP: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("binary+l", ("ma", "mb", "hip", "hipnc", "blend", "spp", "psd", "ztl", "uasset", "umap")),
    ("binary+Fl", ("exr", "png", "jpg", "tif", "mp4", "zip", "abc", "usdc", "vdb")),
    ("text", ("usda", "json", "py", "txt", "mtlx")),
)


def recommended_typemap() -> str:
    """The recommended typemap, in the form ``p4 typemap`` takes: exclusive
    locks for unmergeable authoring files, uncompressed storage for files that
    are already compressed, and mergeable text for text."""
    lines = ["TypeMap:"]
    for file_type, extensions in RECOMMENDED_TYPEMAP:
        lines += [f"\t{file_type} //....{ext}" for ext in extensions]
    return "\n".join(lines) + "\n"


# --- the checks ---------------------------------------------------------------


def _config(session: Session) -> Check:
    config = session.config
    shown = ", ".join(f"{k}={getattr(config, k)}" for k in ("port", "user", "client", "depot", "root")
                      if getattr(config, k))
    files = sorted({v for v in config.sources.values() if v not in ("arguments", "setup")})
    where = f"read from {', '.join(files)}" if files else "no config files; using the Perforce environment"
    return Check("Configuration", CheckStatus.OK, f"{shown or 'nothing set'} ({where})")


def _connection(session: Session) -> Check:
    try:
        info = session.info()
    except ClioError as e:
        return Check("Server", CheckStatus.FAIL, f"Cannot reach the Perforce server: {getattr(e, 'message', e)}",
                     "Check the network and the server address (P4PORT, or 'port' in the config)")
    version = info.get("serverVersion", "").split("/")
    short = f"{version[1]} {version[2]}" if len(version) > 2 else info.get("serverVersion", "unknown version")
    return Check("Server", CheckStatus.OK, f"{info.get('serverAddress', session.config.port)}, {short}")


def _login(session: Session) -> Check:
    try:
        text = session.login_status()
    except ClioError as e:
        return Check("Login", CheckStatus.FAIL, getattr(e, "message", str(e)), getattr(e, "hint", "") or "Run 'clio login'")
    result = session.backend.run("login", ["-s"])
    seconds = int(result.records[0].get("TicketExpiration", 0) or 0) if result.records else 0
    if seconds < 3600:
        return Check("Login", CheckStatus.WARN, text, "The ticket expires soon; run 'clio login' to renew it")
    return Check("Login", CheckStatus.OK, text)


def _workspace(session: Session) -> Check:
    try:
        paths = session.paths
        client = session.client
    except ClioError as e:
        return Check("Workspace", CheckStatus.FAIL, getattr(e, "message", str(e)),
                     getattr(e, "hint", "") or "Run 'clio setup'")
    if not paths.root.is_dir():
        return Check("Workspace", CheckStatus.FAIL, f"The workspace folder {paths.root} does not exist",
                     "Create it, or run 'clio setup' with another root")
    return Check("Workspace", CheckStatus.OK, f"{client}: {paths.depot_root} at {paths.root}")


def _client_options(session: Session) -> Check:
    try:
        spec = session.backend.run("client", ["-o", session.client]).records[0]
    except (ClioError, IndexError):
        return Check("Workspace options", CheckStatus.SKIP, "Could not read the workspace")
    options = spec.get("Options", "").split()
    if "allwrite" in options:
        return Check(
            "Workspace options", CheckStatus.WARN,
            "The workspace has 'allwrite': every file is always writable",
            "Clio tells edited files apart by their read-only flag, so with allwrite 'status' and 'save' "
            "are slower and an unlocked edit is easier to make by mistake. Use 'noallwrite'.")
    note = "; revision numbers are used for USD reload timestamps" if "modtime" in options else ""
    return Check("Workspace options", CheckStatus.OK, f"{' '.join(options)}{note}")


def _parallel(session: Session) -> Check:
    result = session.backend.run("configure", ["show", "net.parallel.max"])
    value = next((int(r["Value"]) for r in result.records if r.get("Value", "").isdigit()), None)
    if value is None and result.errors:
        return Check("Parallel transfers", CheckStatus.SKIP,
                     "Reading server settings needs admin rights",
                     "Ask the Perforce admin to set net.parallel.max (for example 8) so 'get' can run in parallel")
    if not value:
        return Check("Parallel transfers", CheckStatus.WARN, "net.parallel.max is not set on the server",
                     "Ask the Perforce admin to set net.parallel.max (for example 8): parallel 'get' is "
                     "often the biggest speed-up for large files")
    return Check("Parallel transfers", CheckStatus.OK, f"net.parallel.max = {value}")


def _typemap(session: Session) -> Check:
    result = session.backend.run("typemap", ["-o"])
    if result.errors or not result.records:
        return Check("Typemap", CheckStatus.SKIP, "Could not read the typemap")
    record = result.records[0]
    have = {" ".join(v.split()) for k, v in record.items() if k.startswith("TypeMap")}
    missing: list[str] = []
    for file_type, extensions in RECOMMENDED_TYPEMAP:
        missing += [f"{file_type} //....{ext}" for ext in extensions if f"{file_type} //....{ext}" not in have]
    if not missing:
        return Check("Typemap", CheckStatus.OK, "The recommended file types are set")
    sample = ", ".join(missing[:3]) + (f" and {len(missing) - 3} more" if len(missing) > 3 else "")
    return Check("Typemap", CheckStatus.WARN, f"{len(missing)} recommended typemap line(s) are missing: {sample}",
                 "Unmergeable files need exclusive locks (+l), and compressed files should skip server "
                 "compression (+F). Give the admin the output of 'clio doctor --typemap'.")


def _usd(session: Session) -> Check:
    try:
        from pxr import Plug, Usd
    except ImportError:
        return Check("USD", CheckStatus.SKIP, "USD's Python bindings are not installed in this Python")
    from deda.clio import usd as clio_usd

    major, minor, patch = Usd.GetVersion()
    version = f"{minor}.{patch:02d}" if major == 0 else f"{major}.{minor}.{patch}"
    bundled = clio_usd.plugin_path()
    if bundled is None:
        return Check("USD", CheckStatus.WARN, f"USD {version}, but this Clio was built without the USD plugin",
                     "USD will open files without Clio's resolver. 'deda.clio.usd.prepare' still works.")
    registered = Plug.Registry().GetPluginWithName("clioUsd")
    on_path = str(bundled) in os.environ.get("PXR_PLUGINPATH_NAME", "").split(os.pathsep)
    if registered is None and not on_path:
        return Check("USD", CheckStatus.WARN, f"USD {version}; the Clio plugin is bundled but not registered",
                     f"Set PXR_PLUGINPATH_NAME={bundled} before USD starts, or call "
                     "deda.clio.usd.register_plugin() first")
    return Check("USD", CheckStatus.OK, f"USD {version}; the Clio plugin is bundled and registered")


def _store(session: Session) -> Check:
    from deda.clio import _core

    store = Path(_core.Settings.parse("depot=//x/y;root=/x").version_store)
    try:
        store.mkdir(parents=True, exist_ok=True)
        probe = store / ".clio-doctor"
        probe.write_text("ok")
        probe.unlink()
    except OSError as e:
        return Check("Version store", CheckStatus.FAIL, f"{store} is not writable: {e}",
                     "Set CLIO_VERSION_STORE to a folder you can write to")
    free = shutil.disk_usage(store).free
    gib = free / 2**30
    if gib < 5:
        return Check("Version store", CheckStatus.WARN, f"{store}: only {gib:.1f} GiB free",
                     "Pinned versions and manifests are stored here; free some space or set CLIO_VERSION_STORE")
    return Check("Version store", CheckStatus.OK, f"{store}: {gib:.0f} GiB free")


# --- running them -------------------------------------------------------------


def run_doctor(session: Session) -> DoctorReport:
    """Run every check. Checks that need the server are skipped when it
    cannot be reached."""
    checks: list[Check] = []

    def run(check: Callable[[Session], Check], name: str) -> Check:
        try:
            result = check(session)
        except ClioError as e:  # for example not logged in: this check cannot run
            result = Check(name, CheckStatus.SKIP, f"Could not check: {getattr(e, 'message', e)}",
                           getattr(e, "hint", ""))
        except Exception as e:  # a bug or surprise in one check must not hide the rest
            result = Check(name, CheckStatus.FAIL, f"The check itself failed: {type(e).__name__}: {e}")
        checks.append(result)
        return result

    run(_config, "Configuration")
    server = run(_connection, "Server")
    needs_server: list[tuple[Callable[[Session], Check], str]] = [
        (_login, "Login"), (_workspace, "Workspace"), (_client_options, "Workspace options"),
        (_parallel, "Parallel transfers"), (_typemap, "Typemap"),
    ]
    for check, name in needs_server:
        if server.status is CheckStatus.FAIL:
            checks.append(Check(name, CheckStatus.SKIP, "The server could not be reached"))
        else:
            run(check, name)
    run(_usd, "USD")
    run(_store, "Version store")
    return DoctorReport(tuple(checks))
