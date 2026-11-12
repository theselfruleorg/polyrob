"""`owner_ask(blocks_goal_ids=…)` — a rail that needs an OWNER DECISION raises a durable board ask.

2026-09-20 (intel 09:57Z): the 6-hourly TKN rebuy skipped 12 h on a gate-1 A/B
question the agent had asked ONCE in chat and never re-raised — nothing agent-callable
reached `GoalBoard.create_ask`, so the question lived in a note to himself and
vanished from every owner seat. The store already dedups OPEN asks (a matching
one is refreshed), so re-asking is safe.
"""
import asyncio
from types import SimpleNamespace

import pytest

from agents.task.goals.board import GoalBoard


class _Registry:
    def __init__(self):
        self.actions = {}

    def action(self, description, param_model=None, **kw):
        def deco(fn):
            self.actions[fn.__name__] = (fn, param_model, description)
            return fn
        return deco


@pytest.fixture(autouse=True)
def _owner(monkeypatch):
    monkeypatch.setattr("core.instance.resolve_owner_principal", lambda *a, **k: "alice")

    async def _deliver(*a, **k):
        return "sent"

    monkeypatch.setattr("core.surfaces.user_delivery.deliver_user_message", _deliver)


@pytest.fixture
def ask(tmp_path, monkeypatch):
    from tools.controller.owner_ask_action import register_owner_ask_action
    board = GoalBoard(str(tmp_path / "goals.db"))
    monkeypatch.setattr("tools.controller.owner_ask_action._board", lambda c: board)
    controller = SimpleNamespace(registry=_Registry(), user_id="alice", session_id="rail-sess",
                                 orchestrator=None, container=None)
    register_owner_ask_action(controller)
    fn, model, _ = controller.registry.actions["owner_ask"]
    return SimpleNamespace(board=board, call=lambda ctx, **kw: asyncio.run(fn(model(**kw), ctx)))


def _ctx(session_id="rail-sess", user_id="alice", **kw):
    return SimpleNamespace(session_id=session_id, parent_session_id=None, user_id=user_id, **kw)


def test_a_retired_ask_verb_does_not_come_back():
    from tools.goal_tools import GoalTool
    assert not hasattr(GoalTool, "goal_ask")


def test_blocks_goal_ids_are_recorded_and_named(ask):
    from core.autonomy_marker import mark_autonomous
    g = ask.board.create(user_id="alice", title="buy TKN tranche 15", force=True)
    mark_autonomous("ask-own-goal", g.id)
    r = ask.call(_ctx(session_id="ask-own-goal"), question="decide X please", blocks_goal_ids=[g.id])
    assert not r.error and g.id in r.extracted_content
    a = ask.board.asks(user_id="alice", status="open")[0]
    assert g.id in (a.payload.get("blocks_goal_ids") or [])


def test_a_hold_never_waits_for_a_card_in_a_live_turn(ask, monkeypatch):
    from core.surfaces import cards
    monkeypatch.setattr("core.security.owner_turn.is_genuine_owner_turn", lambda ctx: True)
    g = ask.board.create(user_id="alice", title="buy TKN tranche 15", force=True)
    r = ask.call(_ctx(session_id="s-live-hold"), question="decide X please",
                 options=["Yes", "No"], blocks_goal_ids=[g.id])
    assert r.error is None and g.id in r.extracted_content
    assert cards.store().open_cards("alice") == []


def test_autonomous_ask_cannot_hold_an_unrelated_goal(ask):
    from core.autonomy_marker import mark_autonomous
    goal = ask.board.create(user_id="alice", title="unrelated goal", force=True)
    mark_autonomous("other-goal-run", "own-goal")
    result = ask.call(_ctx(session_id="other-goal-run"), question="please block this goal",
                      blocks_goal_ids=[goal.id])
    assert result.error and "own goal" in result.error
    assert ask.board.get(goal.id).status != "blocked"
    assert ask.board.asks(user_id="alice", status="open") == []


# --- 116 W2.3: holds_this_job, the cron twin of blocks_goal_ids ----------------

def test_holds_this_job_holds_the_asking_job(ask):
    from core.autonomy_marker import mark_autonomous
    from core.cron_expect import job_held_by
    mark_autonomous("cron-run-sess", cron_job_id="job-outreach")
    r = ask.call(_ctx(session_id="cron-run-sess"), question="expand the list or stop?",
                 holds_this_job=True)
    assert not r.error and "HELD" in r.extracted_content
    a = ask.board.asks(user_id="alice", status="open")[0]
    assert a.payload["rail_id"] == "cron:job-outreach"
    assert a.payload["holds_rail"] is True and a.payload["holds_rail_via"] == "owner_ask"
    assert job_held_by(ask.board.db_path, "alice", "job-outreach")["ask_id"] == a.id


def test_a_cron_ask_without_holds_this_job_holds_nothing(ask):
    from core.autonomy_marker import mark_autonomous
    from core.cron_expect import job_held_by
    mark_autonomous("cron-run-sess2", cron_job_id="job-quiet")
    r = ask.call(_ctx(session_id="cron-run-sess2"), question="expand the list or stop?")
    assert not r.error and "HELD" not in r.extracted_content
    assert job_held_by(ask.board.db_path, "alice", "job-quiet") is None


def test_holds_this_job_outside_a_job_run_is_refused(ask):
    r = ask.call(_ctx(session_id="plain-chat"), question="expand the list or stop?",
                 holds_this_job=True)
    assert r.error and "scheduled job" in r.error
    assert ask.board.asks(user_id="alice", status="open") == []
