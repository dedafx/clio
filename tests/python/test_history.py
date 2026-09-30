"""History and activity: the cache and its server-call budget (design doc
§9.4, §9.6)."""

from __future__ import annotations

import stat

import pytest

from deda import clio
from deda.clio.history import HistoryCache


@pytest.fixture(autouse=True)
def fresh_cache():
    HistoryCache.clear_all()
    yield
    HistoryCache.clear_all()


def save_version(session: clio.Session, asset: str, text: str, message: str) -> None:
    local = session.paths.local_path(asset)
    if local.exists():
        session.workspace.lock(asset)
        local.chmod(local.stat().st_mode | stat.S_IWRITE)
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_text(text)
    session.workspace.save(asset, message=message)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def alice(make_session):
    session = make_session("clio_tester")
    clock = Clock()
    session.history._clock = clock
    session.clock = clock
    return session


def calls(session: clio.Session) -> dict[str, int]:
    return dict(session.backend.calls)


def delta(before: dict[str, int], after: dict[str, int]) -> dict[str, int]:
    return {k: after.get(k, 0) - before.get(k, 0) for k in after if after.get(k, 0) != before.get(k, 0)}


def test_revisions_are_cached_and_refreshed_with_one_cheap_call(alice):
    for i in range(1, 4):
        save_version(alice, "props/crate.ma", f"v{i}", f"crate v{i}")

    before = calls(alice)
    page = alice.history.revisions("props/crate.ma", limit=20)
    assert [r.rev for r in page.items] == [3, 2, 1]
    assert page.items[0].description == "crate v3"
    assert page.items[0].user == "clio_tester"
    assert page.freshness is clio.Freshness.FRESH
    assert page.next_cursor is None
    assert delta(before, calls(alice)) == {"filelog": 1}

    # Within the polling interval: no server call at all.
    before = calls(alice)
    alice.history.revisions("props/crate.ma")
    assert delta(before, calls(alice)) == {}

    # After it, with nothing new: one 'changes -m1'.
    alice.clock.now += 60
    before = calls(alice)
    assert [r.rev for r in alice.history.revisions("props/crate.ma").items] == [3, 2, 1]
    assert delta(before, calls(alice)) == {"changes": 1}


def test_a_new_revision_fetches_only_the_delta(alice, make_session):
    save_version(alice, "a.ma", "v1", "v1")
    alice.history.revisions("a.ma")

    save_version(alice, "a.ma", "v2", "v2")  # this process's save marks it stale
    before = calls(alice)
    page = alice.history.revisions("a.ma")
    assert [r.rev for r in page.items] == [2, 1]
    assert delta(before, calls(alice)) == {"changes": 1, "filelog": 1}


def test_other_users_revisions_show_up_after_the_interval(alice, make_session):
    save_version(alice, "a.ma", "v1", "v1")
    bob = make_session("bob")
    bob.workspace.get()
    assert [r.rev for r in alice.history.revisions("a.ma").items] == [1]

    save_version(bob, "a.ma", "v2", "bob v2")
    assert [r.rev for r in alice.history.revisions("a.ma").items] == [1]  # still within the interval
    alice.clock.now += 60
    page = alice.history.revisions("a.ma")
    assert [(r.rev, r.user) for r in page.items] == [(2, "bob"), (1, "clio_tester")]


def test_paging_to_older_revisions(alice):
    for i in range(1, 6):
        save_version(alice, "a.ma", f"v{i}", f"v{i}")
    first = alice.history.revisions("a.ma", limit=2)
    assert [r.rev for r in first.items] == [5, 4]
    assert first.next_cursor == 4
    second = alice.history.revisions("a.ma", limit=2, before=first.next_cursor)
    assert [r.rev for r in second.items] == [3, 2]
    third = alice.history.revisions("a.ma", limit=2, before=second.next_cursor)
    assert [r.rev for r in third.items] == [1]
    assert third.next_cursor is None

    # Every page is cached now.
    before = calls(alice)
    assert [r.rev for r in alice.history.revisions("a.ma", limit=2, before=4).items] == [3, 2]
    assert delta(before, calls(alice)) == {}


def test_activity_for_a_folder(alice):
    save_version(alice, "props/a.ma", "1", "props one")
    save_version(alice, "chars/b.ma", "1", "chars one")
    save_version(alice, "props/c.ma", "1", "props two")

    page = alice.history.activity("props")
    assert [c.description for c in page.items] == ["props two", "props one"]
    assert [c.description for c in alice.history.activity().items] == ["props two", "chars one", "props one"]

    before = calls(alice)
    alice.history.activity("props")
    assert delta(before, calls(alice)) == {}
    alice.clock.now += 60
    save_version(alice, "props/d.ma", "1", "props three")
    assert alice.history.activity("props").items[0].description == "props three"


def test_history_of_a_folder_is_refused(alice):
    save_version(alice, "props/a.ma", "1", "one")
    with pytest.raises(clio.NotInWorkspaceError):
        alice.history.revisions("props/")


def test_one_cache_per_server_and_user(alice):
    save_version(alice, "a.ma", "v1", "v1")
    assert alice.history.cache is HistoryCache.for_server(alice.server_id, alice.user)
    assert HistoryCache.for_server(alice.server_id, "someone_else") is not alice.history.cache
