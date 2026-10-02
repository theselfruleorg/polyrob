"""CR-L02 (2026-09-23): the PolicyGate replay guard was inert for the EVM
verbs, because every idempotency key ended in ``uuid4()``. The key is now
derived from the intent that would be signed — chain, destination, calldata
hash, value, nonce — plus the turn.
"""
import pytest

from core.wallet import simulation
from tests.unit.tools.defi.test_trade_real_guard import (
    ROUTER, USDC, _Rail, _approve_sim, _genuine_ctx, _tool)
from tests.unit.tools.defi.test_trade_real_guard import _pinned_rpc  # noqa: F401  (autouse)
from tools.defi.trade_tool import ApproveParams, _intent_idem

TX = {"to": USDC, "data": "0x095ea7b3" + "00" * 64, "value": 0, "nonce": 7}


def test_the_key_is_stable_for_the_same_intent_in_the_same_turn():
    ctx = _genuine_ctx()
    assert _intent_idem("approve", "base", TX, ctx) == _intent_idem("approve", "base", dict(TX), ctx)


@pytest.mark.parametrize("change", [
    {"to": ROUTER}, {"data": "0x095ea7b3" + "11" * 64}, {"value": 1}, {"nonce": 8}])
def test_a_different_intent_gets_a_different_key(change):
    ctx = _genuine_ctx()
    assert _intent_idem("approve", "base", TX, ctx) != _intent_idem(
        "approve", "base", TX | change, ctx)


def test_a_different_turn_gets_a_different_key():
    a, b = _genuine_ctx(), _genuine_ctx()
    b.session_id = "another-session"
    assert _intent_idem("approve", "base", TX, a) != _intent_idem("approve", "base", TX, b)


@pytest.mark.asyncio
async def test_the_same_approve_twice_in_one_turn_is_refused_as_a_replay(monkeypatch):
    monkeypatch.setattr(simulation, "simulate", _approve_sim)
    tool, gate = _tool()
    params = ApproveParams(token=USDC, spender=ROUTER, amount=1.0,
                           max_spend_usd=2.0, dry_run=False)
    first = await tool.approve_token(params, execution_context=_genuine_ctx())
    assert first.error is None and "CONFIRMED" in (first.extracted_content or "")
    second = await tool.approve_token(params, execution_context=_genuine_ctx())
    text = second.extracted_content or ""
    assert "replay" in text and "NOT SENT" in text
    assert len(gate.audit_log) == 1
    _Rail.last = None
