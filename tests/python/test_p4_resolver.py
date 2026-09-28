"""AssetResolver against a throwaway p4d (skipped without CLIO_TEST_P4D)."""

import threading
from pathlib import Path

from deda import clio


def test_connection_runs_tagged_commands(p4_server):
    info = p4_server.connection().run_or_throw("info")
    assert info.records[0]["userName"] == "clio_tester"


def test_latest_syncs_into_workspace(p4_server):
    p4_server.submit({"props/crate/crate.usda": "#usda 1.0\n"}, "crate v1")
    p4_server.clear_workspace()

    resolver = clio.AssetResolver(clio.Settings.parse(p4_server.settings_text()))
    asset = resolver.resolve(clio.AssetIdentifier.parse("clio:/props/crate/crate.usda"))
    assert asset is not None
    assert Path(asset["local_path"]) == p4_server.workspace_root / "props/crate/crate.usda"
    assert asset["depot_path"] == "//depot/proj/props/crate/crate.usda"
    assert Path(asset["local_path"]).read_text() == "#usda 1.0\n"

    assert resolver.resolve(clio.AssetIdentifier.parse("clio:/nope.usda")) is None


def test_context_pin_uses_version_store(p4_server):
    v1 = p4_server.submit({"a.usda": "v1"}, "v1")
    p4_server.submit({"a.usda": "v2"}, "v2")

    resolver = clio.AssetResolver(clio.Settings.parse(p4_server.settings_text(pin=f"@{v1}")))
    asset = resolver.resolve(clio.AssetIdentifier.parse("clio:/a.usda"))
    assert Path(asset["local_path"]).read_text() == "v1"
    assert Path(asset["local_path"]).is_relative_to(p4_server.version_store)
    assert (p4_server.workspace_root / "a.usda").read_text() == "v2"


def test_resolves_from_many_threads(p4_server):
    files = {f"set/prop_{i}.usda": f"prop {i}" for i in range(8)}
    p4_server.submit(files, "props")
    p4_server.clear_workspace()

    resolver = clio.AssetResolver(clio.Settings.parse(p4_server.settings_text()))
    results = {}

    def work(name):
        results[name] = resolver.resolve(clio.AssetIdentifier.parse(f"clio:/{name}"))

    threads = [threading.Thread(target=work, args=(name,)) for name in files]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    for name, content in files.items():
        assert Path(results[name]["local_path"]).read_text() == content
