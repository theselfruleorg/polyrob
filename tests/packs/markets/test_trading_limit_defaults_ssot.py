"""Each venue's trading-limit defaults live once: on the TradingLimits dataclass.

from_dict, the API request models and the configure route take the default
from the dataclass; the values below pin each venue's current numbers.
"""

from dataclasses import asdict

from polyrob_markets.hyperliquid import api_models as hl_api
from polyrob_markets.hyperliquid.store_models import TradingLimits as HLLimits
from polyrob_markets.polymarket import api_models as pm_api
from polyrob_markets.polymarket.store_models import TradingLimits as PMLimits


def test_polymarket_defaults_pinned():
    d = PMLimits()
    assert (d.max_order_size_usd, d.max_total_exposure_usd, d.max_position_per_market_usd) == (1000, 5000, 2000)
    assert (d.min_liquidity_required, d.max_spread_tolerance, d.require_confirmation_above_usd) == (10000, 0.05, 500)
    assert d.enable_autonomous_trading is False
    assert d.allowed_categories == ["*"] and d.blocked_markets == []


def test_hyperliquid_defaults_pinned():
    d = HLLimits()
    assert (d.max_order_size_usd, d.max_total_exposure_usd, d.max_position_per_market_usd) == (1000.0, 10000.0, 5000.0)
    assert (d.max_leverage, d.max_daily_loss_usd, d.require_confirmation_above_usd) == (5, 500.0, 500.0)
    assert (d.max_spread_tolerance, d.min_liquidity_required) == (0.01, 50000.0)
    assert d.enable_autonomous_trading is False
    assert d.allowed_coins == [] and d.blocked_coins == []


def test_from_dict_missing_keys_take_dataclass_defaults():
    assert PMLimits.from_dict({}) == PMLimits()
    assert PMLimits.from_dict({"x": 1}) == PMLimits()
    assert HLLimits.from_dict({}) == HLLimits()
    pm = PMLimits.from_dict({"max_order_size_usd": 7})
    assert pm.max_order_size_usd == 7 and pm.max_total_exposure_usd == 5000
    hl = HLLimits.from_dict({"max_leverage": 3})
    assert hl.max_leverage == 3 and hl.max_order_size_usd == 1000.0


def test_from_dict_round_trip():
    for cls in (PMLimits, HLLimits):
        assert cls.from_dict(cls().to_dict()) == cls()
        assert set(cls().to_dict()) == set(asdict(cls()))


def test_api_model_defaults_match_dataclass():
    req = pm_api.TradingLimitsRequest()
    d = PMLimits()
    for k, v in req.model_dump().items():
        assert getattr(d, k) == v, k
    fields = hl_api.ConfigureHyperliquidRequest.model_fields
    h = HLLimits()
    for k in ("max_order_size_usd", "max_leverage", "enable_autonomous_trading"):
        assert fields[k].default == getattr(h, k), k
