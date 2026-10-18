"""Everyday workflow: get, lock, save, status, discard (design doc §5.2).

Every action checks the server directly; nothing here trusts a cache
(design doc §9, rule 4). Commands are batched: one Perforce command per
step, whatever the number of files (§8.1).
"""

from __future__ import annotations

import os
import re
import shutil
import stat
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from deda.clio import _core
from deda.clio._paths import Target, escape
from deda.clio.backends.base import Result
from deda.clio.errors import (
    ConfirmationRequiredError,
    LockedByOtherError,
    NothingToSaveError,
    NotInWorkspaceError,
    OutOfDateError,
    ServerPolicyError,
    WorkflowError,
)
from deda.clio.events import CancelToken, ProgressCallback, ProgressEvent
from deda.clio.models import (
    Change,
    EditReport,
    FileState,
    FileStatus,
    Lock,
    SaveResult,
    SyncedFile,
    SyncPlan,
    SyncReport,
)

if TYPE_CHECKING:
    from deda.clio.session import Session

__all__ = ["DiscardResult", "Workspace"]

PathArg = str | os.PathLike

_QUIET_WARNINGS = re.compile(r"file\(s\) up-to-date|no file\(s\) to reconcile|not opened on this client|"
                             r"file\(s\) not opened|- not locked|already locked by you")
_OPENED_STATE = {
    "edit": FileState.EDITING,
    "integrate": FileState.EDITING,
    "add": FileState.ADDING,
    "move/add": FileState.ADDING,
    "branch": FileState.ADDING,
    "delete": FileState.DELETING,
    "move/delete": FileState.DELETING,
}
_DELETED_AT_HEAD = ("delete", "move/delete", "purge", "archive")
#: Files per reconcile command when only some files need a closer look.
_RECONCILE_CHUNK = 200


@dataclass(frozen=True, slots=True)
class DiscardResult:
    asset_paths: tuple[str, ...]
    #: Where copies of the discarded files were kept, if any were.
    backup_dir: Path | None


def _size_is_suspect(head_type: str, depot_size: int | None, local_size: int) -> bool:
    """Whether a read-only file's size says it was changed (and so is worth
    a closer look by Perforce). Only the size of the head revision is known,
    and only some types keep their size on disk: binary files always;
    text files unless the client converts line endings, which on Windows
    only makes them bigger, so only a smaller file is suspicious there.
    Unicode types are converted, so their size says nothing."""
    if depot_size is None:
        return False
    base = head_type.split("+", 1)[0]
    if base in ("binary", "ubinary"):
        return local_size != depot_size
    if base in ("text", "xtext", "ctext", "ktext"):
        return local_size < depot_size if os.name == "nt" else local_size != depot_size
    return False


def _int(value: str | None) -> int | None:
    return int(value) if value not in (None, "", "none") else None


def _others(record: dict[str, str], key: str) -> tuple[str, ...]:
    return tuple(v for k, v in sorted(record.items()) if re.fullmatch(key + r"\d+", k))


def _parse_pin(version: str | _core.Pin | None) -> _core.Pin:
    if version is None:
        return _core.Pin.latest()
    if isinstance(version, _core.Pin):
        return version
    text = version.strip()
    return _core.Pin.latest() if text.lower() in ("head", "#head", "latest") else _core.Pin.parse(text)


class Workspace:
    """The artist's workspace for one project."""

    def __init__(self, session: Session) -> None:
        self._session = session

    # --- helpers --------------------------------------------------------

    @property
    def root(self) -> Path:
        return self._session.paths.root

    def _run(self, command: str, args: Sequence[str] = (), input: str | None = None) -> Result:
        return self._session.backend.run(command, args, input)

    def _check(self, result: Result, what: str) -> Result:
        if result.errors:
            raise WorkflowError(f"Could not {what}", p4_messages=result.texts)
        return result

    def _dirs(self, depot_paths: Iterable[str]) -> set[str]:
        result = self._run("dirs", list(depot_paths))
        return {r["dir"] for r in result.records if "dir" in r}

    def targets(self, paths: Sequence[PathArg], *, local_base: Path | None = None) -> list[Target]:
        """Resolve path arguments (see :meth:`ProjectPaths.targets`)."""
        return self._session.paths.targets(paths, is_depot_folder=self._dirs, local_base=local_base)

    def _specs(self, targets: Sequence[Target], rev: str = "") -> list[str]:
        depot = self._session.paths.depot_root
        return [t.depot_spec(depot) + rev for t in targets]

    def _add_specs(self, targets: Sequence[Target]) -> list[str]:
        """Specs for commands that can add files (reconcile, add): a file
        that is not in the depot yet is only found by its local path, which
        '-f' takes literally even with @ # % * in it. Folders stay depot
        paths."""
        s = self._session
        return [
            t.depot_spec(s.paths.depot_root) if t.is_folder else os.fspath(s.paths.local_path(t.asset_path))
            for t in targets
        ]

    def _asset(self, depot_path: str) -> str:
        return self._session.paths.asset_path_of_depot(depot_path)

    def _parallel_args(self) -> list[str]:
        c = self._session.config
        if c.parallel_threads <= 1 or c.tickets:
            # With a ticket file set on the connection ('tickets'), the extra
            # connections of a parallel transfer still read P4TICKETS from
            # the environment and fail to log in: transfer serially.
            return []
        return [f"--parallel=threads={c.parallel_threads},min={c.parallel_min_files}"]

    def _warnings(self, result: Result) -> tuple[str, ...]:
        return tuple(w for w in result.warnings if not _QUIET_WARNINGS.search(w))

    # --- get ------------------------------------------------------------

    def get(
        self,
        *paths: PathArg,
        version: str | _core.Pin | None = None,
        preview: bool = False,
        force: bool = False,
        progress: ProgressCallback | None = None,
        cancel: CancelToken | None = None,
        local_base: Path | None = None,
    ) -> SyncReport | SyncPlan:
        """Get files (``p4 sync``), latest by default.

        ``version`` is ``latest``, ``have``, ``@<change>``, ``@<label>`` or
        ``#<revision>``. ``preview=True`` returns a :class:`SyncPlan`
        (what would be transferred) and changes nothing. ``force=True``
        rewrites files that are already current. Transfers run in parallel
        when the server allows it (§8.2). No paths means the whole project.
        """
        pin = _parse_pin(version)
        specs = self._specs(self.targets(paths, local_base=local_base), pin.p4_rev_spec())
        force_args = ["-f"] if force else []
        if cancel:
            cancel.raise_if_cancelled()
        plan: SyncPlan | None = None
        if preview or progress:
            plan = self._plan(self._check(self._run("sync", ["-n", *force_args, *specs]), "preview the get"))
            if preview:
                return plan
            progress(ProgressEvent("get", 0, plan.file_count, 0, plan.bytes_to_transfer))
            if cancel:
                cancel.raise_if_cancelled()
        result = self._check(self._run("sync", ["-q", *force_args, *self._parallel_args(), *specs]), "get files")
        totals = [r for r in result.records if "totalFileCount" in r]
        count = sum(int(r["totalFileCount"]) for r in totals)
        size = sum(int(r.get("totalFileSize", 0)) for r in totals)
        if progress:
            progress(ProgressEvent("get", count, count, size, size))
        report = SyncReport(count, size, self._warnings(result))
        if count:
            self._session.events.emit("files_synced", report=report)
        return report

    def _plan(self, result: Result) -> SyncPlan:
        files = tuple(
            SyncedFile(self._asset(r["depotFile"]), r["depotFile"], _int(r.get("rev")), r.get("action", ""),
                       _int(r.get("fileSize")))
            for r in result.records
            if "depotFile" in r
        )
        totals = [r for r in result.records if "totalFileCount" in r]
        count = sum(int(r["totalFileCount"]) for r in totals) if totals else len(files)
        size = sum(int(r.get("totalFileSize", 0)) for r in totals) if totals else sum(f.size or 0 for f in files)
        return SyncPlan(count, size, files)

    # --- lock / unlock --------------------------------------------------

    def lock(
        self, *paths: PathArg, allow_out_of_date: bool = False, local_base: Path | None = None
    ) -> tuple[Lock, ...]:
        """Lock files so nobody else can change them, and open them for
        editing (``p4 edit`` + ``p4 lock``). New files are opened for add.

        Raises :class:`LockedByOtherError` if someone else has a file locked
        or exclusively open, and :class:`OutOfDateError` if the file on disk
        is not the latest version (get it first), unless
        ``allow_out_of_date`` is set.
        """
        s = self._session
        targets = self.targets(paths, local_base=local_base)
        specs = [t.depot_spec(s.paths.depot_root) for t in targets]
        fstat = self._run("fstat", specs)
        records = {r["depotFile"]: r for r in fstat.records if "depotFile" in r}

        to_edit: list[str] = []
        already_open: list[str] = []
        out_of_date: list[str] = []
        for depot, rec in records.items():
            deleted = rec.get("headAction") in _DELETED_AT_HEAD
            if rec.get("action"):
                already_open.append(depot)
                continue
            if deleted:
                continue
            self._raise_if_locked_by_other(depot, rec)
            have, head = _int(rec.get("haveRev")), _int(rec.get("headRev"))
            if have != head:
                out_of_date.append(depot)
            to_edit.append(depot)
        if out_of_date and not allow_out_of_date:
            raise OutOfDateError(
                "The file on disk is not the latest version" if len(out_of_date) == 1
                else f"{len(out_of_date)} files on disk are not the latest version",
                paths=[self._asset(d) for d in out_of_date],
                hint="Get the latest version first ('clio get'), then lock",
            )
        to_add: list[str] = []  # local paths, for 'add -f'
        to_add_depot: list[str] = []
        for target in targets:
            depot = s.paths.depot_path(target.asset_path)
            if target.is_folder:
                continue
            rec = records.get(depot)
            if rec is None or (rec.get("headAction") in _DELETED_AT_HEAD and not rec.get("action")):
                if not s.paths.local_path(target.asset_path).is_file():
                    raise NotInWorkspaceError(
                        f"{target.asset_path} is not in Perforce and not on disk",
                        hint="Check the path, or create the file first",
                    )
                to_add.append(os.fspath(s.paths.local_path(target.asset_path)))
                to_add_depot.append(depot)

        elsewhere = self._open_elsewhere([*already_open, *to_edit, *to_add_depot])

        opened: set[str] = set(already_open)
        messages: list[str] = []
        for command, files in (("edit", to_edit), ("add", to_add)):
            if files:
                args = ["-f", *files] if command == "add" else files
                result = self._run(command, args)
                opened.update(r["depotFile"] for r in result.records if r.get("action"))
                messages.extend(result.texts)
        for depot in [*to_edit, *to_add_depot]:
            if depot not in opened:
                self._raise_open_failure(depot, messages)
        if not opened:
            return ()
        result = self._run("lock", sorted(opened))
        for text in result.texts:
            if match := re.search(r"^(//.+?) - (?:already )?locked by (\S+)@(\S+)", text):
                self._raise_locked(match.group(1), match.group(2), match.group(3), result.texts)
        self._check(result, "lock files")
        user, client = s.user, s.client
        locks = tuple(Lock(self._asset(d), d, user, client, elsewhere.get(d, ())) for d in sorted(opened))
        for lock in locks:
            s.events.emit("file_locked", lock=lock)
        return locks

    def _open_elsewhere(self, depot_files: Sequence[str]) -> dict[str, tuple[str, ...]]:
        """Who has the same assets open in related branches (parent,
        siblings, children), in one ``fstat`` (design doc §7.3). With
        ``cross_branch_locks = "block"`` a hit raises
        :class:`LockedByOtherError`; with ``"off"`` nothing is checked."""
        s = self._session
        mode = s.config.cross_branch_locks
        if mode == "off" or not depot_files or not s.stream:
            return {}
        related = s.branches.related()
        if not related:
            return {}
        asked: dict[str, str] = {}  # other branch's depot path -> our depot path
        for depot in depot_files:
            asset = escape(self._asset(depot))
            for branch in related:
                asked[f"{branch.path}/{asset}"] = depot
        found: dict[str, list[str]] = {}
        for rec in self._run("fstat", list(asked)).records:
            other = rec.get("depotFile")
            if other not in asked:
                continue
            holders = list(_others(rec, "otherOpen"))
            locked = set(_others(rec, "otherLock"))
            stream = other[: -len(escape(self._asset(asked[other]))) - 1]
            for holder in holders:
                if mode == "block":
                    user, _, workspace = holder.partition("@")
                    asset = self._asset(asked[other])
                    raise LockedByOtherError(
                        f"{asset} is {'locked' if holder in locked else 'open'} by {user} in {stream}",
                        path=asset, user=user, workspace=workspace, branch=stream,
                        hint=f"Ask {user} to save or unlock it there first; this project blocks cross-branch edits",
                    )
                found.setdefault(asked[other], []).append(f"{holder} ({stream})")
        return {depot: tuple(holders) for depot, holders in found.items()}

    def switch(self, branch: str, *, share_open_files: bool = False):
        """Move this workspace to another branch (see
        :meth:`Branches.switch`)."""
        return self._session.branches.switch(branch, share_open_files=share_open_files)

    def _raise_if_locked_by_other(self, depot: str, rec: dict[str, str]) -> None:
        holder = None
        if "otherLock" in rec:
            holder = next(iter(_others(rec, "otherLock")), None) or next(iter(_others(rec, "otherOpen")), "someone")
        elif "+l" in rec.get("headType", "") and _others(rec, "otherOpen"):
            holder = _others(rec, "otherOpen")[0]
        if holder:
            user, _, workspace = holder.partition("@")
            self._raise_locked(depot, user, workspace, [])

    def _raise_locked(self, depot: str, user: str, workspace: str, messages: Sequence[str]) -> None:
        asset = self._asset(depot)
        raise LockedByOtherError(
            f"{asset} is locked by {user}",
            path=asset,
            user=user,
            workspace=workspace,
            hint=f"Ask {user} to save or unlock it (workspace {workspace})",
            p4_messages=messages,
        )

    def _raise_open_failure(self, depot: str, messages: Sequence[str]) -> None:
        for text in messages:
            if text.startswith(depot) and (m := re.search(r"(?:locked|also opened) by (\S+)@(\S+)", text)):
                self._raise_locked(depot, m.group(1), m.group(2), messages)
        raise WorkflowError(f"Could not open {self._asset(depot)} for editing", p4_messages=messages)

    def open_for_edit(self, *paths: PathArg, local_base: Path | None = None) -> EditReport:
        """Get files ready to be written, as the USD plugin does when a layer
        is saved (design doc §10.6): open them for edit (a ``+l`` file type
        locks them by that), in one ``fstat`` and one ``edit`` however many
        files there are. Never submits.

        Files that are not in Perforce yet are reported in ``new``; write
        them, then call :meth:`add_new`. Raises :class:`LockedByOtherError`
        before opening anything if someone else holds a lock, and
        :class:`NotInWorkspaceError` for a file that is in Perforce but not in
        this workspace. A copy that is not the latest version is a warning.
        """
        s = self._session
        targets = self.targets(paths, local_base=local_base)
        folders = [t.asset_path or "." for t in targets if t.is_folder]
        if folders:
            raise WorkflowError(f"open_for_edit takes files, not folders: {', '.join(folders)}",
                                hint="List the files to be written")
        records = {r["depotFile"]: r for r in
                   self._run("fstat", [t.depot_spec(s.paths.depot_root) for t in targets]).records
                   if "depotFile" in r}

        to_edit: list[Target] = []
        already: list[str] = []
        new: list[str] = []
        warnings: list[str] = []
        for target in targets:
            depot = s.paths.depot_path(target.asset_path)
            rec = records.get(depot)
            if rec is not None and rec.get("action"):
                already.append(target.asset_path)
            elif rec is None or rec.get("headAction") in _DELETED_AT_HEAD:
                new.append(target.asset_path)
            else:
                self._raise_if_locked_by_other(depot, rec)
                have, head = _int(rec.get("haveRev")), _int(rec.get("headRev"))
                if have is None:
                    raise NotInWorkspaceError(
                        f"{target.asset_path} is in Perforce but not in your workspace",
                        hint="Get the file first ('clio get'), then change it")
                if have != head:
                    warnings.append(f"{target.asset_path}: your copy is not the latest version")
                to_edit.append(target)

        opened = list(already)
        if to_edit:
            result = self._run("edit", [os.fspath(s.paths.local_path(t.asset_path)) for t in to_edit])
            done = {r["depotFile"] for r in result.records if r.get("action")}
            for target in to_edit:
                depot = s.paths.depot_path(target.asset_path)
                if depot not in done:
                    self._raise_open_failure(depot, result.texts)
                opened.append(target.asset_path)
        return EditReport(tuple(sorted(opened)), tuple(sorted(new)), tuple(warnings))

    def add_new(self, *paths: PathArg, local_base: Path | None = None) -> tuple[str, ...]:
        """Open files for add (``p4 add``), in one command: the second half
        of :meth:`open_for_edit` for files it reported as new. A path that was
        not written is skipped. Returns the asset paths opened for add."""
        s = self._session
        files = []
        for target in self.targets(paths, local_base=local_base):
            local = s.paths.local_path(target.asset_path)
            if not target.is_folder and local.is_file():
                files.append((target.asset_path, os.fspath(local)))
        if not files:
            return ()
        result = self._check(self._run("add", ["-f", *(local for _, local in files)]), "add files")
        opened = {s.paths.asset_path_of_depot(r["depotFile"]) for r in result.records if r.get("action")}
        return tuple(sorted(opened))

    def unlock(self, *paths: PathArg, local_base: Path | None = None) -> tuple[str, ...]:
        """Release locks. Files that were not changed are closed again
        (``p4 revert -a``); changed files stay open for ``save``."""
        specs = self._specs(self.targets(paths, local_base=local_base))
        result = self._run("unlock", specs)
        unlocked = tuple(self._asset(r["depotFile"]) for r in result.records if "depotFile" in r)
        self._check(self._run("revert", ["-a", *specs]), "close unchanged files")
        for asset in unlocked:
            self._session.events.emit("file_unlocked", asset_path=asset)
        return unlocked

    # --- save -----------------------------------------------------------

    def save(self, *paths: PathArg, message: str, local_base: Path | None = None,
             validate: bool = True) -> SaveResult:
        """Save (submit) every added, changed or deleted file under
        ``paths``, in a new change with ``message``.

        Changes are detected on disk (a scoped ``p4 reconcile``), so files
        edited without being locked are included. If the server refuses the
        submit (a trigger, or a file that changed on the server), the change
        stays pending and the local files are untouched (§5.2).

        Validation hooks (:mod:`deda.clio.hooks`) run first; an error from one
        raises :class:`~deda.clio.ValidationError` and nothing is submitted.
        ``validate=False`` skips them.
        """
        if not message.strip():
            raise WorkflowError("A save needs a description", hint="Say what changed, for example -m 'Crate: UV fix'")
        s = self._session
        targets = self.targets(paths, local_base=local_base)
        specs = self._specs(targets)
        number = self._new_change(message)
        try:
            records = [r for r in self._run("fstat", ["-Ol", *specs]).records if "depotFile" in r]
            self._run("reopen", ["-c", str(number), *specs])
            self._disk_changes(targets, records, ["-c", str(number)])
            opened = [r for r in self._run("opened", ["-c", str(number)]).records if "depotFile" in r]
            if not opened:
                raise NothingToSaveError("Nothing to save: no added, changed or deleted files",
                                         hint="Change files first, or check the paths")
            self._precheck_submit(number)
            warnings = self._validate(opened, message) if validate else ()
        except Exception:
            self._abandon_change(number)
            raise
        result = self._run("submit", ["-c", str(number), *self._parallel_args()])
        if result.errors:
            self._raise_submit_failure(number, result)
        submitted = next((int(r["submittedChange"]) for r in result.records if "submittedChange" in r), number)
        by_action: dict[str, list[str]] = {"added": [], "edited": [], "deleted": []}
        for r in opened:
            action = r.get("action", "")
            key = "added" if action in ("add", "move/add", "branch") else (
                "deleted" if action in ("delete", "move/delete") else "edited")
            by_action[key].append(self._asset(r["depotFile"]))
        change = Change(submitted, message, s.user, s.client, datetime.now(),
                        tuple(r["depotFile"] for r in opened))
        s.events.emit("change_submitted", change=change)
        s.history.note_submitted(change.files)
        return SaveResult(change, *(tuple(by_action[k]) for k in ("added", "edited", "deleted")), warnings)

    def _validate(self, opened: Sequence[dict[str, str]], message: str) -> tuple[str, ...]:
        """Run the validation hooks on the files of the change. Returns the
        warnings; raises on errors."""
        from deda.clio.hooks import HookContext, HookFile, run_hooks

        s = self._session
        files = tuple(
            HookFile(self._asset(r["depotFile"]), r["depotFile"], s.paths.local_of_depot(r["depotFile"]),
                     r.get("action", ""), r.get("type", ""))
            for r in opened
        )
        report = run_hooks(HookContext(s, files, message))
        return tuple(str(i) for i in report.warnings)

    def _new_change(self, message: str) -> int:
        s = self._session
        description = "\n".join("\t" + line for line in message.strip().splitlines())
        spec = f"Change: new\nClient: {s.client}\nUser: {s.user}\nStatus: new\nDescription:\n{description}\n"
        result = self._check(self._run("change", ["-i"], spec), "create a change")
        match = result.find(r"Change (\d+) created")
        if not match:
            raise WorkflowError("Could not create a change", p4_messages=result.texts)
        return int(match.group(1))

    def _abandon_change(self, number: int) -> None:
        """Put files back in the default change and delete an unsubmitted,
        now empty change. Local files are never touched."""
        files = [r["depotFile"] for r in self._run("opened", ["-c", str(number)]).records if "depotFile" in r]
        if files:
            self._run("reopen", ["-c", "default", *files])
        self._run("change", ["-d", str(number)])

    def _precheck_submit(self, number: int) -> None:
        # The files of the change, filtered on the server: one short command
        # rather than one argument per file.
        scope = self._session.paths.depot_root + "/..."
        records = [r for r in self._run("fstat", ["-Ro", "-e", str(number), scope]).records if "depotFile" in r]
        stale = []
        for rec in records:
            self._raise_if_locked_by_other(rec["depotFile"], rec)
            if rec.get("action") in ("edit", "delete") and _int(rec.get("haveRev")) != _int(rec.get("headRev")):
                stale.append(rec["depotFile"])
        if stale:
            raise OutOfDateError(
                "Someone saved a newer version of " + (
                    self._asset(stale[0]) if len(stale) == 1 else f"{len(stale)} of these files"),
                paths=[self._asset(d) for d in stale],
                hint="Nothing was saved. Merging binary files comes with branching; for now keep a copy, "
                     "get the latest version and redo the change",
            )

    def _raise_submit_failure(self, number: int, result: Result) -> None:
        text = "\n".join(result.texts)
        if "must resolve" in text or "out of date" in text:
            raise OutOfDateError(f"Change {number} was not saved: files changed on the server",
                                 hint=f"Change {number} is still pending", p4_messages=result.texts)
        if "trigger" in text.lower() or "validation failed" in text.lower():
            raise ServerPolicyError(
                f"The server refused change {number}",
                change=number,
                hint=f"Fix what the server reports, then save again (change {number} is still pending)",
                p4_messages=result.texts,
            )
        raise WorkflowError(f"Change {number} was not saved", hint=f"Change {number} is still pending",
                            p4_messages=result.texts)

    # --- status ---------------------------------------------------------

    def status(self, *paths: PathArg, check_disk: bool = True, local_base: Path | None = None) -> list[FileStatus]:
        """Where each file stands: synced, out of date, being edited,
        locked, and (with ``check_disk``) changed on disk without Perforce
        knowing. Asks the server live; two commands whatever the file count.
        """
        s = self._session
        targets = self.targets(paths, local_base=local_base)
        specs = self._specs(targets)
        statuses: dict[str, FileStatus] = {}
        records = [r for r in self._run("fstat", ["-Ol", *specs]).records if r.get("depotFile")]
        for rec in records:
            depot = rec["depotFile"]
            action = rec.get("action")
            have, head = _int(rec.get("haveRev")), _int(rec.get("headRev"))
            deleted_at_head = rec.get("headAction") in _DELETED_AT_HEAD
            if action:
                state = _OPENED_STATE.get(action, FileState.EDITING)
            elif deleted_at_head and have is None:
                continue
            elif have is None:
                state = FileState.MISSING
            elif deleted_at_head or have != head:
                state = FileState.OUT_OF_DATE
            else:
                state = FileState.SYNCED
            locked_by = None
            if "otherLock" in rec:
                locked_by = next(iter(_others(rec, "otherLock")), None) or "someone"
            elif "+l" in rec.get("headType", "") and _others(rec, "otherOpen"):
                locked_by = _others(rec, "otherOpen")[0]
            asset = self._asset(depot)
            statuses[depot] = FileStatus(
                asset, depot, s.paths.local_path(asset), state, have, head, _int(rec.get("headChange")),
                "ourLock" in rec, locked_by, _others(rec, "otherOpen"),
            )
        if check_disk:
            local_state = {"edit": FileState.MODIFIED, "add": FileState.NEW, "delete": FileState.DELETED}
            for rec in self._disk_changes(targets, records, ["-n"]):
                depot, state = rec.get("depotFile"), local_state.get(rec.get("action", ""))
                if not depot or not state:
                    continue
                current = statuses.get(depot)
                if current is None:
                    asset = self._asset(depot)
                    statuses[depot] = FileStatus(asset, depot, s.paths.local_path(asset), state)
                elif current.state in (FileState.SYNCED, FileState.OUT_OF_DATE, FileState.MISSING):
                    statuses[depot] = replace(current, state=state)
        return sorted(statuses.values(), key=lambda f: f.asset_path)

    def _disk_changes(
        self, targets: Sequence[Target], records: Sequence[dict[str, str]], reconcile_args: Sequence[str]
    ) -> list[dict[str, str]]:
        """Run ``p4 reconcile`` for what changed on disk without Perforce
        knowing, and return its records. ``records`` are ``fstat -Ol`` records
        for the targets; ``reconcile_args`` are ``-n`` (just look) or
        ``-c N`` (open into a change).

        ``p4 reconcile`` opens and hashes every file, which costs about
        3 ms a file on Windows (virus scanners) against a few microseconds
        to ``stat`` it. So the files are looked at first without opening
        them, and only the suspicious ones go to reconcile: a file that is
        missing, writable (Perforce leaves unopened files read-only), or the
        wrong size (see :func:`_size_is_suspect`). Files already opened are
        left alone. New files are found by a separate scoped ``reconcile
        -a``, which does not open the files that are already known.
        Perforce still makes every final decision.
        """
        suspects: list[str] = []
        for rec in records:
            if rec.get("action") or _int(rec.get("haveRev")) is None or "clientFile" not in rec:
                continue  # opened, or not in the workspace
            local = rec["clientFile"]
            try:
                info = os.stat(local)
            except OSError:  # missing, or unreadable: let Perforce say
                suspects.append(local)
                continue
            if info.st_mode & stat.S_IWRITE or (
                    rec.get("haveRev") == rec.get("headRev")
                    and _size_is_suspect(rec.get("headType", ""), _int(rec.get("fileSize")), info.st_size)):
                suspects.append(local)
        found = list(self._check(
            self._run("reconcile", [*reconcile_args, "-a", "-f", *self._add_specs(targets)]),
            "detect new files").records)
        for start in range(0, len(suspects), _RECONCILE_CHUNK):
            chunk = suspects[start:start + _RECONCILE_CHUNK]
            found.extend(self._check(
                self._run("reconcile", [*reconcile_args, "-e", "-d", "-f", *chunk]),
                "detect changed files").records)
        return found

    # --- discard --------------------------------------------------------

    def discard(
        self, *paths: PathArg, confirm: bool = False, backup: bool = True, local_base: Path | None = None
    ) -> DiscardResult:
        """Throw away local changes to opened files (``p4 revert``) and
        release their locks. Needs ``confirm=True``. With ``backup`` (the
        default) changed files are copied to a timestamped folder first
        (design doc §11). Files opened for add stay on disk.
        """
        if not confirm:
            raise ConfirmationRequiredError("Discarding changes needs confirmation",
                                            hint="Pass confirm=True (or --yes on the command line)")
        s = self._session
        specs = self._specs(self.targets(paths, local_base=local_base))
        opened = [r for r in self._run("opened", specs).records if "depotFile" in r]
        if not opened:
            return DiscardResult((), None)
        backup_dir = None
        if backup:
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            candidate = s.config.backup_path / (s.config.project or s.client) / stamp
            for rec in opened:
                if rec.get("action") in ("delete", "move/delete"):
                    continue
                asset = self._asset(rec["depotFile"])
                local = s.paths.local_path(asset)
                if local.is_file():
                    dest = candidate.joinpath(*asset.split("/"))
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(local, dest)
                    backup_dir = candidate
        self._check(self._run("revert", specs), "discard changes")
        assets = tuple(self._asset(r["depotFile"]) for r in opened)
        s.events.emit("files_discarded", asset_paths=assets, backup_dir=backup_dir)
        return DiscardResult(assets, backup_dir)

