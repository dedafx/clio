"""Assets: named groups of files, found by asset resolver plugins."""

from __future__ import annotations

import json
import os
import stat
from importlib import metadata

import pytest
from click.testing import CliRunner

from deda import clio
from deda.clio import assets
from deda.clio.cli import cli

#: What a Dedaverse plugin would look up in the project's database: "Bob" is
#: a 3d character made of parts that live in different folders.
BOB = [
    "chars/bob/model/bob_mesh.usda",
    "chars/bob/look/bob_look.usda",
    "chars/bob/tex/",                      # a folder of textures
    "chars/bob/rig/bob_skel.usda",
    "chars/bob/anim/walk.usda",
]


class DedaversePlugin:
    name = "dedaverse"

    def files(self, session, asset_id):
        if asset_id == "Bob":
            return BOB + ["chars/bob/rig/bob_skel.usda"]  # a duplicate on purpose
        raise clio.AssetNotFoundError(f"No Dedaverse asset named {asset_id}")


class ImaginePlugin:
    """An Imagine asset: a photograph and all its variants."""

    name = "imagine"

    def files(self, session, asset_id):
        return [f"photos/{asset_id}/original.tif", f"photos/{asset_id}/print_v1.tif", f"photos/{asset_id}/print_v2.tif"]


def _write(session, relative: str, text: str) -> None:
    path = session.paths.local_path(relative)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.chmod(path.stat().st_mode | stat.S_IWRITE)
    path.write_text(text)


@pytest.fixture
def alice(make_session):
    session = make_session()
    for relative in [b for b in BOB if not b.endswith("/")] + ["chars/bob/tex/skin.png", "chars/bob/tex/eyes.png",
                                                                "chars/alice/model/alice_mesh.usda"]:
        _write(session, relative, relative)
    session.workspace.save(message="Bob and Alice")
    session.assets.register(DedaversePlugin())
    session.assets.register(ImaginePlugin())
    return session


def _opened(session) -> dict[str, str]:
    result = session.backend.run("opened", [f"{session.paths.depot_root}/..."])
    return {session.paths.asset_path_of_depot(r["depotFile"]): r["action"] for r in result.records}


def test_an_asset_is_the_files_its_plugin_lists(alice):
    found = alice.assets.files("dedaverse:Bob")

    assert found.kind == "dedaverse" and found.asset_id == "Bob" and found.spec == "dedaverse:Bob"
    assert found.paths == tuple(BOB)  # the duplicate is gone, the order is the plugin's
    assert alice.assets.kinds() == ("dedaverse", "folder", "imagine")


def test_a_bare_path_is_the_folder_asset(alice):
    found = alice.assets.files("chars/alice")
    assert (found.kind, found.paths) == ("folder", ("chars/alice/",))
    # "Bob" is not a prefix of a known kind, so it is a folder named Bob.
    assert alice.assets.files("Bob").paths == ("Bob/",)
    assert alice.assets.files("C:/work/crate").paths == ("C:/work/crate/",)


def test_the_default_kind_can_be_set(make_session):
    session = make_session(asset_resolver="imagine")
    session.assets.register(ImaginePlugin())
    assert session.assets.files("IMG_0042").paths[0] == "photos/IMG_0042/original.tif"
    session2 = make_session("bob", asset_resolver="nope")
    with pytest.raises(clio.ConfigError, match="Unknown asset resolver 'nope'"):
        session2.assets.files("x")


def test_actions_run_on_every_part_of_the_asset(alice, make_session):
    # A fresh workspace: get the whole asset, and only the asset.
    fresh = make_session("kim")
    fresh.assets.register(DedaversePlugin())
    report = fresh.assets.get("dedaverse:Bob")
    assert report.file_count == 6  # four files, and the two in the textures folder
    assert fresh.paths.local_path("chars/bob/anim/walk.usda").is_file()
    assert not fresh.paths.local_path("chars/alice/model/alice_mesh.usda").exists()  # not part of Bob

    locks = fresh.assets.lock("dedaverse:Bob")
    assert {lk.asset_path for lk in locks} == {"chars/bob/model/bob_mesh.usda", "chars/bob/look/bob_look.usda",
                                               "chars/bob/rig/bob_skel.usda", "chars/bob/anim/walk.usda",
                                               "chars/bob/tex/skin.png", "chars/bob/tex/eyes.png"}
    assert set(_opened(fresh).values()) == {"edit"}

    _write(fresh, "chars/bob/look/bob_look.usda", "new look")
    status = {f.asset_path: f.state for f in fresh.assets.status("dedaverse:Bob")}
    assert status["chars/bob/look/bob_look.usda"] is clio.FileState.EDITING

    result = fresh.assets.save("dedaverse:Bob", message="Bob: new look")
    # One change for the whole asset: the locked files go in it with the one that changed.
    assert len(result.edited) == 6 and "chars/bob/look/bob_look.usda" in result.edited
    assert fresh.assets.unlock("dedaverse:Bob") == ()
    activity = fresh.assets.activity("dedaverse:Bob")
    assert [c.description for c in activity][:2] == ["Bob: new look", "Bob and Alice"]


def test_an_imagine_asset_groups_the_variants_of_a_photo(alice):
    for name in ("original", "print_v1", "print_v2"):
        _write(alice, f"photos/IMG_7/{name}.tif", name)
    alice.workspace.save(message="photo")

    status = alice.assets.status("imagine:IMG_7", check_disk=False)

    assert [f.asset_path for f in status] == [f"photos/IMG_7/{n}.tif" for n in ("original", "print_v1", "print_v2")]


def test_unknown_assets_and_empty_assets_are_errors_with_hints(alice):
    with pytest.raises(clio.AssetNotFoundError, match="No Dedaverse asset named Zed"):
        alice.assets.files("dedaverse:Zed")

    class Empty:
        name = "empty"

        def files(self, session, asset_id):
            return []

    alice.assets.register(Empty())
    with pytest.raises(clio.AssetNotFoundError) as caught:
        alice.assets.files("empty:Anything")
    assert caught.value.exit_code == 15 and caught.value.hint

    with pytest.raises(clio.AssetNotFoundError):
        alice.assets.files("folder:")


def test_a_plugin_that_breaks_is_named(alice):
    class Broken:
        name = "broken"

        def files(self, session, asset_id):
            raise KeyError("database is down")

    alice.assets.register(Broken())
    with pytest.raises(clio.WorkflowError) as caught:
        alice.assets.files("broken:Bob")
    assert "'broken' failed for 'Bob'" in caught.value.message and "database is down" in caught.value.message


def test_plugins_come_from_entry_points(make_session, monkeypatch):
    class FakeEntryPoint:
        name = "dedaverse"

        def load(self):
            return DedaversePlugin  # a class: Clio makes the plugin

    monkeypatch.setattr(metadata, "entry_points",
                        lambda group=None: [FakeEntryPoint()] if group == assets.ENTRY_POINT_GROUP else [])
    session = make_session()
    assert session.assets.files("dedaverse:Bob").paths == tuple(BOB)

    class Broken(FakeEntryPoint):
        def load(self):
            raise ImportError("no module named dedaverse")

    monkeypatch.setattr(metadata, "entry_points",
                        lambda group=None: [Broken()] if group == assets.ENTRY_POINT_GROUP else [])
    with pytest.raises(clio.ConfigError, match="could not be loaded"):
        session.assets.files("Bob")


@pytest.fixture
def run(alice, p4_server, monkeypatch):
    runner = CliRunner()
    # The CLI builds its own session, so the plugin comes from an entry point.
    class FakeEntryPoint:
        name = "dedaverse"

        def load(self):
            return DedaversePlugin

    monkeypatch.setattr(metadata, "entry_points",
                        lambda group=None: [FakeEntryPoint()] if group == assets.ENTRY_POINT_GROUP else [])

    def invoke(*args: str):
        monkeypatch.chdir(alice.paths.root)
        monkeypatch.setenv("P4TICKETS", os.fspath(p4_server.tickets))
        base = ["--port", p4_server.port, "--user", alice.user, "--client", alice.client]
        return runner.invoke(cli, [*base, *args], catch_exceptions=False)

    return invoke


def test_cli_asset_commands(run, alice):
    files = run("asset", "files", "dedaverse:Bob")
    assert files.exit_code == 0 and files.output.splitlines() == BOB

    _write(alice, "chars/bob/look/bob_look.usda", "edited without a lock")
    status = run("asset", "status", "dedaverse:Bob")
    assert status.exit_code == 0 and "chars/bob/look/bob_look.usda" in status.output
    assert "alice_mesh" not in status.output

    saved = run("asset", "save", "dedaverse:Bob", "-m", "Bob: look")
    assert saved.exit_code == 0 and saved.output.startswith("Saved change")

    missing = run("asset", "files", "dedaverse:Zed")
    assert missing.exit_code == 15 and "No Dedaverse asset named Zed" in missing.output

    as_json = json.loads(run("--json", "asset", "files", "chars/alice").output)
    assert as_json["paths"] == ["chars/alice/"] and as_json["kind"] == "folder"
