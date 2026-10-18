"""``clio doctor`` against a throwaway p4d."""

from __future__ import annotations

import json
import os

import pytest
from click.testing import CliRunner

from deda import clio
from deda.clio.cli import cli
from deda.clio.doctor import CheckStatus, recommended_typemap, run_doctor


def _status(report, name: str) -> CheckStatus:
    return next(c.status for c in report.checks if c.name == name)


def _check(report, name: str):
    return next(c for c in report.checks if c.name == name)


@pytest.fixture
def alice(make_session):
    return make_session()


def test_a_working_setup_has_no_failures(alice, monkeypatch, tmp_path):
    monkeypatch.setenv("CLIO_VERSION_STORE", os.fspath(tmp_path / "store"))
    report = run_doctor(alice)

    assert report.ok
    assert [c.name for c in report.checks] == [
        "Configuration", "Server", "Login", "Workspace", "Workspace options", "Parallel transfers", "Typemap",
        "USD", "Version store"]
    assert _status(report, "Server") is CheckStatus.OK
    assert _status(report, "Login") is CheckStatus.OK
    assert _status(report, "Workspace") is CheckStatus.OK
    assert _status(report, "Version store") is CheckStatus.OK
    assert "//depot/proj" in _check(report, "Workspace").message
    # The throwaway server has no typemap and no parallel setting: advice, not failure.
    assert _status(report, "Typemap") is CheckStatus.WARN
    assert "clio doctor --typemap" in _check(report, "Typemap").hint
    assert _status(report, "Parallel transfers") in (CheckStatus.WARN, CheckStatus.SKIP)


def test_the_recommended_typemap_clears_the_typemap_warning(alice, p4_server):
    text = recommended_typemap()
    assert text.startswith("TypeMap:\n\tbinary+l //....ma\n")
    assert "\ttext //....usda\n" in text
    p4_server.connection().run_or_throw("typemap", ["-i"], text)

    report = run_doctor(alice)

    assert _status(report, "Typemap") is CheckStatus.OK


def test_an_allwrite_workspace_is_a_warning(alice, p4_server):
    spec = (f"Client: {alice.client}\nOwner: {alice.user}\nRoot: {alice.paths.root}\n"
            "Options: allwrite noclobber nocompress unlocked nomodtime normdir\nLineEnd: local\n"
            f"View:\n\t//depot/proj/... //{alice.client}/...\n")
    alice.backend.run("client", ["-i"], spec)

    check = _check(run_doctor(alice), "Workspace options")

    assert check.status is CheckStatus.WARN and "allwrite" in check.message
    assert "noallwrite" in check.hint


def test_modtime_is_mentioned(alice):
    spec = (f"Client: {alice.client}\nOwner: {alice.user}\nRoot: {alice.paths.root}\n"
            "Options: noallwrite noclobber nocompress unlocked modtime normdir\nLineEnd: local\n"
            f"View:\n\t//depot/proj/... //{alice.client}/...\n")
    alice.backend.run("client", ["-i"], spec)

    check = _check(run_doctor(alice), "Workspace options")

    assert check.status is CheckStatus.OK and "timestamps" in check.message


def test_an_unreachable_server_fails_once_and_skips_the_rest(tmp_path, monkeypatch):
    monkeypatch.setenv("CLIO_VERSION_STORE", os.fspath(tmp_path / "store"))
    session = clio.connect(port="localhost:1", user="nobody", client="none", connect_timeout=2,
                           root=os.fspath(tmp_path / "ws"), depot="//depot/proj")
    try:
        report = run_doctor(session)
    finally:
        session.close()

    assert not report.ok
    assert _status(report, "Server") is CheckStatus.FAIL
    assert "network" in _check(report, "Server").hint
    for name in ("Login", "Workspace", "Workspace options", "Parallel transfers", "Typemap"):
        assert _status(report, name) is CheckStatus.SKIP
    assert _status(report, "Configuration") is CheckStatus.OK  # does not need the server
    assert _status(report, "Version store") is CheckStatus.OK


def _logged_out(p4_server, alice, tmp_path):
    """The same user and workspace, with no ticket (a new ticket file)."""
    return clio.connect(port=p4_server.port, user=alice.user, client=alice.client,
                        tickets=os.fspath(tmp_path / "no-tickets"), root=os.fspath(alice.paths.root),
                        depot="//depot/proj")


def test_not_logged_in_is_a_failure_with_a_hint(alice, p4_server, tmp_path):
    session = _logged_out(p4_server, alice, tmp_path)
    try:
        report = run_doctor(session)
    finally:
        session.close()

    assert not report.ok
    login = _check(report, "Login")
    assert login.status is CheckStatus.FAIL and "clio login" in login.hint
    # Checks that need the ticket are skipped, not reported as bugs.
    assert _status(report, "Typemap") in (CheckStatus.SKIP, CheckStatus.WARN)
    assert all("itself failed" not in c.message for c in report.checks)


def test_a_missing_workspace_folder_fails(alice):
    import shutil

    shutil.rmtree(alice.paths.root)

    check = _check(run_doctor(alice), "Workspace")

    assert check.status is CheckStatus.FAIL and "does not exist" in check.message


def test_a_check_that_breaks_does_not_hide_the_others(alice, monkeypatch):
    from deda.clio import doctor

    def boom(session):
        raise RuntimeError("surprise")

    monkeypatch.setattr(doctor, "_typemap", boom)
    report = run_doctor(alice)

    assert "surprise" in _check(report, "Typemap").message
    assert _status(report, "Typemap") is CheckStatus.FAIL
    assert _status(report, "Server") is CheckStatus.OK


@pytest.fixture
def run(alice, p4_server, monkeypatch, tmp_path):
    runner = CliRunner()
    monkeypatch.setenv("CLIO_VERSION_STORE", os.fspath(tmp_path / "store"))

    def invoke(*args: str):
        monkeypatch.chdir(alice.paths.root)
        monkeypatch.setenv("P4TICKETS", os.fspath(p4_server.tickets))
        base = ["--port", p4_server.port, "--user", alice.user, "--client", alice.client]
        return runner.invoke(cli, [*base, *args], catch_exceptions=False)

    return invoke


def test_cli_doctor(run):
    result = run("doctor")
    assert result.exit_code == 0, result.output
    assert "[ok  ] Server:" in result.output
    assert "[warn] Typemap:" in result.output
    assert "warning(s)" in result.output

    as_json = json.loads(run("--json", "doctor").output)
    assert {c["name"] for c in as_json["checks"]} >= {"Server", "Login", "Typemap"}
    assert all(c["status"] in ("ok", "warn", "fail", "skip") for c in as_json["checks"])


def test_cli_doctor_typemap_needs_no_server(run):
    result = run("doctor", "--typemap")
    assert result.exit_code == 0
    assert result.output == recommended_typemap()


def test_cli_doctor_exits_1_on_failure(run, alice, p4_server, monkeypatch, tmp_path):
    runner = CliRunner()
    monkeypatch.chdir(alice.paths.root)
    monkeypatch.setenv("P4TICKETS", os.fspath(tmp_path / "no-tickets"))
    base = ["--port", p4_server.port, "--user", alice.user, "--client", alice.client]
    result = runner.invoke(cli, [*base, "doctor"], catch_exceptions=False)
    assert result.exit_code == 1
    assert "[FAIL] Login:" in result.output
