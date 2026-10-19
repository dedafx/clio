"""Option A (design doc §10.2): ``deda.clio.usd.prepare`` makes a stage's
files present with plain USD, no plugin. It uses the same core as the
plugin, so the files land in the same places.

``prepare_for_edit`` and ``finish_edit`` are the Python counterpart of the
plugin's write side (§10.6).
"""

from __future__ import annotations

import pytest

from deda import clio
from deda.clio import usd as clio_usd

pytest.importorskip("pxr", reason="USD Python bindings (pxr) are not importable")
from pxr import Usd  # noqa: E402


def _crate(version: int, extra: str = "") -> str:
    return (f'#usda 1.0\n(\n    subLayers = [@./geo.usda@]\n)\n'
            f'def "Crate" {{\n    int version = {version}\n{extra}}}\n')


def _geo(version: int) -> str:
    return f'#usda 1.0\ndef "Geo" {{\n    int version = {version}\n}}\n'


_SHOT = '#usda 1.0\n(\n    subLayers = [@../../assets/crate/crate.usda@]\n)\n'


def _submit_project(server) -> int:
    v1 = server.submit({
        "assets/crate/crate.usda": _crate(1),
        "assets/crate/geo.usda": _geo(1),
        "shots/sh010/shot.usda": _SHOT,
    }, "v1")
    server.submit({"assets/crate/crate.usda": _crate(2), "assets/crate/geo.usda": _geo(2)}, "v2")
    return v1


def _values(path) -> tuple[int, int]:
    stage = Usd.Stage.Open(str(path))
    return (stage.GetPrimAtPath("/Crate").GetAttribute("version").Get(),
            stage.GetPrimAtPath("/Geo").GetAttribute("version").Get())


def _clear(session) -> None:
    """Take every file out of the session's workspace."""
    session.backend.run("sync", ["-q", f"{session.paths.depot_root}/...#none"])


@pytest.fixture
def alice(make_session, p4_server):
    return make_session()


def test_latest_prepares_the_whole_stage_in_waves(alice, p4_server, tmp_path):
    _submit_project(p4_server)
    assert not (alice.paths.root / "shots/sh010/shot.usda").exists()

    report = clio_usd.prepare(alice, "shots/sh010/shot.usda", pin="latest", store=tmp_path / "store")

    assert [p.name for p in report.layers] == ["shot.usda", "crate.usda", "geo.usda"]
    assert report.waves == 3  # shot, then crate, then geo
    assert report.sync_commands <= report.waves  # one batched sync per wave, not one per file
    assert not report.missing and not report.warnings
    assert report.root_path == alice.paths.root / "shots/sh010/shot.usda"
    assert _values(report.root_path) == (2, 2)  # plain USD, no plugin


def test_have_keeps_files_on_disk_and_syncs_missing_ones(alice, p4_server, tmp_path):
    _submit_project(p4_server)
    alice.workspace.get("assets/crate/crate.usda", version="#1")  # on disk at v1, head is 2

    report = clio_usd.prepare(alice, "shots/sh010/shot.usda", store=tmp_path / "store")

    assert _values(report.root_path) == (1, 2)  # crate as it was; geo synced while preparing


def test_a_change_pin_goes_to_the_version_store_and_leaves_the_workspace_alone(alice, p4_server, tmp_path):
    v1 = _submit_project(p4_server)
    store = tmp_path / "store"

    report = clio_usd.prepare(alice, "shots/sh010/shot.usda", pin=f"@{v1}", store=store)

    assert report.root_path.is_relative_to(store)
    assert all(p.is_relative_to(store) for p in report.layers)  # relative sublayers stay at the pin
    assert _values(report.root_path) == (1, 1)
    assert not (alice.paths.root / "assets/crate/crate.usda").exists()


def test_prepare_places_files_where_the_core_resolver_does(alice, p4_server, tmp_path):
    from deda.clio import _core

    v1 = _submit_project(p4_server)
    store = tmp_path / "store"
    report = clio_usd.prepare(alice, "shots/sh010/shot.usda", pin=f"@{v1}", store=store)

    settings = clio_usd.resolver_settings(alice, f"@{v1}", store=store)
    resolver = _core.AssetResolver(settings)
    resolved = resolver.resolve_path(alice.paths.root / "shots/sh010/shot.usda")
    assert resolved["local_path"] == report.root_path


def test_assets_and_payload_filters(alice, p4_server, tmp_path):
    shot = ('#usda 1.0\n'
            'def "Prop" (\n    prepend payload = @../../assets/crate/crate.usda@\n) {\n}\n'
            'def "Ref" (\n    prepend references = @../../assets/crate/geo.usda@</Geo>\n) {\n}\n')
    p4_server.submit({
        "assets/crate/crate.usda": _crate(1, '    asset tex = @./tex.png@\n'),
        "assets/crate/geo.usda": _geo(1),
        "assets/crate/tex.png": "not really a png",
        "shots/sh010/shot.usda": shot,
    }, "project")
    store = tmp_path / "store"

    everything = clio_usd.prepare(alice, "shots/sh010/shot.usda", pin="latest", store=store)
    assert {p.name for p in everything.layers} == {"shot.usda", "crate.usda", "geo.usda"}
    assert [p.name for p in everything.assets] == ["tex.png"]
    assert (alice.paths.root / "assets/crate/tex.png").is_file()

    _clear(alice)
    layers_only = clio_usd.prepare(alice, "shots/sh010/shot.usda", pin="latest", assets=False, store=store)
    assert not layers_only.assets
    assert not (alice.paths.root / "assets/crate/tex.png").exists()

    _clear(alice)
    no_payloads = clio_usd.prepare(alice, "shots/sh010/shot.usda", pin="latest", payloads="none", store=store)
    assert {p.name for p in no_payloads.layers} == {"shot.usda", "geo.usda"}
    assert not (alice.paths.root / "assets/crate/crate.usda").exists()

    _clear(alice)
    chosen = clio_usd.prepare(alice, "shots/sh010/shot.usda", pin="latest", store=store,
                              payloads=lambda prim, asset: prim == "/Prop" and asset.endswith("crate.usda"))
    assert "crate.usda" in {p.name for p in chosen.layers}


def test_a_reference_that_does_not_exist_is_reported_not_raised(alice, p4_server, tmp_path):
    p4_server.submit({"shots/sh010/shot.usda":
                      '#usda 1.0\n(\n    subLayers = [@./nowhere.usda@]\n)\n'}, "dangling")

    report = clio_usd.prepare(alice, "shots/sh010/shot.usda", pin="latest", store=tmp_path / "store")

    assert report.missing == ("./nowhere.usda",)
    assert [p.name for p in report.layers] == ["shot.usda"]


def test_a_root_that_is_nowhere_raises(alice, tmp_path):
    with pytest.raises(clio.ClioError):
        clio_usd.prepare(alice, "shots/missing.usda", pin="latest", store=tmp_path / "store")


def test_progress_and_cancel(alice, p4_server, tmp_path):
    _submit_project(p4_server)
    events = []
    clio_usd.prepare(alice, "shots/sh010/shot.usda", pin="latest", store=tmp_path / "store",
                     progress=events.append)
    assert events and events[-1].phase == "prepare" and events[-1].done >= 1

    token = clio.CancelToken()
    token.cancel()
    with pytest.raises(clio.CancelledError):
        clio_usd.prepare(alice, "shots/sh010/shot.usda", pin="latest", store=tmp_path / "store", cancel=token)


# --- the write side ---------------------------------------------------------


def _opened(session) -> dict[str, str]:
    result = session.backend.run("opened", [f"{session.paths.depot_root}/..."])
    return {session.paths.asset_path_of_depot(r["depotFile"]): r["action"] for r in result.records}


def test_prepare_for_edit_opens_files_and_finish_edit_adds_new_ones(alice, p4_server):
    _submit_project(p4_server)
    alice.workspace.get()

    report = clio_usd.prepare_for_edit(alice, ["assets/crate/crate.usda", "assets/crate/new.usda"])

    assert report.opened == ("assets/crate/crate.usda",)
    assert report.new == ("assets/crate/new.usda",)
    assert _opened(alice) == {"assets/crate/crate.usda": "edit"}

    alice.paths.local_path("assets/crate/new.usda").write_text("#usda 1.0\n")
    assert clio_usd.finish_edit(alice, report) == ("assets/crate/new.usda",)
    assert _opened(alice) == {"assets/crate/crate.usda": "edit", "assets/crate/new.usda": "add"}

    # A second call finds the first file open already.
    again = clio_usd.prepare_for_edit(alice, ["assets/crate/crate.usda"])
    assert again.opened == ("assets/crate/crate.usda",)


def test_prepare_for_edit_refuses_a_locked_file_before_opening_anything(make_session, p4_server):
    _submit_project(p4_server)
    alice = make_session()
    bob = make_session("bob")
    bob.workspace.get()
    bob.workspace.lock("assets/crate/crate.usda")
    alice.workspace.get()

    with pytest.raises(clio.LockedByOtherError) as caught:
        clio_usd.prepare_for_edit(alice, ["assets/crate/geo.usda", "assets/crate/crate.usda"])

    assert caught.value.user == "bob"
    assert _opened(alice) == {}  # geo.usda was not opened either


def test_prepare_for_edit_warns_about_an_old_copy_and_refuses_a_file_not_in_the_workspace(alice, p4_server):
    _submit_project(p4_server)
    alice.workspace.get("assets/crate/crate.usda", version="#1")

    report = clio_usd.prepare_for_edit(alice, ["assets/crate/crate.usda"])
    assert any("not the latest" in w for w in report.warnings)

    with pytest.raises(clio.NotInWorkspaceError):
        clio_usd.prepare_for_edit(alice, ["assets/crate/geo.usda"])
