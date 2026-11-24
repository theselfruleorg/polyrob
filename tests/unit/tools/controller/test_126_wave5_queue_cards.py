"""126 wave 5 (owner, 2026-11-21): a MONEY call the approval queue would park is ONE
call card; Confirm runs it once (no wait, no retry, no grant hash). ``/approve <card
id>`` is the typed form of that Confirm; ``/pending`` names the open call cards."""
import asyncio

import pytest

from core.surfaces import call_cards, card_list, cards
from tools.controller import approval_queue as aq
from tools.controller import call_card_run as RUN
from tools.controller.approval import make_approval_hook


@pytest.fixture
def owner(monkeypatch, tmp_path):
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "u1")
    monkeypatch.setattr("core.instance.is_owner_uid", lambda uid, **kw: uid == "u1")
    monkeypatch.setattr(cards, "_STORE", cards.CardStore(str(tmp_path / "cards.db")))
    pushed = []

    async def push(container, user_id, card, session_id=None):
        pushed.append(card)
        return "sent"
    monkeypatch.setattr(cards, "push", push)
    return pushed


def _ctx():
    from agents.task.loop_types.execution_context import ActionExecutionContext
    return ActionExecutionContext(session_id="s1", user_id="u1", role="orchestrator")


def _provider(tmp_path):
    from modules.goals.board import GoalBoard
    return aq.OwnerQueueApprover(board=GoalBoard(str(tmp_path / "goals.db")),
                                 poll_interval=0.02, container=None)


PARAMS = {"chain": "base", "token": "0x" + "11" * 20, "to": "0x" + "22" * 20,
          "amount": 3.0, "max_spend_usd": 4.0, "dry_run": False}


@pytest.mark.asyncio
async def test_a_parked_money_call_is_one_call_card_and_no_ask(owner, tmp_path):
    provider = _provider(tmp_path)
    hook = make_approval_hook(provider, ["defi_trade_transfer"], timeout=5)
    text = await hook("defi_trade_transfer", dict(PARAMS), _ctx())
    card, = owner
    assert text and f"card {card.card_id}" in text and "ONE tap" in text
    step, = call_cards.steps_of(card)
    assert step == {"do": "defi_trade_transfer", "args": PARAMS}
    from modules.goals.board import ASK_OPEN
    assert not provider._board().asks(user_id="u1", status=ASK_OPEN)


@pytest.mark.asyncio
async def test_a_tool_no_card_runs_keeps_the_queue(owner, tmp_path):
    assert await RUN.card_for_parked_call("shell_run", {"command": "ls"}, _ctx()) is None
    assert not owner


def _typed(user_id, line):
    """A typed `/approve|/reject <card id>` the way every seat runs it: the card's tap
    (``surfaces.card_press.call_card_tap``), then the ONE press. None = not a call card."""
    from surfaces.card_press import PressSeat, call_card_tap, press
    tap = call_card_tap(user_id, line)
    if tap is None:
        return None
    cid, act = cards.parse_card_token(tap)

    async def _no_line(line, deliver):
        raise AssertionError("a call card runs no seat line")
    return asyncio.run(press(PressSeat(seat="repl", user_id=user_id, container=None,
                                       run_line=_no_line), cid, act))


def test_approve_with_a_card_id_confirms_it_and_reject_cancels(owner):
    a = call_cards.make("u1", [{"do": "lp_seed_confirm", "args": {"ref": "s0000000000"}}],
                        title="t")
    out = _typed("u1", f"/approve {a.card_id}")
    assert "step 1/1 (lp_seed_confirm): STOPPED" in out           # no such declaration
    assert cards.store().get(a.card_id).state == cards.S_FAILED
    b = call_cards.make("u1", [{"do": "lp_seed_confirm", "args": {"ref": "s0000000000"}}],
                        title="t")
    assert "Cancelled" in _typed("u1", f"/reject {b.card_id}")
    assert _typed("u1", "/approve not-a-card") is None
    assert _typed("someone", f"/approve {b.card_id}") is None


def test_pending_names_the_open_call_cards(owner):
    c = call_cards.make("u1", [{"do": "lp_seed_confirm", "args": {"ref": "s0000000000"}}],
                        title="Move a pool position")
    lines = card_list.call_cards_lines("u1")
    assert lines[0].startswith("1 money call(s)") and f"/card_{c.card_id}_ok" in lines[1]
    assert card_list.call_cards_lines("someone") == []


@pytest.mark.asyncio
async def test_a_retried_call_reuses_its_open_card_never_a_second(owner, tmp_path):
    """Two cards of one call are two sends after two taps: a retry names the open card."""
    hook = make_approval_hook(_provider(tmp_path), ["defi_trade_transfer"], timeout=5)
    first = await hook("defi_trade_transfer", dict(PARAMS), _ctx())
    again = await hook("defi_trade_transfer", dict(PARAMS, why="retry", max_spend_usd=4.5),
                       _ctx())
    card, = owner                                   # one card pushed, not two
    assert f"card {card.card_id}" in first and f"card {card.card_id}" in again
    assert "made no second card" in again
    assert len([c for c in cards.store().open_cards("u1") if c.kind == cards.KIND_CALL]) == 1
    other = await hook("defi_trade_transfer", dict(PARAMS, amount=5.0), _ctx())
    assert len(owner) == 2 and f"card {owner[1].card_id}" in other   # a different call


@pytest.mark.asyncio
async def test_an_autonomous_run_gets_a_card_that_names_the_run(owner, monkeypatch):
    """2026-11-21: a cron run's money call is ONE card, not a line for the owner to type;
    the card says an unattended run asked, and Confirm runs every gate as the owner seat."""
    monkeypatch.setattr("core.security.owner_turn.is_autonomous_session",
                        lambda ctx, ts=None: True)
    res = await RUN.need_owner(RUN._Seat(), _ctx(), title="t", body="b",
                               steps=[{"do": "defi_trade_transfer", "args": dict(PARAMS)}])
    assert not res.error
    card, = owner
    assert "an autonomous run (goal or cron)" in card.body and "not by you" in card.body
