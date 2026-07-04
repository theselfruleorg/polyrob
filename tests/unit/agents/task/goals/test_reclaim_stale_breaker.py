"""H12: reclaim_stale returned a crashed goal (expired claim) to 'ready' with no
consecutive_failures bump. record_failure (the only path that trips the circuit
breaker) never runs when the goal KILLS its worker process (OOM/segfault/SIGKILL), so a
poison-pill goal crash-loops forever, never reaching 'blocked'. reclaim_stale must count
the crash as a failure and trip the breaker at max_retries.
"""
from agents.task.goals.board import GoalBoard, STATUS_READY, STATUS_RUNNING, STATUS_BLOCKED


class _Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


def test_reclaim_stale_bumps_failures_and_blocks_crash_loop(tmp_path):
    clock = _Clock()
    b = GoalBoard(str(tmp_path / "goals.db"), clock=clock)
    g = b.create(user_id="u1", title="poison", max_retries=1)
    b.claim(g.id, "w1", ttl_seconds=100)
    assert b.get(g.id).status == STATUS_RUNNING

    clock.advance(200)  # claim TTL expired — worker crashed without recording a failure
    n = b.reclaim_stale()
    assert n == 1

    got = b.get(g.id)
    assert got.consecutive_failures == 1
    assert got.status == STATUS_BLOCKED  # max_retries=1 reached -> breaker tripped


def test_reclaim_stale_requeues_below_threshold(tmp_path):
    clock = _Clock()
    b = GoalBoard(str(tmp_path / "goals.db"), clock=clock)
    g = b.create(user_id="u1", title="transient", max_retries=3)
    b.claim(g.id, "w1", ttl_seconds=100)

    clock.advance(200)
    b.reclaim_stale()

    got = b.get(g.id)
    assert got.consecutive_failures == 1
    assert got.status == STATUS_READY  # still below threshold -> retry
