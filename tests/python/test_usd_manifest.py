"""Version manifests (design doc §10.8): create, save, fetch, check, diff and
the ``clio usd manifest`` commands. Loading a stage through a manifest with
the plugin is tested in test_usd_resolver.py.
"""

from __future__ import annotations

import json
import os
import stat

import pytest
from click.testing import CliRunner

from deda import clio
from deda.clio import _core
from deda.clio import usd as clio_usd
from deda.clio.cli import cli

pytest.importorskip("pxr", reason="USD Python bindings (pxr) are not importable")


def _crate(version: int) -> str:
    return (f'#usda 1.0\n(\n    subLayers = [@./geo.usda@]\n)\n'
            f'def "Crate" {{\n    int version = {version}\n    asset tex = @./tex.png@\n}}\n')


def _geo(version: int) -> str:
    return f'#usda 1.0\ndef "Geo" {{\n    int version = {version}\n}}\n'


_SHOT = '#usda 1.0\n(\n    subLayers = [@../../assets/crate/crate.usda@]\n)\n'
_ROOT = "shots/sh010/shot.usda"


def _submit_project(server) -> int:
    v1 = server.submit({
        "assets/crate/crate.usda": _crate(1),
        "assets/crate/geo.usda": _geo(1),
        "assets/crate/tex.png": "not really a png, version 1",
        _ROOT: _SHOT,
    }, "v1")
    server.submit({"assets/crate/crate.usda": _crate(2), "assets/crate/geo.usda": _geo(2)}, "v2")
    return v1


@pytest.fixture
def alice(make_session, p4_server):
    session = make_session()
    _submit_project(p4_server)
    return session


def _revs(manifest) -> dict[str, int]:
    return {path: f.rev for path, f in manifest.files.items()}


def test_have_records_the_revisions_in_the_workspace(alice, tmp_path):
    alice.workspace.get("assets/crate/crate.usda", version="#1")  # on disk at v1, head is 2
    alice.workspace.get("assets/crate/geo.usda", version="#2")
    alice.workspace.get("assets/crate/tex.png")
    alice.workspace.get(_ROOT)

    manifest = clio_usd.Manifest.create(alice, _ROOT, store=tmp_path / "store")

    assert _revs(manifest) == {
        "assets/crate/crate.usda": 1, "assets/crate/geo.usda": 2, "assets/crate/tex.png": 1, _ROOT: 1}
    assert manifest.depot == "//depot/proj"
    assert manifest.root_layer == _ROOT
    assert manifest.source_pin == "have"
    assert manifest.created_by == f"{alice.user}@{alice.client}"
    assert manifest.usd_version.startswith("26.") or manifest.usd_version.startswith("25.")
    geo = manifest.files["assets/crate/geo.usda"]
    assert len(geo.digest) == 32 and geo.size > 0 and geo.type.startswith("text") and geo.change > 0


def test_latest_and_change_pins_choose_the_revisions(alice, p4_server, tmp_path):
    v1 = 0
    result = alice.backend.run("changes", ["-m", "5", "-s", "submitted", "//depot/proj/..."])
    v1 = min(int(r["change"]) for r in result.records)
    store = tmp_path / "store"

    latest = clio_usd.Manifest.create(alice, _ROOT, pin="latest", store=store)
    assert _revs(latest)["assets/crate/crate.usda"] == 2
    assert latest.source_pin == "latest"

    pinned = clio_usd.Manifest.create(alice, _ROOT, pin=f"@{v1}", store=store)
    assert _revs(pinned)["assets/crate/crate.usda"] == 1
    assert pinned.source_pin == f"@{v1}"
    # Files come from the version store, but are named by the same project paths.
    assert set(pinned.files) == set(latest.files)


def test_saved_manifests_have_a_stable_id_that_the_core_agrees_with(alice, tmp_path):
    manifest = clio_usd.Manifest.create(alice, _ROOT, pin="latest", store=tmp_path / "store")
    path = manifest.save(tmp_path / "sh010.clio-manifest.json")

    loaded = clio_usd.Manifest.load(path)
    assert loaded == manifest
    assert loaded.sha256 == manifest.sha256 == _core.sha256_hex(path.read_text(encoding="utf-8"))
    core = _core.Manifest.load(os.fspath(path))
    assert core.sha256 == manifest.sha256
    assert core.folder_name == "manifest-" + manifest.sha256[:16]
    assert set(core.entries()) == set(manifest.files)

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["clio_manifest"] == 1 and data["files"][_ROOT]["rev"] == 1


def test_a_file_that_cannot_be_reproduced_stops_the_manifest(alice, p4_server, tmp_path):
    store = tmp_path / "store"
    alice.workspace.get()
    crate = alice.paths.local_path("assets/crate/crate.usda")

    # Opened for edit: the farm would get the submitted revision, not this one.
    alice.workspace.open_for_edit("assets/crate/crate.usda")
    with pytest.raises(clio.ManifestError) as caught:
        clio_usd.Manifest.create(alice, _ROOT, store=store)
    assert any("assets/crate/crate.usda" in p and "open for edit" in p for p in caught.value.problems)
    alice.backend.run("revert", [os.fspath(crate)])

    # Changed on disk without being opened: made writable by hand, then edited.
    geo = alice.paths.local_path("assets/crate/geo.usda")
    geo.chmod(geo.stat().st_mode | stat.S_IWRITE)
    geo.write_text("#usda 1.0\n# edited by hand\n")
    with pytest.raises(clio.ManifestError) as caught:
        clio_usd.Manifest.create(alice, _ROOT, store=store)
    assert any("geo.usda" in p and "changed on disk" in p for p in caught.value.problems)
    alice.workspace.get("assets/crate/geo.usda", force=True)

    # Not in Perforce at all.
    crate.chmod(crate.stat().st_mode | stat.S_IWRITE)
    crate.write_text(_crate(2).replace("./tex.png", "./extra.png"))
    alice.paths.local_path("assets/crate/extra.png").write_text("local only")
    alice.backend.run("edit", [os.fspath(crate)])  # opened, so it is a different problem too
    with pytest.raises(clio.ManifestError) as caught:
        clio_usd.Manifest.create(alice, _ROOT, store=store)
    assert any("extra.png" in p and "not in Perforce" in p for p in caught.value.problems)


def test_a_reference_that_cannot_be_provided_is_an_error_unless_allowed(alice, p4_server, tmp_path):
    p4_server.submit({"shots/sh020/shot.usda": '#usda 1.0\n(\n    subLayers = [@./nowhere.usda@]\n)\n'}, "dangling")
    store = tmp_path / "store"

    with pytest.raises(clio.ManifestError) as caught:
        clio_usd.Manifest.create(alice, "shots/sh020/shot.usda", pin="latest", store=store)
    assert any("nowhere.usda" in p for p in caught.value.problems)

    manifest = clio_usd.Manifest.create(alice, "shots/sh020/shot.usda", pin="latest", store=store,
                                        allow_missing=True)
    assert list(manifest.files) == ["shots/sh020/shot.usda"]


def test_extra_files_and_payload_filters(alice, p4_server, tmp_path):
    p4_server.submit({"render/overrides.json": "{}",
                      "shots/sh030/shot.usda": '#usda 1.0\ndef "P" (\n    prepend payload = @../../assets/crate/geo.usda@\n) {\n}\n'},
                     "extra and payload")
    store = tmp_path / "store"

    with_all = clio_usd.Manifest.create(alice, "shots/sh030/shot.usda", pin="latest", store=store,
                                        extra=["render/overrides.json"])
    assert "render/overrides.json" in with_all.files and "assets/crate/geo.usda" in with_all.files

    without = clio_usd.Manifest.create(alice, "shots/sh030/shot.usda", pin="latest", store=store, payloads="none")
    assert "assets/crate/geo.usda" not in without.files


def test_diff(alice, tmp_path):
    store = tmp_path / "store"
    alice.workspace.get()
    first = clio_usd.Manifest.create(alice, _ROOT, pin="latest", store=store)
    older = clio_usd.Manifest.create(alice, _ROOT, pin=f"@{_oldest(alice)}", store=store)

    assert first.diff(first).same
    d = older.diff(first)
    assert d.changed["assets/crate/crate.usda"] == (1, 2)
    assert not d.added and not d.removed

    smaller = clio_usd.Manifest(first.depot, {k: v for k, v in first.files.items() if k != "assets/crate/tex.png"})
    assert first.diff(smaller).removed == ("assets/crate/tex.png",)
    assert smaller.diff(first).added == ("assets/crate/tex.png",)


def _oldest(session) -> int:
    result = session.backend.run("changes", ["-m", "10", "-s", "submitted", "//depot/proj/..."])
    return min(int(r["change"]) for r in result.records)


def test_fetch_check_and_the_resolver(alice, tmp_path):
    store = tmp_path / "store"
    manifest = clio_usd.Manifest.create(alice, _ROOT, pin=f"@{_oldest(alice)}", store=store)
    path = manifest.save(tmp_path / "m.json")

    check = manifest.check(alice, store=store)
    assert check.ok and not check.problems

    seen: list[tuple[int, int, str]] = []
    report = manifest.fetch(alice, store=store, parallel=4, progress=lambda d, t, p: seen.append((d, t, p)))
    assert report.ok and report.files == 4
    assert report.fetched == 4 and report.bytes_fetched > 0
    assert len(seen) == 4 and seen[-1][:2] == (4, 4)

    again = manifest.fetch(alice, store=store)
    assert again.present == 4 and again.fetched == 0

    # A farm resolver (verify policy, no server) provides exactly these files.
    resolver = _core.AssetResolver(manifest.settings(alice, path, store=store))
    assert resolver.has_manifest
    crate = resolver.resolve(_core.AssetIdentifier.parse("clio:/assets/crate/crate.usda"))
    assert "version = 1" in crate["local_path"].read_text()
    assert crate["local_path"].parent.parent.parent.name == _core.Manifest.load(os.fspath(path)).folder_name
    assert resolver.resolve(_core.AssetIdentifier.parse("clio:/assets/crate/other.usda")) is None


def test_a_manifest_of_another_project_is_refused(alice, tmp_path):
    other = clio_usd.Manifest("//elsewhere/main", {"a.usda": clio_usd.ManifestFile(1)})
    with pytest.raises(clio.ManifestError):
        other.fetch(alice, store=tmp_path / "store")
    with pytest.raises(clio.ManifestError):
        other.check(alice, store=tmp_path / "store")


def test_a_file_that_is_not_a_manifest_is_refused(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text('{"clio_manifest": 2, "depot": "//d", "files": {}}')
    with pytest.raises(clio.ConfigError):
        clio_usd.Manifest.load(bad)
    with pytest.raises(clio.ConfigError):
        clio_usd.Manifest.load(tmp_path / "missing.json")


# --- the command line -------------------------------------------------------


@pytest.fixture
def run(alice, p4_server, monkeypatch, tmp_path):
    runner = CliRunner()

    def invoke(*args: str):
        monkeypatch.chdir(alice.paths.root)
        monkeypatch.setenv("P4TICKETS", os.fspath(p4_server.tickets))
        base = ["--port", p4_server.port, "--user", alice.user, "--client", alice.client]
        return runner.invoke(cli, [*base, *args], catch_exceptions=False)

    return invoke


def test_cli_create_fetch_check_diff(run, alice, tmp_path):
    store = os.fspath(tmp_path / "store")
    out = tmp_path / "sh010.json"
    created = run("usd", "manifest", "create", _ROOT, "--pin", "latest", "-o", os.fspath(out), "--store", store)
    assert created.exit_code == 0, created.output
    assert "4 file(s)" in created.output

    fetched = run("usd", "manifest", "fetch", os.fspath(out), "--store", store)
    assert fetched.exit_code == 0, fetched.output
    assert "4 fetched" in fetched.output

    checked = run("--json", "usd", "manifest", "check", os.fspath(out))
    assert checked.exit_code == 0 and json.loads(checked.output) == {"problems": []}

    older = tmp_path / "older.json"
    assert run("usd", "manifest", "create", _ROOT, "--pin", f"@{_oldest(alice)}", "-o", os.fspath(older),
               "--store", store).exit_code == 0
    same = run("usd", "manifest", "diff", os.fspath(out), os.fspath(out))
    assert same.exit_code == 0 and "same revisions" in same.output
    different = run("usd", "manifest", "diff", os.fspath(older), os.fspath(out))
    assert different.exit_code == 1
    assert "changed  assets/crate/crate.usda  #1 -> #2" in different.output


def test_cli_reports_why_a_manifest_cannot_be_made(run, alice, tmp_path):
    alice.workspace.get()
    alice.workspace.open_for_edit("assets/crate/geo.usda")
    result = run("usd", "manifest", "create", _ROOT, "-o", os.fspath(tmp_path / "x.json"),
                 "--store", os.fspath(tmp_path / "store"))
    assert result.exit_code == 13
    assert "assets/crate/geo.usda: open for edit" in result.output
    assert not (tmp_path / "x.json").exists()
