# Using Clio with USD

USD layers in a Clio project contain **ordinary paths**, such as
`@../../assets/crate/crate.usda@`. Any USD, with or without Clio, can open
them from files on disk. Clio adds to that and never gets in the way:

* **Without Clio**, USD opens the files that are already on disk.
* **With the Clio plugin installed and a Clio context**, USD fetches each
  file it needs from Perforce first, at the version you ask for, and then
  opens it.
* **With the plugin installed but no Clio context**, or when Perforce is
  unavailable, USD opens the files on disk as usual.

This guide is for people using the `deda-clio` package. For how it is
built and why, see [building.md](building.md) and [design.md](design.md) §10.

> **Status: v0.1 scaffold.** Reading works: latest, have, change and label
> pins, with fallback to local files. Saving layers back to Perforce and
> batching many fetches into one request are not done yet. See
> [Current limitations](#current-limitations).

Supported: Python 3.13, and OpenUSD 26.08 and 25.08 (both tested), plus
versions in between, with a plugin built for that USD version. No DCC
integrations yet.

---

## Quick start

```python
import os
from deda.clio import usd as clio_usd

# 1. Install the plugin. Do this before USD creates its resolver (ideally
#    set PXR_PLUGINPATH_NAME in the environment before starting Python).
os.environ["PXR_PLUGINPATH_NAME"] = str(clio_usd.plugin_path())

from pxr import Usd

# 2. Say where the project lives in Perforce and on disk.
ctx = clio_usd.create_context(
    "depot=//imagine/main;"          # depot path of the project root
    "root=/work/imagine;"            # where your client workspace maps it
    "client=sam_imagine;"            # your Perforce workspace
    "port=ssl:perforce:1666;user=sam"
)

# 3. Open the stage by its normal path. Missing or out-of-date files are
#    fetched from Perforce as USD needs them.
stage = Usd.Stage.Open("/work/imagine/shots/sq010/sh0100/shot.usda", ctx)
```

Before you start:

* **Log in once with `p4 login`.** Clio never asks for a password, because
  it runs on USD's worker threads where a prompt would hang. It uses your
  existing Perforce ticket. Without one, it falls back to the files on disk
  and prints a warning.
* **Your client workspace must map `depot` to `root`.** For example,
  `//imagine/main/...` must map to `/work/imagine/...`. Clio does not
  create workspaces yet.

## Writing paths in layers

Use paths that plain USD can open:

| Kind | Example | How plain USD finds it | How Clio finds it |
|---|---|---|---|
| **Relative** (recommended) | `@./geo.usda@`, `@../../assets/crate/crate.usda@` | Next to the layer that refers to it | The same, and fetches it at the stage's version |
| **Project-rooted** | `@assets/crate/crate.usda@` | Next to the layer, then through `PXR_AR_DEFAULT_SEARCH_PATH=<project root>` | Next to the layer, then the workspace root, then USD's search paths |
| **Absolute** | `@/work/imagine/assets/crate/crate.usda@` | Directly, on machines with the same folder layout | The same, if inside the workspace root |

Relative paths are the most portable: a copied folder opens anywhere with
no settings at all. Avoid absolute paths in files you share.

---

## What happens when a stage opens

The test suite (`tests/python/test_usd_resolver.py`) uses this depot:

```
//depot/proj/shots/sh010/shot.usda       subLayers = [@../../assets/crate/crate.usda@]
//depot/proj/assets/crate/crate.usda    subLayers = [@./geo.usda@];  def "Crate" { int version = 2 }
//depot/proj/assets/crate/geo.usda      def "Geo" { int version = 2 }
```

Change 1 submitted version 1 of each file. A later change submitted
version 2 of the crate and geo.

### Opening with the default pin (`have`)

```python
ctx = clio_usd.create_context("depot=//depot/proj;root=/ws;...")
stage = Usd.Stage.Open("/ws/shots/sh010/shot.usda", ctx)
```

The rule is simple: **a file already on disk is loaded as it is; a file not
on this machine yet is synced as its layer is resolved.** Here the
workspace starts empty, so every file is synced.

1. **USD uses Clio's resolver.** With the plugin installed, Clio replaces
   USD's default resolver with a subclass of it (`ClioResolver`).
2. **The context turns Clio on.** `create_context` validates the settings
   and returns a context. `Usd.Stage.Open` binds it while it opens layers,
   including on USD's worker threads.
3. **The root layer is resolved.** `/ws/shots/sh010/shot.usda` is inside
   the workspace root, so Clio handles it:
   * the pin is `have` (the default);
   * the file is not on disk, so Clio maps the path to
     `//depot/proj/shots/sh010/shot.usda` and runs
     `p4 sync -q //depot/proj/shots/sh010/shot.usda#head` through the
     Perforce C++ API, inside the USD process;
   * Clio returns `/ws/shots/sh010/shot.usda`, which now exists.

   Had the file already been on disk, at any revision, Clio would have
   returned it without contacting the server.
4. **Sublayers resolve the same way.** USD reads the shot, anchors
   `../../assets/crate/crate.usda` next to it, and asks Clio for
   `/ws/assets/crate/crate.usda`. Clio syncs it. Then the crate's
   `./geo.usda` is synced too.
5. **The stage shows version 2**, and the three files are in your
   workspace, exactly as if you had run `p4 sync` on them.

If the crate had already been on disk at version 1 and the geo had not,
the stage would show crate version 1 (on disk) and geo version 2 (synced).
The test `test_have_loads_files_on_disk_as_they_are_and_syncs_missing_ones`
covers exactly this. To bring files already on disk up to date as well,
use `pin=latest`.

### Opening at a changelist

Same stage, with `pin=@1` in the context:

1. The shot path is inside the workspace root, but the pin is historical.
   Clio does **not** change your workspace. It fetches the version into
   the **version store**, a read-only folder that mirrors the depot for
   each version:
   ```
   <store>/<server>/<depot>/change-1/shots/sh010/shot.usda
   ```
   It uses `p4 print` to a temporary file, then renames it, so readers
   never see a half-written file. Stored files are made read-only, so
   nothing edits a version in place. Clio returns this path.
2. USD anchors `../../assets/crate/crate.usda` next to that file, which
   gives `<store>/.../change-1/assets/crate/crate.usda`. Clio recognises
   the `change-1` folder and fetches the crate **at change 1**. The crate's
   `./geo.usda` works the same way.
3. The stage shows version 1 for the crate and the geo. Your workspace
   still has version 2.

Because the version store has the same layout as the depot, relative paths
inside a pinned layer always stay at the pinned version. Stored versions
are reused later, even by other processes, because a changelist snapshot
never changes. Labels are the exception (below).

### When Clio steps aside

For each path, Clio decides whether it can help. If not, USD's default
resolver handles it exactly as it would without Clio:

* no Clio context is bound (and `CLIO_RESOLVER_CONTEXT` is not set);
* the path is outside the workspace root and the version store;
* the file is not in Perforce, for example a new local file;
* Perforce cannot be used (see [Working without Perforce](#working-without-perforce)).

---

## Choosing a version: pins

The **pin** is set on the context and applies to the whole stage.

| Pin | Meaning | Files come from |
|---|---|---|
| `have` (default) | The file on disk, whatever its revision. A file not on disk yet is synced at the latest revision (or restored, if it was deleted outside Perforce). | Your workspace |
| `latest` | Head revision, even for files already on disk | `p4 sync` into your workspace |
| `@<change>` | The depot as of that changelist | Version store (`p4 print`) |
| `@<label>` | The revisions tagged by a Perforce label | Version store, fetched again once per process |

* Labels can be **moved** by a Perforce admin, so Clio fetches a label's
  files again the first time each process uses them, and never trusts an
  older copy on disk.
* Plain paths cannot carry their own pin. To mix versions (for example one
  asset held at an older change), open that asset through its own stage
  or reference with a different context, or use a label that tags exactly
  the revisions you want.

## Settings reference

The context string is `key=value` pairs separated by `;`. Order doesn't
matter, and the values may not contain `;` or `=`. A string without `=` is
passed to USD's default resolver as a search path, as before.

| Key | Required | Meaning |
|---|---|---|
| `depot` | yes | Depot path of the project root, e.g. `//imagine/main` (no trailing `/`, no wildcards) |
| `root` | yes | Local folder your client workspace maps `depot` to |
| `client` | usually | Your Perforce workspace (client) name |
| `port`, `user` | no | Perforce server and user. Default: your Perforce environment (`P4PORT`, `P4USER`, `P4CONFIG`, ...) |
| `tickets` | no | Ticket file, if not the default (`P4TICKETS` or `~/.p4tickets`) |
| `store` | no | Version store folder. Default: `$CLIO_VERSION_STORE`, else `$XDG_CACHE_HOME/clio/versions`, else `~/.cache/clio/versions` (`%LOCALAPPDATA%\clio\versions` on Windows) |
| `pin` | no | `have` (default), `latest`, `@<change>`, `@<label>` |
| `policy` | no | `sync` (default), `verify` or `offline` (below) |
| `timeout` | no | Seconds before a Perforce command is cancelled. Default `120`, `0` = none |
| `connect_timeout` | no | Seconds to wait for an unreachable server before falling back to local files. Default `10`, `0` = the OS default (can be over 2 minutes) |

Check a string in Python before handing it to USD:

```python
from deda import clio
clio.Settings.parse(settings)   # raises clio.ConfigError with the reason
```

### Policies

| Policy | Contacts the server? | Use it for |
|---|---|---|
| `sync` | Yes, to fetch what is missing or out of date. Falls back to local files, with a warning, if the server is unavailable. | Everyday work |
| `verify` | No. A file must already be on disk at the required version. Never falls back. | Render farms and reproducible builds, after a prefetch step (planned: version manifests, design §10.8) |
| `offline` | No. Uses what is on disk (for historical pins: the version store, then the workspace). | Working without a connection |

### A default context for every stage

Set `CLIO_RESOLVER_CONTEXT` to a settings string before USD starts. Stages
opened without an explicit Clio context use it. It is read once per
process; if it is invalid, USD prints one warning and Clio stays off.

---

## Working without Perforce

* **Server unreachable, not logged in, or refusing the request:** with the
  `sync` policy, Clio uses the file on disk and prints one warning per
  path, for example:
  ```
  Warning: clio: /work/imagine/assets/crate/crate.usda: Perforce is not available (...);
           using the local file /work/imagine/assets/crate/crate.usda, which may not be version latest
  ```
  For a historical pin, Clio uses the version store if it has that
  version, otherwise the file in your workspace, **which may be a different
  version**. The warning says so.
* **No waiting on a dead server.** Clio gives up after `connect_timeout`
  seconds (default 10), then does not contact the server again for 60
  seconds or until you call `RefreshContext`. A stage with hundreds of
  layers pays the timeout once.
* **`verify`** never falls back, so a farm job fails rather than render
  the wrong version.

Answers that came from a fallback are not remembered, so the exact version
is fetched as soon as the server is reachable again.

## Sharing files

Because layers contain ordinary paths, **anyone can open them from a copy
of the files, with or without Clio**:

| Recipient has | Result |
|---|---|
| Plain USD and a copy of the folder | Opens. Relative paths work as they are. For project-rooted paths, set `PXR_AR_DEFAULT_SEARCH_PATH` to the copy's root. |
| The Clio plugin, no Perforce access | Opens, as with plain USD (no context), or with `policy=offline`. |
| The Clio plugin and access to your Perforce server | Opens, and fetches what is missing. |

A copy needs every file the stage uses. With Clio, open the stage once at
the version you want to send: every file it needs is then on disk, in your
workspace (latest) or in the version store (a pinned version), with the
same layout as the depot.

---

## Picking up new versions

Clio remembers what it resolved, **in memory only**, so opening more
stages in the same session doesn't ask the server again. The server is
always the authority, and nothing survives the process. To see newer
submits in a running session:

```python
from pxr import Ar
Ar.GetResolver().RefreshContext(ctx)   # forget remembered answers for this context
stage.Reload()                         # USD re-opens layers whose files changed
```

## Asset information

```python
from pxr import Ar

resolver = Ar.GetResolver()
with Ar.ResolverContextBinder(ctx):      # outside a stage, bind the context yourself
    path = resolver.Resolve("/work/imagine/assets/crate/crate.usda")
    info = resolver.GetAssetInfo("/work/imagine/assets/crate/crate.usda", path)
info.version                       # "latest", "@18234", ...
info.resolverInfo["depotPath"]     # "//imagine/main/assets/crate/crate.usda"
```

`GetAssetInfo` makes no server calls. Outside the project, or with no Clio
context, it returns what USD's default resolver returns.

## Using the same rules from Python, without USD

`deda.clio` exposes the resolver core. It is the same C++ code the plugin
uses, so the answers match:

```python
from deda import clio

resolver = clio.AssetResolver(clio.Settings.parse(settings))
asset = resolver.resolve_path("/work/imagine/assets/crate/crate.usda")
asset["local_path"], asset["depot_path"], asset["pin"], asset["warning"]
```

`resolve_path` takes a local path, as USD does. `resolve` takes a
project-relative identifier (`clio.AssetIdentifier.parse("clio:/assets/crate/crate.usda?change=18234")`),
a notation for the Python API only; it is never written in layers. Both
are thread-safe and release the GIL.

## Errors

The resolver never raises into USD. If Clio cannot help, it prints a
warning and USD's default resolver handles the path.

| Message contains | Cause | Fix |
|---|---|---|
| `Perforce is not available ...; using the local file` | Server unreachable, not logged in, or refused | Nothing if the local file is fine; otherwise fix the connection or `p4 login`, then `RefreshContext` |
| `invalid resolver context` | Bad settings string | Check it with `clio.Settings.parse` |
| `$CLIO_RESOLVER_CONTEXT is invalid` | Bad default settings | Fix the variable |
| `timed out after N s` | A command took longer than `timeout` | Check the connection, or raise `timeout=` |
| Layer missing, no clio warning | The file is not on disk and not in Perforce at that version, or `verify` found nothing | Check the path and pin |

To confirm Clio is the active resolver:

```python
from pxr import Plug, Usd
Usd.Stage.CreateInMemory()     # makes USD create its resolver
assert Plug.Registry().GetPluginWithName("clioUsd").isLoaded
```

---

## Current limitations

1. **Saving layers does not check out files.** USD writes to the local
   file. Clio does not run `p4 edit`, take a lock or add new files yet
   (design §10.6). With Perforce's default workspace options, synced files
   are read-only, so check them out with `p4 edit` before saving.
2. **Fetches are not batched yet.** Each missing file is fetched on its
   own, one at a time per context, so large stages open slower than they
   will once fetches are batched (design §10.3).
3. **`latest` is remembered until `RefreshContext`.** The planned 30–60 s
   automatic re-check (design §8.3) is not implemented yet.
4. **Clio is the primary resolver.** Only one primary resolver can be
   active in a process, so Clio cannot yet be combined with another
   custom resolver (for example a DCC's or a studio's). No DCCs are
   targeted yet.
5. **Pins apply to the whole stage.** Layers cannot hold a pin on a single
   path (see [Choosing a version](#choosing-a-version-pins)).
6. **Project-rooted search paths may cost one server call per probe.**
   USD first looks for `assets/x.usda` next to the current layer. If that
   location is inside the project, Clio asks the server once, and
   remembers a "not found" answer until refresh. Relative paths avoid
   this.
7. **The connect time limit is Linux/macOS only** for now. The workspace
   lock and read-only stored files are implemented for Windows too, but
   are only tested on Linux.
8. **The version store is organised by server address.** The folder is
   named after the effective `P4PORT` (from the settings or the Perforce
   environment). Two spellings of one server's address get separate
   folders, which is safe. If a server is replaced by a different one at
   the same address, delete that server's folder in the version store,
   because its change numbers mean something else now.
9. **One plugin build per USD version.** A plugin built for 26.08 does not
   work in 25.08, and the other way round.
