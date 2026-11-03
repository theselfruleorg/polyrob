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

TKN = "0x5555555555555555555555555555555555555555"
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
                        lambda chain, t: SimpleNamespace(decimals=18, symbol="TKN"))
    t = DefiTradeTool(wallet=_Wallet(_Gate()), rail_factory=_Rail, guard_fn=guard,
                      price_fn=lambda *a: 1e-5, fallback_price_fn=lambda *a: None)
    t._notify_tx = lambda *a, **kw: None
    return t, captured


def _params(**kw):
    return ApproveParams(**(dict(chain="robinhood", token=TKN, spender=ROW.position_manager,
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
    assert intent.expected_allowance_grants == ((TKN, ROW.position_manager, 1000 * 10 ** 18),)
    _tok, _sp, _amt, exp = abi.decode(
        [{"type": "address"}, {"type": "address"}, {"type": "uint160"}, {"type": "uint48"}],
        "0x" + tx["data"][10:])
    assert time.time() < exp <= time.time() + lp_v4_verbs.PERMIT2_GRANT_TTL_S + 5
    # and the guard's rule recognises exactly this grant
    assert liquidity_guard.permit2_grant_declared(
        intent, tx, ROW.permit2, TKN, ROW.position_manager, 1000 * 10 ** 18)


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


def test_a_reverted_lp_add_leaves_the_lp_caps(monkeypatch):
    """098 review (TS-23): a reverted add moved nothing; it no longer fills
    LP_ETH_CAP / LP_ETH_DAILY_CAP."""
    import time as _t
    from types import SimpleNamespace
    from core.money.receipt_state import voided_refs
    from tools.defi.lp_v4_verbs import caps_refusal
    monkeypatch.setenv("LP_ETH_CAP", "1")
    monkeypatch.setenv("LP_ETH_DAILY_CAP", "1")
    monkeypatch.setenv("LP_ETH_FLOOR", "0")
    now = _t.time()
    audit = [{"action": "lp_add", "native_raw": str(10 ** 18), "ts": now, "result_ref": "0xa",
              "receipt_state": "pending_receipt"},
             {"action": "receipt", "result_ref": "0xa", "receipt_state": "reverted", "ts": now}]
    gate = SimpleNamespace(audit_log=audit, _voided=lambda: voided_refs(audit))
    assert caps_refusal(gate, 10 ** 17, 10 ** 19, now=now) is None
    gate_live = SimpleNamespace(audit_log=audit[:1], _voided=lambda: set())
    assert "LP_ETH_CAP" in caps_refusal(gate_live, 10 ** 17, 10 ** 19, now=now)
