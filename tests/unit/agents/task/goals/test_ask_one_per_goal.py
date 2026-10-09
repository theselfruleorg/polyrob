"""One open ask per blocked goal (intel 2026-10-04).

Prod 10-04: the agent's own ask plus the automatic "Unblock goal: …" ask reached
the owner as two questions for one block (07:14/07:18, 12:26, 15:41). Their titles
differ, so the title-similarity dedup let both through.
"""
import pytest

from agents.task.goals.board import ASK_OBSOLETE, ASK_OPEN, GoalBoard


@pytest.fixture
def board(tmp_path):
    return GoalBoard(str(tmp_path / "g.db"))


def _agent_ask(board, goal_id, what):
    return board.create_ask(
        user_id="rob", what=what,
        extra_payload={"origin": "agent", "rail_id": f"goal:{goal_id}",
                       "options": {"A": "yes", "B": "no"}})


def _auto_ask(board, goal_id, title="Promote 6551"):
    return board.create_ask(user_id="rob", what=f"Unblock goal: {title}",
                            why="repeated failures", blocks_goal_ids=[goal_id],
                            extra_payload={"auto_unblock": True})


def test_auto_ask_after_agent_ask_reuses_it(board):
    g = board.create(user_id="rob", title="Promote 6551")
    a = _agent_ask(board, g.id, "Which channel: A) Reddit B) Farcaster?")
    b = _auto_ask(board, g.id)
    assert b.id == a.id
    open_ = board.asks(user_id="rob", status=ASK_OPEN)
    assert [x.id for x in open_] == [a.id]
    assert g.id in board.get(a.id).payload["blocks_goal_ids"]


def test_agent_ask_after_auto_ask_replaces_it_and_keeps_holding(board):
    g = board.create(user_id="rob", title="Promote 6551")
    auto = _auto_ask(board, g.id)
    a = _agent_ask(board, g.id, "Which channel: A) Reddit B) Farcaster?")
    assert a.id != auto.id
    assert board.get(auto.id).status == ASK_OBSOLETE
    open_ = board.asks(user_id="rob", status=ASK_OPEN)
    assert [x.id for x in open_] == [a.id]
    assert board.get(a.id).payload["blocks_goal_ids"] == [g.id]
    assert board.get(a.id).payload["options"] == {"A": "yes", "B": "no"}


def test_two_agent_asks_for_one_goal_stay_one(board):
    g = board.create(user_id="rob", title="Fix DM CHECK")
    a = _agent_ask(board, g.id, "DM CHECK still says inbound is unreadable. May I amend it?")
    b = _agent_ask(board, g.id, "Approve the DM CHECK cron fix: A) apply B) leave")
    assert b.id == a.id
    assert len(board.asks(user_id="rob", status=ASK_OPEN)) == 1


def test_asks_about_different_goals_stay_separate(board):
    g1 = board.create(user_id="rob", title="goal one")
    g2 = board.create(user_id="rob", title="goal two entirely different")
    _auto_ask(board, g1.id, "goal one")
    _auto_ask(board, g2.id, "goal two entirely different")
    assert len(board.asks(user_id="rob", status=ASK_OPEN)) == 2


def test_force_and_kinded_asks_are_untouched(board):
    g = board.create(user_id="rob", title="x")
    kinded = board.create_ask(user_id="rob", what="Objective at its goal budget: x",
                              blocks_goal_ids=[g.id],
                              extra_payload={"kind": "objective_budget_spent"})
    auto = _auto_ask(board, g.id, "x")
    assert auto.id != kinded.id
    forced = board.create_ask(user_id="rob", what="Approve tool?", blocks_goal_ids=[g.id],
                              force=True)
    assert forced.id not in (kinded.id, auto.id)
