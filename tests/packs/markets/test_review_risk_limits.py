"""Adversarial live data must not turn configured venue limits into no-ops."""
import types
from unittest.mock import AsyncMock, Mock

import pytest

from polyrob_markets.risk_limits import RiskLimitError, check_book, check_exposure
from polyrob_markets.polymarket.store_models import TradingLimits
from polyrob_markets.polymarket.service import PolymarketTool, GetOpenOrdersParams
from polyrob_markets.hyperliquid.service import HyperliquidTool
from ._risk_fixtures import polymarket, hyperliquid


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1, True])
def test_nonfinite_and_negative_exposure_refused(value):
    with pytest.raises((RiskLimitError, ValueError)):
        check_exposure(TradingLimits(), [{"market_id": "m1", "value": value}], [], "m1", 1)


def test_pending_orders_count_toward_total_and_market_caps():
    orders = [{"market_id": "m1", "size": 1000, "size_matched": 100, "price": .5}]
    with pytest.raises(RiskLimitError, match="Per-market"):
        check_exposure(TradingLimits(max_position_per_market_usd=500), [], orders, "m1", 51)
    with pytest.raises(RiskLimitError, match="Total exposure"):
        check_exposure(TradingLimits(max_total_exposure_usd=500), [], orders, "other", 51)
    check_exposure(TradingLimits(), [], orders, "m1", 1)


def test_far_away_book_quotes_do_not_satisfy_liquidity():
    book = {"success": True, "bids": [{"price": .5, "size": 1}, {"price": .1, "size": 1e6}],
            "asks": [{"price": .51, "size": 1}, {"price": .9, "size": 1e6}]}
    with pytest.raises(RiskLimitError, match="liquidity"):
        check_book(TradingLimits(), book)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure, expected", [
    ("blocked_slug", "blocked"), ("blocked_condition", "blocked"),
    ("category", "category"), ("token", "Token"), ("spread", "spread"),
    ("liquidity", "liquidity"), ("unreadable", "Cannot verify"),
    ("pending", "Per-market"), ("slug_position", "Per-market"),
])
async def test_polymarket_live_limits(monkeypatch, failure, expected):
    tool = PolymarketTool.__new__(PolymarketTool)
    creds = types.SimpleNamespace(trading_limits=TradingLimits(max_position_per_market_usd=100))
    polymarket(tool, creds, monkeypatch)
    market = tool.get_market_details.return_value["market"]
    market["slug"] = "election"
    params = types.SimpleNamespace(market_id="election", token_id="t1", side="BUY", size_usd=10)
    limits = creds.trading_limits
    if failure == "blocked_slug": limits.blocked_markets = ["election"]
    elif failure == "blocked_condition": limits.blocked_markets = ["m1"]
    elif failure == "category": limits.allowed_categories = ["sports"]
    elif failure == "token": params.token_id = "different-market-token"
    elif failure == "spread": tool.get_orderbook.return_value["asks"][0]["price"] = .8
    elif failure == "liquidity": tool.get_orderbook.return_value["asks"][0]["size"] = 0; tool.get_orderbook.return_value["bids"][0]["size"] = 0
    elif failure == "unreadable": tool.get_open_orders.return_value = {"success": False}
    elif failure == "pending": tool.get_open_orders.return_value["orders"] = [
        {"market_id": "m1", "side": "BUY", "size": 200, "size_matched": 0, "price": .5}]
    elif failure == "slug_position": tool.get_all_positions.return_value["positions"] = [{"market_id": "m1", "value": 95}]
    assert expected in await tool._check_position_limit(limits, params)


@pytest.mark.asyncio
async def test_polymarket_sdk_order_fields_and_filter(monkeypatch):
    tool = PolymarketTool(config=types.SimpleNamespace(), container=None)
    monkeypatch.setattr(tool, "ensure_initialized", AsyncMock())
    monkeypatch.setattr(tool, "rate_limit", AsyncMock())
    monkeypatch.setattr(tool, "_get_user_credentials", AsyncMock(return_value=types.SimpleNamespace(demo_mode=False)))
    client = types.SimpleNamespace(get_open_orders=Mock(return_value=[{
        "id": "order", "market": "condition", "asset_id": "token", "side": "BUY",
        "price": ".5", "original_size": "100", "size_matched": "25"}]))
    monkeypatch.setattr(tool, "_get_authenticated_client", AsyncMock(return_value=(client, None)))
    result = await tool.get_open_orders(GetOpenOrdersParams(market_id="condition"))
    assert result["success"]
    assert result["orders"][0]["size"] == 100
    assert result["orders"][0]["size_matched"] == 25
    assert client.get_open_orders.call_args.args[0].market == "condition"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure, expected", [
    ("leverage", "leverage"), ("market_cap", "Per-market"),
    ("pending", "Total exposure"), ("nan", "Invalid numeric"),
    ("coin", "native perpetual"), ("liquidity", "liquidity"),
])
async def test_hyperliquid_live_limits(monkeypatch, failure, expected):
    tool = HyperliquidTool(config=types.SimpleNamespace(), container=None)
    credentials = types.SimpleNamespace(trading_limits=types.SimpleNamespace())
    hyperliquid(tool, credentials, monkeypatch)
    monkeypatch.setattr(tool, "rate_limit", AsyncMock())
    coin = "ETH"
    if failure == "leverage":
        tool._http_client.post.return_value.json = lambda: {"coin": coin, "user": credentials.wallet_address, "leverage": {"value": 50}}
    elif failure == "market_cap":
        credentials.trading_limits.max_position_per_market_usd = 1
    elif failure == "pending":
        credentials.trading_limits.max_total_exposure_usd = 100
        tool.get_open_orders.return_value["orders"] = [{"coin": "BTC", "price": 1, "size": 95}]
    elif failure == "nan": tool.get_account_state.return_value["total_ntl_pos"] = float("nan")
    elif failure == "coin": coin = "xyz:TEST"
    elif failure == "liquidity": tool.get_orderbook.return_value["asks"][0]["size"] = 0; tool.get_orderbook.return_value["bids"][0]["size"] = 0
    ok, error = await tool._check_exposure(credentials, 10, False, coin)
    assert not ok and expected in error


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["nan", "truncated", "fees", "missing_pnl"])
async def test_daily_loss_cannot_be_disabled_by_incomplete_data(monkeypatch, failure):
    from datetime import datetime, timezone
    tool = HyperliquidTool(config=types.SimpleNamespace(), container=None)
    credentials = types.SimpleNamespace(trading_limits=types.SimpleNamespace())
    hyperliquid(tool, credentials, monkeypatch)
    row = {"time": int(datetime.now(timezone.utc).timestamp() * 1000),
           "closed_pnl": 0, "fee": 0, "fee_token": "USDC"}
    if failure == "nan": row["closed_pnl"] = float("nan")
    elif failure == "fees": row["fee"] = 501
    elif failure == "missing_pnl": del row["closed_pnl"]
    tool.get_fills.return_value["fills"] = [row] * (500 if failure == "truncated" else 1)
    assert not (await tool._check_daily_loss(credentials, False))[0]


@pytest.mark.asyncio
async def test_rejected_live_limits_do_not_reach_signer(monkeypatch):
    from core.wallet.policy import PolicyGate
    from polyrob_markets.hyperliquid.service import PlaceLimitOrderParams
    from .test_hl_policy_gate_wiring import _tool
    for flag in ("CRYPTO_TRADE_LIVE_ENABLED", "HYPERLIQUID_TRADING_ENABLED"):
        monkeypatch.setenv(flag, "true")
    monkeypatch.setenv("HYPERLIQUID_TRADE_MAX_USD", "1000")
    tool, exchange = _tool(monkeypatch, PolicyGate(max_per_tx_usd=1000))
    tool.get_orderbook.return_value["asks"][0]["price"] = 200
    result = await tool.place_limit_order(PlaceLimitOrderParams(max_usd=1000, coin="ETH", is_buy=True, size=.1, price=100))
    assert not result["success"] and "spread" in result["error"]
    assert not exchange.calls


@pytest.mark.asyncio
async def test_polymarket_positions_read_more_than_first_page(monkeypatch):
    from polyrob_markets.polymarket.service import GetPositionsParams
    from .test_pm_data_api_reads import _tool
    tool = _tool(monkeypatch, {})
    row = {"conditionId": "m1", "size": 1, "curPrice": .5}
    pages = [[row] * 500, [row]]
    offsets = []
    async def get(url, params):
        offsets.append(params["offset"])
        return types.SimpleNamespace(raise_for_status=Mock(), json=lambda: pages.pop(0))
    tool._http_client.get = get
    result = await tool.get_all_positions(GetPositionsParams())
    assert result["success"] and len(result["positions"]) == 501
    assert offsets == [0, 500]
