"""The Clio USD plugin against a throwaway p4d.

Layers use ordinary paths. The plugin replaces USD's default resolver with
a subclass of it: with a Clio context, files inside the project are fetched
from Perforce before USD reads them; without one, it behaves like the
default resolver; and without the plugin, plain USD opens the same files
from disk.

Skipped unless USD's Python bindings are importable and the plugin was
built (installed with the wheel, or pointed to by CLIO_TEST_USD_PLUGIN).
USD reads PXR_PLUGINPATH_NAME only when its resolver is first created, so
each scenario runs in a fresh subprocess.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from deda.clio import usd as clio_usd

pxr = pytest.importorskip("pxr", reason="USD Python bindings (pxr) are not importable")


def _plugin_resources() -> Path:
    override = os.environ.get("CLIO_TEST_USD_PLUGIN")
    path = Path(override) if override else clio_usd.plugin_path()
    if path is None or not (path / "plugInfo.json").is_file():
        pytest.skip("clio USD plugin not built (set CLIO_TEST_USD_PLUGIN or build with CLIO_BUILD_USD=ON)")
    return path


def _env(plugin: Path | None, extra: dict | None = None) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in ("PXR_PLUGINPATH_NAME", "CLIO_RESOLVER_CONTEXT")}
    if plugin is not None:
        env["PXR_PLUGINPATH_NAME"] = os.fspath(plugin)
    env.update(extra or {})
    return env


_SCRIPT = textwrap.dedent(
    """
    import json, sys
    from pxr import Ar, Plug, Usd

    settings, root = sys.argv[1], sys.argv[2]
    if settings:
        stage = Usd.Stage.Open(root, Ar.GetResolver().CreateContextFromString(settings))
    else:
        stage = Usd.Stage.Open(root)
    crate = stage.GetPrimAtPath("/Crate")
    geo = stage.GetPrimAtPath("/Geo")
    print(json.dumps({
        # The plugin is loaded only if USD chose ClioResolver as its resolver.
        "clio_loaded": bool((Plug.Registry().GetPluginWithName("clioUsd") or None)
                            and Plug.Registry().GetPluginWithName("clioUsd").isLoaded),
        "value": crate.GetAttribute("version").Get() if crate else None,
        "geo": geo.GetAttribute("version").Get() if geo else None,
        "real_paths": [layer.realPath for layer in stage.GetLayerStack() if layer.realPath],
    }))
    """
)


def _open_stage(settings: str, root: Path, *, plugin: Path | None, extra_env: dict | None = None):
    out = subprocess.run([sys.executable, "-c", _SCRIPT, settings, os.fspath(root)],
                         env=_env(plugin, extra_env), capture_output=True, text=True, check=False)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1]), out.stderr


def _crate(version: int) -> str:
    return (f'#usda 1.0\n(\n    subLayers = [@./geo.usda@]\n)\n'
            f'def "Crate" {{\n    int version = {version}\n}}\n')


def _geo(version: int) -> str:
    return f'#usda 1.0\ndef "Geo" {{\n    int version = {version}\n}}\n'


_SHOT = '#usda 1.0\n(\n    subLayers = [@../../assets/crate/crate.usda@]\n)\n'
_SHOT_SEARCH = '#usda 1.0\n(\n    subLayers = [@assets/crate/crate.usda@]\n)\n'


def _submit_v1_v2(server) -> int:
    v1 = server.submit({
        "assets/crate/crate.usda": _crate(1),
        "assets/crate/geo.usda": _geo(1),
        "shots/sh010/shot.usda": _SHOT,
    }, "v1")
    server.submit({"assets/crate/crate.usda": _crate(2), "assets/crate/geo.usda": _geo(2)}, "v2")
    return v1


def _shot(server) -> Path:
    return server.workspace_root / "shots/sh010/shot.usda"


def test_plugin_is_the_primary_resolver(p4_server):
    resources = _plugin_resources()
    p4_server.submit({"shots/sh010/shot.usda": "#usda 1.0\n"}, "shot")
    result, _ = _open_stage("", _shot(p4_server), plugin=resources)
    assert result["clio_loaded"]


def test_latest_fetches_the_stage_and_its_relative_sublayers(p4_server):
    resources = _plugin_resources()
    _submit_v1_v2(p4_server)
    p4_server.clear_workspace()
    assert not _shot(p4_server).exists()

    result, _ = _open_stage(p4_server.settings_text(pin="latest"), _shot(p4_server), plugin=resources)

    assert (result["value"], result["geo"]) == (2, 2)
    assert (p4_server.workspace_root / "assets/crate/geo.usda").is_file()


def test_have_loads_files_on_disk_as_they_are_and_syncs_missing_ones(p4_server):
    """The default pin: the version on disk is the version loaded; a file not
    yet on this machine is synced as its layer is resolved."""
    resources = _plugin_resources()
    _submit_v1_v2(p4_server)
    conn = p4_server.connection()
    conn.run_or_throw("sync", ["-q", "//depot/proj/assets/crate/crate.usda#1"])  # on disk at v1
    conn.run_or_throw("sync", ["-q", "//depot/proj/assets/crate/geo.usda#none"])  # not on disk
    geo = p4_server.workspace_root / "assets/crate/geo.usda"
    assert not geo.exists()

    result, _ = _open_stage(p4_server.settings_text(), _shot(p4_server), plugin=resources)

    assert result["value"] == 1  # crate: the version on disk, although head is 2
    assert result["geo"] == 2    # geo: synced (latest) while the stage loaded
    assert geo.is_file()


def test_change_pin_keeps_relative_sublayers_at_that_version(p4_server):
    resources = _plugin_resources()
    v1 = _submit_v1_v2(p4_server)

    result, _ = _open_stage(p4_server.settings_text(pin=f"@{v1}"), _shot(p4_server), plugin=resources)

    assert (result["value"], result["geo"]) == (1, 1)
    assert len(result["real_paths"]) == 3
    assert all(Path(p).is_relative_to(p4_server.version_store) for p in result["real_paths"])
    # The workspace still has the latest version.
    assert "version = 2" in (p4_server.workspace_root / "assets/crate/crate.usda").read_text()


def test_project_rooted_search_paths_resolve_under_the_workspace_root(p4_server):
    resources = _plugin_resources()
    p4_server.submit({
        "assets/crate/crate.usda": _crate(1),
        "assets/crate/geo.usda": _geo(1),
        "shots/sh010/shot.usda": _SHOT_SEARCH,
    }, "search paths")
    p4_server.clear_workspace()

    result, _ = _open_stage(p4_server.settings_text(), _shot(p4_server), plugin=resources)

    assert (result["value"], result["geo"]) == (1, 1)


def test_plain_usd_opens_the_same_files_without_clio(p4_server):
    _submit_v1_v2(p4_server)  # the files are in the workspace
    result, _ = _open_stage("", _shot(p4_server), plugin=None)
    assert not result["clio_loaded"]
    assert (result["value"], result["geo"]) == (2, 2)


def test_plain_usd_opens_search_paths_with_the_default_search_path(p4_server):
    p4_server.submit({
        "assets/crate/crate.usda": _crate(1),
        "assets/crate/geo.usda": _geo(1),
        "shots/sh010/shot.usda": _SHOT_SEARCH,
    }, "search paths")
    result, _ = _open_stage("", _shot(p4_server), plugin=None,
                            extra_env={"PXR_AR_DEFAULT_SEARCH_PATH": os.fspath(p4_server.workspace_root)})
    assert (result["value"], result["geo"]) == (1, 1)


def test_plugin_without_a_clio_context_behaves_like_the_default_resolver(p4_server):
    resources = _plugin_resources()
    _submit_v1_v2(p4_server)
    result, stderr = _open_stage("", _shot(p4_server), plugin=resources)
    assert (result["value"], result["geo"]) == (2, 2)
    assert "clio" not in stderr.lower()


def test_stage_opens_from_local_files_when_perforce_is_unavailable(p4_server):
    resources = _plugin_resources()
    _submit_v1_v2(p4_server)
    # latest needs the server for every file; nothing listens on this port.
    settings = p4_server.settings_text(port="localhost:1", pin="latest")

    result, stderr = _open_stage(settings, _shot(p4_server), plugin=resources)

    assert (result["value"], result["geo"]) == (2, 2)
    assert "Perforce is not available" in stderr


_REFRESH_SCRIPT = textwrap.dedent(
    """
    import json, sys
    from pxr import Ar, Usd

    ctx = Ar.GetResolver().CreateContextFromString(sys.argv[1])
    stage = Usd.Stage.Open(sys.argv[2], ctx)
    before = stage.GetPrimAtPath("/Crate").GetAttribute("version").Get()
    print("READY", flush=True)
    sys.stdin.readline()  # the test submits a new version meanwhile
    Ar.GetResolver().RefreshContext(ctx)
    stage.Reload()
    after = stage.GetPrimAtPath("/Crate").GetAttribute("version").Get()
    print(json.dumps({"before": before, "after": after}), flush=True)
    """
)


def test_refresh_and_reload_pick_up_a_new_version(p4_server):
    resources = _plugin_resources()
    p4_server.submit({
        "assets/crate/crate.usda": _crate(1),
        "assets/crate/geo.usda": _geo(1),
        "shots/sh010/shot.usda": _SHOT,
    }, "v1")

    proc = subprocess.Popen(
        [sys.executable, "-c", _REFRESH_SCRIPT, p4_server.settings_text(pin="latest"),
         os.fspath(_shot(p4_server))],
        env=_env(resources), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    assert proc.stdout.readline().strip() == "READY"
    # Submit version 2 (from the same workspace), then put version 1 back on
    # disk, so version 2 can only arrive through Clio's sync after refresh.
    p4_server.submit({"assets/crate/crate.usda": _crate(2)}, "crate v2")
    p4_server.connection().run_or_throw("sync", ["-q", "//depot/proj/assets/crate/crate.usda#1"])
    out, err = proc.communicate("go\n", timeout=120)
    assert proc.returncode == 0, err

    assert json.loads(out.strip().splitlines()[-1]) == {"before": 1, "after": 2}


_MULTI_CONTEXT_SCRIPT = textwrap.dedent(
    """
    import json, sys
    from pxr import Ar, Usd

    resolver = Ar.GetResolver()
    latest = resolver.CreateContextFromString(sys.argv[1])
    pinned = resolver.CreateContextFromString(sys.argv[2])
    values = []
    for ctx in (latest, pinned, latest):
        stage = Usd.Stage.Open(sys.argv[3], ctx)
        values.append([stage.GetPrimAtPath("/Crate").GetAttribute("version").Get(),
                       stage.GetPrimAtPath("/Geo").GetAttribute("version").Get()])
    print(json.dumps(values))
    """
)


def test_one_process_keeps_stages_at_different_pins_apart(p4_server):
    resources = _plugin_resources()
    v1 = _submit_v1_v2(p4_server)

    out = subprocess.run(
        [sys.executable, "-c", _MULTI_CONTEXT_SCRIPT, p4_server.settings_text(pin="latest"),
         p4_server.settings_text(pin=f"@{v1}"), os.fspath(_shot(p4_server))],
        env=_env(resources), capture_output=True, text=True, check=False,
    )
    assert out.returncode == 0, out.stderr

    assert json.loads(out.stdout.strip().splitlines()[-1]) == [[2, 2], [1, 1], [2, 2]]


# --- the write side (design §10.6) ---------------------------------------

_SAVE_SCRIPT = textwrap.dedent(
    """
    import json, sys
    from pxr import Ar, Sdf, Tf

    settings, path, mode = sys.argv[1:4]
    ctx = Ar.GetResolver().CreateContextFromString(settings) if settings else None
    errors = []
    saved = False
    try:
        with Ar.ResolverContextBinder(ctx) if ctx else Ar.ResolverContextBinder(Ar.ResolverContext()):
            mark = Tf.Error.Mark()
            if mode == "new":
                layer = Sdf.Layer.CreateNew(path)
            else:
                layer = Sdf.Layer.FindOrOpen(path)
            prim = Sdf.PrimSpec(layer, "Written", Sdf.SpecifierDef)
            Sdf.AttributeSpec(prim, "version", Sdf.ValueTypeNames.Int).default = 99
            saved = bool(layer.Save())
            errors = [e.commentary for e in mark.GetErrors()]
    except Exception as e:  # Tf errors surface as exceptions in some builds
        errors.append(str(e))
    print(json.dumps({"saved": saved, "errors": errors}))
    """
)


def _save_layer(settings: str, path: Path, mode: str, *, plugin: Path | None):
    out = subprocess.run([sys.executable, "-c", _SAVE_SCRIPT, settings, os.fspath(path), mode],
                         env=_env(plugin), capture_output=True, text=True, check=False)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1]), out.stderr


def _opened(server) -> dict[str, str]:
    result = server.connection().run("opened", ["//depot/proj/..."])
    return {r["depotFile"].removeprefix("//depot/proj/"): r["action"] for r in result.records}


def test_saving_a_layer_opens_it_for_edit(p4_server):
    resources = _plugin_resources()
    _submit_v1_v2(p4_server)
    crate = p4_server.workspace_root / "assets/crate/crate.usda"

    result, stderr = _save_layer(p4_server.settings_text(), crate, "edit", plugin=resources)

    assert result["saved"], (result, stderr)
    assert "Written" in crate.read_text()
    assert _opened(p4_server) == {"assets/crate/crate.usda": "edit"}  # opened, never submitted


def test_saving_a_new_layer_adds_it_after_it_is_written(p4_server):
    resources = _plugin_resources()
    _submit_v1_v2(p4_server)
    new_layer = p4_server.workspace_root / "assets/crate/new_layer.usda"

    result, stderr = _save_layer(p4_server.settings_text(), new_layer, "new", plugin=resources)

    assert result["saved"], (result, stderr)
    assert new_layer.is_file()
    assert _opened(p4_server) == {"assets/crate/new_layer.usda": "add"}


def _lock_from_another_workspace(server, tmp_path: Path, depot_file: str) -> None:
    from deda import clio

    root = tmp_path / "other_ws"
    root.mkdir()
    spec = (f"Client: other_ws\nOwner: clio_tester\nRoot: {root}\n"
            "Options: allwrite noclobber nocompress unlocked nomodtime normdir\nLineEnd: local\n"
            "View:\n\t//depot/proj/... //other_ws/...\n")
    server.connection().run_or_throw("client", ["-i"], spec)
    other = clio.Connection(port=server.port, user="clio_tester", client="other_ws", cwd=os.fspath(root),
                            tickets=os.fspath(server.tickets))
    other.run_or_throw("sync", ["-q", depot_file])
    other.run_or_throw("edit", [depot_file])
    other.run_or_throw("lock", [depot_file])


def test_a_save_is_refused_while_someone_else_holds_the_lock(p4_server, tmp_path):
    resources = _plugin_resources()
    _submit_v1_v2(p4_server)
    crate = p4_server.workspace_root / "assets/crate/crate.usda"
    before = crate.read_text()
    _lock_from_another_workspace(p4_server, tmp_path, "//depot/proj/assets/crate/crate.usda")

    result, stderr = _save_layer(p4_server.settings_text(), crate, "edit", plugin=resources)

    assert not result["saved"]
    assert "locked by clio_tester (workspace other_ws)" in " ".join(result["errors"]) + stderr
    assert crate.read_text() == before  # nothing was lost or half-written
    assert "assets/crate/crate.usda" not in _opened(p4_server)  # not opened here


def test_a_pinned_version_in_the_store_cannot_be_saved_over(p4_server):
    resources = _plugin_resources()
    v1 = _submit_v1_v2(p4_server)
    # Fetch it into the version store first.
    _open_stage(p4_server.settings_text(pin=f"@{v1}"), _shot(p4_server), plugin=resources)
    stored = next(p for p in p4_server.version_store.rglob("crate.usda"))
    before = stored.read_text()

    result, stderr = _save_layer(p4_server.settings_text(pin=f"@{v1}"), stored, "edit", plugin=resources)

    assert not result["saved"]
    assert stored.read_text() == before


def test_saving_without_a_clio_context_does_not_touch_perforce(p4_server):
    resources = _plugin_resources()
    _submit_v1_v2(p4_server)
    crate = p4_server.workspace_root / "assets/crate/crate.usda"

    result, _ = _save_layer("", crate, "edit", plugin=resources)

    assert result["saved"]
    assert _opened(p4_server) == {}


def test_a_save_goes_ahead_with_a_warning_when_perforce_is_unavailable(p4_server):
    resources = _plugin_resources()
    _submit_v1_v2(p4_server)
    crate = p4_server.workspace_root / "assets/crate/crate.usda"

    result, stderr = _save_layer(p4_server.settings_text(port="localhost:1"), crate, "edit", plugin=resources)

    assert result["saved"]
    assert "Perforce is not available" in stderr
    assert "Written" in crate.read_text()


_TIMESTAMP_SCRIPT = textwrap.dedent(
    """
    import json, sys
    from pxr import Ar

    settings, path = sys.argv[1:3]
    resolver = Ar.GetResolver()
    with Ar.ResolverContextBinder(resolver.CreateContextFromString(settings)):
        stamp = resolver.GetModificationTimestamp(path, Ar.ResolvedPath(path))
        print(json.dumps({"time": stamp.GetTime() if stamp.IsValid() else None}))
    """
)


def _timestamp(settings: str, path: Path, plugin: Path):
    out = subprocess.run([sys.executable, "-c", _TIMESTAMP_SCRIPT, settings, os.fspath(path)],
                         env=_env(plugin), capture_output=True, text=True, check=False)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])["time"]


def _set_client_options(server, options: str) -> None:
    spec = (f"Client: clio_test_ws\nOwner: clio_tester\nRoot: {server.workspace_root}\n"
            f"Options: {options}\nLineEnd: local\nView:\n\t//depot/proj/... //clio_test_ws/...\n")
    server.connection().run_or_throw("client", ["-i"], spec)


def test_modtime_client_timestamp_is_the_workspace_revision(p4_server):
    resources = _plugin_resources()
    _submit_v1_v2(p4_server)  # crate.usda is at revision 2
    crate = p4_server.workspace_root / "assets/crate/crate.usda"

    # The default test client has nomodtime: the file's own time is used.
    assert _timestamp(p4_server.settings_text(), crate, resources) == pytest.approx(crate.stat().st_mtime, abs=1)

    _set_client_options(p4_server, "allwrite noclobber nocompress unlocked modtime rmdir")
    assert _timestamp(p4_server.settings_text(), crate, resources) == 2.0
    p4_server.connection().run_or_throw("sync", ["-q", "//depot/proj/assets/crate/crate.usda#1"])
    assert _timestamp(p4_server.settings_text(), crate, resources) == 1.0
