"""Which branch a revision came from: integration records in history rows
(design doc §9.3), at no extra server call."""

from __future__ import annotations

import os
import stat

import pytest
from click.testing import CliRunner

from deda.clio.cli import cli
from deda.clio.models import Integration, Revision


def _save(session, files: dict[str, str], message: str) -> int:
    existing = [a for a in files if session.paths.local_path(a).exists()]
    if existing:
        session.workspace.lock(*existing)
    for relative, text in files.items():
        path = session.paths.local_path(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            path.chmod(path.stat().st_mode | stat.S_IWRITE)
        path.write_text(text)
    return session.workspace.save(*files, message=message).change.number


@pytest.fixture
def alice(make_stream_session):
    session = make_stream_session("clio_tester")
    _save(session, {"props/crate.ma": "v1"}, "Crate v1")
    return session


def test_an_ordinary_edit_has_no_integration_record(alice):
    _save(alice, {"props/crate.ma": "v2"}, "Crate v2")
    page = alice.history.revisions("props/crate.ma")
    assert all(r.integrations == () and r.came_from is None for r in page.items)


def test_a_branch_revision_says_it_was_branched_from_main(alice):
    alice.branches.create("crate-damage")
    _save(alice, {"props/crate.ma": "damaged"}, "Damage pass")

    newest, branched = alice.history.revisions("props/crate.ma").items[:2]
    assert newest.integrations == () and newest.action == "edit"
    origin = branched.came_from
    assert origin is not None and origin.how == "branch from"
    assert origin.depot_path == "//imagine/main/props/crate.ma" and origin.branch == "main"
    assert origin.end_rev == 1 and not origin.into


def test_publishing_shows_where_the_content_came_from_and_where_it_went(alice):
    alice.branches.create("crate-damage")
    _save(alice, {"props/crate.ma": "damaged"}, "Damage pass")
    alice.branches.publish()

    # Still on the branch: the history of the branch file shows its edit going up.
    top = alice.history.revisions("props/crate.ma").items[0]
    assert any(i.into and i.branch == "main" for i in top.integrations)

    alice.branches.switch("main")
    main = alice.history.revisions("props/crate.ma").items
    assert main[0].came_from is not None and main[0].came_from.branch == "crate-damage"
    assert main[0].came_from.how in ("copy from", "merge from")
    assert isinstance(main[0], Revision) and isinstance(main[0].integrations[0], Integration)


def test_the_history_command_names_the_source_branch(alice, p4_server, monkeypatch):
    alice.branches.create("crate-damage")
    _save(alice, {"props/crate.ma": "damaged"}, "Damage pass")
    alice.branches.publish()
    alice.branches.switch("main")

    monkeypatch.setenv("P4TICKETS", os.fspath(p4_server.tickets))
    monkeypatch.chdir(alice.paths.root)
    base = ["--port", p4_server.port, "--user", alice.user, "--client", alice.client]
    result = CliRunner().invoke(cli, [*base, "history", "props/crate.ma"], catch_exceptions=False)
    assert result.exit_code == 0, result.output
    assert "from crate-damage" in result.output


def test_a_history_row_costs_no_extra_server_call(alice):
    alice.branches.create("crate-damage")
    before = dict(alice.backend.calls)
    alice.history.revisions("props/crate.ma")
    after = alice.backend.calls
    assert {k: after[k] - before.get(k, 0) for k in after if after[k] != before.get(k, 0)} == {"filelog": 1}
