"""Complete live snapshots for order submission tests; no signing or network calls."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock


def polymarket(tool, credentials, monkeypatch, *, market_id="m1", token_id="t1"):
    from polyrob_markets.polymarket.store_models import TradingLimits
    credentials.trading_limits = TradingLimits(**vars(credentials.trading_limits))
    monkeypatch.setattr(tool, "get_market_details", AsyncMock(return_value={"success": True, "market": {
        "condition_id": market_id, "slug": market_id, "category": "politics",
        "active": True, "closed": False, "outcomes": [{"token_id": token_id}],
    }}))
    monkeypatch.setattr(tool, "get_orderbook", AsyncMock(return_value={"success": True,
        "bids": [{"price": .5, "size": 100000}], "asks": [{"price": .51, "size": 100000}]}))
    monkeypatch.setattr(tool, "get_all_positions", AsyncMock(return_value={"success": True, "positions": []}))
    monkeypatch.setattr(tool, "get_open_orders", AsyncMock(return_value={"success": True, "orders": []}))


def hyperliquid(tool, credentials, monkeypatch):
    from polyrob_markets.hyperliquid.store_models import TradingLimits
    credentials.trading_limits = TradingLimits(**vars(credentials.trading_limits))
    credentials.api_url = "https://api.hyperliquid.xyz"
    credentials.wallet_address = "0x" + "a" * 40
    monkeypatch.setattr(tool, "_resolve_query_address", lambda _: credentials.wallet_address)
    monkeypatch.setattr(tool, "get_account_state", AsyncMock(return_value={"success": True,
        "total_ntl_pos": 0, "positions": []}))
    monkeypatch.setattr(tool, "get_open_orders", AsyncMock(return_value={"success": True, "orders": []}))
    monkeypatch.setattr(tool, "get_fills", AsyncMock(return_value={"success": True, "fills": []}))
    monkeypatch.setattr(tool, "get_orderbook", AsyncMock(return_value={"success": True,
        "bids": [{"price": 100, "size": 1000}], "asks": [{"price": 100.1, "size": 1000}]}))
    tool._http_client = SimpleNamespace(post=AsyncMock(return_value=SimpleNamespace(
        raise_for_status=Mock(), json=lambda: {"coin": "ETH", "user": credentials.wallet_address,
                                             "leverage": {"value": 2}})))
