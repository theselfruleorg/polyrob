"""T11 — live-trade kill-switch. Real orders submit ONLY when the master switch +
the per-venue switch are on AND size <= the per-venue live cap; otherwise dry-run.
"""
import pytest
from tools.crypto_trade_gate import evaluate_live_trade, evaluate_live_mutation


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
    # Availability lives on the (lazily-loading) adapter now, not on service.py.
    monkeypatch.setattr("tools.polymarket.clob_adapter.CLOB_AVAILABLE", True)

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
    ctx = ActionExecutionContext(user_id="local", role="orchestrator")
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
    r = trade_turn_refusal(ActionExecutionContext(user_id="local", role="orchestrator"), object())
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
    r = trade_turn_refusal(ActionExecutionContext(user_id="local", role="orchestrator"), object())
    assert r and ("forged" in r.lower() or "owner must drive" in r.lower())


# ---- M10 (2026-08-22): evaluate_live_mutation — cancel/leverage kill-switch --------
# (cancel_*/update_leverage previously reached the venue whenever
# credentials.can_trade() was true, even with CRYPTO_TRADE_LIVE_ENABLED off.)

def test_cancel_is_blocked_when_live_trading_is_off(monkeypatch):
    monkeypatch.delenv("CRYPTO_TRADE_LIVE_ENABLED", raising=False)
    d = evaluate_live_mutation("hyperliquid", risk_reducing=False)
    assert d.live is False


def test_cancel_is_ALLOWED_while_halted_because_it_reduces_risk(monkeypatch):
    """A halted OWNER with an open position must still be able to close it by hand.
    Blocking a cancel strands the position — the kill-switch is meant to stop new
    risk, not to trap existing risk. NOTE (R16): this exercises evaluate_live_mutation
    in isolation, which has no execution_context and cannot itself distinguish an
    owner-direct call from an agent-loop turn — that OWNER-only narrowing is
    trade_turn_refusal's job (see the trade_turn_refusal tests below); callers must
    run both gates together, in order, as every real call site does."""
    monkeypatch.setenv("CRYPTO_TRADE_LIVE_ENABLED", "true")
    monkeypatch.setenv("HYPERLIQUID_TRADING_ENABLED", "true")
    monkeypatch.setattr(
        "core.config_policy.AutonomyConfig.autonomy_halted", staticmethod(lambda: True))
    assert evaluate_live_mutation("hyperliquid", risk_reducing=True).live is True


def test_update_leverage_is_NOT_risk_reducing_and_is_blocked_while_halted(monkeypatch):
    monkeypatch.setenv("CRYPTO_TRADE_LIVE_ENABLED", "true")
    monkeypatch.setenv("HYPERLIQUID_TRADING_ENABLED", "true")
    monkeypatch.setattr(
        "core.config_policy.AutonomyConfig.autonomy_halted", staticmethod(lambda: True))
    assert evaluate_live_mutation("hyperliquid", risk_reducing=False).live is False


def test_a_halt_probe_that_raises_blocks_a_non_reducing_mutation(monkeypatch):
    monkeypatch.setenv("CRYPTO_TRADE_LIVE_ENABLED", "true")
    monkeypatch.setenv("HYPERLIQUID_TRADING_ENABLED", "true")

    def _boom():
        raise RuntimeError("probe")
    monkeypatch.setattr(
        "core.config_policy.AutonomyConfig.autonomy_halted", staticmethod(_boom))
    assert evaluate_live_mutation("hyperliquid", risk_reducing=False).live is False


def test_mutation_master_off_blocks_even_if_venue_on(monkeypatch):
    monkeypatch.setenv("CRYPTO_TRADE_LIVE_ENABLED", "off")
    monkeypatch.setenv("HYPERLIQUID_TRADING_ENABLED", "true")
    assert evaluate_live_mutation("hyperliquid", risk_reducing=True).live is False


def test_mutation_venue_off_blocks_even_if_master_on(monkeypatch):
    monkeypatch.setenv("CRYPTO_TRADE_LIVE_ENABLED", "true")
    monkeypatch.setenv("HYPERLIQUID_TRADING_ENABLED", "off")
    assert evaluate_live_mutation("hyperliquid", risk_reducing=True).live is False


def test_mutation_not_probed_at_all_when_risk_reducing_and_not_halted(monkeypatch):
    # A risk-reducing mutation is unconditionally allowed once the switches are on —
    # it never needs to consult the halt probe.
    monkeypatch.setenv("CRYPTO_TRADE_LIVE_ENABLED", "true")
    monkeypatch.setenv("POLYMARKET_TRADING_ENABLED", "true")
    d = evaluate_live_mutation("polymarket", risk_reducing=True)
    assert d.live is True


def test_mutation_polymarket_leverage_analog_blocked_while_halted(monkeypatch):
    # Polymarket has no leverage concept, but the same risk_reducing=False path
    # must behave identically across venues (venue-agnostic gate logic).
    monkeypatch.setenv("CRYPTO_TRADE_LIVE_ENABLED", "true")
    monkeypatch.setenv("POLYMARKET_TRADING_ENABLED", "true")
    monkeypatch.setattr(
        "core.config_policy.AutonomyConfig.autonomy_halted", staticmethod(lambda: True))
    assert evaluate_live_mutation("polymarket", risk_reducing=False).live is False


# ---- M10/R16: trade_turn_refusal's risk_reducing carve-out is OWNER-ONLY ----------
# (default risk_reducing=False keeps every existing call site — orders, leverage,
# approve/revoke_agent — byte-identical to before this change. And even with
# risk_reducing=True, the kill-switch bar only lifts for a literal owner-direct call
# — execution_context is None — never for an agent-loop turn, genuine or not.)

def test_trade_turn_refusal_default_still_blocks_during_halt(monkeypatch):
    monkeypatch.setenv("AUTONOMY_HALT", "1")
    from tools.crypto_trade_gate import trade_turn_refusal
    r = trade_turn_refusal(None, object())  # no risk_reducing kwarg -> default False
    assert r and "halt" in r.lower()


def test_trade_turn_refusal_owner_direct_risk_reducing_survives_halt(monkeypatch):
    # (a) halted + execution_context is None (the owner acting directly) + risk_reducing
    # => allowed. This is the ONLY combination the carve-out covers.
    monkeypatch.setenv("AUTONOMY_HALT", "1")
    from tools.crypto_trade_gate import trade_turn_refusal
    r = trade_turn_refusal(None, object(), risk_reducing=True)
    assert r is None  # not refused: no context (not forged) + risk-reducing skips bar 1


def test_trade_turn_refusal_genuine_agent_turn_risk_reducing_STILL_blocked_by_halt(monkeypatch):
    """R16 (2026-08-22): (b) halted + a genuine, NON-FORGED agent-loop turn
    (role="orchestrator", no forged turn_kind) + risk_reducing=True must still be
    REFUSED. This is the regression R16 closes: the review proved that without this
    narrowing, `not risk_reducing` alone let ANY non-forged origin — including a
    plain LLM-driven agent turn — bypass the kill-switch just because the verb
    happened to be a cancel, so a halted owner's agent kept mutating live orders.
    The carve-out is for a literal owner-direct call (execution_context is None)
    ONLY; presence of a context — genuine or not — means an agent-loop turn, and
    the kill-switch bar applies to it exactly as if risk_reducing were False."""
    monkeypatch.setenv("AUTONOMY_HALT", "1")
    from tools.crypto_trade_gate import trade_turn_refusal
    from tools.controller.execution_context import ActionExecutionContext
    ctx = ActionExecutionContext(user_id="local", role="orchestrator")  # genuine owner-turn, NOT forged
    r = trade_turn_refusal(ctx, object(), risk_reducing=True)
    assert r and "halt" in r.lower()


def test_trade_turn_refusal_risk_reducing_still_blocks_forged_turn(monkeypatch):
    # risk_reducing never weakens the forged-turn bar (bar 2), which is unconditional
    # regardless of execution_context or halt state.
    monkeypatch.delenv("AUTONOMY_HALT", raising=False)
    from tools.crypto_trade_gate import trade_turn_refusal
    from tools.controller.execution_context import ActionExecutionContext
    ctx = ActionExecutionContext()  # role defaults to "leaf" == forged
    r = trade_turn_refusal(ctx, object(), risk_reducing=True)
    assert r and ("forged" in r.lower() or "owner must drive" in r.lower())


@pytest.fixture(autouse=True)
def _wallet_owner_identity(monkeypatch):
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "local")
