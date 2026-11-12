"""The signer answers itself what the agent could only declare.

``is_resource_call`` and ``held_balance_raw`` arrive in the request, so a compromised
agent sets them. A resource call is a zero-value call charged the fee only: the signer
signs it only for a contract and function its own file lists. ``held_balance_raw``
waives the spend-grade price bar: the signer reads the balance, and a balance it could
not read waives nothing.
"""
import dataclasses

import pytest

from core.signer import protocol
from core.signer.caps import SignerConfigError, parse_signer_config, render_signer_toml
from tests.unit.core.signer.conftest import make_config, native_intent, native_tx

TOKEN = "0x" + "44" * 20
CONTRACT = "0x" + "55" * 20
SELECTOR = "0xf2fde38b"
PIN = f"base:{CONTRACT}:{SELECTOR}"
HUGE = 2 ** 255        # what a compromised agent would declare


def _resource(rig, **cfg):
    intent = native_intent(to=CONTRACT, amount_raw=0, is_resource_call=True,
                           resource_recipient="0x" + "66" * 20)
    tx = native_tx(to=CONTRACT, value=0, data=SELECTOR + "00" * 32)
    return intent, tx


def test_a_resource_call_the_file_does_not_list_is_refused(rig):
    intent, tx = _resource(rig)
    resp = rig.call("evm.send", rig.body(intent, tx))
    assert not resp["ok"] and resp["code"] == protocol.GUARD_REFUSED
    assert "resource_calls" in resp["reason"] and PIN in resp["reason"]


def test_a_listed_resource_call_passes_the_pin(make_rig):
    rig = make_rig(policy={"resource_calls": [PIN.upper().replace("BASE", "base").replace("0X", "0x")]})
    intent, tx = _resource(rig)
    assert rig.service._own_view(intent, tx) is intent
    other = dict(tx, data="0x40c10f19" + "00" * 64)
    with pytest.raises(Exception) as exc:
        rig.service._own_view(intent, other)
    assert "resource_calls" in str(exc.value)


@pytest.mark.parametrize("rows", [
    "base:0x55:0xf2fde38b", ["base:0x" + "55" * 20], ["base:" + CONTRACT + ":0xf2fd"], [7],
])
def test_a_bad_resource_call_row_does_not_load(tmp_path, rows):
    with pytest.raises(SignerConfigError):
        make_config(tmp_path, policy={"resource_calls": rows})


def test_the_rows_survive_a_render(tmp_path):
    import tomllib
    cfg = make_config(tmp_path, policy={"resource_calls": [PIN]})
    assert cfg.resource_calls == (("base", CONTRACT, SELECTOR),)
    assert parse_signer_config(tomllib.loads(render_signer_toml(cfg))).resource_calls == \
        cfg.resource_calls
    assert cfg.summary()["resource_calls"] == 1


def test_the_signer_reads_the_held_balance_itself(rig):
    seen = []
    rig.service._balance_fn = lambda chain, holder, token: seen.append((chain, holder, token)) or 123
    declared = native_intent(token=TOKEN, **{"held_balance_raw": HUGE})
    own = rig.service._own_view(declared, native_tx())
    assert own.held_balance_raw == 123
    assert seen == [("base", rig.service.wallet.operational_signer().address, TOKEN)]


def test_a_balance_the_signer_cannot_read_waives_nothing(rig):
    def boom(chain, holder, token):
        raise RuntimeError("rpc down")
    rig.service._balance_fn = boom
    own = rig.service._own_view(native_intent(token=TOKEN, **{"held_balance_raw": HUGE}), native_tx())
    assert own.held_balance_raw is None


def test_no_declared_balance_means_no_read(rig):
    rig.service._balance_fn = lambda *a: (_ for _ in ()).throw(AssertionError("no read"))
    intent = native_intent()
    assert rig.service._own_view(intent, native_tx()) is intent


@pytest.mark.parametrize("name", ["lp_outflows", "lp_inflows", "lp_held_balances"])
@pytest.mark.parametrize("leg", [[TOKEN, -1], [TOKEN, 1.5], [TOKEN], [7, 1]])
def test_the_wire_codec_refuses_a_bad_liquidity_leg(name, leg):
    wire = protocol.intent_to_wire(native_intent())
    wire[name] = [leg]
    with pytest.raises(protocol.ProtocolError):
        protocol.intent_from_wire(wire)


def test_the_wire_codec_keeps_a_native_liquidity_leg():
    wire = protocol.intent_to_wire(native_intent())
    wire["lp_outflows"] = [[None, 5], [TOKEN, 0]]
    assert tuple(protocol.intent_from_wire(wire).lp_outflows) == ((None, 5), (TOKEN, 0))
