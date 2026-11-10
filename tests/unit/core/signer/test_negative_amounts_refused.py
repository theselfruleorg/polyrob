"""A negative declared amount must never reach the value the caps judge.

The guard ADDS a declared allowance grant to the value of a send, so a negative
grant cancelled it: a large send was charged a few dollars and signed. The wire
codec refuses it, and the guard refuses it for a caller that builds the intent
in process.
"""
import dataclasses

import pytest

from core.signer import protocol
from core.wallet.tx_guard import _price_allowance_grants
from tests.unit.core.signer.conftest import FakeRail, native_intent

TOKEN = "0x" + "11" * 20
SPENDER = "0x" + "22" * 20


def _wire(**over):
    wire = protocol.intent_to_wire(native_intent())
    wire.update(over)
    return wire


@pytest.mark.parametrize("over", [
    {"expected_allowance_grants": [[TOKEN, SPENDER, -(10 ** 20)]]},
    {"expected_allowance_grants": [[TOKEN, SPENDER, 1.5]]},
    {"expected_allowance_grants": [[TOKEN, SPENDER, True]]},
    {"expected_allowance_grants": [[TOKEN, SPENDER]]},
    {"amount_raw": -1},
    {"amount_raw": "5"},
    {"held_balance_raw": -1},
    {"min_inflow_raw": -1},
    {"max_spend_usd": -1.0},
])
def test_the_wire_codec_refuses_a_bad_amount(over):
    with pytest.raises(protocol.ProtocolError):
        protocol.intent_from_wire(_wire(**over))


def test_the_wire_codec_keeps_a_zero_grant_and_a_plain_intent():
    back = protocol.intent_from_wire(_wire(expected_allowance_grants=[[TOKEN, SPENDER, 0]]))
    assert tuple(back.expected_allowance_grants) == ((TOKEN, SPENDER, 0),)
    assert protocol.intent_from_wire(_wire()).amount_raw == native_intent().amount_raw


def test_the_guard_refuses_a_negative_grant_built_in_process():
    intent = dataclasses.replace(
        native_intent(), expected_allowance_grants=((TOKEN, SPENDER, -(10 ** 20)),))
    total, refusal = _price_allowance_grants(
        intent, price_fn=lambda chain, token: 1.0, fallback_price_fn=None,
        execution_context=None)
    assert total == 0.0 and refusal is not None and not refusal.allowed
    assert "non-negative" in refusal.reason


def test_the_signer_does_not_sign_a_send_with_a_negative_grant(rig):
    body = rig.body(native_intent())
    body["intent"]["expected_allowance_grants"] = [[TOKEN, SPENDER, -(10 ** 20)]]
    resp = rig.call("evm.send", body)
    assert resp["ok"] is False, resp
    assert FakeRail.sent == []
