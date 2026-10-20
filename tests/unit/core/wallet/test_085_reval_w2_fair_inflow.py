"""085 W2 revalidation (money M-1): a declared inflow keeps a generic call off
the payout shape only when it is FAIR — a canonical or own receive token worth
at least half the declared outflow at the spend-grade price. 1 wei of a junk
token back, or an unpriced receive token, is a payout: the owner taps it."""
from core.config_policy.spend_lane import payout_shaped
from core.wallet import tx_guard
from core.wallet.simulation import Deltas
from tests.unit.core.wallet.test_085_w2_payout_shape import (  # noqa: F401 (fixture)
    HOLDER, _authorize, _env)

WETH = "0x4200000000000000000000000000000000000006"
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
JUNK = "0x9999999999999999999999999999999999999999"
ATTACKER = "0x8888888888888888888888888888888888888888"
VALUE = 4 * 10 ** 15                     # 0.004 ETH = $10 at $2500


def _price(chain, addr):
    return {WETH.lower(): 2500.0, USDC.lower(): 1.0, JUNK.lower(): 1.0}.get(str(addr).lower())


def _call(receive_token, receive_min_raw, price_fn=_price):
    params = {"chain": "base", "value": VALUE / 1e18, "receive_token": receive_token,
              "receive_min_raw": receive_min_raw}
    payout = payout_shaped("defi_trade_call", params, price_fn=price_fn)
    tx = {"to": ATTACKER, "data": "0x12345678", "value": VALUE, "chainId": 8453,
          "gas": 80000, "maxFeePerGas": 10 ** 8}
    intent = tx_guard.TxIntent(chain="base", token=None, to=ATTACKER, amount_raw=VALUE,
                               max_spend_usd=25, idempotency_key=f"k-{receive_token}-{receive_min_raw}",
                               inflow_token=receive_token, min_inflow_raw=receive_min_raw,
                               payout_shape=payout)
    deltas = Deltas(ok=True, native_delta=-VALUE,
                    token_deltas={receive_token: receive_min_raw}, gas_used=60000)
    return payout, _authorize(intent, tx, deltas, price_fn=price_fn)


def test_one_wei_of_a_junk_token_back_is_an_owner_payout():
    payout, d = _call(JUNK, 1)
    assert payout is True
    assert d.allowed is False and d.lane == "owner_queue", d.reason


def test_one_wei_of_a_canonical_token_back_is_still_a_payout():
    payout, d = _call(USDC, 1)
    assert payout is True and d.lane == "owner_queue", d.reason


def test_a_fair_canonical_inflow_stays_autonomous():
    payout, d = _call(USDC, 9_000_000)                  # $9 back for $10 out
    assert payout is False
    assert d.allowed is True and d.lane == "autonomous", d.reason


def test_an_unpriced_receive_token_is_an_owner_payout():
    def no_usdc(chain, addr):
        return None if str(addr).lower() == USDC.lower() else _price(chain, addr)
    payout = payout_shaped("defi_trade_call", {
        "chain": "base", "value": 0.004, "receive_token": USDC,
        "receive_min_raw": 9_000_000}, price_fn=no_usdc)
    assert payout is True


def test_no_registered_price_is_a_payout(monkeypatch):
    from core.config_policy import spend_lane
    monkeypatch.setattr(spend_lane, "_SPEND_PRICE", None)
    assert payout_shaped("defi_trade_call", {
        "chain": "base", "value": 0.004, "receive_token": USDC, "receive_min_raw": 9_000_000})


def test_the_agents_own_token_counts_as_trusted(monkeypatch):
    monkeypatch.setattr("core.wallet.token_provenance.own_token",
                        lambda chain, addr, **k: {"kind": "launch"})
    def priced(chain, addr):
        return 1.0 if str(addr).lower() == JUNK.lower() else _price(chain, addr)
    monkeypatch.setattr("core.wallet.tx_guard._decimals_for", lambda chain, token: 18)
    assert not payout_shaped("defi_trade_call", {
        "chain": "base", "value": 0.004, "receive_token": JUNK,
        "receive_min_raw": 9 * 10 ** 18}, price_fn=priced)
