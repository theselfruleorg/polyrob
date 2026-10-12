"""The exact USDC allowance to the Relay depository never outlives a bridge that
did not move value: an approve whose receipt is unknown and a deposit that
landed REVERTED both revoke it."""
from types import SimpleNamespace

from tools.defi.bridge_evm_leg import EvmTokenOriginLeg, PreparedLeg

TOKEN = "0x" + "a" * 40
DEPOSITORY = "0x" + "b" * 40


class _Rail:
    def __init__(self, receipt):
        self.receipt = receipt
        self.sent = []

    def build_call(self, *, to, data, value):
        return {"to": to, "data": data, "value": value}

    def size_gas(self, tx, gas):
        return tx

    def sign_and_send(self, tx):
        self.sent.append(tx)
        return f"0xhash{len(self.sent)}"

    def await_receipt(self, tx_hash):
        return self.receipt


def _allow(*_a, **_k):
    return SimpleNamespace(allowed=True, lane="autonomous", reason="ok",
                           amount_usd=1.0, sim_gas_used=None)


def _leg(rail):
    leg = EvmTokenOriginLeg(chain="base", signer=SimpleNamespace(address="0x" + "c" * 40),
                            token=TOKEN, rail_factory=lambda **_k: rail, authorize_fn=_allow)
    leg._ctx = dict(tx_data={"to": DEPOSITORY, "data": "0x"}, depository=DEPOSITORY,
                    amount_in_raw=1_000_000, declared_usd=1.0, gate=None,
                    execution_context=None, tool=None, idempotency_key="k")
    return leg


def _is_revoke(tx):
    # approve(spender, 0): the amount word is all zeros
    return tx["to"] == TOKEN and tx["data"].endswith("0" * 64)


def test_an_approve_with_no_receipt_is_revoked():
    rail = _Rail(SimpleNamespace(succeeded=False, status="pending"))
    leg = _leg(rail)
    out = leg.send(PreparedLeg(True, "ok", rail=rail, tx={"to": TOKEN, "data": "0x1"}))
    assert out.state == "error"
    assert len(rail.sent) == 2 and _is_revoke(rail.sent[1])
    assert "revoked" in out.detail


def test_a_reverted_approve_sends_no_revoke():
    rail = _Rail(SimpleNamespace(succeeded=False, status="reverted"))
    leg = _leg(rail)
    out = leg.send(PreparedLeg(True, "ok", rail=rail, tx={"to": TOKEN, "data": "0x1"}))
    assert out.state == "error" and len(rail.sent) == 1


def test_a_reverted_deposit_revokes_the_allowance():
    rail = _Rail(None)
    leg = _leg(rail)
    msg = leg.revoke_after_revert(PreparedLeg(True, "ok", rail=rail))
    assert "revoked" in msg and _is_revoke(rail.sent[0])
