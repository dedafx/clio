"""The ``clio`` command line (design doc §12).

Human-readable output by default, ``--json`` for tools. Relative paths are
relative to the current folder; ``//...`` paths are depot paths.

Exit codes: 0 success, 1 other Clio error, 2 usage error, 3 not logged in,
4 server unreachable, 5 locked by someone else, 6 out of date, 7 nothing to
save, 8 refused by the server, 9 cancelled or not confirmed, 10 outside the
workspace, 11 configuration error, 12 conflicts need a choice (branch update),
13 a version manifest cannot be made or used.
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
                if getattr(e, "problems", ()):
                    payload["problems"] = list(e.problems)
                if isinstance(e, clio.ConflictError):
                    payload["conflicts"] = _jsonable(e.conflicts)
                click.echo(json.dumps(payload, indent=2))
            else:
                click.secho(f"error: {getattr(e, 'message', str(e))}", fg="red", err=True)
                if isinstance(e, clio.ConflictError):
                    for conflict in e.conflicts:
                        click.echo(f"  conflict  {conflict.asset_path}  (theirs: {conflict.theirs})", err=True)
                if hint := getattr(e, "hint", ""):
                    click.echo(f"hint: {hint}", err=True)
                for text in getattr(e, "problems", ()):
                    click.echo(f"  {text}", err=True)
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

    def human() -> None:
        for lk in locks:
            click.echo(f"locked  {lk.asset_path}")
            for holder in lk.elsewhere:
                click.secho(f"  warning: also open by {holder}", fg="yellow", err=True)

    state.out.emit(locks, human)


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
@click.option("--no-validate", is_flag=True, help="Skip the validation hooks.")
@pass_state
@handles_errors
def save(state: State, paths: tuple[str, ...], message: str, no_validate: bool) -> None:
    """Save added, changed and deleted files to Perforce."""
    result = state.session.workspace.save(*paths, message=message, local_base=Path.cwd(),
                                          validate=not no_validate)

    def human() -> None:
        click.echo(f"Saved change {result.change.number}")
        for label, items in (("added", result.added), ("edited", result.edited), ("deleted", result.deleted)):
            for item in items:
                click.echo(f"  {label:<8} {item}")
        for warning in result.warnings:
            click.echo(f"warning: {warning}", err=True)

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


# --- branches (design doc §7) ------------------------------------------------


@cli.group()
def branch() -> None:
    """Branches (Perforce streams): list, create, switch, update, publish."""


@branch.command("list")
@pass_state
@handles_errors
def branch_list(state: State) -> None:
    """List the project's branches (* marks yours)."""
    branches = state.session.branches.list()

    def human() -> None:
        for b in branches:
            parent = f"  <- {b.parent.rsplit('/', 1)[-1]}" if b.parent else ""
            click.echo(f"{'*' if b.is_current else ' '} {b.name:<24} {b.kind:<12}{parent}")

    state.out.emit(branches, human)


@branch.command("create")
@click.argument("name")
@click.option("--from", "parent", help="Parent branch (default: your current branch).")
@click.option("--kind", type=click.Choice(["task", "development", "release"]), default="task", show_default=True)
@click.option("--no-switch", is_flag=True, help="Stay on the current branch.")
@pass_state
@handles_errors
def branch_create(state: State, name: str, parent: str | None, kind: str, no_switch: bool) -> None:
    """Create a branch and (by default) switch to it."""
    b = state.session.branches.create(name, parent=parent, kind=kind, switch=not no_switch)
    where = "" if no_switch else "; you are on it"
    state.out.emit(b, lambda: click.echo(f"Created {b.path} from {b.parent}{where}"))


@branch.command("switch")
@click.argument("name")
@click.option("--share-open-files", is_flag=True, help="Share open files as a draft first, then switch.")
@pass_state
@handles_errors
def branch_switch(state: State, name: str, share_open_files: bool) -> None:
    """Move your workspace to another branch."""
    result = state.session.branches.switch(name, share_open_files=share_open_files)

    def human() -> None:
        if result.draft:
            click.echo(f"Your open files were shared as draft {result.draft.number}")
        click.echo(f"On {result.branch.name}; got {result.report.file_count} file(s)")

    state.out.emit(result, human)


@branch.command("update")
@click.option("--mine", "keep_mine", multiple=True, metavar="PATH", help="Keep your version of PATH (repeatable).")
@click.option("--theirs", "take_theirs", multiple=True, metavar="PATH", help="Take the parent's version of PATH.")
@click.option("--both", "keep_both", multiple=True, metavar="PATH",
              help="Keep yours and save the parent's as NAME.theirs.EXT.")
@click.option("--all", "default", type=click.Choice(["mine", "theirs", "both"]),
              help="Settle every conflict this way.")
@click.option("-m", "--message", help="Description of the update.")
@pass_state
@handles_errors
def branch_update(state: State, keep_mine: tuple[str, ...], take_theirs: tuple[str, ...], keep_both: tuple[str, ...],
                  default: str | None, message: str | None) -> None:
    """Bring the parent's changes into your branch."""
    s = state.session
    resolutions: dict[str, str] = {}
    for choice, paths in (("mine", keep_mine), ("theirs", take_theirs), ("both", keep_both)):
        if paths:
            for target in s.workspace.targets(list(paths), local_base=Path.cwd()):
                resolutions[target.asset_path] = choice
    result = s.branches.update(resolutions=resolutions, default=default, message=message)

    def human() -> None:
        if result.change is None:
            click.echo("Already up to date")
            return
        click.echo(f"Updated in change {result.change.number}")
        for label, items in (("taken", result.taken), ("kept mine", result.kept_mine),
                             ("took theirs", result.took_theirs)):
            for item in items:
                click.echo(f"  {label:<11} {item}")
        for copy in result.theirs_copies:
            click.echo(f"  parent's version saved as {copy} (not in Perforce)")

    state.out.emit(result, human)


@branch.command("publish")
@click.option("-m", "--message", help="Description of the publish.")
@pass_state
@handles_errors
def branch_publish(state: State, message: str | None) -> None:
    """Copy your branch's saved work up to its parent."""
    result = state.session.branches.publish(message=message)
    state.out.emit(result, lambda: click.echo(
        f"Published {len(result.files)} file(s) to {result.parent} in change {result.change.number}"))


@branch.command("retire")
@click.argument("name")
@click.option("--yes", is_flag=True, help="Confirm retiring a branch that is not a task branch.")
@pass_state
@handles_errors
def branch_retire(state: State, name: str, yes: bool) -> None:
    """Delete a branch (its history stays)."""
    state.session.branches.retire(name, confirm=yes)
    state.out.emit({"retired": name}, lambda: click.echo(f"Retired {name}"))


# --- drafts -------------------------------------------------------------------


@cli.group()
def draft() -> None:
    """Drafts: share work without saving it (Perforce shelves)."""


@draft.command("share")
@click.argument("paths", nargs=-1)
@click.option("-m", "--message", required=True, help="What the draft is.")
@click.option("--close", is_flag=True, help="Close the files afterwards (the draft keeps the work).")
@pass_state
@handles_errors
def draft_share(state: State, paths: tuple[str, ...], message: str, close: bool) -> None:
    """Share changed files as a draft."""
    d = state.session.drafts.share(*paths, message=message, keep_open=not close, local_base=Path.cwd())
    state.out.emit(d, lambda: click.echo(f"Shared draft {d.number} with {len(d.files)} file(s)"))


@draft.command("list")
@click.option("--all", "everyone", is_flag=True, help="Everyone's drafts, not only yours.")
@pass_state
@handles_errors
def draft_list(state: State, everyone: bool) -> None:
    """List drafts in the project."""
    drafts = state.session.drafts.list(everyone=everyone)

    def human() -> None:
        for d in drafts:
            first = d.description.splitlines()[0] if d.description else ""
            click.echo(f"{d.number:<7} {d.user:<12} {len(d.files):>3} file(s)  {first}")
        if not drafts:
            click.echo("No drafts")

    state.out.emit(drafts, human)


@draft.command("get")
@click.argument("number", type=int)
@pass_state
@handles_errors
def draft_get(state: State, number: int) -> None:
    """Copy a draft's files into your workspace (open, not saved)."""
    got = state.session.drafts.get(number)

    def human() -> None:
        for path in got:
            click.echo(f"got  {path}")

    state.out.emit(got, human)


@draft.command("drop")
@click.argument("number", type=int)
@click.option("--yes", is_flag=True, help="Do not ask for confirmation.")
@pass_state
@handles_errors
def draft_drop(state: State, number: int, yes: bool) -> None:
    """Delete one of your drafts."""
    if not yes:
        if state.out.as_json:
            raise clio.ConfirmationRequiredError(f"Dropping draft {number} needs confirmation", hint="Pass --yes")
        click.confirm(f"Drop draft {number}?", abort=True)
    state.session.drafts.drop(number, confirm=True)
    state.out.emit({"dropped": number}, lambda: click.echo(f"Dropped draft {number}"))


# --- caches -------------------------------------------------------------------


@cli.group("cache")
def cache_group() -> None:
    """The history and content caches (design doc §9)."""


@cache_group.command("clear")
@click.option("--history", "only_history", is_flag=True, help="Only the history cache.")
@click.option("--content", "only_content", is_flag=True, help="Only the content cache.")
@pass_state
@handles_errors
def cache_clear(state: State, only_history: bool, only_content: bool) -> None:
    """Forget cached history and delete cached file content."""
    both = not (only_history or only_content)
    freed = 0
    if both or only_history:
        state.session.history.refresh()
    if both or only_content:
        freed = state.session.content.clear()
    state.out.emit({"content_bytes_freed": freed},
                   lambda: click.echo(f"Cleared the cache{f' ({_size(freed)} of content)' if freed else ''}"))


@cache_group.command("verify")
@click.argument("path", required=False, default="")
@pass_state
@handles_errors
def cache_verify(state: State, path: str) -> None:
    """Check the caches against the server and drop what disagrees."""
    history = state.session.history.verify(path)
    content = state.session.content.verify()
    result = {"history_dropped": history, "content_checked": content.checked,
              "content_removed": list(content.removed)}
    state.out.emit(result, lambda: click.echo(
        f"History: {history} entry(ies) dropped. Content: {content.checked} file(s) checked, "
        f"{len(content.removed)} removed"))


# --- doctor -------------------------------------------------------------------

_STATUS_MARK = {"ok": ("ok  ", "green"), "warn": ("warn", "yellow"), "fail": ("FAIL", "red"), "skip": ("skip", None)}


@cli.command()
@click.option("--typemap", is_flag=True, help="Print the recommended typemap for the Perforce admin and exit.")
@pass_state
@handles_errors
def doctor(state: State, typemap: bool) -> None:
    """Check this machine's setup: config, server, login, workspace, USD."""
    from deda.clio.doctor import CheckStatus, recommended_typemap, run_doctor

    if typemap:
        click.echo(recommended_typemap(), nl=False)
        return
    report = run_doctor(state.session)

    def human() -> None:
        for check in report.checks:
            mark, colour = _STATUS_MARK[check.status.value]
            click.echo(f"[{click.style(mark, fg=colour)}] {check.name}: {check.message}")
            if check.hint:
                click.echo(f"       {check.hint}")
        failed, warned = len(report.by_status(CheckStatus.FAIL)), len(report.by_status(CheckStatus.WARN))
        click.echo("Everything needed works" if report.ok and not warned else
                   f"{failed} failed, {warned} warning(s)")

    state.out.emit(report, human)
    if not report.ok:
        sys.exit(1)


# --- usd ----------------------------------------------------------------------


@cli.group()
def usd() -> None:
    """USD helpers: version manifests for farm jobs."""


@usd.group()
def manifest() -> None:
    """Version manifests: the same files on every machine (design doc §10.8)."""




@manifest.command("create")
@click.argument("root_layer")
@click.option("--pin", default="have", show_default=True, help="have, latest, @CHANGE or @LABEL.")
@click.option("-o", "--output", "output", required=True, type=click.Path(dir_okay=False),
              help="Where to write the manifest.")
@click.option("--extra", multiple=True, help="A file no layer mentions (repeat for more).")
@click.option("--no-payloads", is_flag=True, help="Leave out payloads.")
@click.option("--allow-missing", is_flag=True, help="Write the manifest even if a reference cannot be provided.")
@click.option("--store", type=click.Path(file_okay=False), help="Version store folder.")
@pass_state
@handles_errors
def manifest_create(state: State, root_layer: str, pin: str, output: str, extra: tuple[str, ...],
                    no_payloads: bool, allow_missing: bool, store: str | None) -> None:
    """Record the exact revision of every file ROOT_LAYER can load."""
    from deda.clio.usd import Manifest

    root = _under_cwd(state, root_layer)
    made = Manifest.create(state.session, root, pin=pin, payloads="none" if no_payloads else "all",
                           extra=[_under_cwd(state, e) for e in extra], store=store, allow_missing=allow_missing)
    made.save(output)
    state.out.emit({"path": output, "sha256": made.sha256, "files": len(made.files), "source_pin": made.source_pin},
                   lambda: click.echo(f"Wrote {output}: {len(made.files)} file(s), id {made.sha256}"))


def _under_cwd(state: State, path: str) -> str:
    """A path as typed on the command line: absolute, or relative to the
    current folder when that is inside the workspace, else an asset path."""
    candidate = Path(path)
    if candidate.is_absolute():
        return str(candidate)
    local = Path.cwd() / candidate
    try:
        state.session.paths.asset_path_of_local(local)
    except ClioError:
        return path
    return str(local)


@manifest.command("fetch")
@click.argument("manifest_file", type=click.Path(exists=True, dir_okay=False))
@click.option("--threads", default=8, show_default=True, help="Parallel downloads.")
@click.option("--store", type=click.Path(file_okay=False), help="Version store folder.")
@pass_state
@handles_errors
def manifest_fetch(state: State, manifest_file: str, threads: int, store: str | None) -> None:
    """Download every file of a manifest and check it against its digest."""
    from deda.clio.usd import Manifest

    loaded = Manifest.load(manifest_file)
    report = loaded.fetch(state.session, store=store, parallel=threads)

    def human() -> None:
        click.echo(f"{report.files} file(s): {report.fetched} fetched ({_size(report.bytes_fetched)}), "
                   f"{report.linked} linked, {report.present} already there")
        for failure in report.failures:
            click.secho(f"  failed: {failure}", fg="red", err=True)

    state.out.emit(report, human)
    if not report.ok:
        sys.exit(1)


@manifest.command("check")
@click.argument("manifest_file", type=click.Path(exists=True, dir_okay=False))
@pass_state
@handles_errors
def manifest_check(state: State, manifest_file: str) -> None:
    """Ask the server whether every revision of a manifest can still be fetched."""
    from deda.clio.usd import Manifest

    report = Manifest.load(manifest_file).check(state.session)

    def human() -> None:
        click.echo("All revisions are available" if report.ok else f"{len(report.problems)} problem(s):")
        for problem in report.problems:
            click.secho(f"  {problem}", fg="red", err=True)

    state.out.emit(report, human)
    if not report.ok:
        sys.exit(1)


@manifest.command("diff")
@click.argument("first", type=click.Path(exists=True, dir_okay=False))
@click.argument("second", type=click.Path(exists=True, dir_okay=False))
@pass_state
@handles_errors
def manifest_diff(state: State, first: str, second: str) -> None:
    """What changed between two manifests (no server needed)."""
    from deda.clio.usd import Manifest

    result = Manifest.load(first).diff(Manifest.load(second))

    def human() -> None:
        if result.same:
            click.echo("The manifests list the same revisions")
        for path in result.added:
            click.echo(f"  added    {path}")
        for path in result.removed:
            click.echo(f"  removed  {path}")
        for path, (old, new) in result.changed.items():
            click.echo(f"  changed  {path}  #{old} -> #{new}")

    state.out.emit(result, human)
    if not result.same:
        sys.exit(1)


def _size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"


def main() -> None:
    cli(prog_name="clio")
