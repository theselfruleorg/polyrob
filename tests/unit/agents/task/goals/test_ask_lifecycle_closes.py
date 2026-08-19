"""An ask must close when the goal it blocks no longer needs the owner.

Prod held 44 OPEN asks, the oldest a month old, and several named goals that had
since SUCCEEDED — "Unblock goal: Self-test: write a small utility and get its
tests green" sat open while that goal was `done`. An ask's life was bound to the
owner answering it and to nothing else, so the board accumulated questions that
had already answered themselves. That queue is what the owner has to read.
"""
import pytest

from agents.task.goals.board import (
    ASK_OBSOLETE, ASK_OPEN, GoalBoard, STATUS_BLOCKED,
)


@pytest.fixture
def board(tmp_path):
    return GoalBoard(str(tmp_path / "goals.db"))


def _blocked_goal_with_ask(board, title="ship the thing"):
    g = board.create(user_id="rob", title=title)
    board.block_from_ready(g.id, error="needs an owner decision")
    board.create_ask(user_id="rob", what=f"Unblock goal: {title}",
                     why="repeated failures", blocks_goal_ids=[g.id])
    return g


def test_ask_closes_when_its_only_blocked_goal_succeeds(board):
    g = _blocked_goal_with_ask(board)
    assert len(board.asks(user_id="rob", status=ASK_OPEN)) == 1

    closed = board.close_asks_for_goal("rob", g.id, reason="goal_done")

    assert closed == 1
    assert board.asks(user_id="rob", status=ASK_OPEN) == []
    obsolete = board.asks(user_id="rob", status=ASK_OBSOLETE)
    assert len(obsolete) == 1
    assert (obsolete[0].payload or {}).get("decision") == "obsolete"


def test_an_ask_blocking_two_goals_stays_open_until_both_resolve(board):
    a = board.create(user_id="rob", title="goal a")
    b = board.create(user_id="rob", title="goal b")
    board.create_ask(user_id="rob", what="Grant the x402_pay tool",
                     why="two goals need it", blocks_goal_ids=[a.id, b.id])

    assert board.close_asks_for_goal("rob", a.id, reason="goal_done") == 0
    assert len(board.asks(user_id="rob", status=ASK_OPEN)) == 1

    assert board.close_asks_for_goal("rob", b.id, reason="goal_done") == 1
    assert board.asks(user_id="rob", status=ASK_OPEN) == []


def test_closing_never_crosses_tenants(board):
    g = _blocked_goal_with_ask(board)

    assert board.close_asks_for_goal("someone_else", g.id, reason="goal_done") == 0
    assert len(board.asks(user_id="rob", status=ASK_OPEN)) == 1


def test_an_owner_answered_ask_is_not_reopened_or_reclosed(board):
    g = _blocked_goal_with_ask(board)
    ask = board.asks(user_id="rob", status=ASK_OPEN)[0]
    board.fulfill_ask(ask.id, user_id="rob")

    assert board.close_asks_for_goal("rob", g.id, reason="goal_done") == 0


def test_a_still_blocked_goal_keeps_its_ask(board):
    """Only a goal that no longer needs the owner releases its ask."""
    g = _blocked_goal_with_ask(board)

    assert board.get(g.id).status == STATUS_BLOCKED
    assert board.close_asks_for_goal("rob", g.id, reason="goal_cancelled") == 1


# ---------------------------------------------------------------------------
# Wiring: the terminal transitions release their asks by themselves.
# ---------------------------------------------------------------------------

def test_record_success_closes_the_goals_ask(board):
    g = board.create(user_id="rob", title="ship the thing")
    board.create_ask(user_id="rob", what="Unblock goal: ship the thing",
                     why="repeated failures", blocks_goal_ids=[g.id])
    board.claim(g.id, "w1", ttl_seconds=60)

    board.record_success(g.id, session_id="s1", result="shipped")

    assert board.asks(user_id="rob", status=ASK_OPEN) == [], \
        "a goal that succeeded must not leave the owner an open question about it"


def test_cancel_closes_the_goals_ask(board):
    g = board.create(user_id="rob", title="ship the thing")
    board.create_ask(user_id="rob", what="Unblock goal: ship the thing",
                     why="repeated failures", blocks_goal_ids=[g.id])

    board.cancel(g.id, user_id="rob")

    assert board.asks(user_id="rob", status=ASK_OPEN) == []
