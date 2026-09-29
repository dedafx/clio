"""Tests for the deda.clio._core bindings that need no server."""

import pytest

from deda import clio


def test_version():
    assert clio.__version__ == "0.1.0"


@pytest.mark.parametrize("text", ["latest", "have", "@18234", "@approved", "#12"])
def test_pin_round_trip(text):
    assert str(clio.Pin.parse(text)) == text


def test_pin_properties():
    pin = clio.Pin.parse("@approved")
    assert pin.kind == clio.PinKind.LABEL
    assert pin.label_name == "approved"
    assert pin.is_snapshot
    assert pin.p4_rev_spec() == "@approved"
    assert clio.Pin.parse("#head") == clio.Pin.latest()
    assert len({clio.Pin.parse("@1"), clio.Pin.change(1)}) == 1


def test_errors_are_clio_errors():
    with pytest.raises(clio.PinError):
        clio.Pin.parse("bogus")
    with pytest.raises(clio.ClioError):
        clio.AssetIdentifier.parse("/not/a/clio/path")
    with pytest.raises(clio.ConfigError):
        clio.Settings.parse("root=/w")


def test_identifier_anchoring_inherits_snapshot_pins():
    anchor = clio.AssetIdentifier.parse("clio:/props/crate/crate.usd?change=100")
    child = clio.AssetIdentifier.anchor("./geo/crate_geo.usdc", anchor)
    assert str(child) == "clio:/props/crate/geo/crate_geo.usdc?change=100"
    assert child.relative_path == "props/crate/geo/crate_geo.usdc"
    assert child.pin == clio.Pin.change(100)

    at_rev = clio.AssetIdentifier.parse("clio:/props/crate/crate.usd?rev=3")
    assert clio.AssetIdentifier.anchor("tex.exr", at_rev).pin is None


def test_settings_canonical_form():
    a = clio.Settings.parse("root=/w;depot=//d/main;store=/s;policy=verify")
    b = clio.Settings.parse("policy=verify;store=/s;depot=//d/main;root=/w")
    assert a == b
    assert str(a) == "depot=//d/main;root=/w;store=/s;policy=verify"
    assert a.policy == clio.Policy.VERIFY
    assert a.pin == clio.Pin.have()  # the default: the file on disk, synced only if missing


def test_connection_does_not_prompt_without_callback():
    # No server is contacted before the first command.
    conn = clio.Connection(port="localhost:1", timeout=5)
    assert not conn.is_connected
