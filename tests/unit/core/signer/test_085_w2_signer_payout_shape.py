"""085 W2 across the process boundary: ``TxIntent.payout_shape`` survives the
strict codec, but the signer does NOT honor it. The flag is the agent's own
classification — a compromised agent clears it — so the signer's caps and
ceiling govern a payout exactly as a plain transfer of the same value: inside
the cap envelope it signs, above the signer ceiling it needs the root grant.
"""
from core.signer import protocol
from tests.unit.core.signer.conftest import FakeRail, native_intent


def test_payout_shape_round_trips_the_strict_codec():
    wire = protocol.intent_to_wire(native_intent(payout_shape=True, value_ack=True))
    assert wire["payout_shape"] is True and wire["value_ack"] is True
    back = protocol.intent_from_wire(wire)
    assert back.payout_shape is True and back.value_ack is True


def test_a_payout_inside_the_signer_caps_signs_like_a_transfer(rig):
    sent = rig.call("evm.send", rig.body(native_intent(payout_shape=True)))   # $10
    assert sent["ok"], sent
    assert len(FakeRail.sent) == 1


def test_a_payout_above_the_signer_ceiling_still_needs_root(make_rig):
    rig = make_rig(per_tx_usd=5.0)
    body = rig.body(native_intent(payout_shape=True))
    resp = rig.call("evm.send", body)
    assert resp["ok"] is False and resp["code"] == protocol.APPROVAL_REQUIRED, resp
    assert "PAYOUT" not in resp["reason"]          # the cap held it, not the agent's flag
    assert FakeRail.sent == []
    granted = rig.call("approvals.decide", {"id": resp["approval_id"], "grant": True}, uid=0)
    assert granted["ok"] and granted["result"]["state"] == "granted"
    assert rig.call("evm.send", body)["ok"]
    assert len(FakeRail.sent) == 1


def test_payout_and_plain_transfer_get_the_same_signer_verdict(make_rig):
    rig = make_rig(per_tx_usd=5.0)
    plain = rig.call("evm.send", rig.body(native_intent(idempotency_key="p-1")))
    payout = rig.call("evm.send", rig.body(native_intent(idempotency_key="p-1",
                                                         payout_shape=True)))
    assert plain["code"] == payout["code"] == protocol.APPROVAL_REQUIRED
