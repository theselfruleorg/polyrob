"""086 W6 — the swap verbs: plan on a mandate entry, close_reason on a sell.

Pins:
 - a mandate-run ENTRY with no plan is refused BEFORE any route is asked (EVM and
   Solana — on Solana before the wallet is even read);
 - a mandate-run EXIT needs no plan;
 - ``close_reason`` rides the position context to the book (EVM);
 - a live entry with a plan records it after the guarded run returns; a dry run
   records nothing.
"""
from types import SimpleNamespace

import pytest

from tools.defi.trade_params import SolanaSwapParams, SwapParams
from tools.defi.trade_tool import DefiTradeTool

USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
WETH = "0x4200000000000000000000000000000000000006"
MEME = "0xb200000000000000000000ea8625786a776539fb"
PLAN = {"thesis": "volume breakout", "confidence": 0.6,
        "invalidation": "loses the 1h low", "preset": "memecoin"}


def _ctx(mandate="m1"):
    return SimpleNamespace(metadata={"mandate": mandate}, user_id="u1")


class _Wallet:
    policy = None

    def operational_signer(self):
        return SimpleNamespace(address="0x2222222222222222222222222222222222222222")


def _tool(routes):
    def _route_fn(chain, ti, to_, amt, *, holder, slippage_bps):
        routes.append((ti, to_))
        return None, "stub: no route"
    return DefiTradeTool(wallet=_Wallet(), route_fn=_route_fn,
                         balance_fn=lambda c, h, t: None, price_fn=lambda c, a: None)


@pytest.fixture(autouse=True)
def _no_screen(monkeypatch):
    import tools.defi.buy_screen as bs
    import tools.defi.swap_verb as sv

    async def _ok(*a, **k):
        return None
    monkeypatch.setattr(bs, "evm_buy_refusal", _ok)
    monkeypatch.setattr(sv, "_owner_pinned", lambda *a: False)
    import core.wallet.tokens as tk
    real = tk.get_token_identity
    monkeypatch.setattr(tk, "get_token_identity", lambda c, a: SimpleNamespace(
        decimals=18, symbol="MEME" if a.lower() == MEME else None, address=a)
        if a.lower() == MEME else real(c, a))


@pytest.mark.asyncio
async def test_evm_mandate_entry_without_plan_refused_before_route():
    routes = []
    res = await _tool(routes).swap(SwapParams(chain="base", token_in=USDC, token_out=MEME, amount_in=1.0,
                                              max_spend_usd=2.0), execution_context=_ctx())
    assert res.error and "plan" in res.error and "m1" in res.error
    assert routes == []


@pytest.mark.asyncio
async def test_evm_mandate_exit_needs_no_plan():
    routes = []
    res = await _tool(routes).swap(SwapParams(chain="base", token_in=WETH, token_out=USDC, amount_in=1.0,
                                              max_spend_usd=2.0), execution_context=_ctx())
    assert "plan" not in (res.error or "")
    assert routes, "an exit reaches the route"


@pytest.fixture
def _active_mandate(tmp_path, monkeypatch):
    """The solana guard harness's paused-mandate fixture, with the mandate ACTIVE."""
    import importlib
    monkeypatch.setattr(importlib.import_module("core.config_policy.money_regime"),
                        "unattended_money_allowed", lambda: True)
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "owner")
    monkeypatch.setenv("SOLANA_TRADE_ENABLED", "true")
    for k in ("DEFI_AUTONOMOUS_TURN_TRADING", "DEFI_MONITOR_EXITS", "DEFI_SOLANA_RPC"):
        monkeypatch.delenv(k, raising=False)
    from core import trading_mandate as tm
    from core.money import mandate_gate as mg
    from core.wallet import tx_guard as _tg
    import core.wallet.authority as wa
    import tools.controller.action_registration as ar
    import tools.controller.turn_origin as to
    mpath, book = str(tmp_path / "trading.db"), str(tmp_path / "open_positions.db")
    real = mg.entry_refusal
    monkeypatch.setattr(mg, "entry_refusal",
                        lambda *a, **k: real(*a, **{**k, "path": mpath, "book_path": book}))
    monkeypatch.setattr(_tg, "_autonomous_turn_allowed", lambda ctx, ts, fn: True)
    monkeypatch.setattr(ar, "_is_forged_or_autonomous_turn", lambda c, t: True)
    monkeypatch.setattr(to, "_is_forged_or_autonomous_turn", lambda c, t: True)
    monkeypatch.setattr(wa, "turn_refusal", lambda ctx: None)
    m = tm.create("owner", actor=tm.OWNER_SEAT, budget_usd=10, chains=["solana"], path=mpath)
    return SimpleNamespace(user_id="owner", session_id="s", role="orchestrator",
                           is_sub_agent=False, metadata={"mandate": m.id})


@pytest.mark.asyncio
async def test_solana_mandate_entry_without_plan_refused_before_quote(_active_mandate):
    from tests.unit.tools.defi.test_solana_swap_guard import MEME as SOL_MEME, _params
    from tests.unit.tools.defi.test_solana_swap_guard import _tool as sol_tool
    tool = sol_tool(solana_quote_fn=lambda *a, **k: pytest.fail("quoted without a plan"))
    res = await tool.solana_swap(_params(token_out=SOL_MEME, max_spend_usd=1.0),
                                 execution_context=_active_mandate)
    assert res.error and "plan" in res.error


def _t4_tool(monkeypatch, seen):
    """The T4 harness (a real route over a stubbed pool) with the guarded run
    replaced by a recorder: what the verb hands on is the contract."""
    import tools.defi.providers.univ3 as u
    from tests.unit.tools.defi.test_trade_t4 import _quote, _tool as t4_tool
    monkeypatch.setattr(u, "read_allowance", lambda *a, **k: 10 ** 30)
    tool, _ = t4_tool(quote=_quote())

    async def _run_guarded(**kw):
        seen.update(kw)
        return tool._ar(content="RESULT: CONFIRMED")
    tool._run_guarded = _run_guarded
    return tool


@pytest.mark.asyncio
@pytest.mark.parametrize("dry_run,expect", [(False, 1), (True, 0)])
async def test_plan_recorded_after_a_live_entry_only(monkeypatch, dry_run, expect):
    import core.position_plans as pp
    seen, recorded = {}, []
    monkeypatch.setattr(pp, "record_after_swap", lambda ec, **kw: recorded.append(kw))
    tool = _t4_tool(monkeypatch, seen)
    res = await tool.swap(SwapParams(chain="base", token_in=USDC, token_out=WETH, amount_in=1.0,
                                     max_spend_usd=2.0, plan=PLAN, dry_run=dry_run),
                          execution_context=_ctx())
    assert not res.error, res.error
    assert len(recorded) == expect
    if expect:
        assert recorded[0]["plan"].thesis == "volume breakout"
        assert recorded[0]["address"].lower() == WETH.lower()


@pytest.mark.asyncio
async def test_close_reason_rides_the_position_context(monkeypatch):
    seen = {}
    tool = _t4_tool(monkeypatch, seen)
    res = await tool.swap(SwapParams(chain="base", token_in=USDC, token_out=WETH, amount_in=1.0,
                                     max_spend_usd=2.0, close_reason="take_profit"),
                          execution_context=None)
    assert not res.error, res.error
    assert seen["position_ctx"]["close_reason"] == "take_profit"


def test_close_reason_is_typed():
    with pytest.raises(Exception):
        SwapParams(chain="base", token_in=USDC, token_out=WETH, amount_in=1.0, max_spend_usd=2.0,
                   close_reason="because")
