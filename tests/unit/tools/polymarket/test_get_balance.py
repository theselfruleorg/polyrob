"""T7 — get_balance is implemented but was hidden: route it into the tool surface."""
import types
import pytest

import tools.polymarket.service as svc
from tools.polymarket.service import PolymarketTool


def _async(value):
    async def _coro(*a, **k):
        return value
    return _coro()


def test_get_balance_advertised_in_available_tools():
    tool = PolymarketTool(config=types.SimpleNamespace(), container=None)
    names = {t.get("name") for t in tool.get_available_tools()}
    assert "get_balance" in names


@pytest.mark.asyncio
async def test_get_balance_is_routable_not_unknown(monkeypatch):
    monkeypatch.setattr(svc, "CLOB_AVAILABLE", True)
    tool = PolymarketTool(config=types.SimpleNamespace(), container=None)
    tool._user_id = "u1"
    tool.db = None
    monkeypatch.setattr(tool, "ensure_initialized", lambda: _async(None))
    monkeypatch.setattr(tool, "rate_limit", lambda *a, **k: _async(None))
    # Force the auth path to a clean handled error so we only assert ROUTING.
    monkeypatch.setattr(tool, "_get_authenticated_client", lambda: _async((None, "no creds")))

    res = await tool.execute_action("get_balance", {})
    # Must route to the method (handled error), not fall through to "Unknown tool".
    text = str(getattr(res, "error", "") or res)
    assert "Unknown tool" not in text
