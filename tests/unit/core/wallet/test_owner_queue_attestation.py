"""Audit 2026-11-21 L-7: an ``owner_queue`` guard decision is NOT an authorization.

``tx_guard.authorize`` records an attestation for an ``allowed`` decision only —
and, under ``WALLET_SIGNER=remote``, for ``owner_queue`` too, because the remote
signer re-runs the guard and asks the owner on its own box. In ``local`` and
``shadow`` mode the rail refuses an ``owner_queue`` transaction unless the verb,
at the point where it proved the owner's approval, calls
``attest.record_owner_cleared(intent, tx)`` for that exact transaction.
"""
import pytest
from eth_utils import keccak

from core.signer import attest
from tests.unit.core.signer.conftest import (  # noqa: F401 — fixtures
    ETH_PRICE, native_deltas, native_intent, native_tx, signer_home)


def _authorize(intent, tx):
    from core.wallet import tx_guard
    from core.wallet.policy import PolicyGate
    return tx_guard.authorize(
        intent, tx, holder="0x" + "2" * 40,
        gate=PolicyGate(max_per_tx_usd=500.0, daily_cap_usd=1000.0),
        execution_context=None, simulate_fn=lambda **_: native_deltas(),
        price_fn=lambda c, a: ETH_PRICE, rpc_is_pinned_fn=lambda c: True,
        halted_fn=lambda: False, entry_paused_fn=lambda: False,
        forged_fn=lambda ctx, tool: False, approve_above_usd=0.01)


class _Signer:
    address = "0x" + "1" * 40

    def __init__(self):
        self.signed = []

    def sign_transaction(self, tx):
        self.signed.append(dict(tx))
        return b"synthetic signed bytes"


def _rail(signer):
    from core.wallet.broadcast.evm import EvmRail
    rail = EvmRail("base", signer)
    rail._rpc = lambda m, p, timeout=8.0: (
        hex(8453) if m == "eth_chainId" else "0x" + keccak(b"synthetic signed bytes").hex())
    return rail


@pytest.mark.parametrize("mode", ["local", "shadow"])
def test_an_owner_queue_decision_records_nothing_outside_remote(signer_home, monkeypatch, mode):
    monkeypatch.setenv("WALLET_SIGNER", mode)
    tx = native_tx()
    decision = _authorize(native_intent(), tx)
    assert not decision.allowed and decision.lane == "owner_queue"
    assert attest.peek(tx) is None


def test_the_rail_refuses_an_owner_queue_tx_without_the_owner_step(signer_home):
    from core.wallet.broadcast.evm import BroadcastError
    tx = native_tx()
    assert _authorize(native_intent(), tx).lane == "owner_queue"
    signer = _Signer()
    with pytest.raises(BroadcastError, match="no tx_guard authorization"):
        _rail(signer).sign_and_send(tx)
    assert signer.signed == []


def test_the_owner_step_lets_that_exact_tx_send_once(signer_home):
    from core.wallet.broadcast.evm import BroadcastError
    intent, tx = native_intent(), native_tx()
    assert _authorize(intent, tx).lane == "owner_queue"
    attest.record_owner_cleared(intent, tx)
    other = native_tx(value=tx["value"] + 1)
    signer = _Signer()
    rail = _rail(signer)
    with pytest.raises(BroadcastError, match="no tx_guard authorization"):
        rail.sign_and_send(other)                         # not the cleared tx
    assert rail.sign_and_send(tx)
    assert len(signer.signed) == 1
    with pytest.raises(BroadcastError, match="no tx_guard authorization"):
        rail.sign_and_send(tx)                            # taken once


def test_remote_mode_still_records_the_owner_queue_lane(signer_home, monkeypatch):
    monkeypatch.setenv("WALLET_SIGNER", "remote")
    tx = native_tx()
    assert _authorize(native_intent(), tx).lane == "owner_queue"
    assert attest.peek(tx) is not None


def test_owner_cleared_needs_an_intent():
    with pytest.raises(ValueError):
        attest.record_owner_cleared(None, native_tx())
