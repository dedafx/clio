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
6. **Safe by default.** Clio never deletes or reverts local work unless the
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
| **Discard** | `p4 revert` | Always confirmed; can back up first (§9). |
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
│  Local state:  StatusCache (SQLite, per workspace)                   │
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
  Integration tests run against a real, throwaway `p4d` (§11).
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
│   ├── errors.py               # exception hierarchy (§9)
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
│   ├── cache/                  # SQLite status cache (§8.3)
│   ├── backends/
│   │   ├── base.py             # Backend Protocol, Record types
│   │   ├── p4python.py
│   │   ├── p4cli.py
│   │   └── fake.py
│   ├── cli/
│   │   ├── __init__.py
│   │   └── __main__.py         # `python -m deda.clio.cli`, entry point `clio`
│   └── _native.pyi             # type stubs for the optional extension
├── native/                     # Rust crate for deda.clio._native (phase 2)
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
| `get` of large binaries | Network/disk-bound: within 10% of raw `p4 sync --parallel` |

A `benchmarks/` suite (pytest-benchmark against a local `p4d` with
generated binary files) runs in CI, so regressions are caught.

## 9. Errors and safety

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

## 10. CLI

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
```

Perforce-literate users can use aliases (`sync`, `submit`, `edit`,
`revert`, `shelve`). *(Q4: CLI framework. The recommendation is `click`,
loaded lazily, or `argparse` for zero dependencies.)*

## 11. Testing strategy

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

## 12. Integration with Dedaverse, Imagine, and DCCs

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

## 13. Roadmap

| Phase | Scope |
|---|---|
| **0 — Skeleton** | `pyproject`, namespace package, CI, `FakeBackend`, `p4d` test fixture, error model, config. |
| **1 — Core workflow** | `P4PythonBackend` + pool, connect/login/setup, get (parallel), lock/unlock, save, status (no cache yet), history, CLI for these. Baseline benchmarks. |
| **2 — Branching** | Streams/task streams, switch, update/publish with binary conflict handling, cross-branch lock check, drafts (shelves). |
| **3 — Performance** | StatusCache, cheap change checks, optional watcher. Profile, then `_native` (Rust/PyO3 abi3) for hashing/scan/diff if the benchmarks justify it. |
| **4 — Ecosystem** | Asset resolver plugins, validation hooks, `P4CliBackend` for DCC fallback, `clio doctor`, Dedaverse integration. |

## 14. Open questions

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
