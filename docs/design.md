# Clio — Design Document

| | |
|---|---|
| **Status** | Draft v0.1 — for review |
| **Package** | `deda.clio` (Python namespace package) |
| **Consumers** | Dedaverse, Imagine, future Deda projects, DCC plugins |
| **Backing store** | Perforce server (`p4d`), streams depots |

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

Python is the main interface. Performance-critical paths either run inside
native code that already exists (the P4 C++ API through P4Python, and the
server's parallel transfer) or in a small native extension of our own
(`deda.clio._native`) where measurements show it pays off.

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
│  Local state:  StatusCache  (SQLite, per workspace)            §8.3  │
│                HistoryCache (SQLite, per server + user)        §9    │
│                ContentCache (files by digest: thumbnails, …)   §9    │
│  Views:        FileListView · HistoryView (UI-ready, no Qt)    §9    │
│  Native (opt): deda.clio._native — hashing, FS scan, diffing (§8.4)  │
├──────────────────────────────────────────────────────────────────────┤
│  Backend protocol (typed, tagged records in / out)                   │
│   ├─ P4PythonBackend   (default; P4 C++ API, persistent connections) │
│   ├─ P4CliBackend      (fallback; `p4 -G` marshalled subprocess)     │
│   └─ FakeBackend       (in-memory, for unit tests)                   │
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

* **DCC compatibility.** P4Python is a compiled extension built for one
  Python minor version. DCCs ship their own Pythons (Maya, Houdini, Blender,
  and Unreal all differ). When P4Python is not available for an interpreter,
  `P4CliBackend` falls back to `p4 -G`, which outputs marshalled Python dicts,
  so no text parsing is needed. It is slower (a process spawn plus a
  connection per call) but always works.
* **Testing.** Service logic is tested against `FakeBackend` in milliseconds.
  Integration tests run against a real, throwaway `p4d` (§13).
* **Future.** A different transport (for example a native backend written
  directly against the P4 C++ API) can be added without API changes.

### 4.3 Package layout

`deda` is a **PEP 420 implicit namespace package**. There is no
`deda/__init__.py`, so `deda.clio`, the Dedaverse packages, and others can
be installed independently and still share the `deda.` prefix. *(Open
question Q1: confirm this matches how Dedaverse declares `deda`.)*

```
clio/
├── pyproject.toml              # build backend: maturin (if _native) or hatchling
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
│   │   ├── p4python.py
│   │   ├── p4cli.py
│   │   └── fake.py
│   ├── cli/
│   │   ├── __init__.py
│   │   └── __main__.py         # `python -m deda.clio.cli`, entry point `clio`
│   └── _native.pyi             # type stubs for the optional extension
├── native/                     # Rust crate for deda.clio._native (phase 3)
├── usd_resolver/               # thin C++ `clio:` ArResolver plugin (phase 6, §10.3)
├── tests/
│   ├── unit/                   # FakeBackend
│   └── integration/            # real p4d via rsh: port
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

*(Open question Q5: should this be advisory or enforced by default?)*

## 8. Performance

Principle: **Python orchestrates. Native code moves bytes.** Most of the
heavy work (network transfer, compression, server-side checks) already
happens in native code: the Perforce server and the P4 C++ API. Clio's job
is to call them efficiently, never to put a Python loop on a per-byte or
per-file hot path, and to add native code only where profiling shows a gap.

Recommendations in priority order. Items 1–4 give the largest gains for the
least risk.

### 8.1 Talk to Perforce efficiently

1. **Use P4Python, not subprocess `p4`.** P4Python is a thin binding over the
   official P4 C++ API. It returns tagged dicts directly, with no text
   parsing and no process spawn.
2. **Keep connections open.** Each connect is a TCP (and possibly SSL)
   handshake plus protocol negotiation. A `Session` keeps a pool of
   long-lived connections (for example 1 + N workers). Commands reuse them.
   For the CLI, a single command uses a single connection. *(A later option
   is a lightweight `clio` daemon/agent that holds warm connections and the
   cache for the CLI and all DCCs on a machine. This is deferred until
   measurements justify it.)*
3. **Batch everything.** One `fstat`/`edit`/`add` call for 5,000 files, not
   5,000 calls. Very large file sets go through `-x argfile` (or P4Python
   `input`) to avoid command-line length limits, and are chunked for memory.
4. **Ask only for what you need.** `p4 fstat -T field,list` limits returned
   fields, and `-m` limits rows. Scope every command to the narrowest path
   (the asset folder), never `//...`.
5. **Stream large results.** Use a `P4.OutputHandler` to process records as
   they arrive, instead of building a list of a million dicts in memory.
6. **One connection per thread.** A `P4` object must not be used from two
   threads at the same time. The pool hands out connections, and parallel
   *metadata* work (for example status for several assets) runs on separate
   connections. *(To verify: P4Python's GIL behaviour during `run()` in the
   version we pin. If it holds the GIL, parallel metadata work moves to a
   process pool or the native layer.)*
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

### 8.3 Avoid scanning the disk: the local status cache

The slowest artist-facing operation in Perforce is usually "what did I
change?" (`p4 reconcile`/`status`). On large trees it walks the disk and may
hash every file. Clio avoids it:

* **StatusCache** is a per-workspace SQLite database (WAL mode) that stores,
  for each file, `depot_path, have_rev, size, mtime, digest, open_action,
  lock_state`. It is filled from `p4 have` / `p4 fstat -Ol` (which returns
  server digests and sizes) and updated by every Clio operation.
* **Change detection** compares `(size, mtime)` from a fast directory scan
  against the cache. Only files whose size or mtime changed are hashed.
  Only those files are passed to a scoped `p4 reconcile` (or opened directly),
  using `reconcile -m` so the C++ API also checks modtimes before digests.
* **Optional file watcher** (`watchdog` / OS notifications) marks paths dirty
  as they change, so `status` for an asset returns without scanning at all.
* **UI reads** (lock badges, "out of date" indicators in Dedaverse) come from
  the cache in microseconds. A background refresh keeps it current using the
  cheap check in §8.1.7.

### 8.4 Native extension (`deda.clio._native`), used where it measurably helps

Rule: **no native code without a benchmark showing a real gain** (for
example ≥3× on a realistic workload). Likely candidates, in order:

| Candidate | Why Python is slow here | Native approach |
|---|---|---|
| **Parallel file hashing (MD5)** of multi-GB files, to compare with server digests | Per-file I/O and hashing in one thread. `hashlib` releases the GIL, but orchestration and small reads add overhead. | Rust: memory-mapped / large-buffer reads, a thread pool, GIL released for the whole batch. |
| **Directory scan + stat** of 100k+ files | `os.scandir` is decent, but building Python objects per entry adds up. | Rust (`jwalk`-style parallel walk). Returns only the *changed* entries compared with a snapshot passed in from the cache. |
| **Manifest diffing** (have list vs disk vs server) | Large dict/set operations with many small objects. | Rust sorted-merge on compact arrays. Only the diff crosses into Python. |
| **Compact record decoding** for very large `fstat` results | Dict per record. | Deferred. Only if profiling shows it. |

**Technology recommendation: Rust + PyO3, built with maturin, using the
stable ABI (`abi3`).**

* `abi3` wheels are built **once per OS/architecture** and load in every
  CPython ≥ the minimum version. That is the key benefit for DCC embedding,
  where each DCC has a different Python. A version-specific `.pyd` would
  need a build matrix of DCC × Python × OS.
* PyO3 releases the GIL easily (`py.allow_threads`) and gives memory safety
  for file-walking and threading code.
* The extension is **optional**: every function in `_native` has a pure-Python
  fallback in the same module interface, and `deda.clio` checks at import
  time which one is available. A missing or incompatible binary never breaks
  Clio. It only makes it slower.
* **Do not** reimplement the Perforce protocol, and do not bind the P4 C++ API
  from Rust in v1. P4Python already does that. If P4Python's per-Python-
  version packaging becomes the blocker for DCCs, the next step is a
  nanobind/C++ backend against the P4 C++ API built with the limited API.
  That is a planned escape hatch, not a v1 task.

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
*(Q3: What server version and topology do we target?)*

### 8.6 Python-level hygiene

* **Lazy imports.** The `clio` CLI must start fast (target < 150 ms to first
  output for `clio --help`). Import P4Python, SQLite, and the CLI framework
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
| **Local** | Have revision, local modifications. | On local actions | From the per-workspace StatusCache (§8.3), not from the server. |
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
| **L1: in memory** | Model objects for the current session: recently viewed history, visible rows | Process | LRU, bounded by entry count |
| **L2: HistoryCache** | Server metadata (revisions, changes, integrations, head info, watermarks) | Persistent | SQLite, one DB per server + Perforce user |
| **L3: ContentCache** | File bytes fetched for display: thumbnails, previews, small sidecars, older versions opened for comparison | Persistent, size-limited LRU (e.g. 5 GB) | Folder keyed by server digest + size |

Notes:
* **Why the HistoryCache is not per workspace.** History belongs to the
  server, not to a workspace. One cache per machine, shared by the CLI and
  every DCC, means a file's history is fetched once, whichever tool shows it
  first. It lives in the user cache folder
  (`<user cache dir>/clio/<server-id>/<p4user>/history.db`).
* **Why per Perforce user.** Protections can hide paths from some users. A
  cache must never show a user rows that were fetched with someone else's
  permissions, so caches are never shared across Perforce users.
* **Server identity** comes from `p4 info` (server ID where set, otherwise
  server address plus server root), so that two servers never share a
  cache. *(To confirm against the target server's configuration, Q3.)*
* **Local disk only.** SQLite must not be on a network share. It runs in WAL
  mode with a busy timeout and short write transactions, so several
  processes (CLI, Maya, Houdini) can read and write it at once. If the
  optional per-machine agent (§8.1.2) is built later, it becomes the single
  owner of the cache and the other processes ask it instead.
* **ContentCache is content-addressed.** A file's revision is identified by
  its server digest (MD5) and size. Identical content, such as a file copied
  to a branch, is stored once and never downloaded twice. Content comes from
  `p4 print -o`, which does not change the workspace.

**HistoryCache schema (sketch)**

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
| A resolver can be the **primary** resolver or a **URI resolver** for listed schemes (`"uriSchemes"` in `plugInfo.json`). A URI resolver can never be primary (`canBePrimaryResolver = uriSchemes.empty()`). | `ar/resolver.cpp` ~L244–273 | Clio can own `clio:` paths *without* replacing the resolver a DCC or studio already uses (Houdini, Omniverse, and in-house resolvers are usually primary). |
| When either the asset path or its anchor has a URI scheme, `CreateIdentifier` is called on that scheme's resolver. | `ar/resolver.h` (docs for `_CreateIdentifier`) | Relative paths inside a `clio:` layer are anchored by Clio, which keeps them in the same version snapshot (§10.4). |
| Relative paths are anchored to the anchor layer's **resolved path** (`anchor->GetResolvedPath()`), not its identifier. | `sdf/layerUtils.cpp` L193 | The form of Clio's resolved path decides whether children stay under Clio's control. This is the main prototyping question (§10.4). |
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
local path.

Pros: automatic and complete. It covers any stage opened in any way,
variant switches, payload loads, and `Reload()`. It coexists with the DCC's
primary resolver. Cons: C++ plugin built per USD version; the resolver runs
on USD worker threads.

**C. Clio primary resolver in C++** (plain file paths, Clio decides
everything). Not recommended: it conflicts with DCC and studio resolvers
(only one primary resolver per process), and it would put Clio in the path
of every file USD touches, including files that have nothing to do with
Perforce.

**D. Custom `SdfFileFormat` plugin** (for example `.clio` files as
indirections). Not recommended: it is the wrong layer for this job and does
not cover non-layer assets.

**Recommendation: A first, then B, both over the same core.**
* **MVP: option A.** It delivers "open this stage at version X and
  everything is present" with no compiled USD code, and it is also what the
  farm and batch tools need (one explicit, logged prefetch step).
* **Next: option B**, for automatic behaviour inside DCCs. The resolver
  plugin stays *thin*. All Perforce logic stays in Clio, and the plugin talks
  to Clio as described in §10.3.
* Both use the same **resolution core** (pin rules, where versions are
  stored, the batching pipeline), so a path gives the same file under A and
  under B.

### 10.3 Resolver plugin design (option B)

```
USD worker threads ──► ClioResolver (C++, thin, per USD build)
                         │ fast path: in-process lookup, no server, no IPC
                         │   manifest: identifier + pin → local path, rev, digest
                         │ miss / stale:
                         ▼
                   local Clio agent (one per machine, §8.1)
                         │ coalesces misses from all threads & processes
                         │ one batched parallel sync / p4 print per burst
                         ▼
                        p4d
```

* **Fast path, no IPC.** Most calls are for files that are already present.
  The plugin answers them from a read-only, memory-mapped manifest (or the
  SQLite StatusCache) that the agent keeps current. The target is a few
  microseconds per call, so composition speed is unaffected when everything
  is up to date.
* **Slow path through the agent.** A miss is sent over a local socket or
  named pipe. The calling USD thread blocks until its file is ready. Misses
  from parallel sublayer loading arrive within milliseconds of each other,
  and the agent's batching window (§9.5) turns them into one sync. The agent
  also shares its connection pool and caches across every DCC on the machine.
  **Consequence:** the per-machine agent, deferred in §8.1, becomes a
  requirement for option B.
* **Why not call Python from the plugin:** GIL use on USD worker threads
  (§10.1). The agent can be Python. It runs in its own process, so it never
  competes for a DCC's GIL.
* **ArResolver methods Clio implements:**

| Method | Clio behaviour |
|---|---|
| `_CreateIdentifier` | Normalizes `clio:` paths. Anchors relative paths, keeping the anchor's pin (§10.4). |
| `_Resolve` | Fast-path lookup. On a miss, requests a sync from the agent and waits (with a timeout and a clear error). Returns the local path. |
| `_ResolveForNewAsset` / `_CanWriteAssetToPath` / `_OpenAssetForWrite` | When a layer is saved: opens the file for add or edit (with lock) through the agent, or refuses with `whyNot` ("locked by Sam"). |
| `_IsContextDependentPath` | `true` for `clio:` paths, since the pin comes from the context. |
| `_CreateDefaultContext[ForAsset]`, `_CreateContextFromString` | Build a `ClioResolverContext` (project, branch, pin, policy). The string form, for example `"branch=main;pin=@18234"`, lets DCC UIs and env vars set it. |
| `_GetAssetInfo` | Fills `version` (revision/change number) and `resolverInfo` (depot path, digest), so DCC UIs can show "crate.usd v12 @18234". |
| `_GetModificationTimestamp` | Returns a timestamp derived from the revision or change number, so `Reload()` picks up newly synced versions. |
| `_RefreshContext` | Re-evaluates moving pins ("latest", labels), syncs, and sends `ArNotice::ResolverChanged` for affected contexts. |
| `_OpenAsset` | Opens the local file (`ArFilesystemAsset`). A later option is to stream pinned versions from the ContentCache without writing to the workspace. |

* **Policies in the context:** `sync` (default: fetch what is needed),
  `verify` (fail if not present, no server calls, for farm reproducibility),
  and `offline` (use whatever is on disk and warn).
* **Build and distribution:** the plugin must be compiled against each
  DCC's USD (Houdini, Maya-USD, Omniverse, Blender, usd-core/standalone),
  because the USD C++ ABI and namespace differ per build. Keeping the plugin
  to a few hundred lines with no Perforce code makes these rebuilds cheap.
  Registration uses `PXR_PLUGINPATH_NAME` and a `plugInfo.json` declaring
  `"uriSchemes": ["clio"]`.

### 10.4 Versions, pins, and where files are placed

**Pins** say which version of a path to use:

| Pin | Meaning | Typical use |
|---|---|---|
| `latest` | Head revision on the context's branch | Artists working live |
| `have` | Whatever is in the workspace. No server call. | Offline, or "don't change my files" |
| `@<change>` | Snapshot of the branch at that change number | Reproducible shot, farm render |
| `@<label>` | Perforce label (for example `approved`, `delivery_0412`) | Approved or released versions |
| `#<rev>` on one path | That file's revision | Pinning one asset inside a shot |

Rules:
1. A stage has **one context pin** (usually `latest`, or `@change` for
   reproducibility).
2. A path can **override** it in its asset path:
   `clio:/props/crate/crate.usd?change=18100` or `?label=approved`.
   (Query syntax rather than `#`, to avoid confusion with URI fragments.)
3. **Relative paths inherit the anchor's snapshot pin** (`@change`/`@label`),
   so an asset's internal sublayers and textures are always a consistent
   set. A per-file `#rev` pin is *not* inherited, because it would be
   meaningless for siblings.

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

**Open prototyping question: the form of the resolved path.** Sdf anchors a
layer's relative paths to that layer's *resolved path* (`sdf/layerUtils.cpp`
L193).
* If Clio returns a plain file path (for example
  `/cache/…/crate.usd`), relative children are anchored on the filesystem.
  The primary resolver then handles them, not Clio, and they are not synced
  or pinned. To make that work, the version store must mirror the depot
  layout per snapshot and be pre-filled per asset.
* If Clio returns a `clio:`-form resolved path and opens the local file in
  `_OpenAsset`, children stay under Clio's control. However, any consumer
  that uses resolved paths as file paths directly (some renderers do this
  for textures) would receive a URI.
* A likely answer is **`clio:` resolved paths for layers, file paths for
  non-layer assets**. This must be validated in a prototype with Hydra
  Storm and the renderers you use before the design is fixed.

### 10.5 Authoring conventions (what is written inside layers)

* **Inside an asset** (the asset's own sublayers, geometry, textures):
  relative paths (`./geo/crate_geo.usdc`). The files stay portable and work
  without Clio once synced.
* **Across assets** (shot → asset, set → prop): `clio:` URIs
  (`clio:/props/crate/crate.usd`). They don't depend on each artist's
  workspace root, and they can carry a pin.
* **Deliveries and vendors:** `clio usd localize` rewrites `clio:` paths to
  plain relative paths, using USD's own `UsdUtilsModifyAssetPaths` /
  localization APIs (`usdUtils/localizeAsset.h`), so the result opens
  without Clio.
* Until option B ships, layers can use plain relative paths everywhere, and
  option A handles syncing. Adopting `clio:` later is a path rewrite of
  the cross-asset references only.

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
`revert`, `shelve`). *(Q4: CLI framework. The recommendation is `click`,
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
* **Backend parity tests** run the same integration suite against both
  `P4PythonBackend` and `P4CliBackend`.
* **Benchmarks** (§8.7) and **native/pure parity tests** for `_native`.
* **USD tests** use `usd-core` from PyPI. Generated shot/asset layer trees
  (sublayers, references, payloads, variants, expression variables, UDIMs)
  are submitted to the test `p4d`, and the tests check that `prepare` syncs
  exactly the expected files at the expected revisions, in the expected
  number of server round trips. The resolver plugin (phase 6) gets the same
  suite, run inside each target USD build.

## 14. Integration with Dedaverse, Imagine, and DCCs

* Clio depends only on P4Python (optional), `tomli` (py < 3.11), and the
  optional native wheel. It never imports Dedaverse or Imagine. They depend
  on Clio.
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
| **0 — Skeleton** | `pyproject`, namespace package, CI, `FakeBackend`, `p4d` test fixture, error model, config. |
| **1 — Core workflow** | `P4PythonBackend` + pool, connect/login/setup, get (parallel), lock/unlock, save, status (no cache yet), history backed by the HistoryCache with watermark refresh (§9.3–9.4), CLI for these. Baseline benchmarks. |
| **2 — Branching** | Streams/task streams, switch, update/publish with binary conflict handling, cross-branch lock check, drafts (shelves). |
| **3 — Performance** | StatusCache, cheap change checks, optional watcher. Profile, then `_native` (Rust/PyO3 abi3) for hashing/scan/diff if the benchmarks justify it. |
| **4 — USD prefetch** | `deda.clio.usd.prepare` (option A, §10.2): wave walker, pins, `payloads`/asset policies, version store for historical pins, `prepare_for_edit`, `clio usd prepare/localize` CLI. |
| **5 — Ecosystem** | UI view models (`FileListView`, `HistoryView`) with the request pipeline, ContentCache (§9.5), asset resolver plugins, validation hooks, `P4CliBackend` for DCC fallback, `clio doctor`, Dedaverse integration. |
| **6 — USD resolver** | Per-machine Clio agent (§8.1) and the thin C++ `clio:` URI resolver plugin (§10.3), built per target USD. Starts with a prototype that settles the resolved-path form (§10.4). |

### Backlog

* **Thumbnails and previews** (§9.8).
* **Push notifications** for instant UI refresh (§9.4). MVP polls every
  30–60 s.
* **Streaming pinned versions via `ArAsset`** without writing files (§10.3).

## 16. Open questions

1. **Namespace:** Does Dedaverse use a PEP 420 implicit `deda` namespace
   (no `deda/__init__.py`)? If it ships a `deda/__init__.py`, it must be
   removed or turned into a `pkgutil` namespace in both projects.
2. **Python targets:** Minimum Python version, and which DCCs (and their
   Python versions) must be supported at launch? This sets the `abi3` floor
   and whether `P4CliBackend` is needed in Phase 1.
3. **Server:** Existing Perforce server version and topology (single server,
   proxy, edge)? Is there already a streams depot, or is this greenfield? Can
   we set `net.parallel.max` and the typemap?
4. **CLI framework:** `click` (nicer UX, one dependency) or `argparse` (no
   dependencies)?
5. **Cross-branch locking:** Advisory warning or hard block by default?
6. **Vocabulary:** Do "save / get / lock / draft / publish" suit your
   artists, or do they already know some Perforce terms that should stay?
7. **Asset identity:** Is an asset a folder (all files under a path), or is
   it defined by Dedaverse/Imagine metadata (for example a USD asset or a
   database ID)? This decides how much of the resolver ships in Clio itself.
8. **USD builds:** Which DCCs and USD versions must the resolver plugin
   (option B) support first? Is there already a primary resolver in use
   (studio, Omniverse, Houdini's defaults)?
9. **USD authoring:** Are you happy with relative paths inside assets and
   `clio:` URIs across assets (§10.5), or should everything stay plain
   paths, with Clio relying on pre-open sync only?
10. **Pins:** Is a Perforce **label** the right way to mark approved
    versions, or will Dedaverse/Imagine keep their own version records that
    map to change numbers?

### Decided

* UI refresh: 30–60 s polling for the MVP. Push notifications are in the backlog.
* Thumbnails: out of scope for the MVP. Kept in the backlog (§9.8).
