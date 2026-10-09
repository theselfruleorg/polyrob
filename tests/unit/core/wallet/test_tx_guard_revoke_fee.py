"""A revoke is not refused by its own gas (prod 2026-10-08).

The worst-case fee became part of every valuation (WAL-6), and the declared
max_spend_usd check ran on value + fee. `defi_trade.revoke_approval` declares
$0.01 (a revoke moves nothing), so a $0.05 worst fee refused it and the 0.05
WETH allowance to the LI.FI router stayed live. For a pure revoke the declared
max asserts the $0 value; the fee is charged to the PolicyGate caps. The revoke shape binding
(approve(spender, 0) on the declared token) stays."""
from core.wallet import tx_guard
from core.wallet.policy import PolicyGate
from core.wallet.simulation import Deltas

WETH = "0x4200000000000000000000000000000000000006"
LIFI = "0x1231DEB6f5749EF6cE6943a275A1D3E7486F4EaE"
MALLORY = "0x3333333333333333333333333333333333333333"
HOLDER = "0x2222222222222222222222222222222222222222"
ALICE = "0x70997970C51812dc3A010C7d01b50e0d17dc79C8"

# 100k gas at 0.2 gwei, ETH at $2600 -> ~$0.052 worst fee
_FEE_TX = {"gas": 100_000, "maxFeePerGas": 200_000_000, "value": 0, "chainId": 8453}


def _word(addr):
    return "0" * 24 + addr.lower()[2:]


def _run(intent, tx, deltas, gate=None):
    return tx_guard.authorize(
        intent, {**_FEE_TX, **tx}, holder=HOLDER,
        gate=gate or PolicyGate(max_per_tx_usd=1000.0, daily_cap_usd=10_000.0),
        execution_context=None, simulate_fn=lambda **_: deltas,
        price_fn=lambda c, a: 2600.0,
        rpc_is_pinned_fn=lambda c: True, halted_fn=lambda: False,
        entry_paused_fn=lambda: False, forged_fn=lambda c, t: False)


def _revoke(idem="r"):
    # exactly what trade_tool.revoke_approval declares
    return tx_guard.TxIntent(chain="base", token=WETH, to=LIFI, amount_raw=0,
                             max_spend_usd=0.01, expected_allowance_grants=(),
                             is_allowance_op=True, idempotency_key=idem)


def _revoke_deltas():
    return Deltas(ok=True, native_delta=0, token_deltas={WETH: 0},
                  allowance_deltas={(WETH, LIFI): 0}, gas_used=46_000)


def _good():
    return {"to": WETH, "data": "0x095ea7b3" + _word(LIFI) + "0" * 64}


def test_a_revoke_with_a_real_fee_passes_its_declared_cent():
    d = _run(_revoke(), _good(), _revoke_deltas())
    assert d.allowed, d.reason
    # the fee is still charged to the caps (WAL-6 stays closed)
    assert d.amount_usd >= 0.05


def test_the_revoke_shape_binding_still_holds_with_a_fee():
    forged = {"to": "0x4444444444444444444444444444444444444444",
              "data": "0xf2fde38b" + _word(MALLORY)}
    d = _run(_revoke(), forged, _revoke_deltas())
    assert not d.allowed and "a revoke must be exactly approve" in d.reason
    grant = {"to": WETH, "data": "0x095ea7b3" + _word(LIFI) + f"{1:064x}"}
    assert not _run(_revoke(), grant, _revoke_deltas()).allowed


def test_the_fee_still_counts_against_the_policy_gate_cap():
    tiny = PolicyGate(max_per_tx_usd=0.02, daily_cap_usd=10_000.0)
    d = _run(_revoke(), _good(), _revoke_deltas(), gate=tiny)
    assert not d.allowed and "PolicyGate" in d.reason


def test_a_refused_attempt_does_not_burn_the_retry():
    # an unpriceable fee refuses before the gate; the same intent then passes
    gate = PolicyGate(max_per_tx_usd=1000.0, daily_cap_usd=10_000.0)
    refused = tx_guard.authorize(
        _revoke("same"), {**_FEE_TX, **_good()}, holder=HOLDER, gate=gate,
        execution_context=None, simulate_fn=lambda **_: _revoke_deltas(),
        price_fn=lambda c, a: None, rpc_is_pinned_fn=lambda c: True,
        halted_fn=lambda: False, entry_paused_fn=lambda: False,
        forged_fn=lambda c, t: False)
    assert not refused.allowed
    assert _run(_revoke("same"), _good(), _revoke_deltas(), gate=gate).allowed
