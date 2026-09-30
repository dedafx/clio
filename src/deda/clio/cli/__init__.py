"""The ``clio`` command line (design doc §12).

Human-readable output by default, ``--json`` for tools. Relative paths are
relative to the current folder; ``//...`` paths are depot paths.

Exit codes: 0 success, 1 other Clio error, 2 usage error, 3 not logged in,
4 server unreachable, 5 locked by someone else, 6 out of date, 7 nothing to
save, 8 refused by the server, 9 cancelled or not confirmed, 10 outside the
workspace, 11 configuration error.
"""

from __future__ import annotations

import dataclasses
import enum
import json
import sys
from collections.abc import Callable
from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path
from typing import Any

import click

from deda import clio
from deda.clio.errors import ClioError, ConfigError, WorkflowError

__all__ = ["cli", "main"]


# --- output -------------------------------------------------------------------


def _jsonable(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: _jsonable(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, (Path, clio.Pin)):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, timedelta):
        return value.total_seconds()
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(v) for v in value]
    return value


class Output:
    def __init__(self, as_json: bool) -> None:
        self.as_json = as_json

    def emit(self, value: Any, human: Callable[[], None]) -> None:
        if self.as_json:
            click.echo(json.dumps(_jsonable(value), indent=2))
        else:
            human()


def _exit_code(error: BaseException) -> int:
    if isinstance(error, WorkflowError):
        return error.exit_code
    if isinstance(error, ConfigError):
        return 11
    return 1


# --- context ------------------------------------------------------------------


class State:
    def __init__(self, options: dict[str, Any], as_json: bool) -> None:
        self.options = options
        self.out = Output(as_json)
        self._session: clio.Session | None = None

    @property
    def session(self) -> clio.Session:
        if self._session is None:
            opts = {k: v for k, v in self.options.items() if v is not None}
            self._session = clio.connect(opts.pop("project", None), **opts)
        return self._session

    def close(self) -> None:
        if self._session is not None:
            self._session.close()


pass_state = click.make_pass_decorator(State)


def handles_errors(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Report Clio errors as a message, a hint and an exit code."""

    @wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        state = click.get_current_context().find_object(State)
        try:
            return fn(*args, **kwargs)
        except ClioError as e:
            code = _exit_code(e)
            if state is not None and state.out.as_json:
                payload = {"error": type(e).__name__, "message": getattr(e, "message", str(e)),
                           "hint": getattr(e, "hint", ""), "p4_messages": list(getattr(e, "p4_messages", ()))}
                click.echo(json.dumps(payload, indent=2))
            else:
                click.secho(f"error: {getattr(e, 'message', str(e))}", fg="red", err=True)
                if hint := getattr(e, "hint", ""):
                    click.echo(f"hint: {hint}", err=True)
                for text in getattr(e, "p4_messages", ()):
                    click.echo(f"  perforce: {text}", err=True)
            sys.exit(code)

    return wrapper


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.option("--project", help="Project name from the Clio config.")
@click.option("--port", help="Perforce server (P4PORT).")
@click.option("--user", help="Perforce user (P4USER).")
@click.option("--client", help="Perforce workspace (P4CLIENT).")
@click.option("--json", "as_json", is_flag=True, help="Print JSON instead of text.")
@click.version_option(clio.__version__, prog_name="clio")
@click.pass_context
def cli(ctx: click.Context, project: str | None, port: str | None, user: str | None, client: str | None,
        as_json: bool) -> None:
    """Get, lock and save art assets in Perforce."""
    state = State({"project": project, "port": port, "user": user, "client": client}, as_json)
    ctx.obj = state
    ctx.call_on_close(state.close)


# --- session commands ---------------------------------------------------------


@cli.command()
@click.option("--password-stdin", is_flag=True, help="Read the password from standard input.")
@pass_state
@handles_errors
def login(state: State, password_stdin: bool) -> None:
    """Log in to Perforce (Clio never stores the password)."""
    password = sys.stdin.readline().rstrip("\n") if password_stdin else None
    state.session.login(password, prompt=lambda text: click.prompt(text.strip().rstrip(":"), hide_input=True))
    status = state.session.login_status()
    state.out.emit({"status": status}, lambda: click.echo(f"Logged in: {status}"))


@cli.command()
@pass_state
@handles_errors
def logout(state: State) -> None:
    """Log out of Perforce (removes the ticket)."""
    state.session.logout()
    state.out.emit({"status": "logged out"}, lambda: click.echo("Logged out"))


@cli.command()
@pass_state
@handles_errors
def whoami(state: State) -> None:
    """Show the user, workspace and login status."""
    s = state.session
    info = {"user": s.user, "workspace": s.config.client or s.info().get("clientName", ""),
            "server": s.info().get("serverAddress", s.config.port), "login": s.login_status()}
    state.out.emit(info, lambda: [click.echo(f"{k}: {v}") for k, v in info.items()])


@cli.command()
@click.option("--root", type=click.Path(file_okay=False), help="Local folder for the project.")
@click.option("--depot", help="Depot path of the project root, e.g. //imagine/main.")
@click.option("--stream", help="Stream for the workspace (instead of --depot).")
@click.option("--name", help="Workspace name (default: user_host_project).")
@pass_state
@handles_errors
def setup(state: State, root: str | None, depot: str | None, stream: str | None, name: str | None) -> None:
    """First-time setup: log in if needed and create the workspace."""
    overrides = {k: v for k, v in {"root": root, "depot": depot or stream, "stream": stream}.items() if v}
    if overrides:
        state.options.update(overrides)
    s = state.session
    try:
        s.login_status()
    except clio.AuthError:
        if state.out.as_json:
            raise
        s.login(prompt=lambda text: click.prompt(text.strip().rstrip(":"), hide_input=True))
    workspace = s.setup(name=name)
    result = {"workspace": workspace, "root": s.config.root}
    state.out.emit(result, lambda: click.echo(f"Workspace {workspace} is ready at {s.config.root}\n"
                                              f"Use --client {workspace} or set P4CLIENT={workspace}"))


# --- workflow commands --------------------------------------------------------


_STATE_TEXT = {
    clio.FileState.SYNCED: "up to date",
    clio.FileState.OUT_OF_DATE: "out of date",
    clio.FileState.MISSING: "not on disk",
    clio.FileState.MODIFIED: "changed (not locked)",
    clio.FileState.NEW: "new (not in Perforce)",
    clio.FileState.DELETED: "deleted on disk",
    clio.FileState.EDITING: "editing",
    clio.FileState.ADDING: "adding",
    clio.FileState.DELETING: "deleting",
}


def _status_line(f: clio.FileStatus) -> str:
    version = f" v{f.have_rev or 0}/{f.head_rev}" if f.head_rev else ""
    notes = []
    if f.locked_by_me:
        notes.append("locked by you")
    if f.locked_by:
        notes.append(f"locked by {f.locked_by}")
    elif f.opened_by:
        notes.append("open by " + ", ".join(f.opened_by))
    return f"{_STATE_TEXT[f.state]:<22} {f.asset_path}{version}" + (f"  ({'; '.join(notes)})" if notes else "")


@cli.command()
@click.argument("paths", nargs=-1)
@click.option("--all", "show_all", is_flag=True, help="Also list files that are up to date.")
@click.option("--no-disk", is_flag=True, help="Skip the check for files changed on disk (faster).")
@pass_state
@handles_errors
def status(state: State, paths: tuple[str, ...], show_all: bool, no_disk: bool) -> None:
    """Show changed, locked and out-of-date files."""
    files = state.session.workspace.status(*paths, check_disk=not no_disk, local_base=Path.cwd())
    shown = files if show_all else [f for f in files if f.state is not clio.FileState.SYNCED or f.locked_by]

    def human() -> None:
        for f in shown:
            click.echo(_status_line(f))
        if not shown:
            click.echo("Everything is up to date")

    state.out.emit(shown, human)


@cli.command()
@click.argument("paths", nargs=-1)
@click.option("--version", "version", help="latest (default), have, @CHANGE, @LABEL or #REV.")
@click.option("--preview", is_flag=True, help="Show what would be transferred; change nothing.")
@click.option("--force", is_flag=True, help="Rewrite files even if they are current.")
@pass_state
@handles_errors
def get(state: State, paths: tuple[str, ...], version: str | None, preview: bool, force: bool) -> None:
    """Get files from Perforce (latest by default)."""
    result = state.session.workspace.get(*paths, version=version, preview=preview, force=force,
                                         local_base=Path.cwd())

    def human() -> None:
        if isinstance(result, clio.SyncPlan):
            click.echo(f"Would get {result.file_count} file(s), {_size(result.bytes_to_transfer)}")
            for f in result.files:
                click.echo(f"  {f.action:<8} {f.asset_path}#{f.rev}")
        else:
            click.echo(f"Got {result.file_count} file(s), {_size(result.bytes_transferred)}"
                       if result.file_count else "Already up to date")
            for w in result.warnings:
                click.echo(f"warning: {w}", err=True)

    state.out.emit(result, human)


@cli.command()
@click.argument("paths", nargs=-1, required=True)
@click.option("--allow-out-of-date", is_flag=True, help="Lock even if a newer version exists.")
@pass_state
@handles_errors
def lock(state: State, paths: tuple[str, ...], allow_out_of_date: bool) -> None:
    """Lock files so nobody else can change them."""
    locks = state.session.workspace.lock(*paths, allow_out_of_date=allow_out_of_date, local_base=Path.cwd())
    state.out.emit(locks, lambda: [click.echo(f"locked  {lk.asset_path}") for lk in locks])


@cli.command()
@click.argument("paths", nargs=-1, required=True)
@pass_state
@handles_errors
def unlock(state: State, paths: tuple[str, ...]) -> None:
    """Release locks; unchanged files are closed again."""
    unlocked = state.session.workspace.unlock(*paths, local_base=Path.cwd())

    def human() -> None:
        for p in unlocked:
            click.echo(f"unlocked  {p}")
        if not unlocked:
            click.echo("Nothing was locked")

    state.out.emit(unlocked, human)


@cli.command()
@click.argument("paths", nargs=-1, required=True)
@pass_state
@handles_errors
def who(state: State, paths: tuple[str, ...]) -> None:
    """Show who has files locked or open."""
    files = [f for f in state.session.workspace.status(*paths, check_disk=False, local_base=Path.cwd())
             if f.locked_by or f.opened_by or f.locked_by_me]

    def human() -> None:
        for f in files:
            holders = ([f"{f.locked_by} (lock)"] if f.locked_by else []) + [
                o for o in f.opened_by if o != f.locked_by] + (["you (lock)"] if f.locked_by_me else [])
            click.echo(f"{f.asset_path}: {', '.join(holders)}")
        if not files:
            click.echo("Nobody else has these files locked or open")

    state.out.emit(files, human)


@cli.command()
@click.argument("paths", nargs=-1)
@click.option("-m", "--message", required=True, help="What changed.")
@pass_state
@handles_errors
def save(state: State, paths: tuple[str, ...], message: str) -> None:
    """Save added, changed and deleted files to Perforce."""
    result = state.session.workspace.save(*paths, message=message, local_base=Path.cwd())

    def human() -> None:
        click.echo(f"Saved change {result.change.number}")
        for label, items in (("added", result.added), ("edited", result.edited), ("deleted", result.deleted)):
            for item in items:
                click.echo(f"  {label:<8} {item}")

    state.out.emit(result, human)


@cli.command()
@click.argument("paths", nargs=-1, required=True)
@click.option("--yes", is_flag=True, help="Do not ask for confirmation.")
@click.option("--no-backup", is_flag=True, help="Do not keep a copy of the discarded files.")
@pass_state
@handles_errors
def discard(state: State, paths: tuple[str, ...], yes: bool, no_backup: bool) -> None:
    """Throw away local changes to locked or opened files."""
    ws = state.session.workspace
    if not yes:
        opened = [f for f in ws.status(*paths, check_disk=False, local_base=Path.cwd())
                  if f.state in (clio.FileState.EDITING, clio.FileState.ADDING, clio.FileState.DELETING)]
        if not opened:
            state.out.emit([], lambda: click.echo("Nothing to discard"))
            return
        if state.out.as_json:
            raise clio.ConfirmationRequiredError("Discarding changes needs confirmation", hint="Pass --yes")
        for f in opened:
            click.echo(f"  {_STATE_TEXT[f.state]:<9} {f.asset_path}")
        click.confirm(f"Discard changes to {len(opened)} file(s)?", abort=True)
    result = ws.discard(*paths, confirm=True, backup=not no_backup, local_base=Path.cwd())

    def human() -> None:
        for p in result.asset_paths:
            click.echo(f"discarded  {p}")
        if result.backup_dir:
            click.echo(f"Copies kept in {result.backup_dir}")
        if not result.asset_paths:
            click.echo("Nothing to discard")

    state.out.emit(result, human)


@cli.command()
@click.argument("path")
@click.option("--limit", default=20, show_default=True, help="Number of versions.")
@pass_state
@handles_errors
def history(state: State, path: str, limit: int) -> None:
    """Show the versions of a file."""
    s = state.session
    target = s.workspace.targets([path], local_base=Path.cwd())[0]
    page = s.history.revisions(target.asset_path, limit=limit)

    def human() -> None:
        for r in page.items:
            first = r.description.splitlines()[0] if r.description else ""
            click.echo(f"v{r.rev:<4} change {r.change:<6} {r.time:%Y-%m-%d %H:%M}  {r.user:<12} {r.action:<8} {first}")

    state.out.emit(page, human)


@cli.command()
@click.argument("path", required=False, default="")
@click.option("--limit", default=20, show_default=True, help="Number of changes.")
@pass_state
@handles_errors
def activity(state: State, path: str, limit: int) -> None:
    """Show recent changes in a folder (default: the whole project)."""
    s = state.session
    asset = s.workspace.targets([path], local_base=Path.cwd())[0].asset_path if path else ""
    page = s.history.activity(asset, limit=limit)

    def human() -> None:
        for c in page.items:
            first = c.description.splitlines()[0] if c.description else ""
            when = f"{c.time:%Y-%m-%d %H:%M}" if c.time else ""
            click.echo(f"change {c.number:<6} {when}  {c.user:<12} {first}")

    state.out.emit(page, human)


def _size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"


def main() -> None:
    cli(prog_name="clio")
