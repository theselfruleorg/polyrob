"""080 WS-2 — one owner model for goals: pause is `paused`, retry is the ONE
re-queue, stop is once-or-forever on every path, and the status reason names
the decision a goal waits on."""
import types

import pytest

from agents.task.goals.board import GoalBoard
from core import goal_owner_verbs as V
from core.goal_legibility import origin, owner_bucket, status_reason
from core.goal_suppressions import blocking, stop_goal


@pytest.fixture
def board(tmp_path):
    return GoalBoard(str(tmp_path / "goals.db"))


def test_auto_unblock_key_is_the_boards():
    from agents.task.goals.board_types import ASK_AUTO_UNBLOCK
    assert V._AUTO_UNBLOCK == ASK_AUTO_UNBLOCK


def test_pause_writes_paused_never_blocked_and_is_not_dispatched(board):
    g = board.create(user_id="u", title="write the digest")
    r = V.pause_goal(board, user_id="u", goal=g)
    assert r.ok and r.status == "paused"
    assert board.get(g.id).status == "paused"
    assert all(x.id != g.id for x in board.ready(limit=50))
    assert "Paused by you" in V.reason(board, board.get(g.id), "u")
    assert owner_bucket("paused") == "PAUSED"


def test_retry_lifts_a_pause_and_clears_failures(board):
    g = board.create(user_id="u", title="t1")
    V.pause_goal(board, user_id="u", goal=g)
    r = V.retry_goal(board, user_id="u", goal=board.get(g.id))
    assert r.ok and board.get(g.id).status == "ready"


def test_retry_of_a_paused_dependent_waits_for_its_prerequisite(board):
    a = board.create(user_id="u", title="first thing")
    b = board.create(user_id="u", title="second thing", depends_on=[a.id])
    V.pause_goal(board, user_id="u", goal=board.get(b.id))
    r = V.retry_goal(board, user_id="u", goal=board.get(b.id))
    assert r.ok and board.get(b.id).status == "waiting"


def test_retry_of_a_blocked_goal_clears_failures(board):
    g = board.create(user_id="u", title="t2")
    board.update_status(g.id, "blocked")
    r = V.retry_goal(board, user_id="u", goal=board.get(g.id))
    assert r.ok and "failures cleared" in r.message
    assert board.get(g.id).consecutive_failures == 0


def test_ask_held_goal_names_the_decision_and_retry_does_not_answer_it(board):
    g = board.create(user_id="u", title="post the thread")
    ask = board.create_ask(user_id="u", what="Approve x_post", blocks_goal_ids=[g.id])
    g = board.get(g.id)
    assert g.status == "ready"
    why = V.reason(board, g, "u")
    assert ask.id[:8] in why and "Approve x_post" in why and "/fulfill" in why
    r = V.retry_goal(board, user_id="u", goal=g)
    assert not r.ok and ask.id[:8] in r.message


def test_retry_closes_the_dispatchers_own_unblock_ask(board):
    g = board.create(user_id="u", title="post the thread")
    board.create_ask(user_id="u", what="Unblock goal: post the thread",
                     blocks_goal_ids=[g.id], extra_payload={"auto_unblock": True})
    r = V.retry_goal(board, user_id="u", goal=board.get(g.id))
    assert r.ok, r.message
    assert board.holding_asks(g.id, user_id="u") == []


def test_stop_once_does_not_suppress_and_forever_does(board):
    g = board.create(user_id="u", title="daily gas watch")
    r = stop_goal(board, user_id="u", goal=g)
    assert r.ok and r.stopped and not r.suppressed
    assert board.get(g.id).status == "cancelled"
    assert not blocking(board.db_path, user_id="u", title="daily gas watch")
    g2 = board.create(user_id="u", title="daily gas watch", force=True)
    r2 = stop_goal(board, user_id="u", goal=g2, forever=True)
    assert r2.suppressed and "/goal allow" in r2.message
    assert blocking(board.db_path, user_id="u", title="daily gas watch")


def test_stop_forever_on_a_finished_goal_still_switches_it_off(board):
    g = board.create(user_id="u", title="old one")
    board.cancel(g.id)
    r = stop_goal(board, user_id="u", goal=board.get(g.id), forever=True)
    assert r.ok and r.suppressed and not r.stopped


def _g(status, **kw):
    return types.SimpleNamespace(status=status, payload=kw.pop("payload", {}),
                                 consecutive_failures=kw.get("fails", 0),
                                 last_failure_error=kw.get("err"))


def test_status_reason_is_one_owner_sentence():
    assert status_reason(_g("blocked", fails=2, err="x_post 401",
                            payload={"block_kind": "needs_input"})) == \
        "Failed twice: x_post 401 (it needs something from you)."
    assert status_reason(_g("ready")) == "Waiting its turn."
    assert status_reason(_g("paused")) == "Paused by you."
    tool_ask = types.SimpleNamespace(id="abcdef1234", title="Approve a spend",
                                     payload={"ask_kind": "tool_approval"})
    assert "/pending" in status_reason(_g("ready"), [tool_ask])


def test_retry_help_never_promises_a_stopped_goal(board):
    """Retry refuses a goal /goal stop cancelled; no help line may say otherwise."""
    from cli.commands import goals as cli_goals
    from cli.ui.commands.h_goal_one import HELP_GOAL
    from surfaces.telegram.owner_ops import _GOAL_USAGE
    g = board.create(user_id="u", title="stop me")
    board.cancel(g.id)
    assert not V.retry_goal(board, user_id="u", goal=board.get(g.id)).ok
    for text in (HELP_GOAL[0], _GOAL_USAGE, cli_goals.goals_retry.help or ""):
        assert "stopped or paused" not in text
