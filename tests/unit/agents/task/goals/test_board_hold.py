"""031 T4: an owner pause returns THIS worker's running goals to ready, no failure."""
from agents.task.goals.board import GoalBoard


def test_hold_running_returns_only_this_workers_rows_to_ready(tmp_path):
    b = GoalBoard(str(tmp_path / "goals.db"))
    g1 = b.create(user_id="rob", title="a")
    g2 = b.create(user_id="rob", title="b")
    assert b.claim(g1.id, "goal-dispatch-1", ttl_seconds=60)
    assert b.claim(g2.id, "goal-dispatch-2", ttl_seconds=60)
    held = b.hold_running(worker="goal-dispatch-1", reason="owner pause")
    assert held == [g1.id]
    assert b.get(g1.id).status == "ready" and b.get(g1.id).claim_lock is None
    assert b.get(g2.id).status == "running"
    assert b.get(g1.id).consecutive_failures == 0
    kinds = [e["kind"] for e in b.events(g1.id)]
    assert "held_by_pause" in kinds
    assert b.hold_running(worker="goal-dispatch-1", reason="again") == []


def test_count_created_by_session_uses_audit_stamp(tmp_path):
    b = GoalBoard(str(tmp_path / "goals.db"))
    b.create(user_id="rob", title="x", payload={"created_by_session_id": "s1"})
    b.create(user_id="rob", title="y", payload={"origin_session_id": "s1"})
    b.create(user_id="rob", title="z", payload={"origin_session_id": "s2"})
    assert b.count_created_by_session("s1") == 2
