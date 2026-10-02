"""approve_token(via='permit2'): one exact, expiring Permit2 grant to the pinned
v4 PositionManager (core handoff W6; 048 phase 3)."""
import time
from types import SimpleNamespace

import pytest

from core.wallet import abi, dex_registry, liquidity_guard
from core.wallet.tx_guard import Decision
from tools.defi import lp_v4_verbs
from tools.defi.trade_tool import ApproveParams, DefiTradeTool
from tests.unit.tools.defi.test_deploy_and_call_verbs import _Gate, _Rail, _Wallet

PNL = "0xbBa60AB93Fc409b1A34371CBF6c3173795Ed2c7e"
ROW = dex_registry.row_for("robinhood", "v4")


@pytest.fixture
def tool(monkeypatch):
    captured = []

    def guard(intent, tx, **kw):
        captured.append((intent, tx))
        return Decision(True, "test", "autonomous", 1.0, 60000)
    monkeypatch.setattr(dex_registry, "verify_pins", lambda *a: None)
    import core.wallet.tokens as tokens
    monkeypatch.setattr(tokens, "get_token_identity",
                        lambda chain, t: SimpleNamespace(decimals=18, symbol="PNL"))
    t = DefiTradeTool(wallet=_Wallet(_Gate()), rail_factory=_Rail, guard_fn=guard,
                      price_fn=lambda *a: 1e-5, fallback_price_fn=lambda *a: None)
    t._notify_tx = lambda *a, **kw: None
    return t, captured


def _params(**kw):
    return ApproveParams(**(dict(chain="robinhood", token=PNL, spender=ROW.position_manager,
                                 amount=1000.0, max_spend_usd=5.0, via="permit2") | kw))


@pytest.mark.asyncio
async def test_permit2_grant_is_the_exact_expiring_approve(tool):
    t, captured = tool
    res = await t.approve_token(_params())
    assert res.error is None, res.error
    intent, tx = captured[0]
    assert tx["to"] == ROW.permit2
    assert tx["data"].startswith(abi.selector("approve(address,address,uint160,uint48)"))
    assert intent.is_allowance_op and intent.to == ROW.position_manager
    assert intent.expected_allowance_grants == ((PNL, ROW.position_manager, 1000 * 10 ** 18),)
    _tok, _sp, _amt, exp = abi.decode(
        [{"type": "address"}, {"type": "address"}, {"type": "uint160"}, {"type": "uint48"}],
        "0x" + tx["data"][10:])
    assert time.time() < exp <= time.time() + lp_v4_verbs.PERMIT2_GRANT_TTL_S + 5
    # and the guard's rule recognises exactly this grant
    assert liquidity_guard.permit2_grant_declared(
        intent, tx, ROW.permit2, PNL, ROW.position_manager, 1000 * 10 ** 18)


@pytest.mark.asyncio
async def test_permit2_grant_to_any_other_spender_refuses(tool):
    t, captured = tool
    res = await t.approve_token(_params(spender="0x" + "3" * 40))
    assert "only the pinned v4" in res.error and not captured


@pytest.mark.asyncio
async def test_permit2_on_a_chain_without_a_pinned_v4_refuses(tool):
    t, captured = tool
    res = await t.approve_token(_params(chain="solana"))
    assert res.error and not captured
