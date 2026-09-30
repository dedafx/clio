"""Config, paths and error handling, with no server (FakeBackend)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from deda import clio
from deda.clio._paths import ProjectPaths, escape, unescape
from deda.clio.backends.core import translate_p4_error
from deda.clio.backends.fake import FakeBackend
from deda.clio.config import load_config
from deda.clio.session import Session

# --- config -------------------------------------------------------------------


def test_config_layers(tmp_path, monkeypatch):
    site = tmp_path / "site.toml"
    site.write_text('port = "ssl:site:1666"\nparallel_threads = 8\n[projects.imagine]\ndepot = "//imagine/main"\n')
    user = tmp_path / "user.toml"
    user.write_text('user = "sam"\n[projects.imagine]\nroot = "D:/work/imagine"\n')
    project_dir = tmp_path / "work" / "shot"
    project_dir.mkdir(parents=True)
    (tmp_path / "work" / ".clio.toml").write_text('project = "imagine"\nparallel_threads = 2\n')
    monkeypatch.setenv("CLIO_SITE_CONFIG", str(site))
    monkeypatch.setenv("CLIO_USER_CONFIG", str(user))

    config = load_config(cwd=project_dir, project="imagine", client="sam_ws")
    assert config.port == "ssl:site:1666"
    assert config.user == "sam"
    assert config.depot == "//imagine/main"
    assert config.root == "D:/work/imagine"
    assert config.parallel_threads == 2  # project file beats site
    assert config.client == "sam_ws"  # arguments beat everything
    assert config.sources["parallel_threads"].endswith(".clio.toml")


def test_config_rejects_unknown_keys_and_bad_types(tmp_path, monkeypatch):
    bad = tmp_path / "user.toml"
    monkeypatch.setenv("CLIO_USER_CONFIG", str(bad))
    bad.write_text('prot = "x"\n')
    with pytest.raises(clio.ConfigError, match="prot"):
        load_config(cwd=tmp_path)
    bad.write_text('parallel_threads = "many"\n')
    with pytest.raises(clio.ConfigError, match="parallel_threads"):
        load_config(cwd=tmp_path)
    with pytest.raises(clio.ConfigError):
        load_config(cwd=tmp_path, timeout=True)


# --- paths --------------------------------------------------------------------


def test_escape_round_trip():
    assert escape("crate@2x#1%*.png") == "crate%402x%231%25%2A.png"
    assert unescape("crate%402x%231%25%2A.png") == "crate@2x#1%*.png"
    assert unescape("100%2540.png") == "100%40.png"


def paths(tmp_path: Path) -> ProjectPaths:
    root = tmp_path / "ws"
    (root / "props" / "crate").mkdir(parents=True)
    (root / "props" / "crate" / "crate.ma").write_text("x")
    return ProjectPaths("//imagine/main", root)


def test_targets(tmp_path):
    p = paths(tmp_path)
    asked = []

    def dirs(depot_paths):
        asked.extend(depot_paths)
        return {"//imagine/main/chars/hero"}

    targets = p.targets(
        ["props/crate", "props/crate/crate.ma", "chars/hero", "chars/hero.ma", "sets/", "//imagine/main/fx/..."],
        is_depot_folder=dirs,
    )
    assert [(t.asset_path, t.is_folder) for t in targets] == [
        ("props/crate", True),  # a folder on disk
        ("props/crate/crate.ma", False),  # a file on disk
        ("chars/hero", True),  # a folder on the server
        ("chars/hero.ma", False),
        ("sets", True),  # trailing slash
        ("fx", True),  # depot path with /...
    ]
    assert asked == ["//imagine/main/chars/hero", "//imagine/main/chars/hero.ma"]  # one batched question
    assert targets[0].depot_spec(p.depot_root) == "//imagine/main/props/crate/..."
    assert p.targets([], is_depot_folder=dirs)[0].depot_spec(p.depot_root) == "//imagine/main/..."


def test_targets_from_the_command_line(tmp_path):
    p = paths(tmp_path)
    [t] = p.targets(["crate.ma"], is_depot_folder=lambda _: set(), local_base=p.root / "props" / "crate")
    assert t.asset_path == "props/crate/crate.ma"
    [root] = p.targets(["."], is_depot_folder=lambda _: set(), local_base=p.root)
    assert root.asset_path == "" and root.is_folder
    with pytest.raises(clio.NotInWorkspaceError):
        p.targets([".."], is_depot_folder=lambda _: set(), local_base=p.root)
    with pytest.raises(clio.NotInWorkspaceError):
        p.targets(["../x.ma"], is_depot_folder=lambda _: set())


@pytest.mark.skipif(sys.platform != "win32", reason="Windows paths are case-insensitive")
def test_windows_paths_ignore_case(tmp_path):
    p = paths(tmp_path)
    local = str(p.root / "Props" / "Crate" / "crate.ma").upper()
    assert p.asset_path_of_local(Path(local)).lower() == "props/crate/crate.ma"


# --- errors through the fake backend ----------------------------------------------


def fake_session(backend: FakeBackend) -> Session:
    config = load_config(cwd=Path.cwd(), user="sam", client="sam_ws", depot="//imagine/main", root=str(Path.cwd()))
    backend.on("info", records=[{"userName": "sam", "clientName": "sam_ws", "serverID": "test"}])
    return Session(config, backend=backend)


def test_a_trigger_rejection_leaves_the_change_pending():
    backend = FakeBackend()
    backend.on("change", info=["Change 12 created."])
    backend.on("opened", records=[{"depotFile": "//imagine/main/a.ma", "action": "edit"}])
    backend.on("fstat", records=[{"depotFile": "//imagine/main/a.ma", "action": "edit", "haveRev": "3",
                                  "headRev": "3"}])
    backend.on("submit", errors=["Submit validation failed -- fix problems then use 'p4 submit -c 12'.",
                                 "'check-names' validation failed: bad file name"])
    session = fake_session(backend)
    with pytest.raises(clio.ServerPolicyError) as caught:
        session.workspace.save("//imagine/main/a.ma", message="x")
    assert caught.value.change == 12
    assert any("check-names" in m for m in caught.value.p4_messages)
    # The change was not deleted, and nothing was reverted.
    assert not [c for c in backend.calls if c.command in ("revert",) or c.args[:1] == ("-d",)]


def test_a_failed_precheck_removes_the_new_change():
    backend = FakeBackend()
    backend.on("change", info=["Change 12 created."])
    backend.on("opened", records=[{"depotFile": "//imagine/main/a.ma", "action": "edit"}])
    backend.on("fstat", records=[{"depotFile": "//imagine/main/a.ma", "action": "edit", "haveRev": "2",
                                  "headRev": "3"}])
    session = fake_session(backend)
    with pytest.raises(clio.OutOfDateError):
        session.workspace.save("//imagine/main/a.ma", message="x")
    assert ("change", ("-d", "12")) in [(c.command, c.args) for c in backend.calls]
    assert "submit" not in backend.commands()


def test_core_errors_are_translated():
    auth = translate_p4_error(clio.P4Error("p4 fstat failed: Perforce password (P4PASSWD) invalid or unset."))
    assert isinstance(auth, clio.AuthError) and auth.exit_code == 3
    down = translate_p4_error(clio.P4Error("Connect to server failed; check $P4PORT."))
    assert isinstance(down, clio.ServerUnavailableError)
    other = clio.P4Error("something else")
    assert translate_p4_error(other) is other


def test_depot_root_from_the_workspace():
    backend = FakeBackend()
    backend.on("client", records=[{"Client": "sam_ws", "Root": "D:/work", "Access": "2026/01/01",
                                   "View0": "//imagine/main/... //sam_ws/..."}])
    config = load_config(cwd=Path.cwd(), user="sam", client="sam_ws")
    session = Session(config, backend=backend)
    assert session.paths.depot_root == "//imagine/main"
    assert session.paths.root == Path("D:/work")

    backend.on("client", records=[{"Client": "sam_ws", "Root": "D:/work", "Access": "x", "Stream": "//imagine/dev"}])
    assert Session(config, backend=backend).paths.depot_root == "//imagine/dev"


def test_no_workspace():
    backend = FakeBackend()
    backend.on("info", records=[{"userName": "sam", "clientName": "*unknown*"}])
    session = Session(load_config(cwd=Path.cwd(), user="sam"), backend=backend)
    with pytest.raises(clio.NotInWorkspaceError, match="setup"):
        session.workspace.status()


def test_event_bus_unsubscribe():
    bus = clio.EventBus()
    seen = []
    unsubscribe = bus.subscribe("x", lambda **kw: seen.append(kw))
    bus.emit("x", a=1)
    unsubscribe()
    bus.emit("x", a=2)
    assert seen == [{"a": 1}]
