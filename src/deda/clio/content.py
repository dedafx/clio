"""File bytes fetched for display, kept on disk (design doc §9.3, ContentCache).

A revision's content never changes, and it is identified by the server's
digest and size, so identical content (a file copied to a branch) is stored
once and never downloaded twice. Content comes from ``p4 print -o``, which
does not touch the workspace.

The cache is a folder with a size limit. When it grows past the limit, the
files used least recently are removed. It is for display: a copy that is
missing or damaged is simply fetched again.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from deda.clio import _core
from deda.clio.config import cache_dir
from deda.clio.errors import WorkflowError

if TYPE_CHECKING:
    from deda.clio.session import Session

__all__ = ["ContentCache", "ContentRequest", "VerifyReport"]

#: When over the limit, evict down to this share of it, so that the next
#: fetch does not evict again.
_LOW_WATER = 0.9


@dataclass(frozen=True, slots=True)
class ContentRequest:
    """One revision to show."""

    depot_path: str
    rev: int
    #: The server's MD5 and size of the revision (from ``fstat -Ol`` or
    #: ``filelog``): they name the cached file.
    digest: str
    size: int
    file_type: str = ""

    @property
    def is_text(self) -> bool:
        return self.file_type.startswith(("text", "unicode", "utf8"))


@dataclass(frozen=True, slots=True)
class VerifyReport:
    checked: int
    #: Files that did not match their digest and were removed.
    removed: tuple[str, ...]


class ContentCache:
    """A size-limited, least-recently-used folder of file contents."""

    def __init__(self, folder: str | os.PathLike | None = None, max_bytes: int = 5 * 2**30) -> None:
        self.folder = Path(folder) if folder is not None else cache_dir() / "content"
        self.max_bytes = max_bytes
        self._lock = threading.Lock()
        #: name -> (size on disk), filled on first use
        self._index: dict[str, int] | None = None
        self._total = 0

    # --- naming -----------------------------------------------------------

    def path_for(self, request: ContentRequest) -> Path:
        """Where the content of ``request`` is (or will be) kept: named by
        digest and size, with the file's own extension so that viewers
        recognise it."""
        suffix = PurePosixPath(request.depot_path).suffix
        return self.folder / f"{request.digest.lower()}-{request.size}{suffix}"

    # --- reading ----------------------------------------------------------

    def get(self, session: Session, request: ContentRequest) -> Path:
        """The cached copy of a revision, fetched first if it is not here.
        The returned file is read-only; do not change it. Raises
        :class:`~deda.clio.WorkflowError` if the revision cannot be fetched
        (for example because it was obliterated)."""
        target = self.path_for(request)
        if target.is_file():
            self._touch(target)
            return target
        self.folder.mkdir(parents=True, exist_ok=True)
        temp = target.with_name(f"{target.name}.{os.getpid()}-{threading.get_ident()}.tmp")
        try:
            result = session.backend.run(
                "print", ["-q", "-o", os.fspath(temp), f"{request.depot_path}#{request.rev}"])
            if result.errors or not temp.is_file():
                raise WorkflowError(
                    f"{request.depot_path}#{request.rev} cannot be fetched",
                    hint="It may have been removed from the server; refresh the history",
                    p4_messages=result.texts)
            if request.digest and not request.file_type.startswith("utf16"):
                actual = _core.md5_file(os.fspath(temp), request.is_text)
                if actual != request.digest.upper():
                    raise WorkflowError(
                        f"{request.depot_path}#{request.rev} arrived with another digest than the server reported",
                        hint="Try again; if it persists the server's history has changed (refresh the history)")
            size = temp.stat().st_size
            temp.chmod(0o444)
            os.replace(temp, target)
        finally:
            try:  # p4 print leaves its file read-only, which Windows will not delete
                temp.chmod(0o666)
                temp.unlink()
            except OSError:
                pass
        self._added(target.name, size)
        return target

    def get_many(self, session: Session, requests: Sequence[ContentRequest], *, threads: int = 4) -> dict[
            ContentRequest, Path | WorkflowError]:
        """Several revisions at once, fetched in parallel. A revision that
        cannot be fetched maps to its error instead of stopping the others."""
        unique = list(dict.fromkeys(requests))

        def one(request: ContentRequest) -> Path | WorkflowError:
            try:
                return self.get(session, request)
            except WorkflowError as e:
                return e

        with ThreadPoolExecutor(max_workers=max(1, min(threads, len(unique))), thread_name_prefix="clio-content") as pool:
            return dict(zip(unique, pool.map(one, unique)))

    def contains(self, request: ContentRequest) -> bool:
        return self.path_for(request).is_file()

    # --- size and eviction -------------------------------------------------

    def _scan(self) -> dict[str, int]:
        if self._index is None:
            self._index = {}
            self._total = 0
            if self.folder.is_dir():
                for entry in os.scandir(self.folder):
                    if entry.is_file() and not entry.name.endswith(".tmp"):
                        self._index[entry.name] = entry.stat().st_size
                        self._total += entry.stat().st_size
        return self._index

    @property
    def total_bytes(self) -> int:
        """Bytes in the cache."""
        with self._lock:
            self._scan()
            return self._total

    def _touch(self, path: Path) -> None:
        try:
            os.utime(path)
        except OSError:
            pass

    def _added(self, name: str, size: int) -> None:
        with self._lock:
            index = self._scan()
            self._total += size - index.get(name, 0)
            index[name] = size
            if self._total > self.max_bytes:
                self._evict(keep=name)

    def _evict(self, keep: str) -> None:
        """Remove least recently used files until under the low-water mark.
        Called with the lock held."""
        index = self._scan()
        target = int(self.max_bytes * _LOW_WATER)
        by_age: list[tuple[float, str]] = []
        for name in index:
            if name == keep:
                continue
            try:
                by_age.append(((self.folder / name).stat().st_mtime, name))
            except OSError:
                self._total -= index[name]  # already gone
        for name in (n for _, n in sorted(by_age)):
            if self._total <= target:
                break
            size = index.pop(name, 0)
            self._total -= size
            try:
                (self.folder / name).chmod(0o666)
                (self.folder / name).unlink()
            except OSError:
                pass
        self._index = {k: v for k, v in index.items() if (self.folder / k).exists()}
        self._total = sum(self._index.values())

    # --- housekeeping ------------------------------------------------------

    def clear(self) -> int:
        """Remove everything. Returns the number of bytes freed."""
        with self._lock:
            self._scan()
            freed = self._total
            if self.folder.is_dir():
                for entry in os.scandir(self.folder):
                    if entry.is_file():
                        try:
                            os.chmod(entry.path, 0o666)
                            os.unlink(entry.path)
                        except OSError:
                            pass
            self._index, self._total = {}, 0
            return freed

    def verify(self) -> VerifyReport:
        """Check every file against the digest in its name, and remove the
        ones that do not match (a damaged copy would be shown as if it were
        the server's). Text files are accepted with either line ending."""
        removed: list[str] = []
        checked = 0
        with self._lock:
            index = self._scan()
            for name in list(index):
                path = self.folder / name
                digest = name.split("-", 1)[0].upper()
                checked += 1
                try:
                    ok = digest in (_core.md5_file(os.fspath(path), False), _core.md5_file(os.fspath(path), True))
                except OSError:
                    ok = False
                if not ok:
                    removed.append(name)
                    self._total -= index.pop(name, 0)
                    try:
                        path.chmod(0o666)
                        path.unlink()
                    except OSError:
                        pass
        return VerifyReport(checked, tuple(removed))


def requests_from_revisions(revisions: Iterable) -> list[ContentRequest]:
    """Content requests for :class:`~deda.clio.models.Revision` objects that
    carry a digest and size (``History.revisions`` provides them)."""
    return [ContentRequest(r.depot_path, r.rev, r.digest or "", r.size or 0, r.file_type)
            for r in revisions if r.digest is not None]
