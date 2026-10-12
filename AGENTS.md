# AGENTS.md

Guidance for AI agents working in the Clio repository. Human-facing docs are
in [README.md](README.md) and `docs/`; this file is the short version of what
you need before changing code.

## Project overview

Clio is a Perforce wrapper that lets artists and tools get, lock, save and
branch art assets, and a USD asset resolver that fetches files from Perforce
before USD loads them. USD files keep ordinary paths, so they open with or
without Clio.

* **`clio_core`** (C++17, `cpp/clio_core`): Perforce access over the P4 C++
  API, `clio:` identifiers, pins, resolution, settings. No USD, no Python.
* **`deda.clio._core`** (`cpp/python`): nanobind extension (stable ABI,
  `cp313-abi3`) over `clio_core`.
* **`deda.clio`** (`src/deda/clio`): the Python workflow layer (session,
  workspace, branches, drafts, history) and the `clio` CLI. It talks to
  Perforce through a `Backend` (`backends/`): `CoreBackend` for real use,
  `fake.py` for unit tests.
* **`clioUsd`** (`cpp/clio_usd`): the `clio:` ArResolver plugin, built per
  USD version.

`deda` is a PEP 420 namespace package: there is no `src/deda/__init__.py`.
Do not add one.

Targets: Python 3.13, USD 26.08 (25.08 or later also builds).
Read [docs/design.md](docs/design.md) before a non-trivial change; the
roadmap (§15) says which phase is done (0 to 2) and what is next.

## Design rules that decide most questions

1. **The server is the authority.** Lock, save, get, publish and branch
   actions always ask Perforce; they never decide from a cache. Caches are
   in memory, bounded by time, and lost at exit (design §8.3). Do not add a
   persistent cache without a benchmark that calls for it.
2. **One Perforce command per step, whatever the file count.** Batch files
   into one command (chunk very long lists). No per-file commands in a loop.
3. **Python orchestrates, native code moves bytes.** Add C++ only when a
   benchmark shows a real gain (design §8.4). Measure first
   (`benchmarks/bench_workflow.py`, results in `docs/benchmarks.md`).
4. **Errors say what happened and what to do next.** Raise the specific
   exception in `errors.py` with a `hint`; never leak raw P4 output as the
   only message.
5. **Binary assets, not text merging.** Conflicts are resolved all or nothing
   (keep mine / take theirs / keep both); see design §7.2.

## Commands

Use Python 3.13 and a venv. Perforce's `p4d` is needed for most tests.

```bash
# Build and test (see docs/building.md for dependencies)
cmake --preset dev && cmake --build --preset dev && ctest --preset dev
cmake --preset dev-usd                       # also the USD plugin ($PXR_ROOT)
pip install .                                # Python package + extension
CLIO_BUILD_USD=ON PXR_ROOT=... pip install . # with the USD plugin

export CLIO_TEST_P4D=/path/to/p4d            # tests needing a server skip without it
python -m pytest tests/python
python benchmarks/bench_workflow.py --big 20 --mb 4 --small 2000
```

Python tests import the **installed** `deda.clio`, not `src/`. After editing
Python files either reinstall (`pip install .`) or copy the changed files
into the venv's `site-packages/deda/clio/` while iterating, and make sure
the installed copy matches the repo before trusting a result. The full
Python suite takes about 2.5 minutes because every test starts a `p4d`.

### Windows development notes

* Run CMake and pip from an x64 Native Tools prompt (after `vcvars64.bat`).
* Use the `_dyn` P4API (`/MD`), and OpenSSL from vcpkg
  (`x64-windows-static-md`). See `docs/building.md`.
* Use the same CPython as Dedaverse: the latest 3.13 patch release from
  python.org (3.13.16 as of 2026-10-12, `py -3.13`). Keep Clio's venv in
  `.venv` (git-ignored) and rebuild it when the patch release moves:
  `py -3.13 -m venv .venv`, then `pip install pytest` and `pip install .`.
  Older python.org 3.13 installs (such as 3.13.4) have a `pyconfig.h` that
  defines `Py_GIL_DISABLED` and links fail wanting `python313t.lib`; upgrade
  rather than working around it.
* The extension is abi3 (`cp313-abi3`), so a wheel built on one 3.13 patch
  release loads on any later 3.13; USD's Python modules built against an
  earlier 3.13 patch release also load.
* Per-file costs here are dominated by file opens (about 3 ms each with
  real-time virus scanning) while `stat` is microseconds. Prefer `stat`
  over opening files, and do not conclude a Perforce command is slow before
  checking whether it opens every file (design §8, `docs/benchmarks.md`).
* Windows files in this repo may use CRLF. Keep each file's existing line
  endings when you edit it.

## Code standards

**Python**
* Python 3.13 syntax and type hints on new code: `str | None`, not
  `Optional[str]`. `from __future__ import annotations` as in the existing
  modules.
* Records are `@dataclass(frozen=True, slots=True)`. Avoid per-file Python
  objects on bulk paths.
* Catch specific exceptions; never a bare `except:`.
* Use `pathlib.Path` for paths. Perforce paths (depot paths, `clio:` ids)
  are strings with `/`; escape `@ # % *` with `_paths.escape` when building
  specs from user paths.
* Lazy imports keep `clio --help` fast: import `_core` and the CLI framework
  only where needed.
* Comments explain why, not what. Docstrings say what a caller gets and
  what it raises.

**C++**
* C++17. P4API and OpenSSL are linked statically with hidden symbols so the
  Python module and the USD plugin can share a process. Keep that intact.
* Release the GIL for any binding call that can touch the network or disk.
* One `ClientApi` per thread at a time.

## Testing

* Add or update tests for every behavior change; do not relax or remove a
  test to make a change pass. Fix the code.
* `tests/python/conftest.py` starts a throwaway `p4d` over an `rsh:` port
  (no network listener); use its `make_session` / `make_stream_session`
  fixtures rather than starting servers yourself. Each test file defines
  `alice` and `bob` sessions, and `test_workflow.py` has the `seed` and
  `write` helpers.
* Pure logic gets a unit test without a server (see `test_services_unit.py`,
  which uses the fake backend).
* USD tests need `pxr` importable and are skipped otherwise; on Windows put
  `%PXR_ROOT%\bin` and `%PXR_ROOT%\lib` on `PATH`.
* C++ tests are in `cpp/tests` (doctest) and also use `p4d`.
* State what you ran and what you could not run (for example "not tested on
  Linux or against a TCP server") in the PR description.

## Documentation

Keep docs in step with code: `docs/workflow.md` (everyday use, CLI),
`docs/usd.md`, `docs/building.md`, `docs/benchmarks.md` (numbers, with the
date and machine), and `docs/design.md` (decisions, "as built" notes, the
roadmap status line). Prefer plain words and examples an artist can follow.

## Git workflow

* Branches: `feat/`, `fix/`, `update/`, `chore/` (for example
  `feat/phase3-performance`). Do not commit to `main`; open a PR.
* Commit messages: `<type>(<scope>): <description>`, with a body saying why.
  Types in use: `feat`, `fix`, `perf`, `docs`, `test`, `chore`.
* Stacked work is fine: base a PR on the previous PR's branch and say so in
  the description; retarget to `main` when the parent merges.
* Do not push, open a PR, or touch other repositories unless asked.

## Gotchas

* `p4 reconcile -e/-d` opens and hashes every file; `status` and `save` stat
  files first and send only suspicious ones (`Workspace._disk_changes`).
  Keep any new "what changed on disk" code on that path.
* Unopened synced files are read-only in Perforce; a writable file is a
  clue. Size is a clue only where the type keeps its size on disk
  (`_size_is_suspect`).
* `p4 edit` and `p4 submit` cost a fixed amount per file in the server and
  cannot be reduced by Clio; do not spend effort optimizing around them
  without a TCP and Linux comparison.
* Tickets: with the `tickets` setting Clio transfers serially; parallel
  transfer needs the ticket from `P4TICKETS`. See design §6.1.
* Cross-branch lock checks (`cross_branch_locks = warn | block | off`) add one
  `fstat`; keep lock at 2 to 3 server round trips.
