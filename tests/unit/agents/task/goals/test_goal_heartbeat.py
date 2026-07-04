"""F8 (live-test): in-flight goal claims must be heartbeated so a goal that runs
longer than GOAL_CLAIM_TTL_SEC isn't reclaimed by reclaim_stale and DOUBLE-dispatched.
"""
from agents.task.goals.board import GoalBoard
from agents.task.goals.dispatcher import _heartbeat_interval


def test_heartbeat_interval_third_of_ttl_floored_at_30():
    assert _heartbeat_interval(900) == 300
    assert _heartbeat_interval(120) == 40
    assert _heartbeat_interval(30) == 30   # floor
    assert _heartbeat_interval(3) == 30    # floor


def test_heartbeat_extends_claim_and_prevents_reclaim(tmp_path):
    clk = {"t": 1000.0}
    b = GoalBoard(str(tmp_path / "g.db"), clock=lambda: clk["t"])
    g = b.create(user_id="u", title="long-running goal")
    assert b.claim(g.id, "w", ttl_seconds=100) is not None   # expires at 1100

    clk["t"] = 1090                                           # still claimed
    assert b.heartbeat(g.id, "w", ttl_seconds=100) is True    # now expires at 1190

    clk["t"] = 1150            # past ORIGINAL expiry (1100), before heartbeated (1190)
    assert b.reclaim_stale() == 0                             # heartbeat saved it
    assert b.get(g.id).status == "running"


def test_without_heartbeat_stale_claim_is_reclaimed(tmp_path):
    clk = {"t": 1000.0}
    b = GoalBoard(str(tmp_path / "g.db"), clock=lambda: clk["t"])
    g = b.create(user_id="u", title="goal")
    b.claim(g.id, "w", ttl_seconds=100)                       # expires at 1100
    clk["t"] = 1200                                           # past expiry, no heartbeat
    assert b.reclaim_stale() == 1
    assert b.get(g.id).status == "ready"
