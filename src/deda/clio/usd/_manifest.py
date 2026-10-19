"""Version manifests: the same files on every machine (design doc §10.8).

A manifest records the exact revision of every file a stage can load, with
the server's digest. Every farm machine loads through it and nothing else, so
the same manifest gives the same pixels today or in a year.

Creating a manifest needs USD's Python bindings (it reuses the dependency
walk of :func:`deda.clio.usd.prepare`) and a Perforce connection. Loading,
saving and comparing manifests need neither.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from deda.clio import _core
from deda.clio.errors import ManifestError
from deda.clio.events import CancelToken

if TYPE_CHECKING:
    from deda.clio.session import Session

__all__ = [
    "CheckReport",
    "FetchReport",
    "Manifest",
    "ManifestDiff",
    "ManifestFile",
]

_FORMAT = 1
_DELETED_AT_HEAD = ("delete", "move/delete", "purge", "archive")
#: Files per `fstat` when recording revisions.
_FSTAT_CHUNK = 500


@dataclass(frozen=True, slots=True)
class ManifestFile:
    """One file of a manifest."""

    #: Revision to load.
    rev: int
    #: Change that made the revision (information only).
    change: int = 0
    #: The server's MD5 of the revision, upper-case hex.
    digest: str = ""
    #: The server's size of the revision, in bytes.
    size: int = 0
    #: Perforce file type, for example ``binary+Fl``.
    type: str = ""

    @property
    def is_text(self) -> bool:
        return self.type.startswith(("text", "unicode", "utf8"))


@dataclass(frozen=True, slots=True)
class FetchReport:
    """What :meth:`Manifest.fetch` did."""

    files: int
    #: Downloaded from the server.
    fetched: int
    #: Taken from files already stored for another manifest.
    linked: int
    #: Already in the manifest's folder and correct.
    present: int
    bytes_fetched: int
    #: ``"path: reason"`` for each file that could not be provided.
    failures: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.failures


@dataclass(frozen=True, slots=True)
class CheckReport:
    """Whether every revision of a manifest can still be fetched."""

    #: One sentence per revision that is gone or has changed.
    problems: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.problems


@dataclass(frozen=True, slots=True)
class ManifestDiff:
    """What changed between two manifests."""

    added: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    #: path -> (revision in the first manifest, revision in the second)
    changed: dict[str, tuple[int, int]] = field(default_factory=dict)

    @property
    def same(self) -> bool:
        return not (self.added or self.removed or self.changed)


@dataclass(frozen=True)
class Manifest:
    """The exact revision of every file a stage can load."""

    depot: str
    files: dict[str, ManifestFile]
    root_layer: str = ""
    #: How revisions were chosen: ``have``, ``latest``, ``@<change>`` or ``@<label>``.
    source_pin: str = ""
    server: str = ""
    created: str = ""
    created_by: str = ""
    usd_version: str = ""

    # --- reading and writing ----------------------------------------------

    def to_json(self) -> str:
        """The manifest as JSON text: always the same text for the same
        manifest, so its :attr:`sha256` is the job's version id."""
        data: dict[str, Any] = {
            "clio_manifest": _FORMAT,
            "created": self.created,
            "created_by": self.created_by,
            "server": self.server,
            "depot": self.depot,
            "root_layer": self.root_layer,
            "source_pin": self.source_pin,
            "usd_version": self.usd_version,
            "files": {
                path: {"rev": f.rev, "change": f.change, "digest": f.digest, "size": f.size, "type": f.type}
                for path, f in sorted(self.files.items())
            },
        }
        return json.dumps(data, indent=2, ensure_ascii=False) + "\n"

    @property
    def sha256(self) -> str:
        """SHA-256 of the manifest's text: the id of the manifest. A saved
        manifest has the same id when it is loaded again."""
        return hashlib.sha256(self.to_json().encode("utf-8")).hexdigest()

    def save(self, path: str | os.PathLike) -> Path:
        """Write the manifest to ``path`` (UTF-8, human-readable JSON)."""
        target = Path(path)
        target.write_bytes(self.to_json().encode("utf-8"))
        return target

    @classmethod
    def from_json(cls, text: str) -> Manifest:
        """Read a manifest from its JSON text. Raises
        :class:`~deda.clio.ConfigError` if it is not a manifest this version
        understands (the same checks as the resolver's)."""
        _core.Manifest.parse(text)  # one set of rules for Python and C++
        data = json.loads(text)
        files = {
            path: ManifestFile(int(f["rev"]), int(f.get("change", 0)), str(f.get("digest", "")).upper(),
                               int(f.get("size", 0)), str(f.get("type", "")))
            for path, f in data["files"].items()
        }
        return cls(
            depot=data["depot"], files=files, root_layer=data.get("root_layer") or "",
            source_pin=data.get("source_pin") or "", server=data.get("server") or "",
            created=data.get("created") or "", created_by=data.get("created_by") or "",
            usd_version=data.get("usd_version") or "",
        )

    @classmethod
    def load(cls, path: str | os.PathLike) -> Manifest:
        """Read a manifest file."""
        try:
            text = Path(path).read_bytes().decode("utf-8")
        except OSError as e:
            raise _core.ConfigError(f"Could not read the manifest {path}: {e}") from None
        return cls.from_json(text)

    # --- comparing --------------------------------------------------------

    def diff(self, other: Manifest) -> ManifestDiff:
        """What differs from ``other``: files only in ``other`` are
        ``added``, files only here are ``removed``, and files at another
        revision (or with another digest) are ``changed``."""
        mine, theirs = set(self.files), set(other.files)
        changed = {
            path: (self.files[path].rev, other.files[path].rev)
            for path in sorted(mine & theirs)
            if (self.files[path].rev, self.files[path].digest) != (other.files[path].rev, other.files[path].digest)
        }
        return ManifestDiff(tuple(sorted(theirs - mine)), tuple(sorted(mine - theirs)), changed)

    # --- using it ---------------------------------------------------------

    def settings(
        self, session: Session, path: str | os.PathLike, *, policy: str = "verify", store: str | os.PathLike | None = None
    ) -> _core.Settings:
        """Resolver settings that load through the saved manifest at
        ``path``: the context string for a farm job."""
        from deda.clio.usd._prepare import resolver_settings

        return resolver_settings(session, None, policy=policy, store=store, manifest=path)

    def _resolver(self, session: Session, store: str | os.PathLike | None) -> _core.AssetResolver:
        from deda.clio.usd._prepare import resolver_settings

        return _core.AssetResolver(resolver_settings(session, "have", store=store))

    def _check_project(self, session: Session) -> None:
        if self.depot != session.paths.depot_root:
            raise ManifestError(
                f"The manifest is for {self.depot}, but this workspace is {session.paths.depot_root}",
                hint="Use a workspace of the manifest's project",
            )

    def fetch(
        self,
        session: Session,
        *,
        store: str | os.PathLike | None = None,
        parallel: int = 8,
        progress: Callable[[int, int, str], None] | None = None,
    ) -> FetchReport:
        """Put every file of the manifest in the manifest's folder of the
        version store, with parallel ``p4 print``, checking each against its
        digest. Files another manifest already stored are linked, not fetched
        again. Run it on each farm machine before the job starts; farm
        machines should reach Perforce through a P4 Proxy. Failures are in
        the report, not raised."""
        self._check_project(session)
        raw = self._resolver(session, store).fetch_manifest(_core.Manifest.parse(self.to_json()), parallel, progress)
        return FetchReport(raw["files"], raw["fetched"], raw["linked"], raw["present"], raw["bytes"],
                           tuple(raw["failures"]))

    def check(self, session: Session, *, store: str | os.PathLike | None = None) -> CheckReport:
        """Ask the server whether every revision can still be fetched with the
        manifest's digest (an obliterated or archived revision cannot).
        Worth running before a long re-render."""
        self._check_project(session)
        problems = self._resolver(session, store).check_manifest(_core.Manifest.parse(self.to_json()))
        return CheckReport(tuple(problems))

    # --- creating ---------------------------------------------------------

    @classmethod
    def create(
        cls,
        session: Session,
        root_layer: str | os.PathLike,
        *,
        pin: str | _core.Pin | None = None,
        payloads: str | Callable[[str, str], bool] = "all",
        extra: Sequence[str | os.PathLike] = (),
        store: str | os.PathLike | None = None,
        allow_missing: bool = False,
        cancel: CancelToken | None = None,
    ) -> Manifest:
        """Record the exact revision of every file the stage can load.

        The dependency walk is :func:`deda.clio.usd.prepare`'s, so the result
        is a superset of what one render loads (every variant, and payloads
        unless ``payloads="none"``): a variant switch on the farm must not find
        a file missing. ``extra`` lists files no layer mentions (a path built
        at render time). Which revision is recorded depends on ``pin``:

        =============  ===========================================
        ``have``       the workspace's revision (what you look at)
        ``latest``     the head revision, all read in one moment
        ``@<change>``  the revision at that change, or at the label
        =============  ===========================================

        Raises :class:`~deda.clio.ManifestError`, naming the files, if a
        farm machine could not reproduce them: a file not in Perforce, a file
        open for edit, a file whose content differs from its revision, or (unless
        ``allow_missing``) a reference that could not be provided. Submit or
        revert those files, then try again.
        """
        from deda.clio.usd._prepare import prepare

        pin_text = "have" if pin is None else str(pin)
        report = prepare(session, root_layer, pin=pin_text, payloads=payloads, assets=True, store=store,
                         extra=extra, cancel=cancel)
        problems: list[str] = []
        if report.missing and not allow_missing:
            problems += [f"{ref}: referenced, but not provided" for ref in report.missing]

        recorded, more = _record(session, report.asset_paths, pin_text)
        problems += more
        if problems:
            raise ManifestError(
                f"The manifest cannot be made: {len(problems)} problem(s)",
                hint="Submit (or revert) these files and try again; a farm machine cannot fetch what is not in Perforce",
                problems=problems,
            )
        return cls(
            depot=session.paths.depot_root,
            files=recorded,
            root_layer=report.asset_paths[0],  # the root is found first
            source_pin=pin_text,
            server=_server_name(session),
            created=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            created_by=f"{session.user}@{session.client}",
            usd_version=_usd_version(),
        )


def _server_name(session: Session) -> str:
    return session.config.port or session.info().get("serverAddress", "")


def _usd_version() -> str:
    try:
        from pxr import Usd

        major, minor, patch = Usd.GetVersion()
        return f"{minor}.{patch:02d}" if major == 0 else f"{major}.{minor}.{patch}"
    except ImportError:
        return ""


def _spec_suffix(pin_text: str) -> str:
    """The revision specifier that selects what ``pin_text`` recorded."""
    if pin_text == "have":
        return "#have"
    if pin_text == "latest":
        return ""
    return pin_text if pin_text.startswith("@") else ""


def _record(session: Session, asset_paths: Iterable[str], pin_text: str) -> tuple[dict[str, ManifestFile], list[str]]:
    """Revision, change, digest, size and type of each file, from one
    ``fstat`` per 500 files; and what a farm could not reproduce."""
    paths = list(dict.fromkeys(asset_paths))
    depot = session.paths
    suffix = _spec_suffix(pin_text)
    records: dict[str, dict[str, str]] = {}
    for start in range(0, len(paths), _FSTAT_CHUNK):
        specs = [depot.depot_path(p) + suffix for p in paths[start:start + _FSTAT_CHUNK]]
        result = session.backend.run("fstat", ["-Ol", *specs])
        for rec in result.records:
            if "depotFile" in rec:
                records[depot.asset_path_of_depot(rec["depotFile"])] = rec

    recorded: dict[str, ManifestFile] = {}
    problems: list[str] = []
    to_hash: list[tuple[str, Path, ManifestFile]] = []
    for path in paths:
        rec = records.get(path)
        if rec is None or rec.get("headAction") in _DELETED_AT_HEAD:
            problems.append(f"{path}: not in Perforce")
            continue
        if rec.get("action"):
            problems.append(f"{path}: open for edit ({rec['action']}); submit it first")
            continue
        entry = ManifestFile(int(rec["headRev"]), int(rec.get("headChange", 0)), rec.get("digest", "").upper(),
                             int(rec.get("fileSize", 0)), rec.get("headType", ""))
        recorded[path] = entry
        local = depot.local_path(path)
        # A file in the workspace can be edited without checking it out; one
        # in the version store was fetched and checked by the core.
        if pin_text in ("have", "latest") and entry.digest and local.is_file():
            to_hash.append((path, local, entry))

    with ThreadPoolExecutor(max_workers=8, thread_name_prefix="clio-digest") as pool:
        digests = list(pool.map(lambda item: _core.md5_file(os.fspath(item[1]), item[2].is_text), to_hash))
    for (path, _local, entry), actual in zip(to_hash, digests):
        if actual != entry.digest:
            problems.append(f"{path}: changed on disk without being opened for edit; revision {entry.rev} "
                            "is what a farm machine would get")
    return recorded, problems
