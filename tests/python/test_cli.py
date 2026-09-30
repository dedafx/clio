"""The clio command line against a throwaway p4d."""

from __future__ import annotations

import json
import os
import stat

import pytest
from click.testing import CliRunner

from deda import clio
from deda.clio.cli import cli


@pytest.fixture
def run(make_session, p4_server, monkeypatch):
    alice = make_session("clio_tester")
    bob = make_session("bob")
    runner = CliRunner()

    def invoke(*args: str, user=alice, cwd=None, input=None):
        monkeypatch.chdir(cwd or user.paths.root)
        base = ["--port", p4_server.port, "--user", user.user, "--client", user.client]
        monkeypatch.setenv("P4TICKETS", os.fspath(p4_server.tickets))
        return runner.invoke(cli, [*base, *args], input=input, catch_exceptions=False)

    invoke.alice, invoke.bob = alice, bob
    return invoke


def test_save_status_lock_and_json(run):
    alice, bob = run.alice, run.bob
    (alice.paths.root / "props").mkdir()
    (alice.paths.root / "props" / "crate.ma").write_text("v1")

    status = run("status")
    assert status.exit_code == 0
    assert "new (not in Perforce)" in status.output and "props/crate.ma" in status.output

    saved = run("save", "-m", "Crate v1", "props")
    assert saved.exit_code == 0, saved.output
    assert "added    props/crate.ma" in saved.output

    got = run("get", user=bob)
    assert got.exit_code == 0 and "Got 1 file(s)" in got.output

    # Paths are relative to the current folder.
    locked = run("lock", "crate.ma", cwd=alice.paths.root / "props")
    assert locked.exit_code == 0 and "locked  props/crate.ma" in locked.output

    refused = run("lock", "props/crate.ma", user=bob)
    assert refused.exit_code == 5
    assert "locked by clio_tester" in refused.output

    who = run("--json", "who", "props", user=bob)
    [entry] = json.loads(who.output)
    assert entry["locked_by"] == f"clio_tester@{alice.client}"

    as_json = run("--json", "lock", "props/crate.ma", user=bob)
    assert as_json.exit_code == 5
    error = json.loads(as_json.output)
    assert error["error"] == "LockedByOtherError" and error["hint"]


def test_history_activity_and_preview(run):
    alice = run.alice
    (alice.paths.root / "a.ma").write_text("v1")
    assert run("save", "-m", "first", "a.ma").exit_code == 0
    assert run("lock", "a.ma").exit_code == 0
    path = alice.paths.root / "a.ma"
    path.chmod(path.stat().st_mode | stat.S_IWRITE)
    path.write_text("v2")
    assert run("save", "-m", "second", "a.ma").exit_code == 0

    history = run("history", "a.ma")
    assert history.exit_code == 0
    lines = history.output.splitlines()
    assert lines[0].startswith("v2") and "second" in lines[0]
    assert lines[1].startswith("v1") and "first" in lines[1]

    activity = json.loads(run("--json", "activity").output)
    assert [c["description"] for c in activity["items"]] == ["second", "first"]

    preview = run("get", "--preview", user=run.bob)
    assert "Would get 1 file(s)" in preview.output
    assert not (run.bob.paths.root / "a.ma").exists()


def test_nothing_to_save_and_discard(run):
    alice = run.alice
    (alice.paths.root / "a.ma").write_text("v1")
    run("save", "-m", "first", "a.ma")
    nothing = run("save", "-m", "again", "a.ma")
    assert nothing.exit_code == 7
    assert "Nothing to save" in nothing.output

    run("lock", "a.ma")
    path = alice.paths.root / "a.ma"
    path.chmod(path.stat().st_mode | stat.S_IWRITE)
    path.write_text("oops")
    declined = run("discard", "a.ma", input="n\n")
    assert declined.exit_code == 1  # click.Abort
    assert path.read_text() == "oops"
    done = run("discard", "a.ma", "--yes", "--no-backup")
    assert done.exit_code == 0 and "discarded  a.ma" in done.output
    assert path.read_text() == "v1"


def test_errors_have_exit_codes(run, tmp_path):
    outside = run("get", os.fspath(tmp_path))
    assert outside.exit_code == 10
    assert "outside the workspace" in outside.output


def test_version():
    result = CliRunner().invoke(cli, ["--version"])
    assert result.exit_code == 0 and clio.__version__ in result.output
