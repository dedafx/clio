"""View models: file lists and history panels over the request pipeline
(design doc §9.5), and the server-call budgets of §9.6."""

from __future__ import annotations

import stat
import threading
import time

import pytest

from deda import clio
from deda.clio.models import FileState, Freshness
from deda.clio.views import FileListView, HistoryView
from deda.clio.views._handlers import _fstat_scope
from deda.clio.views._pipeline import PRIORITY_PREFETCH, PRIORITY_VISIBLE, RequestPipeline


# --- the pipeline, without a server ------------------------------------------


@pytest.fixture
def pipeline():
    p = RequestPipeline(window=0.05)
    yield p
    p.close()


def test_requests_inside_the_window_become_one_batch(pipeline):
    batches = []

    def handler(keys):
        batches.append(list(keys))
        return {k: k * 2 for k in keys}

    pipeline.register("double", handler)
    futures = [pipeline.request("double", i) for i in range(10)]

    assert [f.result(timeout=5) for f in futures] == [i * 2 for i in range(10)]
    assert batches == [list(range(10))]
    assert pipeline.stats.batch_sizes == [10]


def test_the_same_key_is_asked_once_and_shared(pipeline):
    asked = []
    pipeline.register("k", lambda keys: asked.extend(keys) or {k: "answer" for k in keys})
    a, b = pipeline.request("k", "same"), pipeline.request("k", "same")
    assert a is b and a.result(timeout=5) == "answer"
    assert asked == ["same"]
    assert pipeline.stats.deduplicated == 1

    # A key that is running is shared too.
    started, release = threading.Event(), threading.Event()

    def slow(keys):
        started.set()
        release.wait(5)
        return {k: 1 for k in keys}

    pipeline.register("slow", slow)
    first = pipeline.request("slow", "x")
    assert started.wait(5)
    assert pipeline.request("slow", "x") is first
    release.set()
    assert first.result(timeout=5) == 1


def test_higher_priority_runs_first_and_a_waiting_request_can_be_raised():
    pipeline = RequestPipeline(window=0.05, workers=1)  # one worker: batches run in the order they are dispatched
    try:
        order = []
        pipeline.register("p", lambda keys: order.extend(keys) or {k: None for k in keys}, max_batch=1)
        pipeline.request("p", "prefetch", PRIORITY_PREFETCH)
        pipeline.request("p", "also-prefetch", PRIORITY_PREFETCH)
        pipeline.request("p", "visible", PRIORITY_VISIBLE)
        pipeline.request("p", "also-prefetch", PRIORITY_VISIBLE)  # raised: now it is visible too
        assert pipeline.wait_idle(5)
        assert sorted(order[:2]) == ["also-prefetch", "visible"] and order[2] == "prefetch"
    finally:
        pipeline.close()


def test_a_request_that_has_not_started_can_be_cancelled(pipeline):
    asked = []
    pipeline.register("c", lambda keys: asked.extend(keys) or {k: 1 for k in keys})
    keep, drop, drop2 = pipeline.request("c", "keep"), pipeline.request("c", "drop"), pipeline.request("c", "gone-1")
    assert pipeline.cancel("c", "drop") and not pipeline.cancel("c", "never-asked")
    assert pipeline.cancel_where("c", lambda key: not key.startswith("gone")) == 1

    assert keep.result(timeout=5) == 1
    assert drop.cancelled() and drop2.cancelled()
    assert asked == ["keep"]
    assert pipeline.stats.cancelled == 2


def test_a_failing_batch_fails_every_request_in_it(pipeline):
    def boom(keys):
        raise RuntimeError("server down")

    pipeline.register("b", boom)
    futures = [pipeline.request("b", i) for i in range(3)]
    for f in futures:
        with pytest.raises(RuntimeError, match="server down"):
            f.result(timeout=5)
    # The pipeline keeps working.
    pipeline.register("ok", lambda keys: {k: "fine" for k in keys})
    assert pipeline.request("ok", 1).result(timeout=5) == "fine"


def test_batches_are_capped_and_close_cancels_waiting_requests():
    p = RequestPipeline(window=0.05)
    p.register("m", lambda keys: {k: 1 for k in keys}, max_batch=4)
    [f.result(timeout=5) for f in [p.request("m", i) for i in range(10)]]
    assert sorted(p.stats.batch_sizes) == [2, 4, 4]
    waiting = p.request("m", "late")
    p.close()
    assert waiting.cancelled() or waiting.done()
    assert p.request("m", "after").cancelled()
    with pytest.raises(KeyError):
        RequestPipeline().request("nothing registered", 1)


def test_signals_connect_disconnect_and_survive_a_bad_slot():
    from deda.clio.views import Signal

    signal, seen = Signal(), []
    off = signal.connect(lambda a, b: seen.append((a, b)))
    signal.connect(lambda a, b: 1 / 0)
    signal.connect(lambda a, b: seen.append("after the bad one"))
    signal.emit(1, 2)
    assert seen == [(1, 2), "after the bad one"]
    off()
    signal.emit(3, 4)
    assert seen == [(1, 2), "after the bad one", "after the bad one"]


# --- a big folder is split ------------------------------------------------------


def test_a_folder_that_is_too_big_for_the_server_is_asked_folder_by_folder():
    from deda.clio.backends.base import P4Message, Result, Severity

    calls = []

    class Backend:
        def run(self, command, args=(), input=None):
            calls.append((command, tuple(a for a in args if a.startswith("//"))))
            spec = args[-1]
            if command == "fstat" and spec == "//d/p/...":
                return Result([], [P4Message(Severity.FAILED, "Too many rows scanned (over 100000); see 'p4 help maxresults'.")])
            if command == "fstat" and spec == "//d/p/*":
                return Result([{"depotFile": "//d/p/top.usda"}])
            if command == "dirs":
                return Result([{"dir": "//d/p/a"}, {"dir": "//d/p/b"}])
            if command == "fstat":
                return Result([{"depotFile": spec.removesuffix("/...") + "/f.usda"}])
            raise AssertionError(command)

    class Session:
        backend = Backend()

    records = _fstat_scope(Session(), "//d/p/...")

    assert sorted(records) == ["//d/p/a/f.usda", "//d/p/b/f.usda", "//d/p/top.usda"]


# --- with a server ----------------------------------------------------------------


def _save(session, files: dict[str, str], message: str = "save") -> None:
    for relative, content in files.items():
        path = session.paths.local_path(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            path.chmod(path.stat().st_mode | stat.S_IWRITE)
        path.write_text(content)
    session.workspace.save(message=message)


@pytest.fixture
def alice(make_session):
    session = make_session()
    _save(session, {f"props/f{i:03}.usda": f"#usda 1.0\n# {i}\n" for i in range(60)} | {"other/x.usda": "x"},
          "sixty files")
    return session


# The views stamp rows with the clock they are given; the pipeline's handlers
# stamp with time.monotonic(). Tests move time forward by adding to it.


def _calls(session) -> dict[str, int]:
    return dict(session.backend.calls)


def _delta(session, before: dict[str, int]) -> dict[str, int]:
    after = _calls(session)
    return {k: after[k] - before.get(k, 0) for k in after if after[k] != before.get(k, 0)}


def test_opening_a_folder_costs_one_fstat(alice):
    before = _calls(alice)
    with FileListView(alice, "props") as view:
        view.ready.result(timeout=10)
        assert len(view) == 60
        assert view[0].asset_path == "props/f000.usda" and view[59].asset_path == "props/f059.usda"
        row = view[3]
        assert (row.have_rev, row.head_rev, row.state) == (1, 1, FileState.SYNCED)
        assert row.version == "v1" and row.size and row.file_type == "text" and row.changed_at is not None
        assert row.locked_by is None and row.freshness is Freshness.FRESH
        assert view.freshness is Freshness.FRESH
        assert view.index_of("props/f003.usda") == 3

    used = _delta(alice, before)
    assert used.get("fstat") == 1
    assert sum(used.values()) <= 3  # the one fstat, plus the client spec the session asks for once


def test_a_second_view_of_the_same_folder_has_its_rows_at_once(alice):
    with FileListView(alice, "props") as first:
        first.ready.result(timeout=10)
    before = _calls(alice)

    with FileListView(alice, "props") as second:
        assert second.ready.done()  # no waiting: the rows are there on return
        assert len(second) == 60

    assert _delta(alice, before) == {}


def test_only_stale_visible_rows_are_asked_for_in_one_batch(alice):
    clock = [0.0]
    changed = []
    with FileListView(alice, "props", volatile_ttl=20, clock=lambda: time.monotonic() + clock[0]) as view:
        view.ready.result(timeout=10)
        view.rows_changed.connect(lambda a, b: changed.append((a, b)))
        before = _calls(alice)

        assert view.set_visible_range(0, 29) == []  # fresh: nothing to ask
        assert _delta(alice, before) == {}

        clock[0] = 25.0  # past the time-to-live
        assert view.freshness is Freshness.STALE
        assert view[0].freshness is Freshness.STALE
        futures = view.set_visible_range(0, 29)
        assert len(futures) == 60  # 30 visible + 30 prefetched below, asked together
        [f.result(timeout=10) for f in futures]
        alice.pipeline.wait_idle(5)

        assert _delta(alice, before) == {"fstat": 1}  # one command, however many rows
        assert view[0].freshness is Freshness.FRESH
        assert changed and changed[0][0] == 0  # the rows were announced as refreshed


def test_scrolling_drops_requests_for_rows_that_left_the_screen(alice):
    clock = [0.0]
    with FileListView(alice, "props", volatile_ttl=1, clock=lambda: time.monotonic() + clock[0]) as view:
        view.ready.result(timeout=10)
        clock[0] = 100.0
        first = view.set_visible_range(0, 9)
        second = view.set_visible_range(40, 49)  # scrolled before the first batch started
        assert [f for f in first if f.cancelled()]  # rows 0-9 and their prefetch were dropped
        [f.result(timeout=10) for f in second if not f.cancelled()]
        alice.pipeline.wait_idle(5)
        assert view[45].freshness is Freshness.FRESH
        assert view[0].freshness is Freshness.STALE


def test_a_lock_by_someone_else_shows_up_when_the_row_is_refreshed(alice, make_session):
    bob = make_session("bob")
    bob.workspace.get()
    clock = [0.0]
    with FileListView(alice, "props", volatile_ttl=5, clock=lambda: time.monotonic() + clock[0]) as view:
        view.ready.result(timeout=10)
        assert view[7].locked_by is None
        bob.workspace.lock("props/f007.usda")

        clock[0] = 30.0
        [f.result(timeout=10) for f in view.set_visible_range(0, 20)]
        alice.pipeline.wait_idle(5)

        assert view[7].locked_by is not None and view[7].locked_by.startswith("bob")
        assert view[6].locked_by is None


def test_new_versions_by_others_change_the_row(alice, make_session):
    bob = make_session("bob")
    bob.workspace.get()
    clock = [0.0]
    events = []
    with FileListView(alice, "props", volatile_ttl=5, clock=lambda: time.monotonic() + clock[0]) as view:
        view.ready.result(timeout=10)
        view.rows_changed.connect(lambda a, b: events.append((a, b)))
        _save(bob, {"props/f002.usda": "changed by bob", "props/f003.usda": "changed too"})

        clock[0] = 30.0
        [f.result(timeout=10) for f in view.set_visible_range(0, 10)]
        alice.pipeline.wait_idle(5)

        assert (view[2].have_rev, view[2].head_rev) == (1, 2)
        assert view[2].state is FileState.OUT_OF_DATE and view[2].version == "v1 of 2"
        assert view[1].state is FileState.SYNCED
        assert any(a <= 2 and b >= 3 for a, b in events)  # neighbouring rows are announced as ranges, not one by one
    assert len(events) == 1


def test_a_deleted_file_leaves_the_list_unless_you_still_have_it(alice, make_session):
    bob = make_session("bob")
    bob.workspace.get()
    # alice has f005 on disk, but not f006
    alice.backend.run("sync", ["-q", alice.paths.depot_path("props/f006.usda") + "#none"])
    clock = [0.0]
    resets = []
    with FileListView(alice, "props", volatile_ttl=5, clock=lambda: time.monotonic() + clock[0]) as view:
        view.ready.result(timeout=10)
        assert view[6].state is FileState.MISSING
        view.reset.connect(lambda: resets.append(len(view)))
        for name in ("f005", "f006"):
            victim = bob.paths.local_path(f"props/{name}.usda")
            victim.chmod(victim.stat().st_mode | stat.S_IWRITE)
            victim.unlink()
        bob.workspace.save(message="remove two")
        clock[0] = 30.0
        [f.result(timeout=10) for f in view.set_visible_range(0, 10)]
        alice.pipeline.wait_idle(5)

        assert len(view) == 59 and view.index_of("props/f006.usda") is None  # alice never had it: gone
        kept = view[view.index_of("props/f005.usda")]  # alice still has it: shown as out of date, deleted
        assert kept.state is FileState.OUT_OF_DATE and kept.deleted
        assert resets == [59]


def test_a_refresh_finds_files_that_others_added(alice, make_session):
    bob = make_session("bob")
    with FileListView(alice, "props") as view:
        view.ready.result(timeout=10)
        resets = []
        view.reset.connect(lambda: resets.append(len(view)))
        _save(bob, {"props/zzz_new.usda": "new"})

        view.refresh().result(timeout=10)

        assert len(view) == 61 and view[60].asset_path == "props/zzz_new.usda"
        assert resets == [61]


def test_changed_by_is_asked_once_per_revision_and_kept(alice):
    before = _calls(alice)
    with FileListView(alice, "props", columns=("version", "changed_by", "changed_at")) as view:
        view.ready.result(timeout=10)
        assert view[0].changed_by is None  # not asked for yet
        view.set_visible_range(0, 29)
        alice.pipeline.wait_idle(5)
        time.sleep(0.2)
        assert view[0].changed_by == alice.user and view[29].changed_by == alice.user
        assert view[30].changed_by is None  # not visible, not asked

        view.set_visible_range(0, 29)
        alice.pipeline.wait_idle(5)
    used = _delta(alice, before)
    assert used.get("filelog") == 1  # one command for 30 rows, and not again

    with FileListView(alice, "props", columns=("changed_by",)) as again:
        assert again[0].changed_by == alice.user  # remembered: immutable data


def test_unknown_columns_are_refused(alice):
    with pytest.raises(ValueError, match="Unknown column.*nope"):
        FileListView(alice, "props", columns=("version", "nope"))


def test_a_view_of_an_unreachable_server_raises_a_clio_error(p4_server):
    session = clio.connect(port="localhost:1", user="nobody", client="none", connect_timeout=2,
                           root=str(p4_server.workspace_root), depot="//depot/proj")
    try:
        # Building a view needs the workspace, which needs the server.
        with pytest.raises(clio.ClioError):
            FileListView(session, "props")
    finally:
        session.close()


# --- history ---------------------------------------------------------------------


@pytest.fixture
def versions(alice):
    for i in range(2, 8):
        _save(alice, {"props/f000.usda": f"#usda 1.0\n# version {i}\n"}, f"change {i}")
    return alice


def test_history_first_page_then_older_pages_on_demand(versions):
    alice = versions
    before = _calls(alice)
    inserted = []
    with HistoryView(alice, "props/f000.usda", page_size=3) as view:
        view.rows_inserted.connect(lambda a, b: inserted.append((a, b)))
        view.ready.result(timeout=10)
        assert len(view) == 3 and [r.rev for r in view] == [7, 6, 5]
        assert view[0].description == "change 7" and view[0].user == alice.user
        assert view.has_more and view.freshness is Freshness.FRESH

        view.fetch_more().result(timeout=10)
        assert [r.rev for r in view] == [7, 6, 5, 4, 3, 2]
        view.fetch_more().result(timeout=10)
        assert [r.rev for r in view][-1] == 1 and not view.has_more
        assert inserted == [(3, 5), (6, 6)]
        assert view.fetch_more().result(timeout=10) is None  # nothing more: answered at once

    assert _delta(alice, before)["filelog"] == 3  # first page, and two older pages


def test_a_cached_history_shows_at_once_and_costs_nothing_while_fresh(versions):
    alice = versions
    with HistoryView(alice, "props/f000.usda", page_size=3) as first:
        first.ready.result(timeout=10)
    before = _calls(alice)

    with HistoryView(alice, "props/f000.usda", page_size=3) as second:
        assert second.ready.done() and len(second) == 3  # no waiting
        alice.pipeline.wait_idle(5)

    assert _delta(alice, before) == {}


def test_newer_revisions_are_inserted_at_the_top_with_a_cheap_check(versions, make_session):
    alice = versions
    bob = make_session("bob")
    bob.workspace.get()
    with HistoryView(alice, "props/f000.usda", page_size=3) as view:
        view.ready.result(timeout=10)
        inserted = []
        view.rows_inserted.connect(lambda a, b: inserted.append((a, b)))
        _save(bob, {"props/f000.usda": "bob's version"}, "bob edits")
        before = _calls(alice)

        view.refresh().result(timeout=10)
        time.sleep(0.2)

        assert [r.rev for r in view][:2] == [8, 7] and view[0].user == "bob"
        assert inserted == [(0, 0)]
        used = _delta(alice, before)
        assert used.get("changes") == 1 and used.get("filelog") == 1  # watermark check, then the delta


def test_history_content_comes_from_the_content_cache(versions, tmp_path):
    alice = versions
    with HistoryView(alice, "props/f000.usda", page_size=3) as view:
        view.ready.result(timeout=10)
        before = _calls(alice)
        path = view.content(0).result(timeout=10)
        assert path.read_bytes().replace(b"\r\n", b"\n") == b"#usda 1.0\n# version 7\n"
        assert view.content(0).result(timeout=10) == path
        assert _delta(alice, before)["print"] == 1  # the second time is the cache


def test_polling_checks_while_visible_and_pauses_while_hidden(versions):
    alice = versions
    with HistoryView(alice, "props/f000.usda", page_size=3) as view:
        view.ready.result(timeout=10)
        base = alice.pipeline.stats.requested
        view.start_polling(interval=0.05)
        time.sleep(0.4)
        assert alice.pipeline.stats.requested > base  # it checked

        view.set_visible(False)
        time.sleep(0.15)
        paused = alice.pipeline.stats.requested
        time.sleep(0.3)
        assert alice.pipeline.stats.requested == paused  # hidden: no checks

        view.set_visible(True)  # becoming visible checks at once
        assert alice.pipeline.stats.requested > paused
        view.stop_polling()


def test_closing_the_session_closes_the_pipeline(make_session):
    session = make_session()
    pipeline = session.pipeline
    session.close()
    assert pipeline.request("rows", "x").cancelled()
