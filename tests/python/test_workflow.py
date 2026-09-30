"""Workflow services against a throwaway p4d: get, lock, save, status,
discard, with two users."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from deda import clio
from deda.clio import FileState


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.chmod(path.stat().st_mode | stat.S_IWRITE)
    path.write_text(text)


def writable(path: Path) -> bool:
    return bool(path.stat().st_mode & stat.S_IWRITE)


def seed(session: clio.Session, files: dict[str, str], message: str = "seed") -> int:
    for asset, text in files.items():
        write(session.paths.local_path(asset), text)
    return session.workspace.save(*files, message=message).change.number


@pytest.fixture
def alice(make_session) -> clio.Session:
    return make_session("clio_tester")


@pytest.fixture
def bob(make_session, alice) -> clio.Session:
    return make_session("bob")


def pending_changes(session: clio.Session) -> list[dict]:
    return session.backend.run("changes", ["-s", "pending", "-c", session.client]).records


def test_setup_creates_a_read_only_workspace(alice):
    info = alice.backend.run("client", ["-o", alice.client]).records[0]
    assert "noallwrite" in info["Options"]
    assert alice.paths.depot_root == "//depot/proj"
    assert alice.paths.root.is_dir()
    # Running setup again is harmless.
    assert alice.setup(name=alice.client) == alice.client


def test_save_adds_edits_and_deletes(alice):
    change = seed(alice, {"props/crate/crate.ma": "v1", "props/crate/tex/albedo.exr": "t1", "props/old.ma": "x"})
    assert change > 0
    ws = alice.workspace
    ws.lock("props/crate/crate.ma")
    write(alice.paths.local_path("props/crate/crate.ma"), "v2")
    write(alice.paths.local_path("props/crate/new.ma"), "n1")
    alice.paths.local_path("props/old.ma").chmod(stat.S_IWRITE | stat.S_IREAD)
    alice.paths.local_path("props/old.ma").unlink()

    result = ws.save("props", message="Crate: damage pass")
    assert result.added == ("props/crate/new.ma",)
    assert result.edited == ("props/crate/crate.ma",)
    assert result.deleted == ("props/old.ma",)
    assert result.change.number > change
    assert not pending_changes(alice)
    # Saved files are closed and read-only again.
    assert not writable(alice.paths.local_path("props/crate/crate.ma"))


def test_nothing_to_save_leaves_no_pending_change(alice):
    seed(alice, {"a.ma": "v1"})
    with pytest.raises(clio.NothingToSaveError):
        alice.workspace.save("a.ma", message="nothing")
    assert not pending_changes(alice)


def test_save_needs_a_message(alice):
    with pytest.raises(clio.WorkflowError, match="description"):
        alice.workspace.save(message="  ")


def test_get_preview_and_versions(alice, bob):
    v1 = seed(alice, {"props/crate.ma": "v1", "props/tex.exr": "t1"})
    alice.workspace.lock("props/crate.ma")
    write(alice.paths.local_path("props/crate.ma"), "v2")
    alice.workspace.save("props", message="v2")

    plan = bob.workspace.get("props", preview=True)
    assert isinstance(plan, clio.SyncPlan)
    assert plan.file_count == 2
    assert {f.asset_path for f in plan.files} == {"props/crate.ma", "props/tex.exr"}
    assert not bob.paths.local_path("props/crate.ma").exists()  # preview changes nothing

    events = []
    report = bob.workspace.get("props", progress=events.append)
    assert report.file_count == 2
    assert bob.paths.local_path("props/crate.ma").read_text() == "v2"
    assert events[0].total == 2 and events[-1].done == 2
    assert not writable(bob.paths.local_path("props/crate.ma"))

    bob.workspace.get("props/crate.ma", version=f"@{v1}")
    assert bob.paths.local_path("props/crate.ma").read_text() == "v1"
    assert bob.workspace.get("props").file_count == 1  # back to latest


def test_get_reports_paths_with_no_files(alice):
    seed(alice, {"a.ma": "v1"})
    report = alice.workspace.get("no/such/folder/")
    assert report.file_count == 0
    assert any("no such file" in w for w in report.warnings)


def test_cancel_before_get(alice):
    seed(alice, {"a.ma": "v1"})
    token = clio.CancelToken()
    token.cancel()
    with pytest.raises(clio.CancelledError):
        alice.workspace.get(cancel=token)


def test_lock_blocks_other_users(alice, bob):
    seed(alice, {"props/crate.ma": "v1"})
    bob.workspace.get()

    locks = alice.workspace.lock("props/crate.ma")
    assert [lk.asset_path for lk in locks] == ["props/crate.ma"]
    assert writable(alice.paths.local_path("props/crate.ma"))

    with pytest.raises(clio.LockedByOtherError) as caught:
        bob.workspace.lock("props/crate.ma")
    assert caught.value.user == "clio_tester"
    assert caught.value.workspace == alice.client
    assert caught.value.path == "props/crate.ma"

    [status] = bob.workspace.status("props/crate.ma")
    assert status.locked_by == f"clio_tester@{alice.client}"
    [mine] = alice.workspace.status("props/crate.ma")
    assert mine.locked_by_me and mine.state is FileState.EDITING

    assert alice.workspace.unlock("props/crate.ma") == ("props/crate.ma",)
    # Unchanged, so it is closed again and bob can lock it.
    assert not writable(alice.paths.local_path("props/crate.ma"))
    assert bob.workspace.lock("props/crate.ma")


def test_lock_needs_the_latest_version(alice, bob):
    seed(alice, {"a.ma": "v1"})
    bob.workspace.get()
    alice.workspace.lock("a.ma")
    write(alice.paths.local_path("a.ma"), "v2")
    alice.workspace.save("a.ma", message="v2")

    with pytest.raises(clio.OutOfDateError) as caught:
        bob.workspace.lock("a.ma")
    assert caught.value.paths == ("a.ma",)
    assert bob.workspace.lock("a.ma", allow_out_of_date=True)


def test_lock_a_whole_folder_and_new_files(alice):
    seed(alice, {"props/crate/a.ma": "a", "props/crate/b.ma": "b"})
    locks = alice.workspace.lock("props/crate")
    assert {lk.asset_path for lk in locks} == {"props/crate/a.ma", "props/crate/b.ma"}

    write(alice.paths.local_path("props/new.ma"), "n")
    assert [lk.asset_path for lk in alice.workspace.lock("props/new.ma")] == ["props/new.ma"]
    with pytest.raises(clio.NotInWorkspaceError):
        alice.workspace.lock("props/missing.ma")


def test_save_refuses_when_someone_saved_a_newer_version(alice, bob):
    seed(alice, {"a.ma": "v1"})
    bob.workspace.get()
    # Bob edits without locking, alice saves first.
    write(bob.paths.local_path("a.ma"), "bob's edit")
    alice.workspace.lock("a.ma")
    write(alice.paths.local_path("a.ma"), "alice v2")
    alice.workspace.save("a.ma", message="alice v2")

    with pytest.raises(clio.OutOfDateError):
        bob.workspace.save("a.ma", message="bob")
    assert not pending_changes(bob)
    assert bob.paths.local_path("a.ma").read_text() == "bob's edit"  # untouched


def test_status_states(alice, bob):
    seed(alice, {"s/synced.ma": "1", "s/stale.ma": "1", "s/edit.ma": "1", "s/gone.ma": "1", "s/mod.ma": "1"})
    bob.workspace.get()
    alice.workspace.lock("s/stale.ma")
    write(alice.paths.local_path("s/stale.ma"), "2")
    alice.workspace.save("s/stale.ma", message="stale for bob")
    seed(alice, {"s/unsynced.ma": "1"}, "not synced by bob")

    ws = bob.workspace
    ws.lock("s/edit.ma")
    write(bob.paths.local_path("s/mod.ma"), "changed without locking")
    write(bob.paths.local_path("s/new.ma"), "new")
    gone = bob.paths.local_path("s/gone.ma")
    gone.chmod(stat.S_IWRITE | stat.S_IREAD)
    gone.unlink()

    states = {f.asset_path: f.state for f in ws.status("s")}
    assert states == {
        "s/synced.ma": FileState.SYNCED,
        "s/stale.ma": FileState.OUT_OF_DATE,
        "s/edit.ma": FileState.EDITING,
        "s/gone.ma": FileState.DELETED,
        "s/mod.ma": FileState.MODIFIED,
        "s/new.ma": FileState.NEW,
        "s/unsynced.ma": FileState.MISSING,
    }
    quick = {f.asset_path: f.state for f in ws.status("s", check_disk=False)}
    assert quick["s/mod.ma"] is FileState.SYNCED  # the disk was not checked
    assert "s/new.ma" not in quick


def test_discard_keeps_a_backup(alice, tmp_path):
    seed(alice, {"a.ma": "v1"})
    with pytest.raises(clio.ConfirmationRequiredError):
        alice.workspace.discard("a.ma")
    alice.workspace.lock("a.ma")
    write(alice.paths.local_path("a.ma"), "unsaved work")

    backup_root = tmp_path / "backups"
    alice.config = alice.config.with_overrides(backup_dir=os.fspath(backup_root))
    result = alice.workspace.discard("a.ma", confirm=True)
    assert result.asset_paths == ("a.ma",)
    assert alice.paths.local_path("a.ma").read_text() == "v1"
    assert (result.backup_dir / "a.ma").read_text() == "unsaved work"
    assert result.backup_dir.is_relative_to(backup_root)


def test_reserved_characters_in_file_names(alice, bob):
    seed(alice, {"tex/crate@2x.png": "a", "tex/50%.png": "b"})
    bob.workspace.get("tex")
    assert bob.paths.local_path("tex/crate@2x.png").read_text() == "a"
    assert [lk.asset_path for lk in bob.workspace.lock("tex/crate@2x.png")] == ["tex/crate@2x.png"]
    write(bob.paths.local_path("tex/crate@2x.png"), "a2")
    assert bob.workspace.save("tex", message="retouch").edited == ("tex/crate@2x.png",)


def test_events(alice):
    seen = []
    for name in ("files_synced", "file_locked", "file_unlocked", "change_submitted"):
        alice.events.subscribe(name, lambda name=name, **payload: seen.append(name))
    seed(alice, {"a.ma": "v1"})
    alice.workspace.lock("a.ma")
    alice.workspace.unlock("a.ma")
    alice.workspace.get("a.ma", force=True)
    assert seen == ["change_submitted", "file_locked", "file_unlocked", "files_synced"]


def test_paths_outside_the_project_are_refused(alice, tmp_path):
    with pytest.raises(clio.NotInWorkspaceError):
        alice.workspace.get(tmp_path / "elsewhere.ma")
    with pytest.raises(clio.NotInWorkspaceError):
        alice.workspace.get("//other/depot/a.ma")
    with pytest.raises(clio.NotInWorkspaceError):
        alice.workspace.get("../outside.ma")


def test_parallel_get_and_save(make_session, p4_server, monkeypatch):
    # Each rsh: connection is its own p4d process, which reads configurables
    # when it starts: set this before the session connects.
    p4_server.connection().run_or_throw("configure", ["set", "net.parallel.max=4"])
    # Parallel transfers need the ticket from the environment (the usual
    # setup), not from the 'tickets' setting.
    monkeypatch.setenv("P4TICKETS", os.fspath(p4_server.tickets))
    alice = make_session("clio_tester", tickets="", parallel_threads=4, parallel_min_files=1)
    assert alice.workspace._parallel_args() == ["--parallel=threads=4,min=1"]
    assert alice.info()["maxParallel"] == "4"
    big = {f"many/f{i}.bin": os.urandom(64).hex() * 2000 for i in range(12)}
    seed(alice, big)
    alice.backend.run("sync", ["//depot/proj/many/...#none"])
    assert not alice.paths.local_path("many/f0.bin").exists()
    assert alice.workspace.get("many").file_count == 12
    assert alice.paths.local_path("many/f11.bin").read_text() == big["many/f11.bin"]


def test_no_parallel_transfer_with_a_ticket_file_setting(make_session):
    alice = make_session("clio_tester", parallel_threads=4)
    assert alice.workspace._parallel_args() == []
