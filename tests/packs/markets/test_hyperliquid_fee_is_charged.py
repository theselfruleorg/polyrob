"""A Hyperliquid order's fee leaves the margin account like the order: the spend caps
are charged notional + the worst fee, never the notional alone."""
import inspect

from polyrob_markets.hyperliquid import service


def test_the_charge_includes_the_worst_fee():
    assert service._with_fee(1000.0) == 1000.0 * (1 + service.HL_WORST_FEE_RATE)
    assert service.HL_WORST_FEE_RATE >= 0.0007          # the base-tier spot taker rate


def test_every_check_submit_and_record_uses_the_fee_inclusive_charge():
    src = inspect.getsource(service)
    assert 'policy.check(venue="hyperliquid", amount_usd=order_value_usd' not in src
    assert src.count('policy.check(venue="hyperliquid", amount_usd=_with_fee(order_value_usd)') == 2
    assert src.count("amount_usd=_with_fee(order_value_usd), counterparty") == 2
    assert src.count('self._user_id, _with_fee(order_value_usd),') == 2
