"""C13 (polyrob-desk handoff-core) — a core run on Robinhood Chain TESTNET 46630, owner opt-in.

The testnet row is ``valueless``: read-only by default; with ``VALUELESS_CHAIN_MONEY`` it is
money-enabled, the guard prices its native coin (and wrapped native) at exactly $0 — so the USD
caps never bind — and the rail still bounds every fee by ``max_fee_wei_per_tx`` in wei. Any
other token stays unpriceable and refuses.
"""
import pytest

from core.wallet import chains, tx_guard
from core.wallet.policy import PolicyGate
from core.wallet.simulation import Deltas

TO = "0x70997970C51812dc3A010C7d01b50e0d17dc79C8"
HOLDER = "0x2222222222222222222222222222222222222222"
TOKEN = "0x" + "ab" * 20


@pytest.fixture
def armed(monkeypatch):
    monkeypatch.setenv("VALUELESS_CHAIN_MONEY", "true")


def test_the_testnet_is_read_only_by_default(monkeypatch):
    monkeypatch.delenv("VALUELESS_CHAIN_MONEY", raising=False)
    row = chains.get("robinhood-testnet")
    assert row.valueless and not row.money_enabled
    assert "robinhood-testnet" not in chains.money_chains()
    from core.wallet.broadcast.evm import EvmRail
    with pytest.raises(ValueError, match="read-only"):
        EvmRail("robinhood-testnet", signer=None)


def test_the_flag_arms_only_valueless_rows(armed):
    assert chains.get("robinhood-testnet").money_enabled
    assert "robinhood-testnet" in chains.money_chains()
    assert not any(r.valueless for r in chains.all_rows() if r.name != "robinhood-testnet")
    from core.wallet.broadcast.evm import EvmRail
    rail = EvmRail("robinhood-testnet", signer=None)
    assert rail.chain_id == 46630 and rail._max_fee_wei == 2 * 10 ** 15


def _run(intent, deltas, tx, price_fn=None):
    return tx_guard.authorize(
        intent, dict(tx, chainId=46630, nonce=1, gas=21_000, maxFeePerGas=10 ** 8), holder=HOLDER,
        gate=PolicyGate(max_per_tx_usd=1.0, daily_cap_usd=1.0), execution_context=None,
        simulate_fn=lambda **_: deltas, price_fn=price_fn, rpc_is_pinned_fn=lambda c: True,
        halted_fn=lambda: False, entry_paused_fn=lambda: False, forged_fn=lambda c, t: False)


def test_native_is_worth_zero_so_the_usd_caps_do_not_bind(armed):
    amount = 5 * 10 ** 18                                     # 5 testnet ETH, a $1 cap
    intent = tx_guard.TxIntent(chain="robinhood-testnet", token=None, to=TO, amount_raw=amount,
                               max_spend_usd=1.0, idempotency_key="t")
    d = _run(intent, Deltas(ok=True, native_delta=-amount, gas_used=21_000),
             {"to": TO, "value": amount, "data": "0x"})
    assert d.allowed is True, d.reason
    assert d.amount_usd == 0.0


def test_a_token_on_the_testnet_stays_unpriceable(armed):
    intent = tx_guard.TxIntent(chain="robinhood-testnet", token=TOKEN, to=TO, amount_raw=10,
                               max_spend_usd=1.0, idempotency_key="t")
    d = _run(intent, Deltas(ok=True, token_deltas={TOKEN: -10}, gas_used=50_000,
                            holder_transfers=((TOKEN, TO.lower(), 10),)),
             {"to": TOKEN, "value": 0, "data": "0xa9059cbb" + "00" * 64})
    assert d.allowed is False


def test_mainnet_prices_are_untouched(armed):
    calls = []
    price = tx_guard._valueless_price_fn("robinhood", lambda c, a: calls.append(a) or 4000.0)
    assert price("robinhood", None) == 4000.0 and calls == [None]
