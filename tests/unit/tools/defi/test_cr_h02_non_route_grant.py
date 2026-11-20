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
    return ApproveParams(chain="base", token=USDC, spender=spender, amount=1.0,
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


@pytest.mark.asyncio
async def test_the_owners_agent_turn_gets_the_exact_grant_as_one_card(monkeypatch):
    """126: the owner tenant on a turn that cannot grant (an agent or autonomous
    turn) gets ONE call card holding the exact grant; nothing is broadcast."""
    import core.wallet.authority as auth
    import tools.controller.action_registration as ar
    from core.surfaces import cards
    monkeypatch.setattr(auth, "turn_refusal", lambda c: None)
    monkeypatch.setattr(ar, "_is_forged_or_autonomous_turn", lambda c, s: True)
    monkeypatch.setattr("core.instance.is_owner_uid", lambda uid, **kw: uid == "owner")
    monkeypatch.setattr(cards, "_STORE", None)
    pushed = []

    async def push(container, user_id, card, session_id=None):
        pushed.append(card)
        return "sent"
    monkeypatch.setattr(cards, "push", push)
    tool, gate = _tool()
    ctx = SimpleNamespace(role="orchestrator", is_sub_agent=False, metadata={},
                          user_id="owner")
    res = await tool.approve_token(_approve(ATTACKER), ctx)
    assert res.error is None and res.metadata["owner_act"]["card_id"] == pushed[0].card_id
    step, = pushed[0].draft["call"]["steps"]
    assert step["do"] == "defi_trade_approve_token"
    assert step["args"]["spender"] == ATTACKER and step["args"]["dry_run"] is False
    assert "standing claim" in pushed[0].body and not gate.recorded


def _owner_card_world(monkeypatch):
    import core.wallet.authority as auth
    import tools.controller.action_registration as ar
    from core.surfaces import cards
    monkeypatch.setattr(auth, "turn_refusal", lambda c: None)
    monkeypatch.setattr(ar, "_is_forged_or_autonomous_turn", lambda c, s: True)
    monkeypatch.setattr("core.instance.is_owner_uid", lambda uid, **kw: uid == "owner")
    monkeypatch.setattr(cards, "_STORE", None)
    pushed = []

    async def push(container, user_id, card, session_id=None):
        pushed.append(card)
        return "sent"
    monkeypatch.setattr(cards, "push", push)
    return pushed


@pytest.mark.asyncio
async def test_an_autonomous_run_gets_the_flat_refusal_not_a_card(monkeypatch):
    """Audit 2026-11-21 L-1: a goal/cron run of the owner tenant asks no one-tap card for
    a standing claim to an unpinned spender; it gets the flat CR-H02 refusal."""
    from core import autonomy_marker
    pushed = _owner_card_world(monkeypatch)
    monkeypatch.setattr(autonomy_marker, "_SESSIONS", type(autonomy_marker._SESSIONS)())
    autonomy_marker.mark_autonomous("goal-run-1", "g_1")
    tool, gate = _tool()
    ctx = SimpleNamespace(role="orchestrator", is_sub_agent=False, metadata={},
                          user_id="owner", session_id="goal-run-1")
    res = await tool.approve_token(_approve(ATTACKER), ctx)
    assert res.error and "pins" in res.error
    assert not pushed and not gate.recorded


@pytest.mark.asyncio
async def test_an_autonomy_probe_fault_makes_no_card(monkeypatch):
    import core.security.owner_turn as ot
    pushed = _owner_card_world(monkeypatch)

    def boom(*a, **k):
        raise RuntimeError("marker down")
    monkeypatch.setattr(ot, "is_autonomous_session", boom)
    tool, gate = _tool()
    ctx = SimpleNamespace(role="orchestrator", is_sub_agent=False, metadata={},
                          user_id="owner", session_id="s1")
    res = await tool.approve_token(_approve(ATTACKER), ctx)
    assert res.error and not pushed and not gate.recorded


@pytest.mark.asyncio
async def test_the_owner_agent_turn_card_names_the_turn_origin_and_session(monkeypatch):
    pushed = _owner_card_world(monkeypatch)
    tool, gate = _tool()
    ctx = SimpleNamespace(role="orchestrator", is_sub_agent=False,
                          metadata={"turn_kind": "self_wake"}, user_id="owner",
                          session_id="chat-42")
    res = await tool.approve_token(_approve(ATTACKER), ctx)
    assert res.error is None, res.error
    body = pushed[0].body
    assert "self-wake re-entry" in body and "session chat-42" in body
