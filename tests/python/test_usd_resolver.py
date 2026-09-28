"""The clio: USD resolver plugin against a throwaway p4d.

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


_SCRIPT = textwrap.dedent(
    """
    import json, sys
    from pxr import Ar, Usd

    assert "clio" in Ar.GetRegisteredURISchemes(), Ar.GetRegisteredURISchemes()
    ctx = Ar.GetResolver().CreateContextFromString("clio", sys.argv[1])
    stage = Usd.Stage.Open(sys.argv[2], ctx)
    prim = stage.GetPrimAtPath("/Crate")
    layers = [layer.identifier for layer in stage.GetLayerStack()]
    print(json.dumps({
        "value": prim.GetAttribute("version").Get() if prim else None,
        "layers": layers,
        "real_paths": [layer.realPath for layer in stage.GetLayerStack()],
    }))
    """
)


def _open_stage(resources: Path, settings: str, root: str) -> dict:
    env = dict(os.environ)
    env["PXR_PLUGINPATH_NAME"] = os.pathsep.join(
        filter(None, [os.fspath(resources), env.get("PXR_PLUGINPATH_NAME")])
    )
    out = subprocess.run(
        [sys.executable, "-c", _SCRIPT, settings, root],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


def _crate(version: int) -> str:
    return f'#usda 1.0\ndef "Crate" {{\n    int version = {version}\n}}\n'


_SHOT = '#usda 1.0\n(\n    subLayers = [@clio:/assets/crate/crate.usda@]\n)\n'


def test_stage_syncs_sublayers_at_latest(p4_server):
    resources = _plugin_resources()
    p4_server.submit({"assets/crate/crate.usda": _crate(1)}, "crate v1")
    p4_server.submit({"assets/crate/crate.usda": _crate(2), "shots/sh010/shot.usda": _SHOT}, "v2 + shot")
    p4_server.clear_workspace()

    result = _open_stage(resources, p4_server.settings_text(), "clio:/shots/sh010/shot.usda")

    assert result["value"] == 2
    assert "clio:/assets/crate/crate.usda" in result["layers"]
    assert (p4_server.workspace_root / "assets/crate/crate.usda").is_file()


def test_stage_at_change_pin_uses_version_store(p4_server):
    resources = _plugin_resources()
    v1 = p4_server.submit({"assets/crate/crate.usda": _crate(1), "shots/sh010/shot.usda": _SHOT}, "v1")
    p4_server.submit({"assets/crate/crate.usda": _crate(2)}, "crate v2")

    result = _open_stage(resources, p4_server.settings_text(pin=f"@{v1}"), "clio:/shots/sh010/shot.usda")

    assert result["value"] == 1
    real_paths = [p for p in result["real_paths"] if p]  # skip the anonymous session layer
    assert len(real_paths) == 2
    assert all(Path(p).is_relative_to(p4_server.version_store) for p in real_paths)
    # The workspace still has the latest version.
    assert "version = 2" in (p4_server.workspace_root / "assets/crate/crate.usda").read_text()


_REFRESH_SCRIPT = textwrap.dedent(
    """
    import json, sys
    from pxr import Ar, Usd

    ctx = Ar.GetResolver().CreateContextFromString("clio", sys.argv[1])
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
    p4_server.submit({"assets/crate/crate.usda": _crate(1), "shots/sh010/shot.usda": _SHOT}, "v1")

    env = dict(os.environ, PXR_PLUGINPATH_NAME=os.fspath(resources))
    proc = subprocess.Popen(
        [sys.executable, "-c", _REFRESH_SCRIPT, p4_server.settings_text(), "clio:/shots/sh010/shot.usda"],
        env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    assert proc.stdout.readline().strip() == "READY"
    p4_server.submit({"assets/crate/crate.usda": _crate(2)}, "crate v2")
    out, err = proc.communicate("go\n", timeout=120)
    assert proc.returncode == 0, err

    assert json.loads(out.strip().splitlines()[-1]) == {"before": 1, "after": 2}


@pytest.mark.xfail(
    strict=True,
    reason="Relative paths inside a clio: layer are anchored to its local file path, "
    "so Clio does not fetch them yet (design doc §10.4)",
)
def test_relative_sublayer_inside_clio_layer_is_fetched(p4_server):
    resources = _plugin_resources()
    crate = '#usda 1.0\n(\n    subLayers = [@./geo.usda@]\n)\ndef "Crate" {\n    int version = 1\n}\n'
    p4_server.submit(
        {
            "assets/crate/crate.usda": crate,
            "assets/crate/geo.usda": '#usda 1.0\ndef "Geo" {}\n',
            "shots/sh010/shot.usda": _SHOT,
        },
        "crate with relative sublayer",
    )
    p4_server.clear_workspace()

    result = _open_stage(resources, p4_server.settings_text(), "clio:/shots/sh010/shot.usda")

    assert len([p for p in result["real_paths"] if p]) == 3
