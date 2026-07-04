"""T8 + v2 reconciliation — Polymarket portfolio reads use the PUBLIC Data API
(no wallet/signing), since py-clob-client-v2 dropped client.get_positions().
"""
import types
import pytest

import tools.polymarket.service as svc
from tools.polymarket.service import PolymarketTool, GetPositionsParams, GetTradeHistoryParams

ADDR = "0x000000000000000000000000000000000000aaaa"


def _async(value):
    async def _coro(*a, **k):
        return value
    return _coro()


def _creds():
    return types.SimpleNamespace(funder_address=ADDR, wallet_address=ADDR, demo_mode=False, enabled=True)


def _tool(monkeypatch, captured):
    tool = PolymarketTool(config=types.SimpleNamespace(), container=None)
    tool._user_id = "u1"
    monkeypatch.setattr(tool, "ensure_initialized", lambda: _async(None))
    monkeypatch.setattr(tool, "rate_limit", lambda *a, **k: _async(None))
    monkeypatch.setattr(tool, "_get_user_credentials", lambda: _async(_creds()))

    # Reads must NOT build an authenticated CLOB client (no signing/wallet).
    def _forbid(*a, **k):
        raise AssertionError("portfolio reads must not call _get_authenticated_client")
    monkeypatch.setattr(tool, "_get_authenticated_client", _forbid)

    class _Resp:
        def raise_for_status(self): return None
        def json(self): return []

    async def _get(url, params=None, **kw):
        captured["url"] = url
        captured["params"] = params or {}
        return _Resp()
    tool._http_client = types.SimpleNamespace(get=_get)
    return tool


@pytest.mark.asyncio
async def test_get_all_positions_uses_public_data_api(monkeypatch):
    captured = {}
    tool = _tool(monkeypatch, captured)
    res = await tool.get_all_positions(GetPositionsParams())
    assert res["success"] is True
    assert "data-api.polymarket.com" in captured["url"]
    assert "positions" in captured["url"]
    assert captured["params"].get("user") == ADDR


@pytest.mark.asyncio
async def test_get_trade_history_uses_public_data_api(monkeypatch):
    captured = {}
    tool = _tool(monkeypatch, captured)
    res = await tool.get_trade_history(GetTradeHistoryParams(limit=10))
    assert res["success"] is True
    assert "data-api.polymarket.com" in captured["url"]
    assert "trades" in captured["url"]
