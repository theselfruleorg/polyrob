"""126 wave 3: a refusal that needs the owner is ONE card of the exact call, and an
agent's money proposal is one tap (the quote runs at once)."""
import asyncio
from types import SimpleNamespace

import pytest

from core.surfaces import act, cards
from core.wallet.tx_guard import Decision
from tests.unit.tools.controller.test_card_actions import (  # noqa: F401 — `_owner` is autouse
    _actions, _ctx, _owner)
from tests.unit.tools.defi.test_trade_t4 import ROUTER, USDC, _tool
from tools.base_tool import _CURRENT_CALL
from tools.defi.trade_tool import ApproveParams


@pytest.fixture
def pushed(monkeypatch):
    monkeypatch.setattr("core.instance.is_owner_uid", lambda uid, **kw: uid == "alice")
    out = []

    async def push(container, user_id, card, session_id=None):
        out.append(card)
        return "sent"
    monkeypatch.setattr(cards, "push", push)
    return out


def _owner_ctx():
    return SimpleNamespace(user_id="alice", role="orchestrator", is_sub_agent=False,
                           metadata={}, session_id="s-owner")


def test_the_guards_owner_lane_is_one_card_at_the_shown_bound(pushed):
    tool, gate = _tool()
    why = "owner approval required: $40.00 is above the autonomous ceiling $10.00"
    tool._guard_fn = lambda intent, tx, **kw: Decision(False, why, "owner_queue", 40.0)
    params = ApproveParams(chain="base", token=USDC, spender=ROUTER, amount=50.0,
                           max_spend_usd=60.0, dry_run=False)
    res = asyncio.run(tool.approve_token(params, _owner_ctx()))
    card, = pushed
    assert res.metadata["owner_act"]["card_id"] == card.card_id
    assert "NOT SENT" in res.extracted_content and "nothing was queued" not in res.extracted_content
    step, = card.draft["call"]["steps"]
    assert step["do"] == "defi_trade_approve_token" and step["args"]["dry_run"] is False
    assert step["args"]["max_spend_usd"] == 42.01            # $40 + 5%, under the declared $60
    assert why in card.body and not gate.recorded


def test_a_dry_run_on_the_owner_lane_raises_no_card(pushed):
    tool, _ = _tool()
    tool._guard_fn = lambda intent, tx, **kw: Decision(False, "owner approval required",
                                                       "owner_queue", 40.0)
    res = asyncio.run(tool.approve_token(ApproveParams(
        chain="base", token=USDC, spender=ROUTER, amount=5.0, max_spend_usd=6.0,
        dry_run=True), _owner_ctx()))
    assert not pushed and "nothing was queued" in res.extracted_content


@pytest.mark.asyncio
async def test_a_live_launch_without_the_owner_is_one_card(pushed, monkeypatch):
    from tests.unit.tools.defi import test_launch_verb as TL
    from tools.defi import launch_verb as LV
    world = TL.world.__wrapped__(monkeypatch) if hasattr(TL.world, "__wrapped__") else None
    if world is None:
        pytest.skip("the launch world fixture is not importable")
    world["approved"] = False
    token = _CURRENT_CALL.set(("defi_trade_launch_token", TL._p()))
    try:
        res = await LV.perform_launch_token(TL._Tool(), TL._p(), _owner_ctx())
    finally:
        _CURRENT_CALL.reset(token)
    card, = pushed
    assert res.metadata["owner_act"]["card_id"] == card.card_id and not world["deploy"]
    assert card.title == "Launch a token"
    assert card.draft["call"]["steps"][0]["do"] == "defi_trade_launch_token"


def _fake_seat(made):
    """The seat's 'Get a quote' tap: press the proposal, make the quote card."""
    async def seat(task_agent, result, spawn, deliver):
        cid, act_ = cards.parse_card_token(result.inbound.text)
        p = cards.press(cid, act_, result.inbound.identity.user_id)
        with cards.lineage(p.card):
            q = cards._new(p.card.user_id, cards.KIND_QUOTE, cards.ORIGIN_SYSTEM,
                           "Quote: /send", body="RESULT: DRY RUN", verb=p.card.verb,
                           args=p.card.args + ["max", "1.05"], typed=p.card.args,
                           ttl=cards.QUOTE_TTL_S)
        made.append(q)
        return SimpleNamespace(card_id=q.card_id)
    return seat


def test_an_agent_proposal_is_one_tap(monkeypatch):
    made = []
    monkeypatch.setattr(act, "_OWNER_COMMAND", _fake_seat(made))
    fn, model, _ = _actions()["propose_action"]
    addr = "0x2FAa2566d98FC6eac6eD5F2DbA182Ffd2142f0e7"
    res = asyncio.run(fn(model(command=f"/send 0.5 native to {addr} on base",
                               why="rebalance to base"), execution_context=_ctx()))
    quote, = made
    assert res.error is None and f"Card {quote.card_id}" in res.extracted_content
    assert "ONE tap on its Confirm" in res.extracted_content
    open_, = cards.store().open_cards("alice")
    assert open_.card_id == quote.card_id and open_.kind == cards.KIND_QUOTE
    assert any(a.command.endswith("_ok") for a in cards.card_actions(open_))


def test_a_proposal_whose_quote_makes_no_card_says_so(monkeypatch):
    async def seat(task_agent, result, spawn, deliver):
        cid, act_ = cards.parse_card_token(result.inbound.text)
        cards.press(cid, act_, result.inbound.identity.user_id)
        return "refused: the wallet is off"
    monkeypatch.setattr(act, "_OWNER_COMMAND", seat)
    fn, model, _ = _actions()["propose_action"]
    res = asyncio.run(fn(model(command="/send 0.5 native to 0x2FAa2566d98FC6eac6eD5F2DbA182"
                                       "Ffd2142f0e7 on base", why="x"), execution_context=_ctx()))
    assert res.error and "did not make a card" in res.error and "wallet is off" in res.error
    assert not cards.store().open_cards("alice")
