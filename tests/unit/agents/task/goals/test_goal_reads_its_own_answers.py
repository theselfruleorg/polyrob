"""B1/B2/B3 (2026-10-13 memory/coherency review): a goal receives the owner's
answer to its OWN ask, whatever its status, and keeps reading it on later runs."""
import pytest

from agents.task.goals.board import GoalBoard
from agents.task.goals.context import build_goal_run_task
from agents.task.goals.rail_answers import decision_note


@pytest.fixture
def board(tmp_path):
    return GoalBoard(str(tmp_path / "g.db"))


def _rail_ask(board, goal_id, what="Which chain: A) Base or B) Solana?"):
    return board.create_ask(user_id="rob", what=what,
                            extra_payload={"origin": "agent", "rail_id": f"goal:{goal_id}",
                                           "rail_kind": "goal"})


def test_owner_ask_answer_reaches_the_goal_that_asked(board):
    """B1: an `owner_ask` from a goal run has no blocks_goal_ids; the answer
    must still land on the goal and in its next run's task."""
    g = board.create(user_id="rob", title="Buy back the token")
    a = _rail_ask(board, g.id)
    ok, _ = board.decide_ask(a.id, user_id="rob", approved=True, answer="A")
    assert ok
    task = build_goal_run_task(board.get(g.id))
    assert "OWNER DECISIONS on what this goal asked" in task
    assert "Which chain" in task and "APPROVED" in task
    assert "\n  A\n" in task


def test_a_decline_is_carried_too(board):
    g = board.create(user_id="rob", title="Post the promo")
    a = _rail_ask(board, g.id, what="Post the promo thread now?")
    board.decide_ask(a.id, user_id="rob", approved=False)
    task = build_goal_run_task(board.get(g.id))
    assert "DECLINED — do not re-ask" in task


def test_held_ready_goal_gets_the_answer(board):
    """B2: a goal held in `ready` by its own goal_ask is not `blocked`; the
    answer used to be written nowhere."""
    g = board.create(user_id="rob", title="Swap on the better chain")
    a = board.create_ask(user_id="rob", what="Base or Solana?", blocks_goal_ids=[g.id])
    assert board.get(g.id).status == "ready"
    ok, unblocked = board.decide_ask(a.id, user_id="rob", approved=True, answer="Solana")
    assert ok and unblocked == 0
    dep = board.get(g.id)
    assert dep.status == "ready"
    assert dep.payload["owner_answers"][-1]["answer"] == "Solana"
    assert "Solana" in build_goal_run_task(dep)


def test_answer_survives_a_later_failure(board):
    """B3: after the next failure the answer used to drop out of the task."""
    g = board.create(user_id="rob", title="Post the launch thread")
    a = _rail_ask(board, g.id, what="Which key should I use?")
    board.decide_ask(a.id, user_id="rob", approved=True, answer="the SANDBOX key")
    board.claim(g.id, "w", ttl_seconds=60)
    board.record_failure(g.id, error="timeout", claim_token=board.get(g.id).claim_token)
    task = build_goal_run_task(board.get(g.id))
    assert "PREVIOUS ATTEMPT" in task
    assert "the SANDBOX key" in task


def test_blocked_goal_still_unblocks_and_answer_is_not_doubled(board):
    g = board.create(user_id="rob", title="Post the launch thread")
    board.claim(g.id, "w", ttl_seconds=60)
    board.record_failure(g.id, error="which key?", claim_token=board.get(g.id).claim_token)
    board.claim(g.id, "w", ttl_seconds=60)
    board.record_failure(g.id, error="which key?", claim_token=board.get(g.id).claim_token)
    a = board.create_ask(user_id="rob", what="Which API key?", blocks_goal_ids=[g.id])
    ok, unblocked = board.decide_ask(a.id, user_id="rob", approved=True, answer="ZEBRA key")
    assert ok and unblocked == 1
    task = build_goal_run_task(board.get(g.id))
    assert "OWNER UNBLOCKED" in task
    assert task.count("ZEBRA key") == 1


def test_decision_note_is_true_for_a_finished_goal(board):
    g = board.create(user_id="rob", title="Old goal")
    a = _rail_ask(board, g.id)
    board.claim(g.id, "w", ttl_seconds=60)
    board.record_success(g.id, result="ok", claim_token=board.get(g.id).claim_token)
    board.decide_ask(a.id, user_id="rob", approved=True, answer="A")
    note = decision_note(board, a.id, 0)
    assert "no run will read" in note and g.id in note


def test_decision_note_for_a_live_goal(board):
    g = board.create(user_id="rob", title="Live goal")
    a = _rail_ask(board, g.id)
    board.decide_ask(a.id, user_id="rob", approved=True, answer="A")
    assert "reads your answer on its next run" in decision_note(board, a.id, 0)


def test_another_tenants_goal_is_never_stamped(board):
    g = board.create(user_id="eve", title="Foreign goal")
    a = _rail_ask(board, g.id)
    board.decide_ask(a.id, user_id="rob", approved=True, answer="A")
    assert "owner_answers" not in (board.get(g.id).payload or {})
