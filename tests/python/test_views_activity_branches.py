"""The activity feed and the branch views (design doc §9.1), with the
server-call budgets of §9.6."""

from __future__ import annotations

import stat
import time
from datetime import datetime, timedelta

import pytest

from deda import clio
from deda.clio.models import Freshness
from deda.clio.views import ActivityView, BranchDiffView, BranchListView, DiffKind, Relation


def _write(session, relative: str, text: str) -> None:
    path = session.paths.local_path(relative)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.chmod(path.stat().st_mode | stat.S_IWRITE)
    path.write_text(text)


def _save(session, files: dict[str, str], message: str) -> int:
    existing = [a for a in files if session.paths.local_path(a).exists()]
    if existing:
        session.workspace.lock(*existing)
    for relative, text in files.items():
        _write(session, relative, text)
    return session.workspace.save(*files, message=message).change.number


def _calls(session) -> dict[str, int]:
    return dict(session.backend.calls)


def _delta(session, before: dict[str, int]) -> dict[str, int]:
    after = _calls(session)
    return {k: after[k] - before.get(k, 0) for k in after if after[k] != before.get(k, 0)}


# --- the activity feed -------------------------------------------------------------


@pytest.fixture
def alice(make_session):
    session = make_session()
    for i in range(1, 26):
        _save(session, {f"props/f{i % 3}.usda": f"version {i}", f"other/o{i}.usda": str(i)} if i == 1 else
              {f"props/f{i % 3}.usda": f"version {i}"}, f"change {i}")
    return session


def test_the_feed_shows_the_newest_page_then_older_ones(alice):
    before = _calls(alice)
    inserted = []
    with ActivityView(alice, "props", page_size=10) as feed:
        feed.rows_inserted.connect(lambda a, b: inserted.append((a, b)))
        feed.ready.result(timeout=10)
        assert len(feed) == 10 and feed[0].summary == "change 25" and feed[9].summary == "change 16"
        assert feed[0].user == alice.user and feed[0].time is not None
        assert feed.has_more and feed.freshness is Freshness.FRESH

        feed.fetch_more().result(timeout=10)
        assert len(feed) == 20 and feed[19].summary == "change 6"
        assert inserted == [(10, 19)]
        feed.fetch_more().result(timeout=10)
        assert len(feed) == 25 and not feed.has_more  # the beginning of the project's history
        assert feed.fetch_more().result(timeout=10) is None

    assert _delta(alice, before)["changes"] <= 4  # a few pages, never one call per row


def test_a_second_feed_has_its_rows_at_once_and_costs_nothing_while_fresh(alice):
    with ActivityView(alice, "props", page_size=10) as first:
        first.ready.result(timeout=10)
    before = _calls(alice)

    with ActivityView(alice, "props", page_size=10) as second:
        assert second.ready.done() and len(second) == 10
        alice.pipeline.wait_idle(5)

    assert _delta(alice, before) == {}


def test_new_changes_by_others_arrive_at_the_top_with_a_cheap_check(alice, make_session):
    bob = make_session("bob")
    bob.workspace.get()
    with ActivityView(alice, "props", page_size=10) as feed:
        feed.ready.result(timeout=10)
        inserted = []
        feed.rows_inserted.connect(lambda a, b: inserted.append((a, b)))
        _save(bob, {"props/f1.usda": "bob was here"}, "bob edits")
        before = _calls(alice)

        feed.refresh().result(timeout=10)

        assert feed[0].summary == "bob edits" and feed[0].user == "bob" and len(feed) == 10
        assert inserted == [(0, 0)]
        used = _delta(alice, before)
        assert used["changes"] == 2  # the watermark check, then only the news


def test_several_paths_are_one_feed_in_order(alice):
    with ActivityView(alice, ["props/f0.usda", "other"], page_size=30) as feed:
        feed.ready.result(timeout=10)
        numbers = [row.number for row in feed]
        assert numbers == sorted(numbers, reverse=True) and len(numbers) == len(set(numbers))
        summaries = {row.summary for row in feed}
        assert "change 1" in summaries  # the change that made other/ files
        assert "change 3" in summaries and "change 4" not in summaries  # f0 is i % 3 == 0


def test_since_makes_a_today_feed(alice):
    with ActivityView(alice, "props", page_size=10, since=datetime.now() + timedelta(hours=1)) as none:
        none.ready.result(timeout=10)
        assert len(none) == 0 and not none.has_more
    with ActivityView(alice, "props", page_size=10, since=datetime.now() - timedelta(hours=1)) as today:
        today.ready.result(timeout=10)
        assert len(today) == 10 and today.has_more


def test_the_files_of_the_changes_on_screen_are_one_describe(alice):
    with ActivityView(alice, "props", page_size=10) as feed:
        feed.ready.result(timeout=10)
        before = _calls(alice)
        futures = [feed.files(i) for i in range(10)]
        answers = [f.result(timeout=10) for f in futures]
        assert answers[0] == (alice.paths.depot_path(f"props/f{25 % 3}.usda"),)
        assert _delta(alice, before) == {"describe": 1}

        again = _calls(alice)
        assert feed.files(0).result(timeout=10) == answers[0]
        assert _delta(alice, again) == {}  # a submitted change never changes: kept


def test_a_feed_of_a_broken_server_reports_the_failure(make_session, p4_server):
    session = clio.connect(port="localhost:1", user="nobody", client="none", connect_timeout=2,
                           root=str(p4_server.workspace_root), depot="//depot/proj")
    try:
        with pytest.raises(clio.ClioError):
            ActivityView(session, "props")
    finally:
        session.close()


# --- branches ----------------------------------------------------------------------


@pytest.fixture
def streams(make_stream_session):
    alice = make_stream_session("clio_tester")
    _save(alice, {"props/crate.ma": "v1", "props/lid.ma": "l1", "props/tex.png": "t1"}, "Crate v1")
    bob = make_stream_session("bob")
    bob.workspace.get()
    return alice, bob


def test_the_branch_list_says_how_each_branch_relates_to_yours(streams):
    alice, bob = streams
    alice.branches.create("crate-damage")
    bob.branches.create("lid-fix")  # bob branches from main too: a sibling of alice's branch
    bob.branches.switch("main")

    with BranchListView(alice) as view:
        view.ready.result(timeout=10)
        alice.pipeline.wait_idle(5)
        by_name = {row.branch.name: row for row in view}
        assert set(by_name) == {"main", "crate-damage", "lid-fix"}
        assert by_name["crate-damage"].relation is Relation.CURRENT
        assert by_name["main"].relation is Relation.PARENT
        assert by_name["lid-fix"].relation is Relation.SIBLING
        assert view.current.branch.name == "crate-damage"
        assert [r.branch.path for r in view] == sorted(r.branch.path for r in view)


def test_a_mainline_has_no_behind_count_and_a_branch_does(streams):
    alice, bob = streams
    with BranchListView(alice) as main_view:
        main_view.ready.result(timeout=10)
        alice.pipeline.wait_idle(5)
        assert main_view.current.relation is Relation.CURRENT
        assert main_view.behind is None  # nothing above main

    alice.branches.create("crate-damage")
    _save(alice, {"props/crate.ma": "damaged"}, "Damage pass")
    _save(bob, {"props/lid.ma": "l2"}, "Lid v2 on main")
    changes = []
    with BranchListView(alice) as view:
        view.behind_changed.connect(lambda: changes.append(view.behind))
        view.ready.result(timeout=10)
        alice.pipeline.wait_idle(5)
        time.sleep(0.2)
        assert view.behind == ("//imagine/crate-damage/props/lid.ma",)
        assert changes and changes[-1] == view.behind

        alice.branches.update()
        view.refresh().result(timeout=10)
        alice.pipeline.wait_idle(5)
        time.sleep(0.2)
        assert view.behind == ()
        assert view.freshness is Freshness.FRESH


def test_branch_list_reports_new_branches(streams):
    alice, bob = streams
    with BranchListView(alice) as view:
        view.ready.result(timeout=10)
        resets = []
        view.reset.connect(lambda: resets.append(len(view)))
        bob.branches.create("lid-fix")

        view.refresh().result(timeout=10)

        assert [r.branch.name for r in view] == ["lid-fix", "main"] and resets == [2]


def test_the_branch_diff_lists_what_differs_from_the_parent(streams):
    alice, bob = streams
    alice.branches.create("crate-damage")
    _save(alice, {"props/crate.ma": "damaged"}, "Damage pass")          # changed on the branch
    _save(alice, {"props/new.ma": "n"}, "A new file on the branch")     # only here
    _save(bob, {"props/lid.ma": "l2"}, "Lid v2 on main")                # changed on main
    _save(bob, {"props/main_only.ma": "m"}, "Added on main")            # only there

    before = _calls(alice)
    with BranchDiffView(alice) as diff:
        diff.ready.result(timeout=10)
        assert (diff.here.name, diff.there.name) == ("crate-damage", "main")
        rows = {r.asset_path: r for r in diff}
        assert set(rows) == {"props/crate.ma", "props/new.ma", "props/lid.ma", "props/main_only.ma"}
        assert rows["props/crate.ma"].kind is DiffKind.DIFFERENT
        assert rows["props/new.ma"].kind is DiffKind.ONLY_HERE and rows["props/new.ma"].there_rev is None
        assert rows["props/main_only.ma"].kind is DiffKind.ONLY_THERE and rows["props/main_only.ma"].here_rev is None
        assert rows["props/lid.ma"].kind is DiffKind.DIFFERENT
        assert "props/tex.png" not in rows  # identical: not listed
        assert [r.asset_path for r in diff] == sorted(rows, key=str.casefold)
        assert diff.freshness is Freshness.FRESH

    assert _delta(alice, before)["diff2"] == 1  # one command however many files

    alice.branches.update()  # take main's changes: only the branch's own work remains different
    with BranchDiffView(alice) as after:
        after.ready.result(timeout=10)
        assert {r.asset_path: r.kind for r in after} == {
            "props/crate.ma": DiffKind.DIFFERENT, "props/new.ma": DiffKind.ONLY_HERE}


def test_the_diff_can_name_the_branches_and_a_mainline_needs_one(streams):
    alice, bob = streams
    alice.branches.create("crate-damage")
    _save(alice, {"props/crate.ma": "damaged"}, "Damage pass")
    with BranchDiffView(alice, "crate-damage", "main") as explicit:
        explicit.ready.result(timeout=10)
        assert [r.asset_path for r in explicit] == ["props/crate.ma"]

    alice.branches.switch("main")
    with pytest.raises(clio.WorkflowError, match="mainline"):
        BranchDiffView(alice)
    with BranchDiffView(alice, "main", "crate-damage") as reversed_view:
        reversed_view.ready.result(timeout=10)
        assert [(r.asset_path, r.kind) for r in reversed_view] == [("props/crate.ma", DiffKind.DIFFERENT)]


def test_refreshing_a_diff_sees_new_work(streams):
    alice, bob = streams
    alice.branches.create("crate-damage")
    with BranchDiffView(alice) as diff:
        diff.ready.result(timeout=10)
        assert len(diff) == 0
        resets = []
        diff.reset.connect(lambda: resets.append(len(diff)))
        _save(alice, {"props/crate.ma": "damaged"}, "Damage pass")

        diff.refresh().result(timeout=10)
        alice.pipeline.wait_idle(5)

        assert [r.asset_path for r in diff] == ["props/crate.ma"] and resets == [1]
