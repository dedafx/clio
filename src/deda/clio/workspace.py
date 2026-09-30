"""Everyday workflow: get, lock, save, status, discard (design doc §5.2).

Every action checks the server directly; nothing here trusts a cache
(design doc §9, rule 4). Commands are batched: one Perforce command per
step, whatever the number of files (§8.1).
"""

from __future__ import annotations

import os
import re
import shutil
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from deda.clio import _core
from deda.clio._paths import Target
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


@dataclass(frozen=True, slots=True)
class DiscardResult:
    asset_paths: tuple[str, ...]
    #: Where copies of the discarded files were kept, if any were.
    backup_dir: Path | None


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
        locks = tuple(Lock(self._asset(d), d, user, client) for d in sorted(opened))
        for lock in locks:
            s.events.emit("file_locked", lock=lock)
        return locks

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

    def save(self, *paths: PathArg, message: str, local_base: Path | None = None) -> SaveResult:
        """Save (submit) every added, changed or deleted file under
        ``paths``, in a new change with ``message``.

        Changes are detected on disk (a scoped ``p4 reconcile``), so files
        edited without being locked are included. If the server refuses the
        submit (a trigger, or a file that changed on the server), the change
        stays pending and the local files are untouched (§5.2).
        """
        if not message.strip():
            raise WorkflowError("A save needs a description", hint="Say what changed, for example -m 'Crate: UV fix'")
        s = self._session
        targets = self.targets(paths, local_base=local_base)
        specs, add_specs = self._specs(targets), self._add_specs(targets)
        number = self._new_change(message)
        try:
            self._run("reopen", ["-c", str(number), *specs])
            self._check(self._run("reconcile", ["-c", str(number), "-e", "-a", "-d", "-f", *add_specs]),
                        "detect changed files")
            opened = [r for r in self._run("opened", ["-c", str(number)]).records if "depotFile" in r]
            if not opened:
                raise NothingToSaveError("Nothing to save: no added, changed or deleted files",
                                         hint="Change files first, or check the paths")
            self._precheck_submit(number)
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
        return SaveResult(change, *(tuple(by_action[k]) for k in ("added", "edited", "deleted")))

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
        for rec in self._run("fstat", specs).records:
            depot = rec.get("depotFile")
            if not depot:
                continue
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
            reconcile = self._run("reconcile", ["-n", "-e", "-a", "-d", "-f", *self._add_specs(targets)])
            local_state = {"edit": FileState.MODIFIED, "add": FileState.NEW, "delete": FileState.DELETED}
            for rec in reconcile.records:
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

