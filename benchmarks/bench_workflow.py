"""Baseline timings for the Phase 1 workflow (design doc §8.7).

Runs against a throwaway p4d through an rsh: port, so it measures Clio and
Perforce on this machine, without network latency. Usage::

    CLIO_TEST_P4D=/path/to/p4d python benchmarks/bench_workflow.py [--big 100] [--small 2000] [--mb 1]

Prints a Markdown table.
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import stat
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

from deda import clio
from deda.clio.history import HistoryCache

PASSWORD = "Bench-12345!"


def timed(label: str, rows: list[tuple[str, float, str]], fn: Callable[[], object], note: str = "") -> object:
    start = time.perf_counter()
    result = fn()
    rows.append((label, time.perf_counter() - start, note))
    return result


def make_server(tmp: Path, p4d: str) -> tuple[str, str]:
    (tmp / "server").mkdir()
    port = f"rsh:{p4d} -r {tmp / 'server'} -L log -J off -i"
    tickets = os.fspath(tmp / "tickets")
    admin = clio.Connection(port=port, user="bench", tickets=tickets, prompt=lambda t, n: PASSWORD)
    admin.run_or_throw("passwd")
    admin.run_or_throw("login")
    admin.run_or_throw("configure", ["set", "net.parallel.max=8"])
    return port, tickets


def session(port: str, tickets: str, root: Path, name: str, threads: int) -> clio.Session:
    # The ticket comes from P4TICKETS, as in normal use: with the 'tickets'
    # setting Clio transfers serially.
    os.environ["P4TICKETS"] = tickets
    s = clio.connect(port=port, user="bench", depot="//depot/proj", root=os.fspath(root),
                     parallel_threads=threads, parallel_min_files=1)
    s.setup(name=name)
    return s


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--big", type=int, default=100, help="number of large files")
    parser.add_argument("--mb", type=int, default=1, help="size of each large file in MB")
    parser.add_argument("--small", type=int, default=2000, help="number of small files")
    args = parser.parse_args()
    p4d = os.environ.get("CLIO_TEST_P4D")
    if not p4d:
        raise SystemExit("Set CLIO_TEST_P4D to a p4d binary")

    tmp = Path(tempfile.mkdtemp(prefix="clio-bench-"))
    rows: list[tuple[str, float, str]] = []
    try:
        port, tickets = make_server(tmp, p4d)
        author = session(port, tickets, tmp / "author", "author", 4)
        payload = os.urandom(args.mb * 1024 * 1024)
        for i in range(args.big):
            path = author.paths.local_path(f"big/f{i:04}.bin")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload[i:] + payload[:i])
        for i in range(args.small):
            path = author.paths.local_path(f"small/d{i // 100:02}/f{i:05}.usda")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"#usda 1.0\n# {i}\n")
        total_mb = args.big * args.mb
        timed(f"save: add {args.big} x {args.mb} MB + {args.small} small files", rows,
              lambda: author.workspace.save(message="seed"))

        for threads in (1, 4):
            reader = session(port, tickets, tmp / f"reader{threads}", f"reader{threads}", threads)
            timed(f"get: {args.big} x {args.mb} MB, {threads} thread(s)", rows, lambda: reader.workspace.get("big"))
            label, seconds, _ = rows[-1]
            rows[-1] = (label, seconds, f"{total_mb / seconds:.0f} MB/s")
            reader.close()

        ws = author.workspace
        timed(f"get --preview: {args.small} small files (nothing to do)", rows,
              lambda: ws.get("small", preview=True))
        timed(f"status --no-disk: {args.small} files", rows, lambda: ws.status("small", check_disk=False))
        timed(f"status (with disk check): {args.small} files", rows, lambda: ws.status("small"))
        timed(f"lock: {args.small} files", rows, lambda: ws.lock("small"))
        for i in range(0, args.small, 10):
            path = author.paths.local_path(f"small/d{i // 100:02}/f{i:05}.usda")
            path.chmod(path.stat().st_mode | stat.S_IWRITE)
            path.write_text(f"#usda 1.0\n# edited {i}\n")
        timed(f"save: {args.small // 10} edited of {args.small} locked", rows,
              lambda: ws.save("small", message="edit"))

        target = "small/d00/f00000.usda"
        for i in range(2, 21):
            ws.lock(target)
            path = author.paths.local_path(target)
            path.chmod(path.stat().st_mode | stat.S_IWRITE)
            path.write_text(f"#usda 1.0\n# v{i}\n")
            ws.save(target, message=f"v{i}")
        HistoryCache.clear_all()
        timed("history: first page (20 of 21 versions)", rows, lambda: author.history.revisions(target))
        timed("history: cached page", rows, lambda: author.history.revisions(target))
        timed("history: freshness check, nothing new", rows, lambda: author.history.revisions(target, max_age=0))
        author.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"Clio {clio.__version__}, {platform.system()} {platform.release()}, {platform.processor() or platform.machine()}, "
          f"Python {platform.python_version()}, rsh: p4d (no network)\n")
    print("| Operation | Time | Note |\n|---|---:|---|")
    for label, seconds, note in rows:
        shown = f"{seconds * 1000:.0f} ms" if seconds < 1 else f"{seconds:.2f} s"
        print(f"| {label} | {shown} | {note} |")


if __name__ == "__main__":
    main()
