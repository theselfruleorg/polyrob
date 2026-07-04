"""T3/T4 — additive read/trade split: polymarket_data / hyperliquid_data are
wallet-free read tools that expose ONLY read actions; the trade tools are unchanged.
"""
import types
import pytest

from tools.polymarket.service import (
    PolymarketTool, PolymarketDataTool, PM_READ_ACTIONS,
)
from tools.hyperliquid.service import (
    HyperliquidTool, HyperliquidDataTool, HL_READ_ACTIONS,
)

PM_TRADE = {"place_limit_order", "place_market_order", "cancel_order",
            "cancel_all_orders", "get_balance"}
HL_TRADE = {"place_limit_order", "place_market_order", "cancel_order",
            "cancel_all_orders", "update_leverage"}


def _async(v):
    async def _c(*a, **k): return v
    return _c()


def _mk(cls):
    # get_actions() walks dir(self) and touches the `container` property; give it a
    # stub so it doesn't reach for the DI singleton (which needs a config).
    tool = cls(config=types.SimpleNamespace(), container=None)
    tool._container = types.SimpleNamespace()
    return tool


# ---- registration chokepoint: get_actions() only exposes reads on data tools ----

def test_polymarket_data_get_actions_excludes_trading():
    tool = _mk(PolymarketDataTool)
    actions = set(tool.get_actions().keys())
    assert actions <= PM_READ_ACTIONS
    assert not (actions & PM_TRADE)
    assert "search_markets" in actions


def test_hyperliquid_data_get_actions_excludes_trading():
    tool = _mk(HyperliquidDataTool)
    actions = set(tool.get_actions().keys())
    assert actions <= HL_READ_ACTIONS
    assert not (actions & HL_TRADE)
    assert "get_funding_rate" in actions


def test_data_tools_have_distinct_ids():
    assert _mk(PolymarketDataTool).tool_id == "polymarket_data"
    assert _mk(HyperliquidDataTool).tool_id == "hyperliquid_data"


def test_available_tools_only_reads_on_data_tool():
    tool = _mk(PolymarketDataTool)
    names = {t.get("name") for t in tool.get_available_tools()}
    assert not (names & PM_TRADE)


# ---- defense in depth: a trade action is refused even if invoked directly ----

@pytest.mark.asyncio
async def test_polymarket_data_refuses_trade_action(monkeypatch):
    tool = _mk(PolymarketDataTool)
    monkeypatch.setattr(tool, "ensure_initialized", lambda: _async(None))
    res = await tool.execute_action("place_limit_order", {
        "market_id": "m", "token_id": "t", "side": "BUY", "price": 0.5, "size_usd": 5})
    err = str(getattr(res, "error", "") or res).lower()
    assert "read-only" in err or "not available" in err or "trade" in err


@pytest.mark.asyncio
async def test_hyperliquid_data_refuses_trade_action(monkeypatch):
    tool = _mk(HyperliquidDataTool)
    monkeypatch.setattr(tool, "ensure_initialized", lambda: _async(None))
    res = await tool.execute_action("place_market_order", {
        "coin": "BTC", "is_buy": True, "size": 0.1})
    err = str(getattr(res, "error", "") or res).lower()
    assert "read-only" in err or "not available" in err or "trade" in err


# ---- trade tools unchanged (still expose their full surface) ----

def test_trade_tools_retain_trading():
    pm = set(_mk(PolymarketTool).get_actions().keys())
    hl = set(_mk(HyperliquidTool).get_actions().keys())
    assert PM_TRADE <= pm
    assert HL_TRADE <= hl
