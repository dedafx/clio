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
