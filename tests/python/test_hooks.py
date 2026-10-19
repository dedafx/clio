"""Validation hooks: checks that run before a save is submitted."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from importlib import metadata

import pytest
from click.testing import CliRunner

from deda import clio
from deda.clio import hooks
from deda.clio.cli import cli
from deda.clio.hooks import HookIssue, IssueSeverity


@pytest.fixture
def alice(make_session):
    return make_session()


def _write(session, relative: str, text: str) -> None:
    path = session.paths.local_path(relative)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _submitted(session) -> list[str]:
    result = session.backend.run("files", [f"{session.paths.depot_root}/..."])
    return sorted(session.paths.asset_path_of_depot(r["depotFile"]) for r in result.records)


def test_an_error_stops_the_save_and_leaves_everything_as_it_was(alice):
    _write(alice, "props/crate.ma", "v1")

    def no_ma(context):
        return [HookIssue(f.asset_path, "Maya files must be saved as .mb") for f in context.files
                if f.asset_path.endswith(".ma")]

    alice.hooks.register("no-ma", no_ma)

    with pytest.raises(clio.ValidationError) as caught:
        alice.workspace.save("props", message="Crate")

    error = caught.value
    assert error.exit_code == 14
    assert error.problems == ("props/crate.ma: Maya files must be saved as .mb",)
    assert "Nothing was saved" in error.hint
    assert _submitted(alice) == []  # nothing reached the server
    assert alice.paths.local_path("props/crate.ma").read_text() == "v1"  # the file is untouched
    # The file is back in the default change, as after any refused save; no pending change is left.
    opened = alice.backend.run("opened", [f"{alice.paths.depot_root}/..."]).records
    assert [r["change"] for r in opened] == ["default"]
    assert alice.backend.run("changes", ["-s", "pending", "-c", alice.client]).records == []


def test_a_warning_is_reported_and_the_save_goes_on(alice):
    _write(alice, "props/crate.ma", "v1")
    alice.hooks.register("loud", lambda c: [HookIssue("props/crate.ma", "large for a Maya file",
                                                      IssueSeverity.WARNING)])

    result = alice.workspace.save("props", message="Crate")

    assert result.warnings == ("props/crate.ma: large for a Maya file",)
    assert _submitted(alice) == ["props/crate.ma"]


def test_a_hook_sees_the_files_actions_and_message(alice):
    _write(alice, "a.usda", "#usda 1.0\n")
    alice.workspace.save(message="first")
    _write(alice, "b.usda", "#usda 1.0\n")
    a = alice.paths.local_path("a.usda")
    a.chmod(a.stat().st_mode | stat.S_IWRITE)
    a.write_text("#usda 1.0\n# edited\n")
    seen = []
    alice.hooks.register("spy", lambda c: seen.append((c.message, sorted((f.asset_path, f.action) for f in c.files),
                                                      c.files[0].local_path.is_absolute(), c.session is alice)))

    alice.workspace.save(message="second")

    assert seen == [("second", [("a.usda", "edit"), ("b.usda", "add")], True, True)]


def test_a_hook_that_raises_is_a_warning_not_a_block(alice):
    _write(alice, "a.usda", "#usda 1.0\n")

    def broken(context):
        raise RuntimeError("oops")

    alice.hooks.register("broken", broken)

    result = alice.workspace.save(message="first")

    assert len(result.warnings) == 1 and "'broken' could not run" in result.warnings[0] and "oops" in result.warnings[0]
    assert _submitted(alice) == ["a.usda"]


def test_validate_false_skips_the_hooks(alice):
    _write(alice, "a.usda", "x")
    alice.hooks.register("never", lambda c: [HookIssue("", "no")])

    result = alice.workspace.save(message="first", validate=False)

    assert result.warnings == () and _submitted(alice) == ["a.usda"]


def test_process_wide_hooks_and_unregistering(alice):
    _write(alice, "a.usda", "x")
    unregister = hooks.register("global-no", lambda c: [HookIssue("", "blocked by the studio")])
    try:
        with pytest.raises(clio.ValidationError, match="1 problem"):
            alice.workspace.save(message="first")
    finally:
        unregister()
    assert alice.workspace.save(message="first").change.number > 0


def test_entry_point_hooks_run(alice, monkeypatch):
    _write(alice, "a.usda", "x")

    class FakeEntryPoint:
        name = "studio-rule"

        def load(self):
            return lambda c: [HookIssue("a.usda", "studio rule broken")]

    monkeypatch.setattr(metadata, "entry_points", lambda group=None: [FakeEntryPoint()] if group == hooks.ENTRY_POINT_GROUP else [])

    with pytest.raises(clio.ValidationError) as caught:
        alice.workspace.save(message="first")
    assert caught.value.problems == ("a.usda: studio rule broken",)

    class Broken(FakeEntryPoint):
        name = "broken-plugin"

        def load(self):
            raise ImportError("no module")

    monkeypatch.setattr(metadata, "entry_points", lambda group=None: [Broken()] if group == hooks.ENTRY_POINT_GROUP else [])
    result = alice.workspace.save(message="first")
    assert any("'broken-plugin' could not be loaded" in w for w in result.warnings)


# --- the built-in hooks -------------------------------------------------------


def test_naming_hook_checks_new_files_only(make_session):
    session = make_session(hooks="naming", naming_pattern=r"[a-z0-9_]+\.[a-z0-9]+")
    _write(session, "props/crate_lid.ma", "ok")
    _write(session, "props/Crate Lid.ma", "bad")

    with pytest.raises(clio.ValidationError) as caught:
        session.workspace.save("props", message="Crate")
    assert caught.value.problems == (
        "props/Crate Lid.ma: 'Crate Lid.ma' does not match the naming rule [a-z0-9_]+\\.[a-z0-9]+ (Rename the file before saving)",)

    # The refused file is still open for add in the default change, as after any refused save.
    (session.paths.root / "props" / "Crate Lid.ma").unlink()
    session.backend.run("revert", [session.paths.depot_path("props/Crate Lid.ma")])
    session.workspace.save("props", message="Crate")
    # An old, badly named file does not block later saves: only additions are checked.
    old = session.paths.local_path("props/crate_lid.ma")
    old.chmod(old.stat().st_mode | stat.S_IWRITE)
    old.write_text("changed")
    assert session.workspace.save("props", message="Crate again").change.number > 0


def test_unknown_builtin_hook_name_is_a_config_error(make_session):
    session = make_session(hooks="nope")
    _write(session, "a.usda", "x")
    with pytest.raises(clio.ConfigError, match="Unknown hook 'nope'"):
        session.workspace.save(message="first")
    assert session.backend.run("changes", ["-s", "pending", "-c", session.client]).records == []


def test_hooks_setting_accepts_a_list_and_a_comma_text():
    assert clio.load_config(hooks=["naming", "no-absolute-paths"]).hooks == ("naming", "no-absolute-paths")
    assert clio.load_config(hooks="naming, no-absolute-paths").hooks == ("naming", "no-absolute-paths")
    with pytest.raises(clio.ConfigError):
        clio.load_config(hooks=[1])


pxr = pytest.importorskip("pxr", reason="USD Python bindings (pxr) are not importable")


def test_no_absolute_paths_hook_finds_absolute_references(make_session):
    session = make_session(hooks="no-absolute-paths")
    _write(session, "good.usda", '#usda 1.0\n(\n    subLayers = [@./other.usda@, @tex/a.usda@]\n)\n')
    _write(session, "bad.usda",
           '#usda 1.0\n(\n    subLayers = [@D:/work/shot.usda@]\n)\n'
           'def "P" (\n    prepend references = @/mnt/assets/crate.usda@\n) {\n'
           '    asset tex = @C:\\textures\\a.exr@\n    asset ok = @./b.exr@\n}\n')

    with pytest.raises(clio.ValidationError) as caught:
        session.workspace.save(message="paths")

    problems = "\n".join(caught.value.problems)
    assert "bad.usda: refers to the absolute path D:/work/shot.usda" in problems
    assert "bad.usda: refers to the absolute path /mnt/assets/crate.usda" in problems
    assert "bad.usda: refers to the absolute path C:\\textures\\a.exr" in problems
    assert "good.usda" not in problems and "./b.exr" not in problems
    assert len(caught.value.problems) == 3


# --- the command line ---------------------------------------------------------


def test_cli_save_reports_validation_and_can_skip_it(alice, p4_server, monkeypatch):
    runner = CliRunner()
    monkeypatch.chdir(alice.paths.root)
    monkeypatch.setenv("P4TICKETS", os.fspath(p4_server.tickets))
    base = ["--port", p4_server.port, "--user", alice.user, "--client", alice.client]
    _write(alice, "props/Bad Name.ma", "x")
    Path(os.environ["CLIO_USER_CONFIG"]).write_text(
        'hooks = ["naming"]\nnaming_pattern = \'[a-z_]+\\.ma\'\n')  # a TOML literal string: no escapes
    config: list[str] = []

    refused = runner.invoke(cli, [*base, *config, "save", "-m", "Crate", "props"], catch_exceptions=False)
    assert refused.exit_code == 14
    assert "props/Bad Name.ma: 'Bad Name.ma' does not match" in refused.output

    skipped = runner.invoke(cli, [*base, *config, "save", "-m", "Crate", "--no-validate", "props"],
                            catch_exceptions=False)
    assert skipped.exit_code == 0, skipped.output
