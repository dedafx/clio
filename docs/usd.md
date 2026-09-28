# Using Clio with USD

Clio includes a USD asset resolver for the `clio:` URI scheme. When USD
opens a layer whose path starts with `clio:`, the resolver gets the file
from Perforce (at the version you asked for), puts it on disk, and hands
USD the local path. You open stages as usual; files arrive as they are
needed.

This guide is for people using the `deda-clio` package. For how it is
built and why it is designed this way, see [building.md](building.md) and
[design.md](design.md) §10.

> **Status: v0.1 scaffold.** Reading works (latest, have, change, label
> and revision pins). Saving layers back to Perforce, batching many
> fetches into one request, and relative paths inside `clio:` layers are
> not done yet. See [Current limitations](#current-limitations).

Supported: Python 3.13, OpenUSD 26.08 (and 25.08 or later, with a plugin
built for that USD version). No DCC integrations yet.

---

## Quick start

```python
import os
from deda.clio import usd as clio_usd

# 1. Make the plugin visible to USD. Do this before pxr creates its
#    resolver (ideally set it in the environment before starting Python).
os.environ["PXR_PLUGINPATH_NAME"] = str(clio_usd.plugin_path())

from pxr import Usd

# 2. Describe where the project lives in Perforce and on disk.
settings = (
    "depot=//imagine/main;"          # depot path of the project root
    "root=/work/imagine;"            # where your client workspace maps it
    "client=sam_imagine;"            # your Perforce workspace
    "port=ssl:perforce:1666;user=sam"
)
ctx = clio_usd.create_context(settings)

# 3. Open a stage. Layers are fetched from Perforce as USD needs them.
stage = Usd.Stage.Open("clio:/shots/sq010/sh0100/shot.usda", ctx)
```

In a layer, refer to other assets by `clio:` path:

```usda
#usda 1.0
(
    subLayers = [@clio:/assets/crate/crate.usda@]
)
```

Before you start:

* **Log in once with `p4 login`.** The resolver never asks for a
  password, because it runs on USD's worker threads where a prompt would
  hang. It uses your existing Perforce ticket.
* **Your client workspace must map `depot` to `root`.** For example,
  `//imagine/main/...` must map to `/work/imagine/...`. Clio does not
  create workspaces yet.

---

## What happens when a stage opens

Here is what happened in the two scenarios in the test suite
(`tests/python/test_usd_resolver.py`), step by step.

The depot contains:

```
//depot/proj/shots/sh010/shot.usda      subLayers = [@clio:/assets/crate/crate.usda@]
//depot/proj/assets/crate/crate.usda   def "Crate" { int version = 2 }   (revision #2; #1 had version = 1)
```

### Scenario 1: open at the latest version

```python
ctx = Ar.GetResolver().CreateContextFromString("clio", "depot=//depot/proj;root=/ws;...")
stage = Usd.Stage.Open("clio:/shots/sh010/shot.usda", ctx)
```

1. **USD finds the resolver.** The plugin's `plugInfo.json` registers
   `ClioResolver` for the `clio` scheme. USD sends every `clio:` path to it
   and every other path to its normal resolver. The two don't interfere.
2. **The context is created.** `CreateContextFromString("clio", ...)`
   validates the settings string and returns a context holding it.
   `Usd.Stage.Open` binds that context while it opens layers, including on
   USD's worker threads.
3. **USD resolves the root layer.** For `clio:/shots/sh010/shot.usda` the
   resolver:
   * reads the context and picks the **pin**: the path has none, and the
     context says `latest` (the default);
   * maps the path to the depot, giving `//depot/proj/shots/sh010/shot.usda`;
   * runs `p4 sync -q //depot/proj/shots/sh010/shot.usda#head` through
     the Perforce C++ API, inside the USD process;
   * returns the local file, `/ws/shots/sh010/shot.usda`.
4. **USD reads the layer** from that local file and finds the sublayer
   `@clio:/assets/crate/crate.usda@`.
5. **USD resolves the sublayer.** It goes through the same steps:
   `p4 sync` of `crate.usda#head`, which returns `/ws/assets/crate/crate.usda`.
6. **The stage is composed.** `/Crate.version` is `2`, and both files are
   now in your workspace, exactly as if you had run `p4 sync` on them.

### Scenario 2: open at a changelist

Same stage, but the context pins the whole stage to the changelist that
submitted revision #1 (say change 1):

```python
ctx = Ar.GetResolver().CreateContextFromString("clio", "depot=//depot/proj;root=/ws;...;pin=@1")
stage = Usd.Stage.Open("clio:/shots/sh010/shot.usda", ctx)
```

1. The pin is now `@1`, a **historical** version. Clio does **not** sync
   your workspace to it. Old versions never overwrite the files you work
   on.
2. Clio checks the **version store**, a read-only folder of old versions:
   ```
   <store>/<server>/<depot>/change-1/shots/sh010/shot.usda
   ```
   If the file is not there, Clio runs
   `p4 print -q -o <temp> //depot/proj/shots/sh010/shot.usda@1` and moves
   it into place. Readers never see a half-written file.
3. The sublayer is fetched the same way, into `.../change-1/assets/crate/crate.usda`.
4. The stage shows `/Crate.version == 1`. Your workspace still has
   revision #2. A second stage opened at `latest` in the same session
   would show `2`.

Files in the version store are reused later, even by other processes,
because a changelist snapshot never changes. Labels are the exception
(below).

---

## Choosing a version: pins

A **pin** says which version to use.

| Pin | Where it is set | Meaning | Files come from |
|---|---|---|---|
| `latest` (default) | context `pin=latest` | Head revision | `p4 sync` into your workspace |
| `have` | context `pin=have` | Whatever is already in your workspace. No server call. | Your workspace |
| `@<change>` | context `pin=@18234`, or path `?change=18234` | The depot as of that changelist | Version store (`p4 print`) |
| `@<label>` | context `pin=@approved`, or path `?label=approved` | The revisions tagged by a Perforce label | Version store, fetched again once per process |
| `#<rev>` | path only: `?rev=3` | One file's revision | Version store |

* A pin on the path wins over the context pin:
  `@clio:/assets/crate/crate.usda?change=18100@` stays at change 18100
  even when the stage is opened at `latest`.
* `#<rev>` applies to one file, so it can only be written on a path, never
  as a context pin.
* **Labels can be moved** by a Perforce admin. Clio therefore fetches a
  label's files from the server again the first time each process uses
  them, and never trusts an older copy on disk.

## Settings reference

The context string is `key=value` pairs separated by `;`. Order doesn't
matter, and the values may not contain `;` or `=`.

| Key | Required | Meaning |
|---|---|---|
| `depot` | yes | Depot path of the project root, e.g. `//imagine/main` (no trailing `/`, no wildcards) |
| `root` | yes | Local folder your client workspace maps `depot` to |
| `client` | usually | Your Perforce workspace (client) name |
| `port`, `user` | no | Perforce server and user. Default: your Perforce environment (`P4PORT`, `P4USER`, `P4CONFIG`, ...) |
| `tickets` | no | Ticket file, if not the default (`P4TICKETS` or `~/.p4tickets`) |
| `store` | no | Version store folder. Default: `$CLIO_VERSION_STORE`, else `$XDG_CACHE_HOME/clio/versions`, else `~/.cache/clio/versions` (`%LOCALAPPDATA%\clio\versions` on Windows) |
| `pin` | no | `latest` (default), `have`, `@<change>`, `@<label>` |
| `policy` | no | `sync` (default), `verify` or `offline` (below) |
| `timeout` | no | Seconds before a Perforce command is cancelled. Default `120`, `0` = none |

You can check a string in Python before handing it to USD:

```python
from deda import clio
clio.Settings.parse(settings)   # raises clio.ConfigError with the reason
```

### Policies

| Policy | Contacts the server? | Use it for |
|---|---|---|
| `sync` | Yes, to fetch what is missing or out of date | Everyday work |
| `verify` | No. A resolve fails if the file is not already on disk at the required version. | Render farms and reproducible builds, where a prefetch step has already fetched everything |
| `offline` | No. Uses whatever is on disk. | Working without a connection |

### A default context for every stage

Set `CLIO_RESOLVER_CONTEXT` to a settings string before USD starts. Stages
opened without an explicit context use it. It is read once per process;
if it is invalid, USD prints one warning and `clio:` paths do not resolve.

---

## Picking up new versions

Clio remembers what it resolved, **in memory only**, so opening more
stages in the same session doesn't ask the server again. The Perforce
server is always the authority, and nothing Clio remembers survives the
process. To see newer submits in a running session:

```python
from pxr import Ar
Ar.GetResolver().RefreshContext(ctx)   # forget remembered answers for this context
stage.Reload()                         # USD re-opens layers whose files changed
```

`RefreshContext` also tells USD that results for this context may have
changed. A new process always starts fresh.

## Asset information

```python
from pxr import Ar

resolver = Ar.GetResolver()
with Ar.ResolverContextBinder(ctx):          # outside a stage, bind the context yourself
    path = resolver.Resolve("clio:/assets/crate/crate.usda")
    info = resolver.GetAssetInfo("clio:/assets/crate/crate.usda", path)
info.version                       # "latest", "@18234", ...
info.resolverInfo["depotPath"]     # "//imagine/main/assets/crate/crate.usda"
```

`GetAssetInfo` itself makes no server calls. Calls made without a bound
context (and without `CLIO_RESOLVER_CONTEXT`) return empty results and
print a one-time "no resolver context" warning.

## Using the same rules from Python, without USD

`deda.clio` exposes the resolver core directly. It is the same C++ code
the USD plugin uses, so the answers match:

```python
from deda import clio

resolver = clio.AssetResolver(clio.Settings.parse(settings))
asset = resolver.resolve(clio.AssetIdentifier.parse("clio:/assets/crate/crate.usda?change=18234"))
asset["local_path"], asset["depot_path"], asset["pin"]
```

It is thread-safe and releases the GIL while it works.

## Errors

The resolver never raises into USD. A failed resolve prints a USD warning
and the layer is reported as missing, as USD does for any missing file:

```
Warning: clio: resolving 'clio:/assets/crate/crate.usda' failed: p4 sync failed: ...
```

| Message contains | Cause | Fix |
|---|---|---|
| `Clio does not prompt for passwords` / `password (P4PASSWD) invalid or unset` | No valid ticket | `p4 login` (or set `tickets=`) |
| `no resolver context` | Stage opened without a clio context and no `CLIO_RESOLVER_CONTEXT` | Pass a context, or set the variable |
| `invalid resolver context` | Bad settings string | Check it with `clio.Settings.parse` |
| `timed out after N s` | Server slow or unreachable | Check the connection, or raise `timeout=` |
| Layer missing, no clio warning | The file doesn't exist at that version, or `verify`/`offline` found nothing on disk | Check the path and pin |

To confirm the plugin is loaded:

```python
from pxr import Ar
assert "clio" in Ar.GetRegisteredURISchemes()
```

---

## Current limitations

These are known gaps in the v0.1 scaffold, with tests or TODOs in the
code:

1. **Relative paths inside a `clio:` layer are not fetched.** USD anchors
   `@./geo.usda@` to the parent layer's *local file path*, so USD's
   normal resolver handles it, not Clio. It works only if the file is
   already on disk, and never for historical pins. **For now, write every
   layer and asset reference as a `clio:` path.** Fixing this is the open
   question in design doc §10.4. It is tracked by an expected-failure test
   (`test_relative_sublayer_inside_clio_layer_is_fetched`).
2. **Saving layers does not check out files.** USD writes to the local
   file; Clio does not run `p4 edit`, take a lock or add new files yet
   (design §10.6). With Perforce's default workspace options, synced files
   are read-only, so check them out with `p4 edit` before saving.
3. **Fetches are not batched yet.** Each missing layer is fetched on its
   own, one at a time per context. Large stages open slower than they will
   once fetches are batched (design §10.3).
4. **`latest` is remembered until `RefreshContext`.** The planned 30–60 s
   automatic re-check (design §8.3) is not implemented yet.
5. **Non-layer assets** (textures, volumes) resolve through Clio only if
   their asset paths are `clio:` paths. The renderer must also accept the
   local path Clio returns. That is the normal case, but it hasn't been
   tested with specific renderers.
6. **One plugin build per USD version.** A plugin built for USD 26.08 will
   not load in 25.08, and the other way round. Use the wheel or build that
   matches your USD.
