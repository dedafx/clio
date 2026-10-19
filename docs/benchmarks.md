# Benchmarks

Baseline timings for the Phase 1 workflow (design doc §8.7), from
`benchmarks/bench_workflow.py`. Run it with:

```bash
CLIO_TEST_P4D=/path/to/p4d python benchmarks/bench_workflow.py --big 100 --mb 4 --small 2000
```

It starts a throwaway `p4d` through an `rsh:` port, so there is no network:
the numbers show Clio's and Perforce's own costs on one machine.

## Baseline (2026-09-29)

Clio 0.1.0, Windows 11, Intel64 Family 6 Model 165 Stepping 5, GenuineIntel, Python 3.13.9, rsh: p4d (no network)

P4D 2026.1, Clio built with MSVC 2022, the server and workspaces in `%TEMP%`.

| Operation | Time | Note |
|---|---:|---|
| save: add 100 x 4 MB + 2000 small files | 37.74 s |  |
| get: 100 x 4 MB, 1 thread(s) | 4.30 s | 93 MB/s |
| get: 100 x 4 MB, 4 thread(s) | 1.57 s | 254 MB/s |
| get --preview: 2000 small files (nothing to do) | 25 ms |  |
| status --no-disk: 2000 files | 122 ms |  |
| status (with disk check): 2000 files | 10.58 s |  |
| lock: 2000 files | 10.80 s |  |
| save: 200 edited of 2000 locked | 19.33 s |  |
| history: first page (20 of 21 versions) | 32 ms |  |
| history: cached page | 0 ms |  |
| history: freshness check, nothing new | 30 ms |  |

## What the numbers say

* **Every step is one Perforce command, whatever the number of files.** A
  timing of each command (fstat, edit, reconcile, submit) shows no per-file
  overhead in Clio. The time is Perforce's own per-file work.
* **Parallel transfers pay off**: getting 400 MB went from about 95 MB/s
  to 250-290 MB/s with 4 threads (two runs). Parallel transfers are on by default
  (`parallel_threads = 4`); the server must allow them (`net.parallel.max`).
* **Reading server state is cheap**: `status --no-disk` for 2,000 files,
  a preview, and history checks each take tens of milliseconds, and cached
  history pages take no server call at all.
* **Anything that touches every file on disk costs about 5 ms per file**
  on this machine: the disk check in `status` and `save` (`p4 reconcile`
  hashes each local file and compares it with the server), `lock`
  (`p4 edit` makes each file writable), and `submit`. For 2,000 files that
  is 10 to 20 seconds. This is the target for phase 3: scope the disk
  check to files whose size or time changed, and compare with a Linux
  machine and a TCP server, to see how much of the per-file cost is Windows
  file-system overhead (for example real-time antivirus scanning).

## Phase 3: the disk check (2026-10-12)

Same machine and setup as the baseline (20 x 4 MB large files this run).

| Operation | Baseline | Now |
|---|---:|---:|
| status (with disk check): 2000 files | 10.58 s | 0.33 s |
| save: 200 edited of 2000 locked | 19.33 s | 8.28 s |

**Where the time went.** `stat` on a file takes about 3.5 microseconds;
opening it takes about 2.7 ms here (Windows real-time virus scanning).
`p4 reconcile -e` and `-d` open every file, and `-m` (modtime first) made no
difference. `reconcile -a` (new files) does not open known files and
takes 60 ms for 500 files.

**What changed.** `status` and `save` take the sizes from `fstat -Ol`, `stat`
the files themselves, and send only the suspicious ones to
`reconcile -e -d`: missing, writable (Perforce leaves unopened files
read-only), or a different size. Files already opened are skipped. Size
counts for binary files, and for text files only where the client does not
convert line endings (on Windows only a smaller file counts). Perforce
still decides every result.

**Known limit.** A text file changed on Windows and made read-only again
without getting smaller is not noticed by `status`. Locked or edited files
(the normal workflow) are always seen, as they are opened.

**What is left, and is not Clio's.** `p4 edit` costs about 5 ms a file in
the server even with `-k` (no file changes), so `lock` of 2,000 files stays
at 10.8 s; `p4 submit` is about 16 ms a file (read, hash, upload). Both
need a TCP server and a Linux machine to compare before drawing a
conclusion.

## Phase 3: what was not built (2026-10-18)

Same machine, Python 3.13.16, 20 x 4 MB large files and 2,000 small ones.

| Operation | After the disk check | Now |
|---|---:|---:|
| status --no-disk: 2000 files | 122 ms | 102 ms |
| status (with disk check): 2000 files | 330 ms | 355 ms |
| get: 20 x 4 MB, 4 threads | | 500 ms (160 MB/s) |

The connection pool, the miss coalescer and the resolver cache time change
the resolver, not these Python workflow numbers, so they are unchanged
within noise. Tests cover what they do: twelve threads that miss at once run
one `p4 sync`, eight threads asking for one file run one resolve, and a
`latest` answer is trusted for `cache_ttl` and then checked again
(`cpp/tests/test_resolver_cache.cpp`, `cpp/tests/test_coalescer.cpp`).

**C++ hashing, scan and diff: not justified.** The only per-file Python work
left in `status` and `save` is the `stat` loop of `Workspace._disk_changes`.
On 50,000 files it takes 0.52 s: about 10 microseconds a file, so about 1 s for
100,000 files. `fstat -Ol` for 2,000 files takes about 100 ms of that
call, which puts 100,000 files near 5 s, so the loop is under a fifth of a large
`status`. A parallel C++ walk could make the loop 3 to 5 times faster, but
the whole call would be about 15% faster, below the 3x end-to-end bar the
design sets (§8.4). Hashing is not on any Clio path any more: Perforce does
it for the few suspicious files only.

**Status cache and persistent store: not built.** `status` is two server
commands and 100 ms for 2,000 files; a cache would add a way to show stale
lock and edit state for a saving of tens of milliseconds. The UI view models
of phase 5 (§9) are where a time-bounded status cache belongs, with the
request pipeline that needs it. Revisit with a TCP server and a Linux
machine (see the note above).
