"""Branches: Perforce streams, with task streams for asset work (design doc
§7).

* ``create`` makes a stream under its parent and branches the parent's
  files into it (``p4 populate``), then switches the workspace to it.
* ``switch`` re-points the workspace to another stream and syncs only the
  files that differ.
* ``update`` merges the parent's changes down. Binary files cannot be
  merged line by line, so a file changed on both sides is a choice: keep
  mine, take theirs, or keep both (§7.2). It either submits the whole
  update or changes nothing.
* ``publish`` copies the branch up to its parent, refused while the branch
  is behind. It runs in a hidden publishing workspace with ``p4 copy -v``,
  so nothing is downloaded and the artist's workspace is not touched.
* ``retire`` deletes the stream spec. Its history stays in the depot.
"""

from __future__ import annotations

import os
import re
import time
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from deda.clio._paths import escape
from deda.clio.backends.base import Result
from deda.clio.backends.core import CoreBackend
from deda.clio.config import cache_dir
from deda.clio.errors import (
    ConfirmationRequiredError,
    ConflictError,
    LockedByOtherError,
    NothingToSaveError,
    NotInWorkspaceError,
    OutOfDateError,
    WorkflowError,
)
from deda.clio.models import Branch, Change, Conflict, Draft, PublishResult, SwitchResult, SyncReport, UpdateResult

if TYPE_CHECKING:
    from deda.clio.session import Session

__all__ = ["Branches", "Choice"]

#: How to settle a file changed in both the branch and its parent.
Choice = Literal["mine", "theirs", "both"]
_CHOICES = ("mine", "theirs", "both")

_KINDS = ("task", "development", "release")
#: Stream options for branches Clio creates: anyone can submit, changes flow
#: both ways, and a branch must be up to date before it publishes.
_STREAM_OPTIONS = "allsubmit unlocked toparent fromparent mergedown"


def _branch(record: dict[str, str], current: str) -> Branch:
    parent = record.get("Parent", "none")
    return Branch(
        path=record["Stream"],
        name=record.get("Name", record["Stream"].rsplit("/", 1)[-1]),
        parent=None if parent in ("", "none") else parent,
        kind=record.get("Type", ""),
        owner=record.get("Owner", ""),
        description=record.get("desc", record.get("Description", "")).strip(),
        is_current=record["Stream"] == current,
    )


def theirs_copy_path(local: Path) -> Path:
    """Where "keep both" puts the parent's version: ``name.theirs.ext``."""
    return local.with_name(f"{local.stem}.theirs{local.suffix}")


class Branches:
    def __init__(self, session: Session) -> None:
        self._session = session
        self._clock = time.monotonic
        self._listed: tuple[float, list[dict[str, str]]] | None = None

    # --- helpers --------------------------------------------------------

    def _run(self, command: str, args: list[str] | tuple[str, ...] = (), input: str | None = None) -> Result:
        return self._session.backend.run(command, list(args), input)

    def _check(self, result: Result, what: str) -> Result:
        if result.errors:
            raise WorkflowError(f"Could not {what}", p4_messages=result.texts)
        return result

    def _current_path(self) -> str:
        """The workspace's stream. Raises if it is not a stream workspace."""
        s = self._session
        stream = s.stream
        if not stream:
            raise NotInWorkspaceError(
                f"Workspace {s.client} is not on a branch (stream)",
                hint="Branches need a streams depot; create the workspace with 'clio setup --stream //project/main'",
            )
        return stream

    def _depot(self, stream: str) -> str:
        """//imagine/crate-damage -> //imagine"""
        return "//" + stream[2:].split("/", 1)[0]

    def resolve_name(self, name: str) -> str:
        """A branch name ("crate-damage") or path ("//imagine/crate-damage")
        as a stream path, in the current project's streams depot."""
        if name.startswith("//"):
            return name.rstrip("/")
        return f"{self._depot(self._current_path())}/{name}"

    def _opened(self) -> list[dict[str, str]]:
        s = self._session
        return [r for r in self._run("opened", ["-C", s.client]).records if "depotFile" in r]

    def _require_nothing_open(self, action: str) -> None:
        opened = self._opened()
        if opened:
            raise WorkflowError(
                f"Cannot {action}: {len(opened)} file(s) are open in your workspace",
                hint="Save them, discard them, or share them as a draft first ('clio draft share')",
            )

    # --- listing --------------------------------------------------------

    def list(self, *, max_age: float = 0.0) -> list[Branch]:
        """Every branch in the project's streams depot. ``max_age`` (seconds)
        allows a list fetched that recently to be reused."""
        current = self._current_path()
        now = self._clock()
        if self._listed is None or now - self._listed[0] > max_age:
            records = self._check(self._run("streams", [f"{self._depot(current)}/..."]), "list branches").records
            self._listed = (now, [r for r in records if "Stream" in r])
        return sorted((_branch(r, current) for r in self._listed[1]), key=lambda b: b.path)

    def current(self) -> Branch:
        current = self._current_path()
        return self.get(current)

    def get(self, name: str) -> Branch:
        path = self.resolve_name(name)
        records = [r for r in self._run("streams", [path]).records if r.get("Stream") == path]
        if not records:
            raise NotInWorkspaceError(f"There is no branch {path}", hint="See 'clio branch list'")
        return _branch(records[0], self._current_path())

    def related(self, branch: Branch | None = None) -> list[Branch]:
        """The parent, siblings and children of a branch (design doc §7.3)."""
        branches = self.list(max_age=self._session.config.poll_interval)
        if branch is None:
            current = self._current_path()
            branch = next((b for b in branches if b.path == current), None)
            if branch is None:
                branches = self.list()
                branch = next(b for b in branches if b.path == current)
        others = [b for b in branches if b.path != branch.path]
        return [b for b in others
                if b.path == branch.parent or b.parent == branch.path or
                (branch.parent is not None and b.parent == branch.parent)]

    # --- create / switch / retire ---------------------------------------

    def create(
        self,
        name: str,
        *,
        parent: str | None = None,
        kind: str = "task",
        description: str = "",
        switch: bool = True,
    ) -> Branch:
        """Create a branch of ``parent`` (default: the current branch) and
        branch the parent's files into it. Task branches (the default) are
        light: only files that change are copied in the metadata.
        ``switch`` moves the workspace to the new branch."""
        if kind not in _KINDS:
            raise WorkflowError(f"Unknown branch kind '{kind}'", hint=f"Use one of {', '.join(_KINDS)}")
        s = self._session
        parent_path = self.resolve_name(parent) if parent else self._current_path()
        path = self.resolve_name(name)
        if self._depot(path) != self._depot(parent_path):
            raise WorkflowError("A branch must be in the same project as its parent")
        if [r for r in self._run("streams", [path]).records if r.get("Stream") == path]:
            raise WorkflowError(f"Branch {path} already exists", hint=f"Switch to it with 'clio branch switch {name}'")
        text = description or f"Created by {s.user} with Clio."
        spec = (
            f"Stream: {path}\nOwner: {s.user}\nName: {path.rsplit('/', 1)[-1]}\nParent: {parent_path}\n"
            f"Type: {kind}\nParentView: inherit\nOptions: {_STREAM_OPTIONS}\n"
            "Description:\n" + "".join(f"\t{line}\n" for line in text.splitlines()) + "Paths:\n\tshare ...\n"
        )
        self._check(self._run("stream", ["-i"], spec), f"create branch {path}")
        self._listed = None
        populate = self._run("populate", ["-S", path, "-r", "-d", f"Branch {path} from {parent_path}"])
        if populate.errors and not any("No such file" in e or "no file(s)" in e.lower() for e in populate.errors):
            raise WorkflowError(f"Could not copy the files of {parent_path} into {path}",
                                p4_messages=populate.texts)
        branch = self.get(path)
        if switch:
            return self.switch(path).branch
        return branch

    def switch(self, name: str, *, share_open_files: bool = False) -> SwitchResult:
        """Move the workspace to another branch and get its files. Only
        files that differ are transferred.

        Refused while files are open, unless ``share_open_files`` is set:
        then they are shared as a draft first and closed (design doc §11).
        """
        s = self._session
        path = self.resolve_name(name)
        target = self.get(path)
        draft: Draft | None = None
        opened = self._opened()
        if opened:
            if not share_open_files:
                self._require_nothing_open(f"switch to {target.name}")
            draft = s.drafts.share(message=f"Open work before switching to {target.name}", keep_open=False,
                                   detect_changes=False)
        self._check(self._run("client", ["-s", "-S", path]), f"switch to {path}")
        s.forget_workspace_state()
        report: SyncReport = s.workspace.get()  # type: ignore[assignment]  # not a preview
        branch = self.get(path)
        s.events.emit("branch_switched", branch=branch)
        return SwitchResult(branch, report, draft)

    def retire(self, name: str, *, confirm: bool = False) -> None:
        """Delete a branch's stream spec. Its history stays in the depot.
        Task branches are retired freely; others need ``confirm=True``.
        Refused while any workspace (including this one) is on it."""
        path = self.resolve_name(name)
        branch = self.get(path)
        if branch.kind == "mainline":
            raise WorkflowError("The mainline cannot be retired")
        if branch.kind != "task" and not confirm:
            raise ConfirmationRequiredError(f"Retiring the {branch.kind} branch {path} needs confirmation",
                                            hint="Pass confirm=True (or --yes)")
        if branch.is_current:
            raise WorkflowError(f"Your workspace is on {branch.name}",
                                hint="Switch to another branch first ('clio branch switch main')")
        result = self._run("stream", ["-d", path])
        if result.errors:
            hint = "Other workspaces are still on it" if "active clients" in "\n".join(result.errors) else ""
            raise WorkflowError(f"Could not retire {path}", hint=hint, p4_messages=result.texts)
        self._listed = None
        self._session.events.emit("branch_retired", path=path)

    # --- update (merge down) --------------------------------------------

    def behind(self) -> list[str]:
        """Depot paths in the current branch that the parent changed since the
        last update (``p4 merge -n``). Empty when up to date."""
        result = self._run("merge", ["-n"])
        return [r["depotFile"] for r in result.records if "depotFile" in r]

    def update(
        self,
        *,
        resolutions: Mapping[str, Choice] | None = None,
        default: Choice | None = None,
        message: str | None = None,
    ) -> UpdateResult:
        """Bring the parent's changes into the current branch.

        Files changed only in the parent are taken as they are. Files
        changed in both need a choice, given per asset path in
        ``resolutions`` or for all in ``default``: ``"mine"`` keeps this
        branch's version, ``"theirs"`` takes the parent's, ``"both"`` keeps
        mine and writes the parent's next to it as ``name.theirs.ext`` (not
        in Perforce). If a conflict has no choice, nothing is changed and
        :class:`ConflictError` lists the conflicts.
        """
        s = self._session
        for choice in [*(resolutions or {}).values(), *([default] if default else [])]:
            if choice not in _CHOICES:
                raise WorkflowError(f"Unknown choice '{choice}'", hint="Use mine, theirs or both")
        branch = self.current()
        if branch.parent is None:
            raise WorkflowError(f"{branch.name} has no parent to update from")
        self._require_nothing_open("update")
        if not self.behind():
            return UpdateResult(None)

        ws = s.workspace
        text = message or f"Update {branch.name} from {branch.parent.rsplit('/', 1)[-1]}"
        number = ws._new_change(text)
        try:
            self._check(self._run("merge", ["-c", str(number)]), "merge from the parent")
            self._check(self._run("resolve", ["-am", "-c", str(number)]), "resolve the update")
            conflicts = self._conflicts(number, branch)
            choices: dict[str, Choice] = {}
            missing = []
            for conflict in conflicts:
                choice = (resolutions or {}).get(conflict.asset_path) or default
                if choice is None:
                    missing.append(conflict)
                else:
                    choices[conflict.depot_path] = choice
            if missing:
                raise ConflictError(
                    f"{len(missing)} file(s) changed in both {branch.name} and its parent" if len(missing) > 1
                    else f"{missing[0].asset_path} changed in both {branch.name} and its parent",
                    conflicts=conflicts,
                    hint="Choose for each file: keep mine, take theirs, or keep both. Nothing was changed",
                )
            theirs_copies = self._settle(number, conflicts, choices)
            opened = [r for r in self._run("opened", ["-c", str(number)]).records if "depotFile" in r]
        except Exception:
            self._undo(number)
            raise
        if not opened:
            self._undo(number)
            return UpdateResult(None)
        submit = self._run("submit", ["-c", str(number), *ws._parallel_args()])
        if submit.errors:
            raise WorkflowError(f"The update was not saved (change {number} is still pending)",
                                p4_messages=submit.texts)
        submitted = next((int(r["submittedChange"]) for r in submit.records if "submittedChange" in r), number)
        files = tuple(r["depotFile"] for r in opened)
        change = Change(submitted, text, s.user, s.client, datetime.now(), files)
        by_choice = {c: tuple(ws._asset(d) for d, x in choices.items() if x == c) for c in _CHOICES}
        conflicted = set(choices)
        taken = tuple(ws._asset(d) for d in files if d not in conflicted)
        s.events.emit("branch_updated", change=change)
        s.history.note_submitted(files)
        return UpdateResult(change, taken, by_choice["mine"] + by_choice["both"], by_choice["theirs"],
                            tuple(theirs_copies))

    def _conflicts(self, number: int, branch: Branch) -> list[Conflict]:
        ws = self._session.workspace
        records = self._run("fstat", ["-Ru", "-e", str(number), f"{branch.path}/..."]).records
        conflicts = []
        for rec in records:
            depot = rec.get("depotFile")
            if not depot:
                continue
            asset = ws._asset(depot)
            conflicts.append(Conflict(asset, depot, f"{branch.parent}/{escape(asset)}"))
        return conflicts

    def _settle(self, number: int, conflicts: list[Conflict], choices: Mapping[str, Choice]) -> list[Path]:
        s = self._session
        copies: list[Path] = []
        for conflict in conflicts:
            if choices[conflict.depot_path] == "both":
                local = s.paths.local_path(conflict.asset_path)
                copy = theirs_copy_path(local)
                if copy.exists():
                    copy.chmod(copy.stat().st_mode | 0o200)
                self._check(self._run("print", ["-q", "-o", os.fspath(copy), conflict.theirs]),
                            f"save the parent's version of {conflict.asset_path}")
                copies.append(copy)
        for flag, wanted in (("-ay", ("mine", "both")), ("-at", ("theirs",))):
            files = [c.depot_path for c in conflicts if choices[c.depot_path] in wanted]
            if files:
                self._check(self._run("resolve", [flag, *files]), "settle conflicts")
        left = self._run("fstat", ["-Ru", "-e", str(number), f"{s.paths.depot_root}/..."]).records
        if any("depotFile" in r for r in left):
            raise WorkflowError("Some files could not be settled", p4_messages=[r["depotFile"] for r in left
                                                                              if "depotFile" in r])
        return copies

    def _undo(self, number: int) -> None:
        """Revert an unsubmitted update and delete its change. Files the
        update wrote go back to the branch's versions."""
        self._run("revert", ["-c", str(number), f"{self._session.paths.depot_root}/..."])
        self._run("change", ["-d", str(number)])

    # --- publish (copy up) ----------------------------------------------

    def _publish_backend(self, parent: str) -> tuple[CoreBackend, str]:
        """A connection to this user's hidden publishing workspace, pointed
        at ``parent``. It never has files on disk: ``p4 copy -v`` opens
        files without transferring them."""
        s = self._session
        name = f"{s.client}_publish"
        spec = self._run("client", ["-o", name]).records
        exists = bool(spec and (spec[0].get("Access") or spec[0].get("Update")))
        root = cache_dir() / "publish" / name
        if not exists:
            text = (f"Client: {name}\nOwner: {s.user}\nRoot: {root}\n"
                    "Options: noallwrite noclobber nocompress unlocked nomodtime normdir\n"
                    f"Stream: {parent}\nDescription:\n\tClio publishing workspace for {s.client}. No files on disk.\n")
            self._check(self._run("client", ["-i"], text), "create the publishing workspace")
        elif spec[0].get("Stream") != parent:
            self._check(self._run("client", ["-s", "-S", parent, name]), "point the publishing workspace")
        return CoreBackend(s.config.with_overrides("publish", client=name)), name

    def publish(self, *, message: str | None = None) -> PublishResult:
        """Copy the current branch up to its parent. Refused while the
        branch is behind the parent (update first) or has open files (save
        or discard them first: only saved work is published)."""
        s = self._session
        branch = self.current()
        if branch.parent is None:
            raise WorkflowError(f"{branch.name} has no parent to publish to")
        self._require_nothing_open("publish")
        if self.behind():
            raise OutOfDateError(
                f"{branch.name} is behind {branch.parent}",
                hint="Update the branch first ('clio branch update'), then publish",
            )
        text = message or f"Publish {branch.name} to {branch.parent.rsplit('/', 1)[-1]}"
        backend, name = self._publish_backend(branch.parent)
        try:
            created = backend.run("change", ["-i"],
                                  f"Change: new\nClient: {name}\nUser: {s.user}\nStatus: new\nDescription:\n"
                                  + "".join(f"\t{line}\n" for line in text.splitlines()))
            match = created.find(r"Change (\d+) created")
            if not match:
                raise WorkflowError("Could not create the publish change", p4_messages=created.texts)
            number = match.group(1)
            copy = backend.run("copy", ["-v", "-c", number, "-S", branch.path])
            files = tuple(r["depotFile"] for r in copy.records if "depotFile" in r)
            problem = copy.errors or [w for w in copy.warnings if "exclusive" in w or "locked by" in w]
            if problem or not files:
                backend.run("revert", ["-c", number, f"{branch.parent}/..."])
                backend.run("change", ["-d", number])
                if problem:
                    if m := re.search(r"^(//\S+) - .*(?:locked|opened) by (\S+)@(\S+)", "\n".join(copy.texts), re.M):
                        raise LockedByOtherError(
                            f"{m.group(1)} is locked by {m.group(2)} in {branch.parent}",
                            path=m.group(1), user=m.group(2), workspace=m.group(3), branch=branch.parent,
                            hint="Ask them to save or unlock it, then publish again", p4_messages=copy.texts)
                    raise WorkflowError(f"Could not publish {branch.name}", p4_messages=copy.texts)
                raise NothingToSaveError(f"Nothing to publish: {branch.parent} already has everything in "
                                         f"{branch.name}")
            submit = backend.run("submit", ["-c", number, *s.workspace._parallel_args()])
            if submit.errors:
                backend.run("revert", ["-c", number, f"{branch.parent}/..."])
                backend.run("change", ["-d", number])
                raise WorkflowError(f"The server refused to publish {branch.name}; nothing was published",
                                    p4_messages=submit.texts)
            submitted = next((int(r["submittedChange"]) for r in submit.records if "submittedChange" in r),
                             int(number))
        finally:
            backend.close()
        change = Change(submitted, text, s.user, name, datetime.now(), files)
        s.events.emit("branch_published", change=change, branch=branch)
        s.history.note_submitted(files)
        return PublishResult(change, branch.parent, files)
