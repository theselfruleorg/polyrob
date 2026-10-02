"""CR-H02 (2026-09-23): an approval to a spender no route pins.

Approvals booked $0 to the rolling cap, including grants to non-router
spenders, so N grants each under the ceiling cleared an unbounded aggregate.
Now a grant to a non-pinned spender (1) is refused outside a genuine owner
turn and (2) charges its USD to the ledger when it does land.
"""
from types import SimpleNamespace

import pytest

from tests.unit.tools.defi.test_trade_t4 import ROUTER, USDC, _Rail, _tool
from tools.defi.trade_tool import ApproveParams

ATTACKER = "0x1111111111111111111111111111111111111111"


@pytest.fixture(autouse=True)
def _reset_rail():
    # _Rail.last is class-level state shared with test_trade_t4; leave it clean.
    yield
    _Rail.last = None


def _approve(spender, **kw):
    return ApproveParams(token=USDC, spender=spender, amount=1.0,
                         max_spend_usd=2.0, dry_run=False, **kw)


@pytest.mark.asyncio
async def test_owner_grant_to_a_non_pinned_spender_is_charged_to_the_cap():
    tool, gate = _tool()
    res = await tool.approve_token(_approve(ATTACKER))
    assert res.error is None, res.error
    assert gate.recorded and gate.recorded[0]["amount_usd"] == 1.0


@pytest.mark.asyncio
async def test_grant_to_the_pinned_router_still_books_zero():
    tool, gate = _tool()
    res = await tool.approve_token(_approve(ROUTER))
    assert res.error is None, res.error
    assert gate.recorded[0]["amount_usd"] == 0.0


@pytest.mark.asyncio
@pytest.mark.parametrize("ctx", [
    SimpleNamespace(role="leaf", is_sub_agent=False, metadata={}, user_id="u"),
    SimpleNamespace(role="orchestrator", is_sub_agent=True, metadata={}, user_id="u"),
    SimpleNamespace(role="orchestrator", is_sub_agent=False,
                    metadata={"turn_kind": "self_wake"}, user_id="u"),
])
async def test_non_owner_turn_cannot_grant_to_a_non_pinned_spender(ctx, monkeypatch):
    import core.wallet.authority as auth
    monkeypatch.setattr(auth, "turn_refusal", lambda c: None)
    captured = []
    tool, gate = _tool(captured=captured)
    res = await tool.approve_token(_approve(ATTACKER), ctx)
    assert res.error and "pins" in res.error
    assert not captured and not gate.recorded


@pytest.mark.asyncio
async def test_autonomous_goal_turn_cannot_grant_to_a_non_pinned_spender(monkeypatch):
    import core.wallet.authority as auth
    import tools.controller.action_registration as ar
    monkeypatch.setattr(auth, "turn_refusal", lambda c: None)
    monkeypatch.setattr(ar, "_is_forged_or_autonomous_turn", lambda c, s: True)
    tool, gate = _tool()
    ctx = SimpleNamespace(role="orchestrator", is_sub_agent=False, metadata={})
    res = await tool.approve_token(_approve(ATTACKER), ctx)
    assert res.error and "autonomous" in res.error
    assert not gate.recorded


@pytest.mark.asyncio
async def test_genuine_owner_turn_may_grant_and_is_charged(monkeypatch):
    import core.wallet.authority as auth
    import tools.controller.action_registration as ar
    monkeypatch.setattr(auth, "turn_refusal", lambda c: None)
    monkeypatch.setattr(ar, "_is_forged_or_autonomous_turn", lambda c, s: False)
    tool, gate = _tool()
    ctx = SimpleNamespace(role="orchestrator", is_sub_agent=False, metadata={})
    res = await tool.approve_token(_approve(ATTACKER), ctx)
    assert res.error is None, res.error
    assert gate.recorded[0]["amount_usd"] == 1.0


@pytest.mark.asyncio
async def test_probe_failure_refuses(monkeypatch):
    import tools.controller.action_registration as ar
    import core.wallet.authority as auth
    monkeypatch.setattr(auth, "turn_refusal", lambda c: None)

    def boom(c, s):
        raise RuntimeError("probe down")
    monkeypatch.setattr(ar, "_is_forged_or_autonomous_turn", boom)
    tool, gate = _tool()
    res = await tool.approve_token(_approve(ATTACKER), SimpleNamespace(role="orchestrator"))
    assert res.error and "could not be proven" in res.error
    assert not gate.recorded
