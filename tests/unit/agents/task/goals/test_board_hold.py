"""031 T4: an owner pause returns THIS worker's running goals to ready, no failure."""
from modules.goals.board import GoalBoard


def test_hold_running_returns_only_this_workers_rows_to_ready(tmp_path):
    b = GoalBoard(str(tmp_path / "goals.db"))
    g1 = b.create(user_id="alice", title="a")
    g2 = b.create(user_id="alice", title="b")
    assert b.claim(g1.id, "goal-dispatch-1", ttl_seconds=60)
    assert b.claim(g2.id, "goal-dispatch-2", ttl_seconds=60)
    held = b.hold_running(worker="goal-dispatch-1", reason="owner pause")
    assert held == [g1.id]
    assert b.get(g1.id).status == "ready" and b.get(g1.id).claim_lock is None
    assert b.get(g2.id).status == "running"
    assert b.get(g1.id).consecutive_failures == 0
    kinds = [e["kind"] for e in b.events(g1.id)]
    assert "held" in kinds and "held_by_pause" not in kinds   # the legacy alias is gone
    assert b.hold_running(worker="goal-dispatch-1", reason="again") == []


