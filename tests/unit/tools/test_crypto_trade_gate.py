"""T11 — live-trade kill-switch. Real orders submit ONLY when the master switch +
the per-venue switch are on AND size <= the per-venue live cap; otherwise dry-run.
"""
import pytest
from tools.crypto_trade_gate import evaluate_live_trade


def _env(monkeypatch, **kw):
    for k, v in kw.items():
        if v is None:
            monkeypatch.delenv(k, raising=False)
        else:
            monkeypatch.setenv(k, v)


def test_dry_run_by_default(monkeypatch):
    _env(monkeypatch, CRYPTO_TRADE_LIVE_ENABLED=None,
         POLYMARKET_TRADING_ENABLED=None, HYPERLIQUID_TRADING_ENABLED=None)
    assert evaluate_live_trade("polymarket", 1.0).live is False
    assert evaluate_live_trade("hyperliquid", 1.0).live is False


@pytest.mark.parametrize("disabled_value", ["none", "None", "NONE", "off", "false", "0", "no", ""])
def test_disabled_looking_master_value_does_not_arm_live(monkeypatch, disabled_value):
    # M1: a MONEY gate must read every disabled-looking value as OFF. The prior local
    # _bool_env omitted "none" from its falsey-set, so CRYPTO_TRADE_LIVE_ENABLED=none
    # armed live trading. Fold onto core/env.py's canonical falsey-set.
    _env(monkeypatch, CRYPTO_TRADE_LIVE_ENABLED=disabled_value,
         POLYMARKET_TRADING_ENABLED="true", POLYMARKET_TRADE_MAX_USD="50")
    assert evaluate_live_trade("polymarket", 1.0).live is False


@pytest.mark.parametrize("disabled_value", ["none", "off", "false", "0", "no", ""])
def test_disabled_looking_venue_value_does_not_arm_live(monkeypatch, disabled_value):
    _env(monkeypatch, CRYPTO_TRADE_LIVE_ENABLED="true",
         POLYMARKET_TRADING_ENABLED=disabled_value, POLYMARKET_TRADE_MAX_USD="50")
    assert evaluate_live_trade("polymarket", 1.0).live is False


def test_master_off_blocks_even_if_venue_on(monkeypatch):
    _env(monkeypatch, CRYPTO_TRADE_LIVE_ENABLED="off", POLYMARKET_TRADING_ENABLED="true")
    d = evaluate_live_trade("polymarket", 1.0)
    assert d.live is False
    assert "live" in d.reason.lower()


def test_venue_off_blocks_even_if_master_on(monkeypatch):
    _env(monkeypatch, CRYPTO_TRADE_LIVE_ENABLED="true",
         POLYMARKET_TRADING_ENABLED="off")
    assert evaluate_live_trade("polymarket", 1.0).live is False


def test_live_when_both_on_and_within_cap(monkeypatch):
    _env(monkeypatch, CRYPTO_TRADE_LIVE_ENABLED="true",
         HYPERLIQUID_TRADING_ENABLED="true", HYPERLIQUID_TRADE_MAX_USD="50")
    d = evaluate_live_trade("hyperliquid", 25.0)
    assert d.live is True


def test_over_cap_blocks(monkeypatch):
    _env(monkeypatch, CRYPTO_TRADE_LIVE_ENABLED="true",
         HYPERLIQUID_TRADING_ENABLED="true", HYPERLIQUID_TRADE_MAX_USD="10")
    d = evaluate_live_trade("hyperliquid", 100.0)
    assert d.live is False
    assert "cap" in d.reason.lower()


def test_default_cap_is_small(monkeypatch):
    # No explicit cap → a conservative default (<= $5) so a first live run is tiny.
    _env(monkeypatch, CRYPTO_TRADE_LIVE_ENABLED="true",
         POLYMARKET_TRADING_ENABLED="true", POLYMARKET_TRADE_MAX_USD=None)
    assert evaluate_live_trade("polymarket", 4.0).live is True
    assert evaluate_live_trade("polymarket", 6.0).live is False


# ---- integration: the trade method dry-runs by default (flags off) ----

@pytest.mark.asyncio
async def test_polymarket_place_limit_order_dry_runs_by_default(monkeypatch):
    import types
    import tools.polymarket.service as svc
    from tools.polymarket.service import PolymarketTool, PlaceLimitOrderParams

    for k in ("CRYPTO_TRADE_LIVE_ENABLED", "POLYMARKET_TRADING_ENABLED"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(svc, "CLOB_AVAILABLE", True)

    async def _coro(*a, **k):
        return None
    tool = PolymarketTool(config=types.SimpleNamespace(), container=None)
    tool._user_id = "u1"
    tool.db = None
    monkeypatch.setattr(tool, "ensure_initialized", lambda: _coro())
    monkeypatch.setattr(tool, "_get_user_credentials", lambda: _make_creds())

    def _forbid(*a, **k):
        raise AssertionError("must not reach the authenticated client in dry-run")
    monkeypatch.setattr(tool, "_get_authenticated_client", _forbid)

    res = await tool.place_limit_order(PlaceLimitOrderParams(
        market_id="m1", token_id="t1", side="buy", price=0.5, size_usd=5.0))
    assert res["success"] is False
    assert res.get("dry_run") is True


def _make_creds():
    import types
    from tools.polymarket.models import TradingLimits
    async def _c():
        return types.SimpleNamespace(
            demo_mode=False, enabled=True,
            # autonomous trading on so we get past _check_trading_limits to the T11 gate
            trading_limits=TradingLimits(enable_autonomous_trading=True),
        )
    return _c()
