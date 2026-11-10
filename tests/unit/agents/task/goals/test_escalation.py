"""Blocker → owner escalation (§7.2): the message, and the durable ask."""
import pytest

from agents.task.goals import escalation
from agents.task.goals.board import Goal, STATUS_BLOCKED, STATUS_READY


def _blocked_goal():
    return Goal(id="g1", user_id="alice", title="Draft enterprise outreach asset",
                status=STATUS_BLOCKED, consecutive_failures=2, max_retries=2,
                last_failure_error="run did not complete (refusal or empty)")


# --- message builder ---------------------------------------------------------

def test_build_blocker_message_names_goal_and_reason():
    msg = escalation.build_blocker_escalation(_blocked_goal())
    assert "Draft enterprise outreach asset" in msg
    assert "refusal or empty" in msg
    assert "blocked" in msg.lower()


def test_build_blocker_message_asks_for_help():
    msg = escalation.build_blocker_escalation(_blocked_goal())
    # it should be an ASK, not just a status line
    assert "?" in msg or "need" in msg.lower() or "help" in msg.lower()


# --- T2-03/T4-04: the durable ask ---------------------------------------------

@pytest.mark.asyncio
async def test_blocked_goal_creates_ask_in_every_posture(tmp_path, monkeypatch):
    """A blocked goal always leaves a silent, durable ask for `owner fulfill` to consume."""
    monkeypatch.delenv("AUTONOMY_POSTURE", raising=False)         # silent posture
    monkeypatch.delenv("POLYROB_LOCAL", raising=False)
    from agents.task.goals.board import GoalBoard, ASK_OPEN, STATUS_BLOCKED
    from agents.task.goals.dispatcher import GoalDispatcher

    board = GoalBoard(str(tmp_path / "g.db"))
    g = board.create(user_id="alice", title="Post the announcement")
    board.claim(g.id, "w", ttl_seconds=60)
    board.record_failure(g.id, error="needs twitter write access",
        claim_token=board.get(g.id).claim_token)
    board.claim(g.id, "w", ttl_seconds=60)
    board.record_failure(g.id, error="needs twitter write access",
        claim_token=board.get(g.id).claim_token)  # trips breaker
    assert board.get(g.id).status == STATUS_BLOCKED

    class _Agent:
        container = None  # no push target; the ask must still be created

    disp = GoalDispatcher(board, _Agent())
    await disp._maybe_escalate_blocked(board.get(g.id))

    asks = board.asks(user_id="alice", status=ASK_OPEN)
    assert asks, "a blocked goal must leave a durable ask in every posture"
    assert any("Unblock goal" in a.title for a in asks)
