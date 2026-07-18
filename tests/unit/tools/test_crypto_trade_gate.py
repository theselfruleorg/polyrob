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


def test_boundary_amount_equal_to_cap_is_allowed(monkeypatch):
    # Lock the documented inclusive-at-cap semantics (== cap allowed, > cap blocked).
    _env(monkeypatch, CRYPTO_TRADE_LIVE_ENABLED="true",
         HYPERLIQUID_TRADING_ENABLED="true", HYPERLIQUID_TRADE_MAX_USD="5")
    assert evaluate_live_trade("hyperliquid", 5.0).live is True
    assert evaluate_live_trade("hyperliquid", 5.0001).live is False


def test_owner_kill_switch_blocks_live_trade(monkeypatch):
    """H5: the owner kill-switch (autonomy_halted) must block LIVE trades, not only
    x402 payments — otherwise a runaway/compromised session keeps placing orders
    within caps until the daily cap is exhausted."""
    _env(monkeypatch, CRYPTO_TRADE_LIVE_ENABLED="true",
         HYPERLIQUID_TRADING_ENABLED="true", HYPERLIQUID_TRADE_MAX_USD="50",
         AUTONOMY_HALT="1")
    d = evaluate_live_trade("hyperliquid", 10.0)
    assert d.live is False
    assert "halt" in d.reason.lower()


def test_unpriceable_amount_fails_closed(monkeypatch):
    """M10: an order the caller couldn't price (amount_usd is None) must NOT submit —
    the cap can't be checked, so the gate must dry-run, not arm live."""
    _env(monkeypatch, CRYPTO_TRADE_LIVE_ENABLED="true",
         HYPERLIQUID_TRADING_ENABLED="true", HYPERLIQUID_TRADE_MAX_USD="50")
    assert evaluate_live_trade("hyperliquid", None).live is False


def test_non_finite_cap_does_not_disable_the_cap(monkeypatch):
    """M10: a non-finite cap env (nan/inf) made `amount > cap` always False, silently
    voiding the per-trade cap. A garbage cap must clamp to the safe default."""
    _env(monkeypatch, CRYPTO_TRADE_LIVE_ENABLED="true",
         HYPERLIQUID_TRADING_ENABLED="true", HYPERLIQUID_TRADE_MAX_USD="nan")
    assert evaluate_live_trade("hyperliquid", 1_000_000.0).live is False


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


@pytest.mark.asyncio
async def test_polymarket_place_limit_order_refused_for_forged_turn(monkeypatch):
    """H11: a forged/leaf/autonomous turn can never place a live order (parity with
    hyperliquid + x402). Refused before any client/CLOB path."""
    import types
    from tools.polymarket.service import PolymarketTool, PlaceLimitOrderParams
    from tools.controller.execution_context import ActionExecutionContext

    async def _coro(*a, **k):
        return None
    tool = PolymarketTool(config=types.SimpleNamespace(), container=None)
    tool._user_id = "u1"
    tool.db = None
    monkeypatch.setattr(tool, "ensure_initialized", lambda: _coro())
    ctx = ActionExecutionContext()
    ctx.role = "leaf"
    res = await tool.place_limit_order(
        PlaceLimitOrderParams(market_id="m1", token_id="t1", side="buy", price=0.5, size_usd=5.0),
        execution_context=ctx)
    assert res["success"] is False
    assert res.get("forged_turn_blocked") is True


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


# ---- H11: trade_turn_refusal — the shared forged/kill-switch trade gate --------------
# (two independent bars: the owner kill-switch OR a forged/autonomous turn; fail-closed.)

def test_trade_turn_refusal_none_context_allows(monkeypatch):
    # A direct/programmatic/CLI call (no execution_context) is NOT forged; with no halt
    # it proceeds (the flag + cap gates still apply downstream).
    monkeypatch.delenv("AUTONOMY_HALT", raising=False)
    from tools.crypto_trade_gate import trade_turn_refusal
    assert trade_turn_refusal(None, object()) is None


def test_trade_turn_refusal_forged_leaf(monkeypatch):
    # A leaf/delegated/forged turn can never place or mutate an order.
    monkeypatch.delenv("AUTONOMY_HALT", raising=False)
    from tools.crypto_trade_gate import trade_turn_refusal
    from tools.controller.execution_context import ActionExecutionContext
    ctx = ActionExecutionContext()  # role defaults to "leaf" == forged
    r = trade_turn_refusal(ctx, object())
    assert r and ("forged" in r.lower() or "owner must drive" in r.lower())


def test_trade_turn_refusal_genuine_owner_allows(monkeypatch):
    # A genuine owner turn (role=orchestrator, not a sub-agent, no forged turn_kind) with
    # no halt is allowed — the gate must not block real owner-driven trading.
    monkeypatch.delenv("AUTONOMY_HALT", raising=False)
    from tools.crypto_trade_gate import trade_turn_refusal
    from tools.controller.execution_context import ActionExecutionContext
    ctx = ActionExecutionContext(role="orchestrator")
    assert trade_turn_refusal(ctx, object()) is None


def test_trade_turn_refusal_halt_applies_even_without_context(monkeypatch):
    # The kill-switch halts ALL trading regardless of turn origin (parity w/ x402_fetch),
    # so a None context is refused too when halted.
    monkeypatch.setenv("AUTONOMY_HALT", "1")
    from tools.crypto_trade_gate import trade_turn_refusal
    r = trade_turn_refusal(None, object())
    assert r and "halt" in r.lower()


def test_trade_turn_refusal_halt_overrides_genuine_owner(monkeypatch):
    # Even a genuine owner turn is refused while halted.
    monkeypatch.setenv("AUTONOMY_HALT", "1")
    from tools.crypto_trade_gate import trade_turn_refusal
    from tools.controller.execution_context import ActionExecutionContext
    r = trade_turn_refusal(ActionExecutionContext(role="orchestrator"), object())
    assert r and "halt" in r.lower()


def test_trade_turn_refusal_halt_probe_fails_closed(monkeypatch):
    # A kill-switch probe that RAISES must refuse (never silently proceed on a money path).
    import agents.task.constants as constants

    def _boom():
        raise RuntimeError("halt probe down")
    monkeypatch.setattr(constants.AutonomyConfig, "autonomy_halted", staticmethod(_boom))
    from tools.crypto_trade_gate import trade_turn_refusal
    r = trade_turn_refusal(None, object())
    assert r and "closed" in r.lower()


def test_trade_turn_refusal_forged_probe_fails_closed(monkeypatch):
    # A forged-turn probe that RAISES must refuse (can't prove the turn is genuine).
    monkeypatch.delenv("AUTONOMY_HALT", raising=False)
    import tools.controller.action_registration as ar

    def _boom(_ctx, _s):
        raise RuntimeError("forged probe down")
    monkeypatch.setattr(ar, "_is_forged_or_autonomous_turn", _boom)
    from tools.crypto_trade_gate import trade_turn_refusal
    from tools.controller.execution_context import ActionExecutionContext
    r = trade_turn_refusal(ActionExecutionContext(role="orchestrator"), object())
    assert r and ("forged" in r.lower() or "owner must drive" in r.lower())
