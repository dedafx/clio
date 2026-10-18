# Everyday workflow: get, lock, save, branch

This guide covers the `clio` command line and the Python API for the
day-to-day work of an artist: getting files, locking them, saving changes,
and seeing who is doing what. For USD, see [usd.md](usd.md). For the design
behind it, see [design.md](design.md) §5, §7, §9, §11 and §12.

## Words

| Clio | What it means | Perforce |
|---|---|---|
| **get** | Copy files from the server to your disk | `p4 sync` |
| **lock** | Make sure nobody else changes a file while you work on it | `p4 edit` + `p4 lock` |
| **save** | Send your changes to the server, with a message | `p4 reconcile` + `p4 submit` |
| **discard** | Throw away your local changes | `p4 revert` |
| **workspace** | Your copy of the project on disk | client workspace |
| **version** | `latest`, `@1234` (change), `@approved` (label), `#3` (revision) | revision specifier |
| **branch** | A separate line of work, such as a task branch for one asset | stream |
| **update** | Bring the parent branch's changes into your branch | `p4 merge` + `p4 resolve` |
| **publish** | Copy your branch's saved work up to its parent | `p4 copy` |
| **draft** | Work shared without saving it | shelved change |

## First-time setup

```bash
clio --port ssl:perforce:1666 --user sam setup --root D:/work/imagine --depot //imagine/main
```

`setup` logs you in if needed (Clio never stores your password; Perforce
keeps a ticket) and creates a workspace named `sam_<computer>_<project>`
that maps the project to your folder. Files are read-only until you lock
them, so nothing is changed by accident.

Instead of options on every command, put the settings in a config file.
Settings are read in this order, later ones winning:

1. `$CLIO_SITE_CONFIG` (for the studio),
2. your user file (`%APPDATA%\clio\config.toml` on Windows,
   `~/.config/clio/config.toml` elsewhere, or `$CLIO_USER_CONFIG`),
3. `.clio.toml` in the current folder or a folder above it,
4. the usual Perforce settings (`P4PORT`, `P4USER`, `P4CLIENT`, `P4CONFIG`),
5. command options.

```toml
# ~/.config/clio/config.toml
port = "ssl:perforce:1666"
user = "sam"

[projects.imagine]
depot = "//imagine/main"
root = "D:/work/imagine"
client = "sam_ws01_imagine"
```

```bash
clio --project imagine status
```

| Setting | Meaning | Default |
|---|---|---|
| `project` | Picks a `[projects.<name>]` table; names new workspaces | |
| `port`, `user`, `client` | Server, user and workspace | Perforce environment |
| `tickets` | Ticket file | Perforce's default |
| `depot` | Project root in the depot, for example `//imagine/main` | from the workspace |
| `stream` | Stream for new workspaces, instead of `depot` | |
| `root` | Local folder of the project | from the workspace |
| `parallel_threads` | Threads for transfers (1 turns it off) | 4 |
| `parallel_min_files` | Fewest files for a parallel transfer | 8 |
| `poll_interval` | Seconds history stays fresh before a server check | 30 |
| `backup_dir` | Where `discard` keeps copies | `%LOCALAPPDATA%\clio\backups`, `~/.local/state/clio/backups` |
| `cross_branch_locks` | On lock, someone has the file open in a related branch: `warn`, `block` or `off` | `warn` |
| `connect_timeout`, `timeout` | Seconds to reach the server, and per command | 10, 120 |

## The command line

Paths are relative to the current folder, as in any command. `//...` paths
are depot paths. A folder means everything in it; no path means the whole
project. Add `--json` before the command for output that tools can read.

```bash
clio get                          # latest of everything
clio get props/crate --preview    # what would be transferred, nothing changed
clio get props/crate --version @approved

clio lock props/crate/crate.ma    # lock before you edit
clio status                       # what you changed, what is locked or out of date
clio who props/crate              # who has these files locked or open
clio save -m "Crate: damage pass on lid" props/crate
clio unlock props/crate/crate.ma  # release; unchanged files are closed again
clio discard props/crate --yes    # throw away changes (a copy is kept)

clio history props/crate/crate.ma # versions of a file
clio activity props               # recent changes in a folder
clio login | logout | whoami
clio doctor                       # is this machine set up well? (see below)
```

`save` finds what you changed under the paths you give (new, edited and
deleted files), even files you edited without locking them, and saves them
together in one change. If the server refuses it (a trigger, or someone
saved a newer version first), nothing on your disk is changed, and Clio
says what to do next.

**Exit codes:** 0 success, 1 other error, 2 wrong usage, 3 not logged in,
4 server unreachable, 5 locked by someone else, 6 out of date, 7 nothing to
save, 8 refused by the server, 9 cancelled or not confirmed, 10 outside the
workspace, 11 configuration error, 12 conflicts need a choice, 13 a
version manifest cannot be made or used.

### `clio doctor`

`clio doctor` checks the configuration, the server, your login (and when
the ticket expires), the workspace, the workspace options, the server's
parallel-transfer setting, the file-type map, USD and the Clio plugin, and
the version store. Each line is `ok`, `warn` (it works, but slower or riskier
than it needs to), `FAIL` or `skip` (could not be checked, for example
because the server is unreachable), with what to do about it. It exits with 1
only if something failed.

Clio does not change the server. For the admin, `clio doctor --typemap`
prints the recommended file-type map: exclusive locks (`+l`) for files that
cannot be merged, no server compression (`+F`) for files that are already
compressed, and plain text for text. Paste it into `p4 typemap`.

## Branches and drafts

Branches need a **streams depot** (design doc §7): create the workspace
with `clio setup --stream //imagine/main`. A branch is usually a **task
branch** for one piece of work, made from `main` and published back to it.

```bash
clio branch create crate-damage        # branch main and switch to it
clio lock props/crate/crate.ma         # work and save as usual
clio save -m "Crate: damage pass" props/crate

clio branch update                     # bring in what changed on main
clio branch publish -m "Crate damage"  # copy your saved work up to main
clio branch switch main
clio branch retire crate-damage        # when done (history stays)
clio branch list
```

* **Switching** re-points your workspace and transfers only files that
  differ. It is refused while files are open; `--share-open-files` shares
  them as a draft first, so nothing is lost.
* **Update** takes files that changed only on the parent as they are.
  A file changed on **both** sides cannot be merged (art files are not
  text), so you choose: `--mine PATH` keeps your version, `--theirs PATH`
  takes the parent's, `--both PATH` keeps yours and saves the parent's
  next to it as `name.theirs.ext` to compare by hand. `--all mine|theirs|both`
  settles every conflict. Without a choice for each conflict, nothing is
  changed and the conflicts are listed (exit code 12). An update is saved
  as one change, or not at all.
* **Publish** copies saved work from your branch to its parent. It is
  refused while the branch is behind the parent (update first) or while
  you have files open (save or discard them: only saved work is
  published). It runs in a hidden publishing workspace on the server side,
  so nothing is downloaded and your workspace is not touched.
* **Locks across branches.** Perforce locks only stop two people in the
  same branch. When you lock, Clio also looks at the same files in the
  parent, sibling and child branches and warns if someone has them open
  there. A project can make this block the lock instead
  (`cross_branch_locks = "block"`).

**Drafts** share work without saving it: a lead can review it, or someone
else can pick it up.

```bash
clio draft share -m "WIP lid" props/crate   # files stay open for you
clio draft share -m "WIP lid" --close props/crate   # the draft keeps the work
clio draft list [--all]
clio draft get 1234                         # copy into your workspace, open
clio draft drop 1234
```

## Python

```python
from deda import clio

with clio.connect("imagine") as s:
    ws = s.workspace

    plan = ws.get("props/crate", preview=True)
    print(plan.file_count, plan.bytes_to_transfer)
    ws.get("props/crate", progress=print)

    try:
        ws.lock("props/crate/crate.ma")
    except clio.LockedByOtherError as e:
        print(e.message, e.hint)            # "props/crate/crate.ma is locked by kim"

    # ... edit in the DCC ...

    result = ws.save("props/crate", message="Crate: damage pass on lid")
    print(result.change.number, result.added, result.edited, result.deleted)

    for f in ws.status("props/crate"):
        print(f.asset_path, f.state.value, f.locked_by)

    page = s.history.revisions("props/crate/crate.ma", limit=20)
    for r in page.items:
        print(r.rev, r.change, r.user, r.description)
    older = s.history.revisions("props/crate/crate.ma", before=page.next_cursor)

    # Branches (streams) and drafts.
    s.branches.create("crate-damage")             # and switch to it
    try:
        s.branches.update()
    except clio.ConflictError as e:
        choices = {c.asset_path: "mine" for c in e.conflicts}
        s.branches.update(resolutions=choices)
    s.branches.publish(message="Crate damage")
    draft = s.drafts.share("props/crate", message="WIP lid")
```

API paths are **asset paths**, relative to the project root
(`props/crate/crate.ma`), or depot paths, or absolute local paths. Results
are immutable dataclasses (`FileStatus`, `SyncPlan`, `SyncReport`, `Lock`,
`SaveResult`, `Change`, `Revision`, `HistoryPage`) and give each file as an
asset path, a depot path and a local path.

**Errors** are `clio.ClioError`. Workflow errors carry `message`, `hint`
and `p4_messages`: `AuthError`, `ServerUnavailableError`,
`LockedByOtherError` (`path`, `user`, `workspace`), `OutOfDateError`
(`paths`), `NothingToSaveError`, `ServerPolicyError` (`change`),
`CancelledError`, `ConfirmationRequiredError`, `NotInWorkspaceError`.
Perforce warnings such as "file(s) up-to-date" are never raised.

**Background work and events.** `s.submit_bg(ws.get, "environments/forest")`
runs a call on a worker thread and returns a future; each thread uses its
own Perforce connection. `ws.get(..., cancel=token)` stops before the next
Perforce command after `token.cancel()`. `s.events.subscribe(name, fn)`
reports `files_synced`, `file_locked`, `file_unlocked`,
`change_submitted` and `files_discarded`.

**History is cached** in memory, per server and user, for the process
(design §9). A page is served from the cache for `poll_interval` seconds;
after that, one cheap server call checks for anything newer, and only the
new part is fetched. Your own saves are picked up at once. Actions (lock,
save, get, status) always ask the server.

## Current limitations

1. **Within one branch, `save` does not merge.** If someone saved a newer
   version of a file first, `save` refuses and changes nothing. Keep a
   copy, get the latest version and redo the change, or work in a task
   branch, where `branch update` offers keep mine / take theirs / keep both.
2. **Branch updates and publishes are whole-branch.** You cannot update or
   publish only some files. Drafts can only be taken into the branch they
   were made in.
3. **"Keep both" copies are not in Perforce.** `name.theirs.ext` is written
   next to your file for comparison. Delete it when done: a later `save` of
   that folder would add it.
4. **Progress is coarse.** `get` reports the start (with totals from a
   preview) and the end, not each file, and cancelling takes effect between
   Perforce commands.
5. **Parallel transfers need the Perforce ticket from the usual place**
   (the default ticket file or `P4TICKETS`). With the `tickets` setting,
   transfers run one at a time, because Perforce's extra transfer
   connections do not see that ticket file.
6. **Status is live.** Every `status` asks the server; with the disk check
   it also compares every file on disk, which is the slow part for large
   folders (see [benchmarks.md](benchmarks.md)). `--no-disk` skips it.
7. **Workspaces must map the project root one to one** (as `clio setup`
   creates them). Remapped or partial views are not supported.
8. **Discard** reverts opened files only. Files changed on disk without
   being locked are left alone (`clio get --force` restores them).
