"""Audit 2026-11-21 L-7: an ``owner_queue`` bridge leg is signed only after the
owner cleared it — the leg records that exact transaction with
``attest.record_owner_cleared``; ``tx_guard`` no longer records the lane locally.

The fake rail here enforces the real rail's rule: no recorded attestation, no send.
"""
from types import SimpleNamespace

import pytest

from core.signer import attest
from tools.defi.bridge_evm_leg import EvmOriginLeg, EvmTokenOriginLeg, PreparedLeg

TOKEN = "0x" + "a" * 40
DEPOSITORY = "0x" + "b" * 40


@pytest.fixture(autouse=True)
def _clean():
    attest._reset_for_tests()
    yield
    attest._reset_for_tests()


class _AttestRail:
    """Signs only what ``core.signer.attest`` holds, like ``EvmRail.sign_and_send``."""

    def __init__(self):
        self.sent = []

    def build_call(self, *, to, data, value):
        return {"to": to, "data": data, "value": value, "nonce": 5 + len(self.sent),
                "chainId": 8453}

    def size_gas(self, tx, gas):
        return {**tx, "gas": gas}

    def sign_and_send(self, tx):
        if attest.take(tx) is None:
            raise RuntimeError("no tx_guard authorization is recorded; nothing was sent")
        self.sent.append(tx)
        return f"0xhash{len(self.sent)}"

    def await_receipt(self, tx_hash):
        return SimpleNamespace(succeeded=True, status="success")


def _owner_queue(*_a, **_k):
    # What the real guard returns above the ceiling — and, in local mode, it
    # records NOTHING (L-7).
    return SimpleNamespace(allowed=False, lane="owner_queue", reason="above the ceiling",
                           amount_usd=500.0, sim_gas_used=60_000)


def _native_prepared(rail):
    tx = rail.build_call(to=DEPOSITORY, data="0xdead", value=10 ** 16)
    return PreparedLeg(True, "owner_queue", rail=rail, tx=tx, amount_usd=500.0,
                       needs_owner_approval=True, intent=SimpleNamespace(kind="bridge"))


def test_a_native_leg_the_owner_cleared_is_sent():
    rail = _AttestRail()
    out = EvmOriginLeg.send(_native_prepared(rail), owner_cleared=True)
    assert out.state == "sent" and len(rail.sent) == 1


def test_a_native_leg_needing_the_owner_without_owner_cleared_signs_nothing():
    """The native leg records owner-cleared only on the caller's explicit flag, like the
    token leg — the caller's control flow is not the proof."""
    rail = _AttestRail()
    out = EvmOriginLeg.send(_native_prepared(rail))
    assert out.state == "error" and "Nothing was signed" in out.detail
    assert rail.sent == [] and attest.take(rail.build_call(
        to=DEPOSITORY, data="0xdead", value=10 ** 16)) is None


def test_an_owner_queue_tx_with_no_owner_step_is_refused_by_the_rail():
    rail = _AttestRail()
    prepared = _native_prepared(rail)
    with pytest.raises(RuntimeError, match="no tx_guard authorization"):
        rail.sign_and_send(prepared.tx)
    assert rail.sent == []


def test_an_allowed_native_leg_records_nothing_extra():
    rail = _AttestRail()
    prepared = PreparedLeg(True, "ok", rail=rail, tx=rail.build_call(
        to=DEPOSITORY, data="0x", value=1), needs_owner_approval=False,
        intent=SimpleNamespace())
    out = EvmOriginLeg.send(prepared)      # the guard recorded nothing in this fake
    assert out.state == "error" and rail.sent == []


def _token_leg(rail, owner_cleared):
    leg = EvmTokenOriginLeg(chain="base", signer=SimpleNamespace(address="0x" + "c" * 40),
                            token=TOKEN, rail_factory=lambda **_k: rail,
                            authorize_fn=_owner_queue)
    leg._ctx = dict(tx_data={"to": DEPOSITORY, "data": "0xbeef"}, depository=DEPOSITORY,
                    amount_in_raw=1_000_000, declared_usd=500.0, gate=None,
                    execution_context=None, tool=None, idempotency_key="k")
    leg.owner_cleared = owner_cleared
    approve = rail.build_call(to=TOKEN, data="0x1", value=0)
    return leg, PreparedLeg(True, "owner_queue", rail=rail, tx=approve, amount_usd=500.0,
                            needs_owner_approval=True, intent=SimpleNamespace(kind="approve"))


def test_a_token_leg_the_owner_cleared_sends_the_approve_and_the_deposit():
    rail = _AttestRail()
    leg, prepared = _token_leg(rail, owner_cleared=True)
    out = leg.send(prepared)
    assert out.state == "sent", out.detail
    assert [t["to"] for t in rail.sent] == [TOKEN, DEPOSITORY]


def test_a_token_leg_not_cleared_by_the_owner_signs_nothing():
    rail = _AttestRail()
    leg, prepared = _token_leg(rail, owner_cleared=False)
    out = leg.send(prepared)
    assert out.state == "error" and rail.sent == []
    # The leg refuses itself, like the native leg: not the rail's attestation check.
    assert "none is proven" in out.detail and "Nothing was signed" in out.detail
