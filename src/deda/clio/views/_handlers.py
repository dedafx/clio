"""How a session answers the pipeline's requests (design doc §9.5).

One handler per kind of request. Each takes a batch of keys and answers them
with as few server commands as possible. They only read: views never
change anything on the server.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from deda.clio.content import ContentRequest
from deda.clio.history import _revisions
from deda.clio.views._pipeline import RequestPipeline

if TYPE_CHECKING:
    from deda.clio.session import Session

__all__ = ["FSTAT_ARGS", "install"]

#: What a file list needs from ``fstat`` (design doc §8.1, "ask only for what
#: you need"). ``-Ol`` adds the size.
FSTAT_ARGS = ["-Ol", "-T", "depotFile,clientFile,headRev,headChange,headTime,headAction,headType,haveRev,"
                           "action,otherOpen,otherOpen0,otherOpen1,otherOpen2,otherLock,otherLock0,otherLock1,ourLock,fileSize"]

#: Perforce limits that a big folder can run into: the request is split.
_TOO_MANY = ("too many rows", "maxscanrows", "maxresults", "maxlocktime")


def _fstat_scope(session: Session, spec: str, depth: int = 0) -> dict[str, dict[str, str]]:
    """``fstat`` of everything under ``spec``. If the server refuses because
    the folder is too big (``MaxResults``), ask folder by folder instead; the
    user never sees it (design doc §9.4)."""
    result = session.backend.run("fstat", [*FSTAT_ARGS, spec])
    if result.errors and depth < 6 and any(marker in " ".join(result.errors).lower() for marker in _TOO_MANY):
        base = spec.removesuffix("/...")
        records: dict[str, dict[str, str]] = {}
        for rec in session.backend.run("fstat", [*FSTAT_ARGS, f"{base}/*"]).records:
            if "depotFile" in rec:
                records[rec["depotFile"]] = rec
        for folder in session.backend.run("dirs", [f"{base}/*"]).records:
            if "dir" in folder:
                records.update(_fstat_scope(session, f"{folder['dir']}/...", depth + 1))
        return records
    return {rec["depotFile"]: rec for rec in result.records if "depotFile" in rec}


def install(pipeline: RequestPipeline, session: Session) -> None:
    """Register the handlers of ``session`` with its pipeline."""

    def listing(scopes: list[Any]) -> dict[Any, Any]:
        cache = session.history.cache
        answers: dict[Any, Any] = {}
        for scope in scopes:
            records = _fstat_scope(session, scope)
            now = time.monotonic()
            with cache.lock:
                cache.listings[scope] = (records, now)
                cache.listings.move_to_end(scope)
                while len(cache.listings) > 200:
                    cache.listings.popitem(last=False)
            answers[scope] = (records, now)
        return answers

    def rows(depots: list[Any]) -> dict[Any, Any]:
        result = session.backend.run("fstat", [*FSTAT_ARGS, *depots])
        now = time.monotonic()
        found = {rec["depotFile"]: rec for rec in result.records if "depotFile" in rec}
        # A file that is not answered is gone from the server: (None, now).
        return {depot: (found.get(depot), now) for depot in depots}

    def revisions(keys: list[Any]) -> dict[Any, Any]:
        cache = session.history.cache
        depots = list(dict.fromkeys(depot for depot, _ in keys))
        found = {}
        for revision in _revisions(session.backend.run("filelog", ["-m1", "-t", "-l", *depots])):
            cache.remember_revision(revision)
            found[(revision.depot_path, revision.rev)] = revision
        return {key: found.get(key) for key in keys}

    def history(keys: list[Any]) -> dict[Any, Any]:
        # key: (asset path, limit, before, max_age)
        return {key: session.history.revisions(key[0], limit=key[1], before=key[2], max_age=key[3])
                for key in keys}

    def content(requests: list[ContentRequest]) -> dict[Any, Any]:
        return session.content.get_many(session, requests)

    def activity(keys: list[Any]) -> dict[Any, Any]:
        # key: (path, limit, max_age)
        return {key: session.history.activity(key[0], limit=key[1], max_age=key[2]) for key in keys}

    def describe(numbers: list[Any]) -> dict[Any, Any]:
        cache = session.history.cache
        with cache.lock:
            known = {n: cache.change_files[n] for n in numbers if n in cache.change_files}
        missing = [n for n in numbers if n not in known]
        if missing:
            # One command for every change on screen; -s leaves out the diffs.
            result = session.backend.run("describe", ["-s", *map(str, missing)])
            with cache.lock:
                for rec in result.records:
                    if "change" not in rec:
                        continue
                    files, i = [], 0
                    while f"depotFile{i}" in rec:
                        files.append(rec[f"depotFile{i}"])
                        i += 1
                    known[int(rec["change"])] = cache.change_files[int(rec["change"])] = tuple(files)
                while len(cache.change_files) > 5000:
                    cache.change_files.popitem(last=False)
        return {n: known.get(n) for n in numbers}

    def branches(keys: list[Any]) -> dict[Any, Any]:
        listed = session.branches.list(max_age=0)
        return dict.fromkeys(keys, listed)

    def behind(keys: list[Any]) -> dict[Any, Any]:
        return {key: session.branches.behind() for key in keys}

    def branch_diff(keys: list[Any]) -> dict[Any, Any]:
        # key: (this branch's path, the other's). `-q` leaves out identical files.
        answers = {}
        for here, there in keys:
            result = session.backend.run("diff2", ["-q", f"{here}/...", f"{there}/..."])
            answers[(here, there)] = [r for r in result.records if "status" in r]
        return answers

    pipeline.register("listing", listing, max_batch=20)
    pipeline.register("activity", activity, max_batch=20)
    pipeline.register("describe", describe, max_batch=100)
    pipeline.register("branches", branches, max_batch=1)
    pipeline.register("behind", behind, max_batch=1)
    pipeline.register("branchdiff", branch_diff, max_batch=5)
    pipeline.register("rows", rows, max_batch=200)
    pipeline.register("revision", revisions, max_batch=200)
    pipeline.register("history", history, max_batch=20)
    pipeline.register("content", content, max_batch=20)
