"""Branches (streams) and drafts against a throwaway p4d (design doc §7)."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest
from click.testing import CliRunner

from deda import clio
from deda.clio.cli import cli


def write(path: Path, data: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.chmod(path.stat().st_mode | stat.S_IWRITE)
    path.write_text(data)


def edit_and_save(session: clio.Session, files: dict[str, str], message: str) -> int:
    existing = [a for a in files if session.paths.local_path(a).exists()]
    if existing:
        session.workspace.lock(*existing)
    for asset, text in files.items():
        write(session.paths.local_path(asset), text)
    return session.workspace.save(*files, message=message).change.number


def read(session: clio.Session, asset: str) -> str:
    return session.paths.local_path(asset).read_text()


def pending(session: clio.Session) -> list[dict]:
    return session.backend.run("changes", ["-s", "pending", "-c", session.client]).records


@pytest.fixture
def alice(make_stream_session) -> clio.Session:
    s = make_stream_session("clio_tester")
    edit_and_save(s, {"props/crate.ma": "v1", "props/tex.png": "t1", "props/lid.ma": "l1"}, "Crate v1")
    return s


@pytest.fixture
def bob(make_stream_session, alice) -> clio.Session:
    s = make_stream_session("bob")
    s.workspace.get()
    return s


def test_stream_workspace_paths(alice):
    assert alice.stream == "//imagine/main"
    assert alice.paths.depot_root == "//imagine/main"
    [main] = alice.branches.list()
    assert (main.name, main.kind, main.parent, main.is_current) == ("main", "mainline", None, True)


def test_create_switch_and_save_in_a_branch(alice, bob):
    branch = alice.branches.create("crate-damage")
    assert branch.path == "//imagine/crate-damage"
    assert branch.parent == "//imagine/main" and branch.kind == "task" and branch.is_current
    # The workspace follows the branch.
    assert alice.paths.depot_root == "//imagine/crate-damage"
    assert read(alice, "props/crate.ma") == "v1"

    edit_and_save(alice, {"props/crate.ma": "damaged"}, "Damage pass")
    # main is untouched.
    assert bob.workspace.get().file_count == 0
    assert read(bob, "props/crate.ma") == "v1"
    names = {b.name: b for b in alice.branches.list()}
    assert set(names) == {"main", "crate-damage"} and names["crate-damage"].is_current

    switched = alice.branches.switch("main")
    assert switched.branch.name == "main" and switched.draft is None
    assert read(alice, "props/crate.ma") == "v1"
    assert alice.paths.depot_root == "//imagine/main"


def test_publish_copies_saved_work_up(alice, bob):
    alice.branches.create("crate-damage")
    edit_and_save(alice, {"props/crate.ma": "damaged", "props/new.ma": "n"}, "Damage pass")

    result = alice.branches.publish()
    assert result.parent == "//imagine/main"
    assert set(result.files) == {"//imagine/main/props/crate.ma", "//imagine/main/props/new.ma"}
    assert bob.workspace.get().file_count == 2
    assert read(bob, "props/crate.ma") == "damaged"
    # The publishing workspace never gets files.
    pub = alice.backend.run("client", ["-o", f"{alice.client}_publish"]).records[0]
    assert not Path(pub["Root"]).exists() or not any(Path(pub["Root"]).rglob("*"))
    with pytest.raises(clio.NothingToSaveError):
        alice.branches.publish()


def test_publish_is_refused_while_behind_or_with_open_files(alice, bob):
    alice.branches.create("crate-damage")
    edit_and_save(alice, {"props/crate.ma": "damaged"}, "Damage pass")
    edit_and_save(bob, {"props/lid.ma": "l2"}, "Lid v2 on main")
    with pytest.raises(clio.OutOfDateError, match="behind"):
        alice.branches.publish()

    alice.branches.update()
    alice.workspace.lock("props/tex.png")
    with pytest.raises(clio.WorkflowError, match="open"):
        alice.branches.publish()
    alice.workspace.unlock("props/tex.png")
    assert alice.branches.publish().files


def test_update_takes_one_sided_changes(alice, bob):
    alice.branches.create("crate-damage")
    edit_and_save(alice, {"props/crate.ma": "damaged"}, "Damage pass")
    edit_and_save(bob, {"props/lid.ma": "l2"}, "Lid v2 on main")

    assert alice.branches.behind() == ["//imagine/crate-damage/props/lid.ma"]
    result = alice.branches.update()
    assert result.taken == ("props/lid.ma",)
    assert read(alice, "props/lid.ma") == "l2"
    assert read(alice, "props/crate.ma") == "damaged"
    assert alice.branches.update().change is None  # already up to date
    assert not pending(alice)


def test_update_conflicts_need_a_choice_and_change_nothing_without_one(alice, bob):
    alice.branches.create("crate-damage")
    edit_and_save(alice, {"props/crate.ma": "damaged", "props/tex.png": "t-branch"}, "Damage pass")
    edit_and_save(bob, {"props/crate.ma": "main v2", "props/tex.png": "t-main", "props/lid.ma": "l2"}, "main v2")

    with pytest.raises(clio.ConflictError) as caught:
        alice.branches.update()
    assert sorted(c.asset_path for c in caught.value.conflicts) == ["props/crate.ma", "props/tex.png"]
    assert caught.value.conflicts[0].theirs.startswith("//imagine/main/props/")
    # Nothing changed: no pending change, files as they were, still behind.
    assert not pending(alice)
    assert read(alice, "props/crate.ma") == "damaged" and read(alice, "props/lid.ma") == "l1"
    assert alice.branches.behind()

    with pytest.raises(clio.ConflictError):  # one choice is not enough
        alice.branches.update(resolutions={"props/crate.ma": "mine"})

    result = alice.branches.update(resolutions={"props/crate.ma": "both", "props/tex.png": "theirs"})
    assert result.kept_mine == ("props/crate.ma",)
    assert result.took_theirs == ("props/tex.png",)
    assert result.taken == ("props/lid.ma",)
    assert read(alice, "props/crate.ma") == "damaged"
    assert read(alice, "props/tex.png") == "t-main"
    [copy] = result.theirs_copies
    assert copy.name == "crate.theirs.ma" and copy.read_text() == "main v2"
    assert not alice.branches.behind()


def test_update_with_a_default_choice(alice, bob):
    alice.branches.create("crate-damage")
    edit_and_save(alice, {"props/crate.ma": "damaged"}, "Damage pass")
    edit_and_save(bob, {"props/crate.ma": "main v2"}, "main v2")
    result = alice.branches.update(default="mine")
    assert result.kept_mine == ("props/crate.ma",)
    assert read(alice, "props/crate.ma") == "damaged"
    # Publishing now puts the branch's version on main.
    alice.branches.publish()
    bob.workspace.get()
    assert read(bob, "props/crate.ma") == "damaged"


def test_switch_with_open_files(alice):
    alice.branches.create("crate-damage", switch=False)
    alice.workspace.lock("props/crate.ma")
    write(alice.paths.local_path("props/crate.ma"), "unsaved")
    with pytest.raises(clio.WorkflowError, match="open"):
        alice.branches.switch("crate-damage")
    assert alice.stream == "//imagine/main"

    result = alice.branches.switch("crate-damage", share_open_files=True)
    assert result.draft is not None and result.draft.files == ("//imagine/main/props/crate.ma",)
    assert read(alice, "props/crate.ma") == "v1"  # the branch's version

    alice.branches.switch("main")
    assert alice.drafts.get(result.draft.number) == ("props/crate.ma",)
    assert read(alice, "props/crate.ma") == "unsaved"


def test_drafts(alice, bob):
    write(alice.paths.local_path("props/tex.png"), "draft tex")
    draft = alice.drafts.share("props", message="WIP texture")
    assert draft.files == ("//imagine/main/props/tex.png",)
    assert [d.number for d in alice.drafts.list()] == [draft.number]
    assert bob.drafts.list() == []
    [listed] = bob.drafts.list(everyone=True)
    assert listed.description == "WIP texture" and listed.user == "clio_tester"

    assert bob.drafts.get(draft.number) == ("props/tex.png",)
    assert read(bob, "props/tex.png") == "draft tex"

    with pytest.raises(clio.ConfirmationRequiredError):
        alice.drafts.drop(draft.number)
    alice.drafts.drop(draft.number, confirm=True)
    assert alice.drafts.list() == []
    with pytest.raises(clio.NothingToSaveError):
        alice.drafts.share("props/lid.ma", message="nothing")
    assert len(pending(alice)) == 1  # the dropped draft's change still holds alice's open file


def test_a_draft_from_another_branch_is_refused(alice):
    alice.branches.create("crate-damage")
    write(alice.paths.local_path("props/tex.png"), "draft")
    draft = alice.drafts.share("props/tex.png", message="WIP", keep_open=False)
    alice.branches.switch("main")
    with pytest.raises(clio.WorkflowError, match="another branch"):
        alice.drafts.get(draft.number)


def test_cross_branch_lock_warns(alice, bob):
    alice.branches.create("crate-damage")
    bob.workspace.lock("props/crate.ma")  # on main

    [lock] = alice.workspace.lock("props/crate.ma")
    assert lock.elsewhere == (f"bob@{bob.client} (//imagine/main)",)
    [quiet] = alice.workspace.lock("props/lid.ma")
    assert quiet.elsewhere == ()


def test_cross_branch_lock_can_block_or_be_off(make_stream_session, bob, alice):
    alice.branches.create("crate-damage")
    bob.workspace.lock("props/crate.ma")

    blocking = make_stream_session("carol", cross_branch_locks="block")
    blocking.branches.switch("crate-damage")
    with pytest.raises(clio.LockedByOtherError) as caught:
        blocking.workspace.lock("props/crate.ma")
    assert caught.value.branch == "//imagine/main" and caught.value.user == "bob"
    assert not [r for r in blocking.backend.run("opened").records if "depotFile" in r]  # nothing opened

    blocking.config = blocking.config.with_overrides(cross_branch_locks="off")
    before = blocking.backend.calls["streams"]
    [lock] = blocking.workspace.lock("props/crate.ma")
    assert lock.elsewhere == () and blocking.backend.calls["streams"] == before


def test_retire(alice):
    alice.branches.create("crate-damage")
    edit_and_save(alice, {"props/crate.ma": "damaged"}, "Damage pass")
    with pytest.raises(clio.WorkflowError, match="Switch"):
        alice.branches.retire("crate-damage")
    alice.branches.switch("main")
    alice.branches.retire("crate-damage")
    assert [b.name for b in alice.branches.list()] == ["main"]
    # History stays.
    assert alice.backend.run("files", ["//imagine/crate-damage/props/crate.ma"]).records
    with pytest.raises(clio.WorkflowError, match="mainline"):
        alice.branches.retire("main")


def test_classic_workspaces_have_no_branches(make_session):
    s = make_session("clio_tester")
    assert s.stream == ""
    with pytest.raises(clio.NotInWorkspaceError, match="not on a branch"):
        s.branches.list()


def test_cli(alice, bob, p4_server, monkeypatch):
    runner = CliRunner()
    monkeypatch.setenv("P4TICKETS", os.fspath(p4_server.tickets))

    def run(*args, user=alice):
        monkeypatch.chdir(user.paths.root)
        base = ["--port", p4_server.port, "--user", user.user, "--client", user.client]
        return runner.invoke(cli, [*base, *args], catch_exceptions=False)

    created = run("branch", "create", "crate-damage")
    assert created.exit_code == 0, created.output
    alice.forget_workspace_state()
    edit_and_save(alice, {"props/crate.ma": "damaged"}, "Damage pass")
    edit_and_save(bob, {"props/crate.ma": "main v2"}, "main v2")

    listed = run("branch", "list")
    assert "* crate-damage" in listed.output and "main" in listed.output

    conflict = run("branch", "update")
    assert conflict.exit_code == 12
    assert "conflict  props/crate.ma" in conflict.output
    as_json = json.loads(run("--json", "branch", "update").output)
    assert as_json["error"] == "ConflictError" and as_json["conflicts"][0]["asset_path"] == "props/crate.ma"

    updated = run("branch", "update", "--mine", "props/crate.ma")
    assert updated.exit_code == 0, updated.output
    assert "kept mine   props/crate.ma" in updated.output
    published = run("branch", "publish", "-m", "Crate damage")
    assert published.exit_code == 0 and "Published 1 file(s)" in published.output

    write(alice.paths.local_path("props/tex.png"), "draft")
    shared = run("draft", "share", "-m", "WIP tex", "props/tex.png")
    assert shared.exit_code == 0 and "Shared draft" in shared.output
    assert "WIP tex" in run("draft", "list").output
