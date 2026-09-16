"""The injected provider script (042).

It runs in a page we do not control, so the things worth pinning are the ones
that would silently break: the values must be JSON-escaped (never f-string
spliced into JS), and the discovery surface must cover BOTH the legacy global
and EIP-6963 — a dapp that only listens for the event never sees
``window.ethereum``.
"""
import json

import pytest

from tools.dapp_browser.js import BINDING, provider_script

ADDRESS = "0xcAda546f6A6ddDE31B71aB21eF63d3EBF09Fa553"


def _script(**kw):
    return provider_script(**{"address": ADDRESS, "chain_id_hex": "0x2105", **kw})


def test_the_address_and_chain_are_json_escaped_string_literals():
    """Splicing a value straight into JS is how a hostile string becomes code.
    These come from our own wallet today, and the escaping is what keeps that
    from being load-bearing."""
    script = _script()
    assert json.dumps(ADDRESS) in script
    assert json.dumps("0x2105") in script
    assert f"const ADDRESS = {json.dumps(ADDRESS)};" in script


def test_a_quote_in_a_value_cannot_break_out_of_its_literal():
    script = _script(address='0x1";alert(1);//')
    assert 'alert(1)' not in script.replace(json.dumps('0x1";alert(1);//'), "")


def test_both_discovery_surfaces_are_present():
    script = _script()
    assert "window, 'ethereum'" in script          # the legacy global
    assert "eip6963:announceProvider" in script    # what modern dapps listen for
    assert "eip6963:requestProvider" in script
    assert "ethereum#initialized" in script


def test_every_request_that_can_move_anything_crosses_the_binding():
    """Only the two pure echoes are answered in the page. Everything else —
    every send, every signature request — goes to Python."""
    script = _script()
    assert f"window.{BINDING}(" in script
    for method in ("eth_sendTransaction", "personal_sign", "eth_signTypedData_v4"):
        assert f"'{method}'" not in script, (
            f"{method} must not be handled in the page")


def test_the_legacy_shims_are_present():
    """Plenty of live dapps still call send/sendAsync/enable."""
    script = _script()
    for name in ("send(", "sendAsync(", "enable()", "on(", "removeListener("):
        assert name in script


@pytest.mark.parametrize("marker", ["privateKey", "mnemonic", "seed", "sign("])
def test_the_script_carries_no_key_material_and_does_no_signing(marker):
    assert marker not in _script()
