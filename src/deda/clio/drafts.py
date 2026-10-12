"""Drafts: work shared without saving it (Perforce shelves, design doc §3).

A draft is a pending change whose files are stored on the server. A lead
can look at it, another artist can get it into their workspace, and it can
be dropped when no longer needed. Nothing in a draft is saved to the branch.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from deda.clio.backends.base import Result
from deda.clio.errors import ConfirmationRequiredError, NothingToSaveError, NotInWorkspaceError, WorkflowError
from deda.clio.models import Draft

if TYPE_CHECKING:
    from deda.clio.session import Session
    from deda.clio.workspace import PathArg

__all__ = ["Drafts"]


def _drafts(result: Result) -> list[Draft]:
    """Drafts from ``p4 describe -s -S`` records (one per change)."""
    drafts = []
    for rec in result.records:
        if "change" not in rec:
            continue
        files = tuple(v for k, v in sorted(rec.items(), key=lambda kv: (len(kv[0]), kv[0]))
                      if k.startswith("depotFile") and k[9:].isdigit())
        drafts.append(Draft(
            int(rec["change"]), rec.get("desc", "").rstrip("\n"), rec.get("user", ""), rec.get("client", ""),
            datetime.fromtimestamp(int(rec["time"])) if rec.get("time") else None, files,
        ))
    return drafts


class Drafts:
    def __init__(self, session: Session) -> None:
        self._session = session

    def _run(self, command: str, args: list[str], input: str | None = None) -> Result:
        return self._session.backend.run(command, args, input)

    def share(
        self,
        *paths: PathArg,
        message: str,
        keep_open: bool = True,
        detect_changes: bool = True,
        local_base=None,
    ) -> Draft:
        """Share changed files under ``paths`` as a draft.

        With ``detect_changes`` (the default) new, edited and deleted files
        are found on disk, as ``save`` does; otherwise only files already
        open are shared. The files stay open in the workspace unless
        ``keep_open`` is False, in which case they are closed and the local
        copies go back to the branch's version (the draft keeps the work).
        """
        s = self._session
        ws = s.workspace
        if not message.strip():
            raise WorkflowError("A draft needs a description", hint="Say what it is, for example -m 'WIP lid'")
        targets = ws.targets(paths, local_base=local_base)
        specs = ws._specs(targets)
        number = ws._new_change(message)
        try:
            self._run("reopen", ["-c", str(number), *specs])
            if detect_changes:
                result = self._run("reconcile", ["-c", str(number), "-e", "-a", "-d", "-f", *ws._add_specs(targets)])
                if result.errors:
                    raise WorkflowError("Could not detect changed files", p4_messages=result.texts)
            opened = [r["depotFile"] for r in self._run("opened", ["-c", str(number)]).records if "depotFile" in r]
            if not opened:
                raise NothingToSaveError("Nothing to share: no added, changed or deleted files",
                                         hint="Change files first, or check the paths")
            shelved = self._run("shelve", ["-c", str(number)])
            if shelved.errors:
                raise WorkflowError("Could not share the draft", p4_messages=shelved.texts)
        except Exception:
            ws._abandon_change(number)
            raise
        if not keep_open:
            # The draft holds the work now; put the files back. Added files
            # stay on disk as Perforce leaves them.
            self._run("revert", ["-c", str(number), f"{s.paths.depot_root}/..."])
        draft = Draft(number, message, s.user, s.client, datetime.now(), tuple(opened))
        s.events.emit("draft_shared", draft=draft)
        return draft

    def list(self, *, everyone: bool = False, limit: int = 50) -> list[Draft]:
        """Drafts in the project (yours, or ``everyone``'s), newest first."""
        s = self._session
        scope = s.paths.depot_root.split("/")[2]
        args = ["-s", "shelved", "-m", str(limit)]
        if not everyone:
            args += ["-u", s.user]
        changes = [r["change"] for r in self._run("changes", [*args, f"//{scope}/..."]).records if "change" in r]
        if not changes:
            return []
        return _drafts(self._run("describe", ["-s", "-S", *changes]))

    def get(self, number: int) -> tuple[str, ...]:
        """Copy a draft's files into this workspace (opened, not saved).
        The draft must be from the current branch. Returns asset paths."""
        s = self._session
        ws = s.workspace
        [draft] = _drafts(self._run("describe", ["-s", "-S", str(number)])) or [None]
        if draft is None:
            raise NotInWorkspaceError(f"There is no draft {number}", hint="See 'clio draft list'")
        prefix = s.paths.depot_root + "/"
        if draft.files and not all(f.startswith(prefix) for f in draft.files):
            raise WorkflowError(f"Draft {number} is from another branch ({draft.files[0]})",
                                hint="Switch to the branch it was made in first")
        result = self._run("unshelve", ["-s", str(number)])
        got = tuple(ws._asset(r["depotFile"]) for r in result.records if "depotFile" in r)
        if result.errors or (draft.files and not got):
            raise WorkflowError(f"Could not get draft {number}",
                                hint="Files that are already open cannot take a draft; save or discard them first",
                                p4_messages=result.texts)
        return got

    def drop(self, number: int, *, confirm: bool = False) -> None:
        """Delete a draft (your own). Needs ``confirm=True``."""
        if not confirm:
            raise ConfirmationRequiredError(f"Dropping draft {number} needs confirmation",
                                            hint="Pass confirm=True (or --yes)")
        result = self._run("shelve", ["-d", "-c", str(number)])
        if result.errors:
            raise WorkflowError(f"Could not drop draft {number}", p4_messages=result.texts)
        # The change itself may still hold open files (a shared draft keeps
        # them open); then only the stored copy is dropped.
        if not [r for r in self._run("opened", ["-c", str(number)]).records if "depotFile" in r]:
            self._run("change", ["-d", str(number)])
        self._session.events.emit("draft_dropped", number=number)

