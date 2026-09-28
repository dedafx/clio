# Clio — Design Document

| | |
|---|---|
| **Status** | Draft v0.2. The C++ core, Python bindings and USD resolver are scaffolded (see [building.md](building.md)). |
| **Package** | `deda.clio` (Python namespace package) |
| **Targets** | Python 3.13 · USD 26.08 (primary) and USD 25.08 or later · no DCCs yet |
| **Consumers** | Standalone Python, the `clio` CLI, and USD. Dedaverse, Imagine and DCC integrations are later work. |
| **Backing store** | Perforce server (`p4d`), streams depots |

> **Decisions that shape v0.2** (they replace anything older in this document):
> * **Targets:** Python 3.13 and standalone USD 26.08, with USD 25.08 or
>   later also supported (the Ar resolver API is identical between them).
>   No DCC is targeted yet, so DCC-specific builds, loading tests and
>   fallbacks are deferred.
> * **The core is C++ (`clio_core`)**, built on the Perforce C++ API. It is
>   used by the USD resolver plugin and, through nanobind, by Python
>   (`deda.clio._core`). P4Python, the `p4 -G` fallback and the Rust
>   extension are dropped.
> * **No Dedaverse or Imagine integration yet** (§14).

---

## 1. Summary

Clio is a Python library and command-line tool that sits between artists (and
the tools built for them) and a Perforce server. Perforce is very good at
storing large binary files. Its client tools (P4V and `p4`) are hard for
artists to use. Clio keeps Perforce as the storage and locking engine and
replaces the everyday workflow with a small, task-shaped API that uses
artist vocabulary:

* **Get** assets (latest, or a specific version) without learning depot syntax.
* **Lock → edit → save** binaries with exclusive locking handled for you.
* **Branch** a piece of asset work, switch between branches, and **publish** back.
* **Share drafts** of work in progress without submitting.
* See **who has what locked**, and **history** for any asset.

Python is the main interface. Performance-critical work runs in C++:
`clio_core` talks to Perforce through the P4 C++ API and is shared by the
Python extension (`deda.clio._core`) and the USD resolver plugin. The
server's parallel transfer does the heavy byte moving.

## 2. Goals and non-goals

### Goals

1. **Artist-first workflow.** Every common task is one command or one API
   call. Error messages say what happened and what to do next, not raw
   Perforce errors.
2. **Clean, typed Python API** (`deda.clio`) that Dedaverse, Imagine, and DCC
   plugins (Maya, Houdini, Blender, Unreal, Substance, ...) can embed.
3. **A CLI** (`clio`) built only on the public API, so the CLI is never more
   capable than the library.
4. **Easy branching** for asset development, built on Perforce streams.
5. **Performance** that holds up with multi-GB files and workspaces of
   100k+ files (see §8).
6. **UI-ready data access.** Version history and status can be shown in
   list UIs using cached data and a small, fixed number of server calls
   (see §9).
7. **USD-aware retrieval.** A USD stage can be opened with every layer and
   asset it needs present at the correct version (see §10).
8. **Safe by default.** Clio never deletes or reverts local work unless the
   user asks for it explicitly.

### Non-goals (for v1)

* Replacing Perforce administration (users, groups, protections, triggers).
  Clio may *recommend* server settings (§8.5) but will not manage them.
* Replacing P4V for administrators or build engineers.
* Reimplementing the Perforce wire protocol.
* A GUI. The API is designed so Dedaverse or Imagine can build one (progress
  callbacks, cancellation, background execution), but the GUI is out of scope
  here.

## 3. Vocabulary

Clio uses artist-facing terms and maps each one to exactly one Perforce
concept. Once a user knows Perforce, the mapping lets them work out what Clio
did.

| Clio term | Perforce concept | Notes |
|---|---|---|
| **Project** | A streams depot (e.g. `//imagine`) | Picked by config, not typed by hand. |
| **Branch** | A stream (mainline / development / task) | See §7. |
| **Workspace** | A client spec bound to a stream | Clio creates it and names it. |
| **Asset** | A depot path prefix (a folder or file set) | Resolved by an `AssetResolver` (§5.4). |
| **Get / Get latest** | `p4 sync` | Parallel by default. |
| **Lock** / **Start editing** | `p4 edit` on a `+l` file (exclusive open) | Locking is what artists care about. |
| **Save** | `p4 submit` | Adds, edits, and deletes are detected and opened automatically. |
| **Change** | A numbered pending changelist | The CLI hides the default changelist. |
| **Draft** | A shelved changelist | "Share this without saving it." |
| **Discard** | `p4 revert` | Always confirmed; can back up first (§11). |
| **Version** | A file revision / changelist number / label | `crate.ma@v12`, `@label`, `@1234`. |
| **Publish** | `p4 copy` up to the parent stream | |
| **Update branch** | `p4 merge` down from the parent stream | |

## 4. Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│  Consumers: clio CLI · Dedaverse · Imagine · DCC plugins · scripts   │
└───────────────┬──────────────────────────────────────────────────────┘
                │ public API (deda.clio)
┌───────────────▼──────────────────────────────────────────────────────┐
│  Services:  Session · Workspace · Changes · Locks · Branches ·       │
│             Assets · History · Drafts                                │
│  Cross-cutting: Config · Errors · Events/Progress · Cancellation     │
├──────────────────────────────────────────────────────────────────────┤
│  Caches:       in memory only; server is authority         §8.3, §9  │
│  Files:        version store (immutable versions for pins)     §10.4 │
│  Views:        FileListView · HistoryView (UI-ready, no Qt)    §9    │
├──────────────────────────────────────────────────────────────────────┤
│  Backend protocol (typed, tagged records in / out)                   │
│   ├─ CoreBackend  (default; deda.clio._core → clio_core, C++, P4API) │
│   └─ FakeBackend  (in-memory, for unit tests)                        │
├──────────────────────────────────────────────────────────────────────┤
│  clio_core (C++): connections · pins · clio: ids · resolve · caches  │
│  also linked into the clioUsd resolver plugin (§10.3)                │
└───────────────┬──────────────────────────────────────────────────────┘
                │ P4 protocol (TCP/SSL)
        ┌───────▼────────┐        ┌─────────────────┐
        │  p4d / edge    │◄──────►│  P4 Proxy (opt) │ (remote artists)
        └────────────────┘        └─────────────────┘
```

### 4.1 Layers

1. **Backend.** The only layer that knows how Perforce is reached. It
   exposes a small `Protocol` (`run(cmd, args, *, handler=None,
   progress=None) -> Iterator[Record]` plus connection lifecycle). Every
   other layer works with typed records, never with raw `p4` text.
2. **Services.** Workflow logic: "save these files" becomes
   reconcile (scoped) → open for add/edit/delete → lock check → submit →
   refresh the cache. This layer holds the business rules and is fully
   unit-testable against `FakeBackend`.
3. **Public API.** A thin, stable facade that re-exports services and models
   from `deda.clio`. Everything not re-exported is private and can change.
4. **CLI.** A consumer of the public API, with human-readable and `--json`
   output.

### 4.2 Why a backend abstraction

* **One implementation of the rules.** `CoreBackend` calls `clio_core`, the
  same C++ code the USD resolver uses, so Python and USD agree on how every
  path resolves and how Perforce is called.
* **Testing.** Service logic is tested against `FakeBackend` in milliseconds.
  Integration tests run against a real, throwaway `p4d` (§13).

### 4.3 Package layout

`deda` is a **PEP 420 implicit namespace package**. There is no
`deda/__init__.py`, so `deda.clio`, the Dedaverse packages, and others can
be installed independently and still share the `deda.` prefix. *(Open
question Q1: confirm this matches how Dedaverse declares `deda`.)*

```
clio/
├── pyproject.toml              # build backend: scikit-build-core (CMake) + nanobind
├── src/deda/clio/
│   ├── __init__.py             # public API; lazy re-exports
│   ├── _version.py
│   ├── errors.py               # exception hierarchy (§11)
│   ├── models.py               # frozen, slotted dataclasses
│   ├── config.py               # layered config (§6)
│   ├── events.py               # progress / event types, cancellation token
│   ├── session.py
│   ├── workspace.py
│   ├── changes.py
│   ├── locks.py
│   ├── branches.py
│   ├── assets.py
│   ├── history.py
│   ├── drafts.py
│   ├── cache/
│   │   ├── status.py           # per-workspace status cache (§8.3)
│   │   ├── history.py          # server metadata / history cache (§9)
│   │   └── content.py          # digest-addressed content cache (§9)
│   ├── views/                  # UI-ready view models, no Qt dependency (§9)
│   ├── usd/                    # USD prefetch, pins, localize (§10); imports pxr lazily
│   ├── backends/
│   │   ├── base.py             # Backend Protocol, Record types
│   │   ├── core.py             # CoreBackend over deda.clio._core
│   │   └── fake.py
│   ├── cli/
│   │   ├── __init__.py
│   │   └── __main__.py         # `python -m deda.clio.cli`, entry point `clio`
│   └── _core.*.so / .pyd       # nanobind extension over clio_core (abi3, Python ≥ 3.13)
├── CMakeLists.txt · CMakePresets.json · cmake/FindP4API.cmake
├── cpp/
│   ├── clio_core/              # C++ core: P4API connections, pins, clio: ids, resolve (no USD, no Python)
│   ├── python/                 # nanobind module deda.clio._core
│   ├── clio_usd/               # clioUsd: the clio: ArResolver plugin, built per USD version
│   └── tests/                  # C++ tests (doctest) incl. a throwaway p4d
├── tests/python/               # pytest, incl. USD stage tests
└── docs/
```

## 5. Python API

The API is synchronous at its core. Background execution and cancellation
are built in so UIs never block. Every long-running call accepts
`progress=` and `cancel=`.

### 5.1 Connecting

```python
from deda import clio

# Reads config (§6), reuses an existing P4 ticket, and does not prompt
# unless interactive=True.
with clio.connect(project="imagine") as session:
    print(session.user, session.branch.name, session.workspace.root)
```

`clio.connect()` returns a `Session`, which owns a small **connection pool**
(§8.1). A `Session` is thread-safe. Its individual connections are not
shared across threads.

### 5.2 Everyday workflow

```python
with clio.connect(project="imagine") as s:
    ws = s.workspace

    # Get latest for one asset, in parallel, with progress.
    report = ws.get("props/crate", progress=print)
    # Preview first: size and file count without transferring anything.
    plan = ws.get("props/crate", preview=True)
    print(plan.bytes_to_transfer, plan.file_count)

    # Lock before editing. Raises LockedByOtherError(user=..., workspace=...)
    ws.lock("props/crate/crate.ma", "props/crate/tex/crate_albedo.exr")

    # ... artist edits in the DCC ...

    # Save: detects adds/edits/deletes under the given paths (scoped
    # reconcile), opens them, submits, and refreshes the cache.
    change = ws.save("props/crate", message="Crate: damage pass on lid")
    print(change.number, change.files)

    # Status is served from the local cache. Pass refresh=True to verify
    # against disk and server.
    for item in ws.status("props/crate"):
        print(item.path, item.state, item.locked_by)
```

Explicit changelists are available when a tool needs them:

```python
change = ws.new_change("Crate: UV fixes")
change.add_files(paths)          # opens for add/edit/delete as needed
change.share_draft()             # shelve, so a lead can review
change.save()                    # submit
```

**Safety rule:** changelists are *not* submitted automatically when a
`with` block exits, and they are never reverted on an exception. If
something fails, the change stays pending and local files stay untouched.

### 5.3 Branches

```python
b = s.branches.create("crate-damage", parent="main", kind="task")
ws.switch(b)                        # re-points the workspace, syncs deltas only
...
ws.save("props/crate", message="WIP damage")
s.branches.update(b)                # merge-down from parent
s.branches.publish(b, message="Crate damage pass")  # copy-up to parent
s.branches.retire(b)                # delete/unload task stream when done
```

### 5.4 Retrieving assets (the resolver)

"Clio retrieves assets" means consumers ask for assets by **logical name**,
not by depot path.

```python
asset = s.assets.resolve("props/crate")              # -> Asset
asset = s.assets.resolve("props/crate", version="@approved")  # label
asset.files                                           # tuple[FileInfo, ...]

# Materialize one version somewhere else, e.g. a render farm cache,
# without touching the user's workspace (uses `p4 print -o`, no have-list).
s.assets.export(asset, dest="/cache/crate@approved", progress=cb)

# Stream a single file's bytes (thumbnails, previews)
with s.assets.open("props/crate/thumb.png", version="#head") as f:
    data = f.read()
```

`AssetResolver` is a pluggable protocol. The default maps names directly to
paths under the stream root. Dedaverse or Imagine can register their own
resolver (through an entry point, `deda.clio.resolvers`) that maps project
asset IDs, USD asset paths, or database records to depot paths.

### 5.5 Models

All results are immutable `@dataclass(frozen=True, slots=True)` types with
full type hints: `FileInfo`, `FileStatus`, `Change`, `Branch`, `Lock`,
`Revision`, `SyncPlan`, `SyncReport`. Paths are exposed in three forms
(`depot_path`, `local_path: Path`, `asset_path` relative to the stream
root), so callers never have to translate between them.

### 5.6 Background execution and events

```python
fut = s.submit_bg(ws.get, "environments/forest", progress=on_progress)
token = clio.CancelToken()
ws.get("environments/forest", cancel=token)   # token.cancel() from UI thread
```

* `progress` receives `ProgressEvent(phase, done, total, bytes_done,
  bytes_total, current_path)`. It is fed from P4Python's `P4.Progress`
  hook and throttled (for example 20 Hz) so a UI is never flooded.
* `Session.events` is a simple pub/sub (`file_locked`, `change_submitted`,
  `branch_switched`, ...) that Dedaverse can bridge to Qt signals.
* No asyncio is required. An optional `deda.clio.aio` wrapper can come later
  if a consumer needs it.

## 6. Configuration and authentication

Configuration is layered, and later layers win:

1. Built-in defaults
2. Site config: `$CLIO_SITE_CONFIG` or a `clio.toml` shipped with the studio install
3. User config: `~/.config/clio/config.toml` (platform-appropriate dir)
4. Project config: `.clio.toml` at the workspace root (checked in)
5. Standard Perforce environment: `P4CONFIG`, `P4PORT`, `P4USER`, ... (so
   existing setups work)
6. Explicit keyword arguments / CLI flags

Authentication uses **Perforce tickets only**. Clio never stores passwords.
`clio login` wraps `p4 login` (including SSO where the server is set up for
it) and `p4 trust` for SSL fingerprints, with a clear prompt.

Workspaces are created on demand with a predictable name,
`{user}_{host}_{project}`, and a root taken from config. `clio setup` does
the first-time setup in one step.

## 7. Branching model

### 7.1 Recommendation: streams, with task streams for asset work

* Each project is a **streams depot** with a `main` mainline.
* Long-lived lines (`dev`, `release/*`) are **development/release streams**.
* **Per-asset or per-task work** uses **task streams**. They are designed
  for short-lived work: they are cheap to create, only the files that
  actually change are branched in the metadata, and they can be deleted or
  unloaded when finished. This is the "make a branch for this piece of the
  asset" workflow.
* A workspace switches branches by re-pointing its client spec to another
  stream (`p4 client -s -S //imagine/crate-damage`) followed by a sync. Only
  files that differ are transferred, which matters with large binaries.

### 7.2 Binary-aware merging

Binary assets cannot be merged line by line. Clio treats integration as
**choose a version**:

* `update` (merge-down) auto-resolves when only one side changed
  (`accept-theirs`/`accept-yours` safe cases). If both sides changed the same
  binary, it reports a `ConflictSet`. The CLI and API offer *keep mine /
  take theirs / keep both* (save theirs as `name.theirs.ext` for manual
  comparison).
* `publish` (copy-up) is refused while the branch is behind its parent, which
  is standard stream flow control. The error message explains
  "update first".

### 7.3 Locks across branches

Exclusive locking (`+l`) prevents two people from editing the same file
*in the same stream*. It does **not** stop two people from editing the
"same" asset in two different branches, and that is the main source of lost
art work with branching. Clio mitigates this:

* On `lock`, Clio checks the same asset path in **related streams** (parent
  and siblings, configurable) and warns when someone has it open elsewhere
  (`p4 fstat` `otherOpen`/`otherLock`, or `p4 opened -a` scoped to those
  paths). This is advisory by default, and a project can configure it as
  blocking.
* This check is a batched metadata query (one call for all paths across all
  related streams), so it adds one server round trip, not one per file.

*(Open question Q4: should this be advisory or enforced by default?)*

## 8. Performance

Principle: **Python orchestrates. Native code moves bytes.** Most of the
heavy work (network transfer, compression, server-side checks) already
happens in native code: the Perforce server and the P4 C++ API. Clio's job
is to call them efficiently, never to put a Python loop on a per-byte or
per-file hot path, and to add native code only where profiling shows a gap.

Recommendations in priority order. Items 1–4 give the largest gains for the
least risk.

### 8.1 Talk to Perforce efficiently

1. **Use the P4 C++ API directly, not subprocess `p4`.** `clio_core` links
   P4API and receives tagged records directly, with no text parsing and no
   process spawn. The Python bindings release the GIL for every call that
   can touch the network or disk.
2. **Keep connections open.** Each connect is a TCP (and possibly SSL)
   handshake plus protocol negotiation. A `Session` keeps a pool of
   long-lived connections (for example 1 + N workers). Commands reuse them.
   For the CLI, a single command uses a single connection. *(A later option
   is a lightweight `clio` daemon/agent that holds warm connections and the
   cache for the CLI and all DCCs on a machine. This is deferred until
   measurements justify it.)*
3. **Batch everything.** One `fstat`/`edit`/`add` call for 5,000 files, not
   5,000 calls. Very large file sets go through `-x argfile` (or command
   input) to avoid command-line length limits, and are chunked for memory.
4. **Ask only for what you need.** `p4 fstat -T field,list` limits returned
   fields, and `-m` limits rows. Scope every command to the narrowest path
   (the asset folder), never `//...`.
5. **Stream large results.** Use a `P4.OutputHandler` to process records as
   they arrive, instead of building a list of a million dicts in memory.
6. **One connection per thread.** A P4API `ClientApi` must not be used
   from two threads at the same time. `clio_core` hands out connections
   from a pool (the scaffold serializes on one connection per workspace),
   and parallel *metadata* work runs on separate connections, with no GIL
   involved.
7. **Cheap "anything new?" checks.** Before a costly refresh, ask for the
   latest change affecting a path (`p4 changes -m1 -s submitted
   //path/...`). If it has not moved since the last refresh, skip the
   refresh.

### 8.2 Move bytes in parallel (the biggest win for large binaries)

* **Parallel sync / submit** — `p4 sync --parallel=threads=N,...` and
  `p4 submit --parallel=threads=N,...` transfer files over multiple
  connections inside the C++ API. For a workspace full of multi-GB files
  this is often the largest single speed-up available, and it needs no
  native code from us. It requires the server to allow it
  (`net.parallel.max`, optionally `net.parallel.threads` for automatic
  parallelism). Clio turns it on by default and tunes `threads`/`batch`/
  `min` through config.
* **Preview before transfer** — `p4 sync -n` plus `p4 sizes -s` give "this
  will download 42 GB, 310 files" before anything moves.
* **Quiet mode** — `sync -q` when progress comes from the progress hook
  rather than per-file output, which reduces client-side per-file overhead.
* **Out-of-workspace retrieval** uses `p4 print -o`. It does not update the
  have list, so farm/cache exports do not affect the user's workspace state.

### 8.3 Caching: the server is the authority

**Decision (MVP): caches live in memory only.** There is no local
database. The Perforce server is the only source of truth. Anything Clio
remembers is a short-lived, per-process copy that can always be thrown away
and fetched again. A persistent local store (for example SQLite) is
considered only if measurements show the MVP is too slow (Backlog, §15).

Why: a persistent cache must be kept in sync with the server across
sessions, processes and admin changes (obliterate, label moves, retypes),
and a stale answer is worse than a slow one. With memory-only caches,
restarting a tool always starts from the server, and there is no cache file
to repair.

**Staleness rules** (they apply to every cache in Clio, including the
resolver's):

1. **Actions always ask the server.** Lock, save, get, publish and branch
   operations never decide from a cache. Perforce itself enforces locks,
   have-lists and conflicts at the moment of the action.
2. **Only immutable data is kept for the whole process.** A submitted
   revision (`path#rev`), a change number snapshot (`@change`) and file
   content at a revision never change in normal use, so they can be reused
   until the process exits.
3. **Mutable data is bounded by time.** Head revisions, `latest`,
   lock/open state and **labels** (an admin can move them) are re-checked
   after the polling interval (30–60 s, §9.4) or on explicit refresh. They
   are never trusted longer than that.
4. **Refresh is always available.** `refresh()` in Python and
   `_RefreshContext` in USD drop cached answers, so the next request goes
   to the server.
5. **Nothing survives the process.** Except for the version store below,
   every cache is lost at exit.

**The version store is not a cache of server state.** Files fetched for
historical pins (§10.4) are written to disk, because USD must read them as
files. Only content that cannot change is reused across processes: a
change-number snapshot or a file revision. Label pins are re-checked against
the server once per process, because labels can move.

**"What did I change?" without a local database.** Clio asks the server for
the have-list and digests of the scope (`p4 fstat -Ol` on the asset folder)
and runs a scoped `p4 reconcile -m` (modtime checked before digests). This
work is always limited to the asset or folder in question, never the whole
workspace. If that proves too slow on large assets, the first step is an
in-memory index for the session; a persistent store comes after that.

### 8.4 Native code: one C++ core

**Decision:** all native code is C++ in `clio_core`, shared by the Python
extension and the USD resolver. There is no Rust.

* **Python bindings:** nanobind, built as one stable-ABI (`abi3`) module for
  CPython 3.13 and later, and packaged with scikit-build-core.
* **What goes into C++:** Perforce access (P4API), identifier and pin rules,
  resolution, caches, and the hot paths from the table below. Python keeps
  the workflow logic, the CLI and anything that is not performance-bound.
* **Rule:** move more into C++ only when a benchmark shows a real gain (for
  example ≥3× on a realistic workload).

| Candidate | Why Python is slow here | C++ approach |
|---|---|---|
| **Parallel file hashing (MD5)** of multi-GB files, to compare with server digests | Per-file I/O and hashing in one thread, plus orchestration overhead | Large-buffer reads on a thread pool, with the GIL released for the whole batch |
| **Directory scan + stat** of 100k+ files | Building Python objects per entry adds up | Parallel walk that returns only entries changed since a snapshot from the cache |
| **Manifest diffing** (have list vs disk vs server) | Large dict/set operations with many small objects | Sorted merge on compact arrays. Only the diff crosses into Python. |

* **Symbol isolation:** P4API and OpenSSL are linked statically and hidden
  (`-fvisibility=hidden`, `--exclude-libs,ALL` on Linux). The Python module
  and the USD plugin each carry a private copy, so they can be loaded in the
  same process as each other and as Python's own OpenSSL.

### 8.5 Server and depot recommendations

Clio does not configure the server, but it will provide a `clio doctor`
check and a recommended **typemap** for the admins:

| Pattern | Filetype | Why |
|---|---|---|
| `.ma .mb .hip .blend .spp .psd .ztl .uasset .umap` | `binary+l` | Exclusive lock: unmergeable authoring files. |
| `.exr .png .jpg .tif .mp4 .zip .fbx(binary) .abc .usdc .vdb` | `binary+Fl` | `+F` stores the file uncompressed on the server, which skips pointless server-side gzip of data that is already compressed. That saves server CPU and time on submit and sync. |
| Derived / cache outputs (bakes, caches, previews) | `binary+FS<n>` (e.g. `+S3`) | Keep only the last *n* revisions, to control depot growth. |
| `.usda .json .py .txt .mtlx` | `text` | Mergeable. No lock needed. |

Also recommended: **P4 Proxy** (or edge servers) for remote artists, so
repeated syncs of the same large files come from a local cache;
`net.parallel.max` enabled; and adequate `lbr` storage on fast disks.
*(Q2: What server version and topology do we target?)*

### 8.6 Python-level hygiene

* **Lazy imports.** The `clio` CLI must start fast (target < 150 ms to first
  output for `clio --help`). Import the C++ extension and the CLI framework
  only in the code paths that need them. `deda.clio.__init__` uses
  module-level `__getattr__` for lazy re-exports.
* **Slotted, frozen dataclasses** for records. Avoid per-file Python objects
  on bulk paths. Bulk APIs return compact results (counts, paths, errors), and
  per-file detail is available on request.
* **Free-threaded CPython (3.13t+)** is tracked but not relied on. The design
  already keeps shared state inside the pool and the cache.

### 8.7 Performance targets (to validate in Phase 1)

| Operation | Target |
|---|---|
| `clio --help` / cold CLI start | < 150 ms |
| `status` on an asset (cached, 1k files) | < 50 ms |
| `status` on a 100k-file workspace (warm cache, no watcher) | < 2 s |
| `lock` 500 files (incl. cross-branch check) | 2–3 server round trips |
| History panel for a previously viewed file | < 20 ms, 0 server calls (fresh within the polling interval) |
| Open a folder of 500 files in a view | ≤ 2 server calls |
| `get` of large binaries | Network/disk-bound: within 10% of raw `p4 sync --parallel` |

A `benchmarks/` suite (pytest-benchmark against a local `p4d` with
generated binary files) runs in CI, so regressions are caught.

## 9. Server data in user interfaces: history and caching

Clio has no UI in v1, but Dedaverse, Imagine, and DCC panels will show
Perforce data in lists: file browsers with version columns, per-file
version history, asset activity feeds, and lock indicators. Done naively,
every row and every click becomes a server call. With hundreds of rows and
dozens of artists, that is slow for the user and expensive for the server.
This section sets the rules now, so the API does not have to change when a
UI arrives.

**Core rules**

1. **Server data is classified by how it changes.** The class decides
   whether it is cached, for how long, and how it is refreshed (§9.2).
2. **A UI never calls the server per row.** Views ask for the rows that are
   visible, Clio batches them into one call, and the results are shown
   (§9.5).
3. **Show cached data at once, then revalidate** (stale-while-revalidate).
   A UI always gets an instant answer, marked with how fresh it is.
4. **Cached data is for display only.** Any action (lock, save, get,
   publish) checks the server directly. A decision like "is this file
   locked?" is never taken from the cache.
5. **One cheap call answers "has anything changed?"** before any expensive
   refresh (§9.4).

### 9.1 UI scenarios to support

| Scenario | Data shown | Typical size |
|---|---|---|
| **File list / asset browser** with status columns | name, local version vs latest ("v12 of 14"), last changed by/when, size, locked by, out-of-date flag | 50–5,000 rows, ~30 visible |
| **Version history panel** for one file | every revision: version, change number, user, date, description, action, size, thumbnail | 1–1,000+ revisions, first ~20 visible |
| **Asset history** (all files under an asset folder) | changes that touched the asset, grouped by change | 10s–1,000s of changes |
| **Compare versions** | two revisions' metadata and previews side by side | 2 revisions |
| **Activity feed** ("what changed on my branch today") | recent changes in a stream or folder | last N changes |
| **Branch view** | which files differ between a branch and its parent, and which branch a revision came from | up to the size of the branch |
| **Lock / presence badges** | who has a file locked or open, in which branch | the visible rows |

### 9.2 Data classes and caching policy

| Class | Examples | Changes? | Policy |
|---|---|---|---|
| **Immutable** | Submitted revision metadata (`path#rev` → change, action, type, time, size, digest). File content at a revision. | Never, in normal use | **Cache forever.** Fetched once per machine. |
| **Append-only** | A file's or folder's history (new revisions are only ever added). Integration (branch/merge) records. | Grows | **Cache, and fetch only the new part** using a high-water mark (§9.4). |
| **Mostly immutable** | Submitted change descriptions (the owner can edit them with `p4 change -u`, admins with `-f`). | Rarely | Cache. Revalidate lazily (for example when displayed, if older than a day) and on explicit refresh. |
| **Volatile** | Head revision of a path, who has it opened or locked, pending changes, shelves (drafts), stream specs. | Often | **Short TTL** (default 15–30 s) for visible rows only. Always refetched before an action. |
| **Local** | Have revision, local modifications. | On local actions | Asked of the server (have-list) and the disk, scoped to the asset (§8.3). |
| **Never cached** | Pending changelist numbers (they are renumbered on submit), protections, tickets. | — | Always live. |

**Exceptions to "immutable".** Admins can rewrite history with
`p4 obliterate`, `p4 retype`, purge old content with `+S` or `p4 archive`,
or restore a server from a checkpoint. Clio handles this without trying to
detect every case up front:
* A "no such file" or "purged" reply for a cached key deletes that key and
  its dependants, and the view is refreshed.
* The cache is keyed by server identity (§9.3). A replaced or restored
  server gets a new cache.
* `clio cache verify [PATH]` and a low-frequency background check (for
  example a weekly sample) compare cached rows with the server and drop a
  scope if they disagree. `clio cache clear` is always available.

### 9.3 Cache tiers and where they live

| Tier | Contents | Lifetime | Location |
|---|---|---|---|
| **HistoryCache (MVP)** | Server metadata (revisions, changes, integrations, head info, watermarks) and model objects for visible rows | Process | **In memory**, LRU bounded by entry count, one per server + Perforce user |
| **ContentCache (backlog)** | File bytes fetched for display: thumbnails, previews, older versions opened for comparison | Size-limited LRU (e.g. 5 GB) | Folder keyed by server digest + size |
| **Persistent HistoryCache (backlog)** | The same data, kept across sessions | Persistent | SQLite, only if measurements show it is needed (§8.3) |

Notes:
* **MVP: memory only.** The HistoryCache lives in each process and is lost
  at exit (§8.3). The staleness rules in §8.3 apply: immutable rows are
  reused, and mutable rows are re-checked after the polling interval.
* **Why the HistoryCache is not per workspace.** History belongs to the
  server, not to a workspace, so one cache per server serves every
  workspace in the process.
* **Why per Perforce user.** Protections can hide paths from some users. A
  cache must never show a user rows that were fetched with someone else's
  permissions, so caches are never shared across Perforce users.
* **Server identity** comes from `p4 info` (server ID where set, otherwise
  server address plus server root), so that two servers never share a
  cache. *(To confirm against the target server's configuration, Q2.)*
* **If a persistent store is added later** (backlog): SQLite on local disk
  only, never on a network share, in WAL mode so several processes can use
  it, with a versioned schema shared by Python and the C++ resolver.
* **ContentCache is content-addressed.** A file's revision is identified by
  its server digest (MD5) and size. Identical content, such as a file copied
  to a branch, is stored once and never downloaded twice. Content comes from
  `p4 print -o`, which does not change the workspace.

**HistoryCache data model (sketch).** Shown as tables, but held in memory
for the MVP. The same shape would become the schema of a persistent store.

```sql
changes      (change INTEGER PRIMARY KEY, user, client, stream, time,
              description, desc_fetched_at)
revisions    (depot_path, rev, change, action, filetype, time, size, digest,
              PRIMARY KEY (depot_path, rev))
integrations (to_path, to_rev, from_path, from_start_rev, from_end_rev, how)
heads        (depot_path PRIMARY KEY, head_rev, head_change, fetched_at)
presence     (depot_path, user, client, action, is_locked, fetched_at)  -- volatile
scopes       (scope_path PRIMARY KEY, watermark_change, checked_at,
              complete INTEGER)  -- see §9.4
```

Descriptions are stored once per change, not per file, because one change
often contains hundreds of files. Size estimate: about 150–250 bytes per
revision row, so a million revisions is roughly 200 MB. That is acceptable
on a workstation, and the cache can be limited to scopes the user has
actually viewed.

### 9.4 Keeping the cache fresh with few server calls

**Scopes and watermarks.** A *scope* is a depot path the UI is watching (an
asset folder, a stream). For each scope the cache stores a **watermark**:
the highest submitted change number already known for that scope.

**The freshness check (idle cost: one call per scope).**

```
latest = p4 changes -m1 -s submitted //imagine/main/props/crate/...
if latest == watermark:      nothing changed → cache is valid, done
else:                        fetch only the delta (below)
```

`p4 changes -m1` on a scoped path is one of the cheapest queries the server
answers, and it needs no special permissions.

**The delta fetch (cost: one or two calls, whatever the scope size).**

```
p4 -ztag filelog -l -t //imagine/main/props/crate/...@<watermark+1>,@now
```

This returns only the revisions submitted since the watermark, for every
file in the scope. They are appended to `revisions`, `changes`, and `heads`,
and the watermark moves forward. Views showing affected rows get a
`rows_changed` event. *(To verify in Phase 1: `filelog` with a change range
vs `p4 files` on the range plus a batched `p4 describe -s`, whichever costs
the server less on large scopes.)*

**The first fetch of a scope** is the only expensive one. It is paginated
and done lazily:
* The list view needs only head info for its rows: one `p4 fstat` (with
  `-T` limiting fields) for the whole folder.
* Full history is fetched per file when needed (§9.5), not for the whole
  scope, until the scope is marked `complete`.
* Very large scopes are split into sub-folders, so a server limit
  (`MaxScanRows`, `MaxResults`) is never hit. A "too many rows" error makes
  Clio split the request and retry, and is not shown to the user.

**When checks run.**

| Trigger | What happens |
|---|---|
| A view becomes visible, or the app regains focus | Freshness check for its scope |
| While visible | Freshness check every 30–60 s (configurable). Paused when hidden or minimized. |
| After any Clio action (save, get, lock, publish) | Affected scopes are updated from the action's own results. No extra call. |
| User clicks "Refresh" | Freshness check plus volatile data for visible rows |
| Before any action | Live check of the files involved. The cache is not trusted. |

**Decision:** 30–60 s polling is sufficient for the MVP.

**Push notifications (backlog).** Perforce does not push events to
clients. A studio that wants instant updates can add a server
`change-commit` trigger that publishes "change N submitted to //path" to a
small notification service (for example WebSocket or a message queue).
Clio would then use it to replace polling. The cache design does not
change: a notification is simply "run the freshness check for this scope
now". It is not needed for v1.

### 9.5 How a UI asks for data

UIs use **view models** from `deda.clio.views`. They have no Qt
dependency but map directly onto `QAbstractItemModel` (or any other
toolkit). They own batching, prioritization, and cancellation, so each UI
does not have to reinvent them.

```python
from deda import clio
from deda.clio.views import FileListView, HistoryView

s = clio.connect(project="imagine")

# File browser: rows are available at once from the cache.
files = FileListView(s, "props/crate", columns=("version", "head", "locked_by",
                                                 "changed_by", "changed_at"))
files.rows_changed.connect(on_rows_changed)   # (first, last) indices
files.set_visible_range(0, 40)                # only these rows are fetched
row = files[3]      # FileRow(path=..., have_rev=12, head_rev=14,
                    #         locked_by=None, freshness=Freshness.STALE)

# History panel for the selected file: first page from the cache,
# then any newer revisions from the delta fetch.
hist = HistoryView(s, "props/crate/crate.ma", page_size=20)
hist.rows_changed.connect(on_history_changed)
hist.fetch_more()   # next (older) page, when the user scrolls
thumb = hist[0].thumbnail(max_size=256)   # Future[Path], from ContentCache

# Without views: the same data through the service API.
page = s.history.revisions("props/crate/crate.ma", limit=20,
                           max_age=timedelta(seconds=30))
page.items, page.freshness, page.next_cursor
```

Each result carries **freshness** (`FRESH`, `STALE` with its age, or
`LOADING`), so a UI can show a subtle "updating" state instead of blocking.

**Request pipeline** (inside Clio, shared by all views in a process):

1. **Coalesce.** Requests arriving within a short window (for example 30 ms)
   are merged. Scrolling past 40 rows becomes one `fstat` or one
   multi-file `filelog`, not 40 calls.
2. **Deduplicate.** Two views asking for the same key share one in-flight
   request and its result.
3. **Prioritize.** Visible rows first, then the selected file's history,
   then prefetch (the next page, the rows just below the visible range).
4. **Cancel.** Rows scrolled out of view before their request starts are
   dropped. A request that has started finishes and is cached.
5. **Run on the connection pool** (§8.1), never on the UI thread. Results
   come back as events, which the consumer marshals to its UI thread (in Qt,
   a queued signal).

### 9.6 Cached vs live: decision table

| UI need | Source | Server calls |
|---|---|---|
| Open a folder of 500 files | Cache for all rows at once. One `fstat` (limited fields) for visible rows if stale. | 0–1 (+1 freshness check) |
| Scroll the list | Coalesced `fstat` for newly visible stale rows | ≤ 1 per scroll pause |
| Select a file, show history | Cache. If the file has never been viewed: one `filelog -m 20`. | 0–1 |
| Scroll history to older versions | Cache, else `filelog` for the next page | 0–1 per page |
| Thumbnail for a revision | ContentCache, else one `p4 print` (batched for visible revisions) | 0–1 |
| Compare two versions | Metadata from cache. Content from ContentCache or `p4 print` to a temp folder. | 0–2 |
| Lock badge on a row | Cached presence with a 15–30 s TTL, refreshed in the same batched `fstat` | shared with the row refresh |
| Artist clicks **Lock** | **Live** `fstat` plus `edit` on the server, then update the cache | always live |
| Activity feed for a branch | Cache, plus the freshness check and delta fetch | 1 when idle |
| Idle UI | Freshness check per visible scope every 30–60 s | 1 per scope per interval |

**Budget rule (to check in tests):** a user interaction costs at most one or
two server calls, whatever the number of rows. Idle UIs cost at most one
call per visible scope per polling interval. The integration tests count
backend calls per scenario and fail if a change breaks the budget.

### 9.7 Server-side considerations for many UI users

* With many artists running UIs, metadata queries add up. For larger
  studios, recommend a **read-only replica or edge server** so that history
  and status queries don't load the commit server. P4 Proxy caches file
  *content* (useful for thumbnails and syncs), not metadata.
* Keep polling intervals configurable at the site level, so an admin can
  reduce load centrally.
* History that comes from before a branch was made (`filelog -i` follows
  it back into the parent stream) is stored under the parent's paths, so
  it is cached once and shared by every branch made from that parent.

### 9.8 Thumbnails and previews (backlog, not in MVP)

> **Decision:** Thumbnails are out of scope for the MVP and stay in the
> backlog (§15). The design below is kept so that the ContentCache and
> view models don't block adding them later.

Thumbnails are the most frequent binary request in a UI. The recommended
convention is a small sidecar image per asset version (for example
`.clio/thumb.png` next to the asset, submitted with it). Clio then shows it
through `p4 print` and the ContentCache, and each thumbnail is downloaded
at most once per machine. Alternatives are Perforce attributes (`p4
attribute`) or an external thumbnail service. *(Open when this comes out
of the backlog: which convention fits Dedaverse/Imagine? This affects the
save workflow, which would generate the thumbnail.)*

## 10. USD integration: make layers present before USD loads them

**Goal.** When a USD stage is opened, every layer it needs (sublayers,
references, payloads) and, optionally, every non-layer asset (textures,
clips, volumes) is present on disk at the correct version, *before* USD
reads it. That applies whether the file is missing, out of date, or needs a
specific version. This is done through `deda.clio.usd` and, for automatic
behaviour inside DCCs, through a USD asset resolver plugin.

Everything below was checked against the OpenUSD source (`dev` branch,
commit `2a9a571`, September 2026). File references are relative to
`pxr/usd/`.

### 10.1 What the USD source tells us

| Finding | Source | Consequence for Clio |
|---|---|---|
| All asset path resolution goes through `ArResolver` (`_CreateIdentifier`, `_Resolve`, `_OpenAsset`, `_GetModificationTimestamp`, `_GetAssetInfo`, `_OpenAssetForWrite`, ...). | `ar/resolver.h` | A resolver is the one place where Clio can step in for *every* asset USD touches. |
| A resolver can be the **primary** resolver or a **URI resolver** for listed schemes (`"uriSchemes"` in `plugInfo.json`). A URI resolver can never be primary (`canBePrimaryResolver = uriSchemes.empty()`). | `ar/resolver.cpp` ~L244–273 | Option B would have owned `clio:` paths only. The chosen design (option C, §10.2) is a primary resolver built on `ArDefaultResolver`, so plain paths work with and without Clio. |
| When either the asset path or its anchor has a URI scheme, `CreateIdentifier` is called on that scheme's resolver. | `ar/resolver.h` (docs for `_CreateIdentifier`) | Relevant only to option B (`clio:` URIs), which was not kept. |
| Relative paths are anchored to the anchor layer's **resolved path** (`anchor->GetResolvedPath()`), not its identifier. | `sdf/layerUtils.cpp` L193 | Clio returns local file paths and recognises them again, including version-store paths, so relative children keep the pin (§10.4). |
| Resolver contexts (`ArResolverContext`) are bound per stage and per thread. `UsdStage::Open` takes a context, and Pcp rebinds it in worker threads. | `ar/resolver.h` (`_BindContext`), `usd/stage.cpp`, `pcp/layerStack.cpp` ~L1807 | A **Clio context** can carry the version pin (branch, change number, label) for a whole stage. |
| Sublayers are opened **in parallel** (`WorkDispatcher`, controlled by `PCP_ENABLE_PARALLEL_LAYER_PREFETCH`), and composition runs on worker threads. | `pcp/layerStack.cpp` ~L1806–1830, `pcp/cache.cpp` | The resolver is called from many threads at once. Misses must be coalesced and made thread-safe, not synced one at a time. |
| USD drops the GIL when opening layers because "if the layer load happening in another thread needs the GIL, we'd deadlock". | `sdf/layer.cpp` L339–341 | A resolver that calls Python needs the GIL on every call from worker threads. That is possible but slow and fragile. |
| `ArResolver` is exposed to Python only for *calling* it (`Ar.GetResolver().Resolve(...)`). There is no binding to *subclass* it in Python. | `ar/wrapResolver.cpp` | A Clio resolver plugin must be compiled C++, built against each DCC's USD. (Community projects bridge resolvers to Python, but they pay the GIL cost above.) |
| `_GetModificationTimestamp` is what `SdfLayer::Reload` compares to decide whether to reload a layer. | `ar/resolver.h` | After Clio syncs a newer version, `stage.Reload()` picks it up if Clio reports a new timestamp (for example the change number). |
| `_RefreshContext` plus `ArNotice::ResolverChanged` tell stages that resolution results have changed. | `ar/resolver.h`, `ar/notice.h` | "Update to latest" in a DCC means Clio syncs, then sends `ResolverChanged` for affected contexts, and the stages recompose. |
| `_OpenAssetForWrite` / `_CanWriteAssetToPath(whyNot)` are called when layers are saved. | `ar/resolver.h` | Clio can open a layer for edit (and lock it) when USD saves it, or refuse with "locked by Sam in branch crate-damage". |
| `SdfLayer::GetCompositionAssetDependencies()` returns a layer's direct sublayer/reference/payload paths. `UsdUtilsExtractExternalReferences` also returns them, and `UsdUtilsComputeAllDependencies` walks the full tree (including clips, UDIMs, and expression variables) with an optional `processingFunc` callback. All are available in Python. | `sdf/layer.h` ~L415, `usdUtils/dependencies.h`, `usdUtils/wrapDependencies.cpp` | A pure-Python pre-sync is possible without a compiled plugin (§10.2, option A). |
| `SdfLayer::GetExpressionVariables()` and asset path expressions (`` `...${VAR}...` ``) can change which file a path points to. | `sdf/layer.h` ~L1085 | Dependency walkers must evaluate expressions with the stage's variables, or use `ComputeAllDependencies`, which does. |

### 10.2 Options

**A. Pre-open sync in Python (`deda.clio.usd.prepare`).** Before
`Usd.Stage.Open`, Clio walks the dependency tree and syncs what is missing
or out of date, in batches.

```python
from deda.clio import usd as clio_usd

report = clio_usd.prepare(
    session, "shots/sq010/sh0100/shot.usda",
    pin=clio.Pin.change(18234),      # or Pin.label("approved"), Pin.latest(), Pin.have()
    payloads="all",                  # "all" | "none" | callable(prim_path, asset) -> bool
    assets=True,                     # also textures, clips, volumes
    progress=cb)
stage = Usd.Stage.Open(report.root_path)   # every layer is already on disk
```

How it works (the "wave" walker):
1. Sync the root layer.
2. Open the synced layers with `Sdf.Layer.FindOrOpen` and collect their
   `GetCompositionAssetDependencies()`, evaluating expression variables.
3. Batch every path not yet at the required version into **one** parallel
   sync call (§8.2) per wave. Repeat until there are no new paths. A
   typical shot is 3–6 waves deep, so it takes 3–6 server round trips, not
   one per file.
4. Collect non-layer assets (`UsdUtils.ExtractExternalReferences` per
   layer) and sync them in one final batch.
5. Keep the opened layers alive until `Usd.Stage.Open` runs. USD's layer
   registry then reuses them, so each layer is parsed only once.

Pros: pure Python, works in every DCC today, no compiled USD plugin, easy to
debug. Cons: works from layer content, so it is a *superset*. It syncs every
variant's references, not only the selected ones (can be filtered by the
`payloads`/filter callbacks). It only covers stages opened through Clio,
not a file opened from a DCC's own menu. It also cannot react to later
changes, such as a variant switch that brings in a new payload.

**B. Clio URI resolver plugin (`clio:` scheme), in C++.** Layers refer to
assets as `clio:/props/crate/crate.usd`. USD calls Clio for every such path.
Clio makes sure the file is present at the right version, then returns its
local path. *(Chosen first; replaced by option C, see the decision below.)*

Pros: automatic and complete. It covers any stage opened in any way,
variant switches, payload loads, and `Reload()`. It coexists with the DCC's
primary resolver. Cons: C++ plugin built per USD version; the resolver runs
on USD worker threads.

**C. Clio primary resolver in C++** (plain file paths; Clio enhances
USD's default resolver). **Chosen.** The earlier concerns were that it
conflicts with DCC and studio resolvers (only one primary resolver per
process) and that it puts Clio in the path of every file USD touches. The
first does not apply while no DCCs are targeted. The second is handled by
subclassing `ArDefaultResolver`: Clio only acts on paths inside the project
and defers to the default behaviour for everything else.

**D. Custom `SdfFileFormat` plugin** (for example `.clio` files as
indirections). Not recommended: it is the wrong layer for this job and does
not cover non-layer assets.

**Decision (revised): layers contain plain paths, and Clio's compiled C++
resolver is the primary resolver, a subclass of `ArDefaultResolver` that
performs Perforce operations in-process** (option C).

Why the change from `clio:` URIs (option B): Clio must be an enhancement,
not a barrier. Files with `clio:` paths cannot be opened by plain USD, so
they could not be shared with anyone without Clio. With plain paths:

* **Without Clio**, any USD opens the files from disk.
* **With the plugin installed and a Clio context bound**, paths inside the
  project are fetched from Perforce at the context's pin before USD reads
  them.
* **With the plugin but no Clio context**, or when Clio cannot help (a path
  outside the project, a file not in Perforce, the server unavailable),
  resolution falls through to `ArDefaultResolver`.

What this gives up: a pin on a single path inside a layer. Pins are set per
stage, through the context (§10.4).

* **The resolver checks, syncs and fetches files directly** through the
  Perforce C++ API (P4API).
* **Option A (`deda.clio.usd.prepare`) is kept** as the pure-Python path for
  environments without a resolver build (a DCC or USD version not yet
  compiled for), for explicit farm prefetch, and as a test oracle.
* Both use the same **resolution rules** (pins, anchoring, where versions
  are stored, §10.4). One specification, plus one shared test suite, keeps
  them giving the same file for the same path.

### 10.3 Resolver design (in-process Perforce)

The C++ code is split in two, so that the part that must be rebuilt for
every USD build stays small:

```
USD worker threads
      │
      ▼
┌──────────────────────────────────────────────┐
│ clioUsd (ArDefaultResolver subclass, per USD)│  thin adapter: project paths,
│   ClioResolver · ClioResolverContext         │  contexts, fallback, notices
└──────────────────┬───────────────────────────┘
                   │ plain C++ API (no USD types)
┌──────────────────▼───────────────────────────┐
│ clio_core (static library, no USD, no Python)│
│   Config (reads the same clio.toml)          │
│   P4 connection pool (P4API ClientApi)       │
│   Pin + anchoring rules (§10.4)              │
│   In-memory resolve cache (§8.3 rules)       │
│   Miss coalescer: single-flight + batching   │
│   Sync / print / edit / add / lock ops       │
│   Workspace lock (shared with Python Clio)   │
└──────────────────┬───────────────────────────┘
                   │ P4 protocol (TCP/SSL)
                   ▼
                  p4d
```

* **`clio_core`** holds all Perforce and resolution logic. It is built
  once per platform and compiler, not per USD version, and linked
  statically into each `clio_usd` build. It has its own C++ unit tests and
  runs against the same throwaway `p4d` as the Python tests (§13).
* **`clioUsd`** is the only code that includes USD headers: `ClioResolver`
  (a subclass of `ArDefaultResolver`, registered as the primary resolver;
  no URI scheme), the context class, and `plugInfo.json` declaring
  `ClioResolver` with base `ArDefaultResolver`. It is kept to a few hundred
  lines, so building it for a new DCC or USD version is cheap.

**How a resolve works**

1. **Fast path (no server).** `_Resolve` works out the depot path and pin
   (context + anchor + query) and looks it up in the resolver's in-memory
   cache. Following §8.3, immutable pins (`@change`, `#rev`) are cached for
   the process. `latest` and labels are cached until the polling interval
   passes or the context is refreshed. A hit returns the local path in
   microseconds, so composition speed is unchanged when everything is up
   to date. *(Scaffold: results are kept until `refresh`; the time limit
   comes with phase 3.)*
2. **Miss (file missing or stale).** The request goes to the **coalescer**:
   * *Single-flight:* concurrent requests for the same file share one fetch.
   * *Batching window* (for example 10–30 ms, configurable): misses from
     USD's parallel sublayer loading (§10.1) are collected into **one**
     `sync` (latest/have pins, into the workspace) or one `print` batch
     (historical pins, into the version store). The calling USD thread
     waits for its own file only.
   * Transfers for large files use parallel transfer where the pinned P4API
     version supports it. *(To verify: P4API parallel sync needs a
     client-side transfer implementation, which P4Python and `p4` provide.
     Otherwise Clio splits a batch across pooled connections itself.)*
3. **Freshness.** For `latest`, a manifest entry is trusted for the
   context's polling interval (30–60 s, §9.4). The watermark check then
   decides whether anything needs re-checking. `_RefreshContext` forces a
   check and sends `ArNotice::ResolverChanged` if results changed.

**Threading.** USD calls the resolver from many threads (§10.1). P4API
objects must not be shared between threads, so `clio_core` keeps a small
**connection pool** (default 2–4 per process). The coalescer, not the USD
threads, owns the connections. No Python is involved and the GIL is never
taken.

**Credentials and prompts.** The resolver never prompts. A USD worker
thread has no UI, and prompting there would hang the DCC. It uses the
user's existing Perforce ticket (`P4TICKETS`), `P4CONFIG`/`P4ENVIRO`, and
`clio.toml`. If there is no valid ticket, it fails that resolve with an
error ("Clio: not logged in to perforce:1666, run `clio login`") and
treats the context as `offline` until the next refresh, so one missing
login doesn't cause a flood of errors.

**Blocking and timeouts.** A resolve that needs the network blocks a USD
thread, so every server call has a timeout (configurable, default for
example 60 s for metadata, and progress-based for transfers: fail only if
no bytes arrive for N seconds). On failure the resolver returns an empty
resolved path and posts a clear error, and USD reports the missing layer
as usual. Policies (`sync`, `verify`, `offline`) behave as below.

**Shared state with Python Clio.** The resolver and the Python API may
work on the same workspace at the same time (a DCC resolving while the
artist runs `clio get`):
* Both read the same config (`clio.toml`, §6). The C++ side uses a TOML
  parser such as toml++ (header-only).
* Caches are per process and in memory (§8.3), so there is no shared
  cache file to keep consistent. Both sides get their answers from the
  server, which keeps them in agreement.
* Workspace-changing operations (sync, edit, add) take a **per-workspace
  file lock** (`flock`/`LockFileEx` on a file in Clio's user cache folder;
  implemented for sync), shared by Python and C++, so two processes never sync the
  same workspace at once. Read-only work (version-store prints, metadata)
  does not take it.

**Isolating Perforce's OpenSSL from the host's.** This is the main risk of
running Perforce in-process. P4API links OpenSSL, and the host process
(Python's `ssl` module today, DCCs later) loads its own, often different,
OpenSSL version. *Status:* implemented and checked on Linux in the
scaffold. Neither `_core.abi3.so` nor `clioUsd.so` exports any OpenSSL
symbol.
* **Linux:** link P4API and OpenSSL **statically** into `clio_usd`, build
  with `-fvisibility=hidden`, and hide all bundled symbols
  (`-Wl,--exclude-libs,ALL` plus a version script exporting only the USD
  plugin entry points). Without this, symbol clashes can crash the DCC.
* **Windows:** DLL symbols are not process-global, so a statically linked
  OpenSSL is private to the plugin. Link statically anyway, to avoid
  loading the wrong `libssl-*.dll`.
* **macOS:** two-level namespaces isolate symbols. Link statically and hide
  symbols as on Linux.
* When DCCs are targeted (not yet), a **load test per DCC** (open an SSL
  connection from the resolver inside the running DCC, while the DCC's own
  SSL features are active) joins the release checklist for each build.

**ArResolver methods Clio implements:**

| Method | Clio behaviour |
|---|---|
| `_CreateIdentifier` | Inherited from `ArDefaultResolver`: USD's usual anchoring of relative paths and search paths. |
| `_Resolve` | If a Clio context is bound and the path is inside the workspace root or the version store (search paths are tried under the workspace root), fetch it through `clio_core` at the pin and return the local file. Otherwise, or if Clio cannot provide it, `ArDefaultResolver::_Resolve`. *(Scaffold: no coalescer yet; one fetch at a time per context.)* |
| `_ResolveForNewAsset` / `_CanWriteAssetToPath` / `_OpenAssetForWrite` | When a layer is saved: runs `p4 edit` (with lock for `+l` types) or `p4 add` in the artist's pending change, or refuses with `whyNot` ("locked by Sam"). Never submits (§10.6). |
| `_IsContextDependentPath` | `true` for project paths while a Clio context is bound, because they resolve to a different file for each pin. Sdf then looks layers up by resolved path, so stages opened at different pins in one process never share a layer. Otherwise inherited. |
| `_CreateDefaultContext[ForAsset]`, `_CreateContextFromString` | The default resolver's context, plus a `ClioResolverContext` from `$CLIO_RESOLVER_CONTEXT` if set. `CreateContextFromString(settings)` builds a Clio context when the string contains `=`, otherwise the default resolver's search-path context. |
| `_GetAssetInfo` | Fills `version` (revision/change number) and `resolverInfo` (depot path, digest), so DCC UIs can show "crate.usd v12 @18234". |
| `_GetModificationTimestamp` | Returns a timestamp derived from the revision or change number, so `Reload()` picks up newly synced versions. |
| `_RefreshContext` | Re-evaluates moving pins ("latest", labels), syncs, and sends `ArNotice::ResolverChanged` for affected contexts. |
| `_OpenAsset`, `_GetModificationTimestamp`, `_OpenAssetForWrite` | Inherited: resolved paths are ordinary local files. A later option is to stream pinned versions without writing files. |

* **Policies in the context:** `sync` (default: fetch what is needed),
  `verify` (fail if not present, no server calls, for farm reproducibility),
  and `offline` (use whatever is on disk and warn).
* **Enhancement, not a barrier.** Under `sync` and `offline`, if Perforce
  cannot be used (unreachable, not logged in, request refused), the resolver
  uses the local file and warns once per path. Historical pins use the
  version store first, then the workspace file, with a warning that the
  version may differ. After a failure the server is not contacted again for
  60 s (or until `_RefreshContext`), and an unreachable server is abandoned
  after `connect_timeout` (default 10 s). P4API has no connect timeout, so
  Clio checks TCP reachability itself. Only `verify` fails instead.
  Fallback answers are not remembered.
* **Python access.** `deda.clio.usd` builds contexts from strings, so it
  needs no compiled Python module. A wrapped `ClioResolverContext` class for
  Python (through USD's `ArWrapResolverContextForPython`) is optional and
  would need its own per-USD build.
* **Build and distribution:**
  * CMake project under `cpp/`.
  * `clio_core` links P4API (`libclient`, `librpc`, `libsupp`, plus OpenSSL).
  * `clioUsd` links `clio_core` and USD (`ar`, `tf`, `vt`). It is built
    against standalone OpenUSD: **26.08** (primary) and **25.08 or later**.
    The `ArResolver` API is identical between 25.08 and 26.08 (verified in
    the source), so one source tree serves both. Only the binary differs,
    because the USD C++ ABI changes between releases, so each USD version
    needs its own build of the plugin. CMake refuses USD older than 25.08.
    DCC USD builds come later. *Verified:* the plugin builds against both
    26.08 and 25.08, and the USD tests pass on both. A plugin built for
    26.08 fails in 25.08, which confirms one build per version.
  * Registration: `PXR_PLUGINPATH_NAME` pointing at the plugin's
    `plugInfo.json`, which declares `ClioResolver` with base
    `ArDefaultResolver`. USD picks it as the primary resolver
    automatically. `clio doctor` will check that the `clioUsd` plugin is
    loaded.
  * *(To confirm: P4API licence terms for redistributing it statically
    linked inside our plugin.)*
* **The per-machine agent** (§8.1) is no longer needed for the resolver. It
  stays an optional later improvement: one set of connections and caches
  shared across every DCC on a machine.

### 10.4 Versions, pins, and where files are placed

**Pins** say which version of a path to use:

| Pin | Meaning | Typical use |
|---|---|---|
| `have` (default) | The file on disk, whatever its revision. A file not on disk yet is synced at head; a file deleted outside Perforce is restored at the workspace's revision. No server call for files already on disk. | Everyday work: "load what I have, fetch what I'm missing" |
| `latest` | Head revision, synced even for files already on disk | "Give me the newest of everything" |
| `@<change>` | Snapshot of the branch at that change number | Reproducible shot, farm render |
| `@<label>` | Perforce label (for example `approved`, `delivery_0412`) | Approved or released versions |
| `#<rev>` | One file's revision | Python API only (`AssetIdentifier`), not USD |

Rules:
1. A stage has **one pin, set on its context** (usually `have`, `latest`,
   or `@change` / a manifest (§10.8) for reproducibility). Layers contain plain paths, so a single
   path cannot carry its own pin (§10.2).
2. **Relative paths inside a pinned layer stay at the pin.** The version
   store mirrors the depot layout for each version, so a relative path
   anchored next to a pinned layer lands in the same version's folder, and
   Clio fetches it at that version.

**Where versions are placed.**
* `latest` / `have` → the **workspace**, through a normal sync. This is what
  the artist sees and edits.
* Historical pins (`@change`, `@label`, `#rev`) → a **read-only version
  store** filled with `p4 print` (the ContentCache, §9.3). The workspace is
  never touched. Several versions of the same asset can exist side by side
  (two shots pinned to different crate versions in one session), and a
  version is fetched once per machine.
* **Safety:** a file the artist has opened for edit is never overwritten
  by any pin. The resolver returns the artist's local file for `latest` and
  warns that a pinned version differs from local edits.

**The form of the resolved path (settled).** Sdf anchors a layer's
relative paths to that layer's *resolved path* (`sdf/layerUtils.cpp` L193).
Clio returns ordinary local file paths, so renderers and other consumers
that read resolved paths as files work unchanged. Relative children stay
under Clio's control because every local path inside the workspace root or
the version store is recognised again: a workspace path means "the
context's pin", and a version-store path means "the version its folder
holds" (for example `…/change-18234/…`). Tested with nested relative
sublayers at `latest` and at a change pin.

### 10.5 Authoring conventions (what is written inside layers)

* **Plain paths only.** Layers must open in USD without Clio.
* **Relative paths (recommended)** for everything: inside an asset
  (`./geo/crate_geo.usdc`) and across assets
  (`../../assets/crate/crate.usda`). A copied folder opens anywhere with no
  settings.
* **Project-rooted search paths** (`assets/crate/crate.usda`) also work.
  Plain USD needs `PXR_AR_DEFAULT_SEARCH_PATH=<project root>`; Clio
  searches the workspace root automatically. USD first looks next to the
  current layer, which with Clio can cost one server call per probe (the
  "not found" answer is remembered until refresh). Relative paths avoid
  this.
* **No absolute paths** in shared files: they only work on machines with the
  same folder layout.
* **Deliveries:** no path rewriting is needed. Open the stage once with Clio
  at the version to deliver: every file it needs is then on disk, in the
  workspace or the version store, with the depot's layout.

### 10.6 Saving layers (write side)

When USD saves a layer from a DCC, the resolver's write hooks let Clio:
* open the file for edit and lock it if it is a `+l` type, or add it if it
  is new, in the artist's current change;
* refuse the save *before* data is lost if someone else holds the lock
  (`_CanWriteAssetToPath` with a `whyNot` message);
* never submit. Saving a layer is a local action. Submitting stays an
  explicit Clio `save`.

Under option A (no plugin), the same behaviour is available as
`clio_usd.prepare_for_edit(layer_paths)`, which DCC integrations call from
their save callbacks.

### 10.7 Performance notes

* **Waves, not files.** Option A costs one server round trip per
  dependency depth level. Option B turns bursts of parallel misses into one
  batched request. Neither makes a server call per layer.
* **Nothing to do is nearly free.** The freshness check (§9.4) on the
  stage's scopes decides whether any sync is needed at all. For `have` and
  `verify` policies no server call is made.
* **Parse once.** Option A keeps opened layers alive for `Usd.Stage.Open`.
  Option B needs no extra parsing.
* **Payloads stay lazy.** With option B, payloads are synced only when USD
  loads them (`stage.Load(path)`), which suits large sets. With option A,
  the `payloads` policy decides up front.
* **Large binaries** (`.usdc` caches, VDBs, textures) use the parallel
  transfer from §8.2.

### 10.8 Version manifests: the same files on every machine (sketch)

**Problem.** `have` and `latest` depend on the machine: `have` loads
whatever each workspace holds, and `latest` depends on *when* each machine
resolves. A farm render split over 500 machines must load **exactly the same
file revisions** on every frame, on every machine, even while artists keep
submitting. A change-number pin (`@18234`) is close, but it cannot express
what an artist actually looked at: a mix of revisions synced at different
times, or files newer than any single change on their branch.

**Idea.** Before a stage is loaded for batch work, Clio writes a
**manifest**: the exact revision of every file the stage can load. Every
machine then loads through the manifest and nothing else. The manifest is
the unit of reproducibility: the same manifest gives the same pixels, today
or in a year.

#### What a manifest contains

```jsonc
{
  "clio_manifest": 1,
  "created": "2026-09-28T14:02:11Z",
  "created_by": "sam@sam-ws01",
  "server": "ssl:perforce:1666",              // effective P4PORT (§8.3)
  "depot": "//imagine/main",
  "root_layer": "shots/sq010/sh0100/shot.usda", // relative to the depot root
  "source_pin": "have",                         // how revisions were chosen
  "usd_version": "26.08",
  "files": {
    "shots/sq010/sh0100/shot.usda": {"rev": 14, "change": 18230, "digest": "9f2c…", "size": 2481, "type": "text"},
    "assets/crate/crate.usda":      {"rev": 7,  "change": 18102, "digest": "41ab…", "size": 931,  "type": "text"},
    "assets/crate/geo.usdc":        {"rev": 3,  "change": 17990, "digest": "c07e…", "size": 48213377, "type": "binary+Fl"},
    "assets/crate/tex/albedo.exr":  {"rev": 5,  "change": 18011, "digest": "d1f0…", "size": 67108864, "type": "binary+Fl"}
  }
}
```

* One entry per file, keyed by path relative to the depot root. It records
  the **revision** and, for integrity, the server's **digest** (MD5, from
  `p4 fstat -Ol`) and size.
* The manifest is small JSON, human-readable and diffable. Its own
  SHA-256 is the job's version ID ("render job 551 used manifest
  `3b9e…`").

#### Creating a manifest (on the artist's or the submitter's machine)

```python
from deda.clio import usd as clio_usd

manifest = clio_usd.Manifest.create(
    "/work/imagine/shots/sq010/sh0100/shot.usda",
    settings="depot=//imagine/main;root=/work/imagine",
    pin="have",            # or "latest", "@18234", "@approved"
    include="all",         # every dependency, including unselected variants and unloaded payloads
)
manifest.save("sh0100.clio-manifest.json")
```

```
clio usd manifest create shots/sq010/sh0100/shot.usda --pin have -o sh0100.clio-manifest.json
```

1. **Find every dependency.** Resolve the root layer, then walk all
   dependencies with USD's `UsdUtils.ComputeAllDependencies`, which covers
   sublayers, references, payloads, variants, clips, UDIMs and asset-valued
   attributes such as textures. It walks layer contents, so the result is a
   *superset* of what one render loads. That is deliberate: a variant
   switch or payload load on the farm must not find a file missing from the
   manifest. Resolving through Clio during the walk also fetches anything
   missing, with the manifest's pin.
2. **Record revisions, in one server call per batch.** Run `p4 fstat -Ol -T
   haveRev,headRev,headChange,digest,fileSize,headType` on the whole file
   list. Which revision is recorded depends on the pin:

   | Pin | Revision recorded |
   |---|---|
   | `have` | The workspace's have-revision (what the artist is looking at) |
   | `latest` | Head at creation time (all files read in one `fstat`, so they come from one moment) |
   | `@change` / `@label` | The revision at that change or label |

3. **Refuse what the farm cannot reproduce.** A file that is not in
   Perforce, is opened for edit, or whose local digest differs from the
   server digest for its have-revision (edited without checkout) cannot be
   fetched on another machine. Creation fails and lists these files, with a
   hint: submit them, or shelve them. A later option is a `"shelf": 18301`
   field per file, so the farm can fetch unsubmitted work from a shelf.
4. **Write and store the manifest.** It goes next to the render job, and
   can also be submitted to Perforce (for example under
   `//imagine/main/manifests/…`), so any render can be reproduced later.

#### Loading through a manifest (on every farm machine)

The context names the manifest instead of a pin:

```
depot=//imagine/main;root=/farm/ws;manifest=/jobs/551/sh0100.clio-manifest.json;policy=verify
```

* **Materialize once per machine, before rendering:**

  ```
  clio usd manifest fetch /jobs/551/sh0100.clio-manifest.json
  ```

  This downloads every listed revision with batched, parallel `p4 print`
  (§8.2) and checks every file's digest against the manifest. Farm machines
  should reach Perforce through a P4 Proxy near the farm, so 500 machines
  fetching the same files hit the proxy's cache and not the commit server.
* **Where the files go.** The layout mirrors the depot, as the version
  store already does for changes (§10.4), so relative paths keep working:

  ```
  <store>/<server>/<depot>/manifest-<sha256 prefix>/shots/sq010/sh0100/shot.usda
  <store>/<server>/<depot>/manifest-<sha256 prefix>/assets/crate/geo.usdc
  ```

  Large files are stored once in a content-addressed pool
  (`<store>/objects/<md5>`) and hard-linked into each manifest's tree. Two
  manifests that share a 48 MB `geo.usdc` store it once, and a second job
  with a similar manifest fetches only the difference.
* **Resolve.** For a path inside the project or the manifest's tree, the
  resolver looks up the manifest entry and returns the file from the
  manifest's tree. A path whose relative path is **not in the manifest**
  fails under `policy=verify`, with a clear error. That is the integrity
  guarantee: nothing outside the manifest is ever loaded. Under
  `policy=sync` it would be fetched at the manifest's `source_pin` and
  reported as a warning, for interactive use.
* **Render** with `policy=verify`: no server calls during the render. That
  gives deterministic results and no server load mid-render. Resolution
  answers come from the manifest in memory.

#### Guarantees and limits

* **Same manifest, same bytes:** every file is checked against its digest
  when fetched, and stored read-only (§8.3).
* **Anything not in the manifest fails loudly** under `verify`, rather than
  silently loading the machine's own copy.
* **Obliterated or archived revisions** cannot be fetched again. `clio usd
  manifest check` asks the server whether every revision is still
  available, so it is worth running before a long re-render.
* **Dynamic paths** that USD computes at load time, for example asset path
  expressions using stage variables set by the render, must be resolvable
  when the manifest is created. The dependency walk evaluates expression
  variables with the stage's defaults. Paths that depend on values set
  only at render time must be listed explicitly (`--extra PATH…`).

#### API sketch

```python
class Manifest:
    @classmethod
    def create(cls, root_layer, settings, pin="have", include="all", extra=()) -> "Manifest": ...
    @classmethod
    def load(cls, path) -> "Manifest": ...
    def save(self, path) -> None: ...
    def fetch(self, *, progress=None, parallel=8) -> FetchReport: ...   # materialize + verify digests
    def check(self) -> CheckReport: ...                                  # still fetchable?
    def diff(self, other) -> ManifestDiff: ...                           # what changed between two jobs
    @property
    def sha256(self) -> str: ...
```

In C++, `clio_core` gets a `Manifest` type (parse, look up, materialize)
that the resolver uses when the context names a manifest. Creation, which
needs `UsdUtils`, lives in Python (`deda.clio.usd`) and in the `clio` CLI.

## 11. Errors and safety

* **Exception hierarchy** rooted at `ClioError`: `ConnectionError`,
  `AuthError`, `LockedByOtherError(path, user, workspace, branch)`,
  `OutOfDateError`, `ConflictError(conflict_set)`, `NotInWorkspaceError`,
  `BranchError`, `ServerPolicyError` (trigger rejections), `CancelledError`.
  Each has an artist-readable `message` and a `hint` (what to do next).
  The raw Perforce messages are kept in `.p4_messages` for debugging.
* **Warnings are not errors.** Perforce "file(s) up-to-date." and similar
  informational output is never raised.
* **Destructive actions** (`discard`, `get --force`, workspace
  switch with open files) need explicit confirmation in the CLI
  (`--yes` for scripts). In the API they need an explicit keyword
  (`discard(..., confirm=True)`). Before overwriting, `discard` can copy the
  local files to a timestamped backup folder (on by default, configurable).
* **Branch switch** with opened files is refused by default and offers to
  shelve them as a draft first.

## 12. CLI

Entry point `clio` (also `python -m deda.clio.cli`). Human-readable output by
default, `--json` on every command for tooling, and exit codes documented
per error class.

```
clio setup                      # first-time: login, trust, create workspace
clio login | logout | whoami
clio status [PATH...]           # changed / locked / out-of-date, from cache
clio get [PATH...] [--version V] [--preview] [--force]
clio lock PATH... | unlock PATH...
clio who PATH...                # who has it locked/open, in which branch
clio save -m MSG [PATH...]      # detect + open + submit
clio discard PATH... [--no-backup]
clio history PATH [--limit N]
clio draft share [-m MSG] [PATH...] | draft list | draft get ID | draft drop ID
clio branch list | create NAME [--from PARENT] [--task] | switch NAME
clio branch update [NAME] | publish [NAME] -m MSG | retire NAME
clio export ASSET --version V --dest DIR
clio doctor                     # config, connectivity, server settings, typemap
clio cache verify [PATH] | cache clear [PATH]   # history/content caches (§9)
clio usd prepare LAYER [--pin @CHANGE|@LABEL|latest] [--payloads all|none]  # §10.2
clio usd deps LAYER             # list dependencies and their state, no sync
clio usd localize LAYER --dest DIR   # rewrite clio: paths for delivery (§10.5)
```

Perforce-literate users can use aliases (`sync`, `submit`, `edit`,
`revert`, `shelve`). *(Q3: CLI framework. The recommendation is `click`,
loaded lazily, or `argparse` for zero dependencies.)*

## 13. Testing strategy

* **Unit tests** (`tests/unit`) run service logic against `FakeBackend`.
  They are fast and have no server.
* **Integration tests** (`tests/integration`) run against a real, throwaway
  `p4d` started per test session with an `rsh:` port
  (`P4PORT="rsh:p4d -r <tmp> -L log -i"`). That needs no network listener and
  no admin setup, and runs on CI (Linux, Windows, macOS). The fixtures create
  a streams depot, the typemap from §8.5, and multiple users so locks and
  conflicts can be tested.
* **C++ tests** (`cpp/tests`, doctest) cover `clio_core` directly,
  including a throwaway `p4d`. **Python tests** (`tests/python`, pytest)
  cover the bindings and run the same kind of server scenarios.
* **Test server note:** `p4d` 2026.1 requires every user to have a
  password, even on a brand-new server. The fixtures set one and log in
  through Clio's prompt callback, then use a ticket file.
* **Benchmarks** (§8.7).
* **USD tests** open real stages through the `clioUsd` plugin, in a
  subprocess per scenario (USD reads `PXR_PLUGINPATH_NAME` only once). They
  need a USD build with Python bindings for Python 3.13; `usd-core` wheels
  have no C++ headers, so they cannot be used to build the plugin.
  Generated shot/asset layer trees
  (sublayers, references, payloads, variants, expression variables, UDIMs)
  are submitted to the test `p4d`, and the tests check that `prepare` syncs
  exactly the expected files at the expected revisions, in the expected
  number of server round trips. The resolver plugin (phase 6) gets the same
  suite, run inside each target USD build.

## 14. Integration with Dedaverse, Imagine, and DCCs

> **Deferred.** No Dedaverse, Imagine or DCC integration is targeted yet.
> The points below keep Clio ready for it.

* Clio's runtime dependency is its own compiled extension (which contains
  P4API). It never imports Dedaverse or Imagine. They depend on Clio.
* Extension points use **entry points**: `deda.clio.resolvers` (asset
  resolvers) and `deda.clio.hooks` (pre-save validation, for example "no
  absolute texture paths", "file naming convention"). Hooks run
  client-side before submit, so artists get feedback before a server
  trigger rejects the change.
* The event bus (§5.6) lets Dedaverse drive lock icons, progress bars, and
  notifications without polling.

## 15. Roadmap

| Phase | Scope |
|---|---|
| **0 — Scaffold** *(done)* | CMake project; `clio_core` (P4API connections, pins, `clio:` identifiers, settings, resolve for latest/have/historical pins, version store); `deda.clio._core` (nanobind, abi3); the `clioUsd` resolver plugin; C++, Python and USD tests against a throwaway `p4d`. See [building.md](building.md). |
| **1 — Core workflow** | Python services over `CoreBackend`: connect/login/setup, get (parallel), lock/unlock, save, status, history backed by the HistoryCache with watermark refresh (§9.3–9.4), CLI for these. Baseline benchmarks. |
| **2 — Branching** | Streams/task streams, switch, update/publish with binary conflict handling, cross-branch lock check, drafts (shelves). |
| **3 — Performance** | Connection pool and miss coalescer in `clio_core`; time-bounded in-memory caches (§8.3); C++ hashing/scan/diff if benchmarks justify it. Measure; add a persistent store only if the numbers call for it. |
| **4 — USD completion** | Write side (edit/lock on save, §10.6); revision-based timestamps; `deda.clio.usd.prepare` (option A); CI builds against USD 26.08 and 25.08. |
| **4½ — Version manifests** | `Manifest` create/save/load/fetch/check/diff (§10.8), `manifest=` context setting, content-addressed pool, `clio usd manifest` CLI, farm guide (proxy + `policy=verify`). |
| **5 — Ecosystem** | UI view models (`FileListView`, `HistoryView`) with the request pipeline, ContentCache (§9.5), asset resolver plugins, validation hooks, `clio doctor`. Dedaverse/DCC integration when targeted. |

### Backlog

* **Persistent local cache (SQLite)** for status and history, only if
  in-memory caching proves too slow (§8.3).
* **Thumbnails and previews** (§9.8).
* **Push notifications** for instant UI refresh (§9.4). MVP polls every
  30–60 s.
* **Streaming pinned versions via `ArAsset`** without writing files (§10.3).

## 16. Open questions

1. **Namespace:** Does Dedaverse use a PEP 420 implicit `deda` namespace
   (no `deda/__init__.py`)? If it ships a `deda/__init__.py`, it must be
   removed or turned into a `pkgutil` namespace in both projects.
2. **Server:** Existing Perforce server version and topology (single server,
   proxy, edge)? Is there already a streams depot, or is this greenfield? Can
   we set `net.parallel.max` and the typemap?
3. **CLI framework:** `click` (nicer UX, one dependency) or `argparse` (no
   dependencies)?
4. **Cross-branch locking:** Advisory warning or hard block by default?
5. **Vocabulary:** Do "save / get / lock / draft / publish" suit your
   artists, or do they already know some Perforce terms that should stay?
6. **Asset identity:** Is an asset a folder (all files under a path), or is
   it defined by Dedaverse/Imagine metadata (for example a USD asset or a
   database ID)? This decides how much of the resolver ships in Clio itself.
7. **Pins:** Is a Perforce **label** the right way to mark approved
    versions, or will Dedaverse/Imagine keep their own version records that
    map to change numbers?
8. **P4API licence:** confirm that the Perforce C++ API may be
    redistributed statically linked inside our wheel and plugin.
9. **Manifests, storage:** Where should manifests live: next to the render
    job only, or also submitted to Perforce so any render can be reproduced
    later?
10. **Manifests, unsubmitted work:** Should the farm be able to render
    unsubmitted work through shelves, or must everything in a manifest be
    submitted?

### Decided

* Default pin is `have`: a file on disk is loaded as it is; a file not on
  disk is synced as its layer is resolved. `latest` updates everything.
* Farm/batch loads go through a version manifest created before loading
  (§10.8, design sketch).

* USD authoring: layers contain plain paths; Clio's resolver is the primary
  resolver, a subclass of `ArDefaultResolver`, and enhances it only when a
  Clio context is bound (§10.2). `clio:` URIs are no longer used in layers.

* Caching: in memory only for the MVP; the server is the authority
  (§8.3). A persistent SQLite store is in the backlog, only if performance
  requires it.
* Targets: Python 3.13; standalone USD 26.08, plus USD 25.08 or later; no
  DCCs yet.
* Native code: a single C++ core (`clio_core`) for Python (nanobind) and
  USD. No P4Python, no `p4 -G` fallback, no Rust.
* No Dedaverse/Imagine integration yet.

* USD: Clio ships a compiled C++ `clio:` resolver that performs Perforce
  operations in-process through P4API (§10.2–10.3). The Python pre-open
  sync is kept for environments without a resolver build.

* UI refresh: 30–60 s polling for the MVP. Push notifications are in the backlog.
* Thumbnails: out of scope for the MVP. Kept in the backlog (§9.8).
