"""A goal put back to ``ready`` is not held by its own automatic "Unblock goal" ask.

Prod 2026-10-12: two trading goals blocked, the dispatcher filed its automatic
"Unblock goal: …" ask for each, and the goals then went back to ``ready`` — one by
the owner's ``polyrob goals retry``, one by the provider-outage requeue. Both asks
stayed OPEN, so ``ready()`` held both goals forever and nothing dispatched. The
re-queue IS the answer to that ask. An owner-decision ask (a tool approval, an
agent-filed need) still holds the goal.
"""
import time

import pytest

from agents.task.goals.board import (
    ASK_AUTO_UNBLOCK, ASK_OBSOLETE, ASK_OPEN, GoalBoard, STATUS_BLOCKED, STATUS_READY,
)


@pytest.fixture
def board(tmp_path):
    return GoalBoard(str(tmp_path / "g.db"))


def _auto_ask(board, goal):
    return board.create_ask(user_id="rob", what=f"Unblock goal: {goal.title}",
                            blocks_goal_ids=[goal.id],
                            extra_payload={ASK_AUTO_UNBLOCK: True})


def test_owner_retry_releases_the_auto_unblock_ask(board):
    g = board.create(user_id="rob", title="Trading Goal 4")
    board.update_status(g.id, STATUS_BLOCKED)
    ask = _auto_ask(board, g)

    assert board.update_status(g.id, STATUS_READY, reset_failures=True, user_id="rob")

    assert board.get(ask.id).status == ASK_OBSOLETE
    assert [x.id for x in board.ready(limit=10)] == [g.id]


def test_provider_outage_requeue_releases_the_auto_unblock_ask(board):
    g = board.create(user_id="rob", title="Trading Goal 1",
                     payload={"block_kind": "provider_outage"})
    board.update_status(g.id, STATUS_BLOCKED)
    ask = _auto_ask(board, g)
    board._now = lambda: time.time() + 3600  # past the 30-min self-heal window

    assert board.age_out_blocked(max_age_days=14, provider_retry_min=30) == 1

    assert board.get(g.id).status == STATUS_READY
    assert board.get(ask.id).status == ASK_OBSOLETE
    assert [x.id for x in board.ready(limit=10)] == [g.id]


def test_an_owner_decision_ask_still_holds_a_requeued_goal(board):
    g = board.create(user_id="rob", title="Bridge")
    board.update_status(g.id, STATUS_BLOCKED)
    ask = board.create_ask(user_id="rob", what="Approve the bridge?",
                           blocks_goal_ids=[g.id])

    board.update_status(g.id, STATUS_READY, reset_failures=True, user_id="rob")

    assert board.get(ask.id).status == ASK_OPEN
    assert board.ready(limit=10) == []


def test_auto_ask_naming_two_goals_stays_open_for_the_other(board):
    a = board.create(user_id="rob", title="A")
    b = board.create(user_id="rob", title="B")
    ask = board.create_ask(user_id="rob", what="Unblock goals",
                           blocks_goal_ids=[a.id, b.id],
                           extra_payload={ASK_AUTO_UNBLOCK: True})

    board.update_status(a.id, STATUS_READY, reset_failures=True, user_id="rob")

    row = board.get(ask.id)
    assert row.status == ASK_OPEN
    assert row.payload["blocks_goal_ids"] == [b.id]
    assert [x.id for x in board.ready(limit=10)] == [a.id]


def test_unblock_releases_the_auto_unblock_ask(board):
    """Prod 2026-10-14: the owner's X-growth goal gave up, Rob edited its acceptance
    and unblocked it (``unblocked`` event), the auto ask stayed OPEN, and the goal
    sat ``held_by_ask`` with no claim for two hours."""
    g = board.create(user_id="rob", title="X growth")
    board.update_status(g.id, STATUS_BLOCKED)
    ask = _auto_ask(board, g)

    assert board.unblock(g.id, user_id="rob", rationale="acceptance reworded")

    assert board.get(ask.id).status == ASK_OBSOLETE
    assert [x.id for x in board.ready(limit=10)] == [g.id]


def test_unblock_keeps_an_owner_decision_ask(board):
    g = board.create(user_id="rob", title="Bridge")
    board.update_status(g.id, STATUS_BLOCKED)
    ask = board.create_ask(user_id="rob", what="Approve the bridge?",
                           blocks_goal_ids=[g.id])

    assert board.unblock(g.id, user_id="rob")

    assert board.get(ask.id).status == ASK_OPEN
    assert board.ready(limit=10) == []
