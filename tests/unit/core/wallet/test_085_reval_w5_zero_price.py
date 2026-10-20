"""085 W5 item 3 revalidation: 0.0 is "no price" on every priced branch, except
the one true $0 — a ``valueless`` testnet row, where the wrapped native coin is
worth exactly $0 as a TOKEN too (C13)."""
import pytest

from core.wallet import chains, tx_guard
from core.wallet.policy import PolicyGate
from core.wallet.simulation import Deltas
from core.wallet.tx_guard_steps.value import _spend_price

TO = "0x70997970C51812dc3A010C7d01b50e0d17dc79C8"
HOLDER = "0x2222222222222222222222222222222222222222"


def test_spend_price_keeps_zero_only_on_a_valueless_chain():
    assert _spend_price(0.0, "robinhood-testnet") == 0.0
    assert _spend_price(0.0, "base") is None
    assert _spend_price(0.0) is None
    assert _spend_price(-1.0, "robinhood-testnet") is None
    assert _spend_price(2.0, "base") == 2.0


def test_the_testnet_wrapped_native_token_is_worth_zero(monkeypatch):
    monkeypatch.setenv("VALUELESS_CHAIN_MONEY", "true")
    weth = chains.get("robinhood-testnet").wrapped_native
    intent = tx_guard.TxIntent(chain="robinhood-testnet", token=weth, to=TO, amount_raw=10 ** 18,
                               max_spend_usd=1.0, idempotency_key="t-weth")
    deltas = Deltas(ok=True, token_deltas={weth: -10 ** 18}, gas_used=50_000,
                    holder_transfers=((weth, TO.lower(), 10 ** 18),))
    data = "0xa9059cbb" + "0" * 24 + TO[2:].lower() + hex(10 ** 18)[2:].rjust(64, "0")
    d = tx_guard.authorize(
        intent, {"to": weth, "value": 0, "data": data, "chainId": 46630, "nonce": 1,
                 "gas": 60_000, "maxFeePerGas": 10 ** 8},
        holder=HOLDER, gate=PolicyGate(max_per_tx_usd=1.0, daily_cap_usd=1.0),
        execution_context=None, simulate_fn=lambda **_: deltas, price_fn=None,
        rpc_is_pinned_fn=lambda c: True, halted_fn=lambda: False,
        entry_paused_fn=lambda: False, forged_fn=lambda c, t: False)
    assert d.allowed is True, d.reason
    assert d.amount_usd == 0.0
