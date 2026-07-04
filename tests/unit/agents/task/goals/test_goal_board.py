"""W4 — durable goal board: atomic claim, circuit breaker, reclaim, tenant scope."""
import pytest

from agents.task.goals.board import (
    GoalBoard, STATUS_READY, STATUS_RUNNING, STATUS_BLOCKED, STATUS_DONE, STATUS_CANCELLED,
)


class _Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


@pytest.fixture
def board(tmp_path):
    return GoalBoard(str(tmp_path / "goals.db"))


def test_create_requires_user_id(board):
    with pytest.raises(ValueError):
        board.create(user_id="", title="x")


def test_create_and_get(board):
    g = board.create(user_id="u1", title="ship it", body="do the thing", priority=7)
    got = board.get(g.id)
    assert got.title == "ship it"
    assert got.status == STATUS_READY
    assert got.priority == 7
    assert got.user_id == "u1"


def test_claim_is_atomic_single_winner(tmp_path):
    # Two boards on the SAME db (simulating two workers/processes).
    db = str(tmp_path / "goals.db")
    b1 = GoalBoard(db)
    b2 = GoalBoard(db)
    g = b1.create(user_id="u1", title="contended")
    c1 = b1.claim(g.id, "w1", ttl_seconds=900)
    c2 = b2.claim(g.id, "w2", ttl_seconds=900)
    assert (c1 is None) ^ (c2 is None), "exactly one worker must win the claim"
    winner = c1 or c2
    assert winner.status == STATUS_RUNNING


def test_record_success_resets_and_completes(board):
    g = board.create(user_id="u1", title="t")
    board.claim(g.id, "w", ttl_seconds=900)
    board.record_success(g.id, session_id="s1", result="ok")
    got = board.get(g.id)
    assert got.status == STATUS_DONE
    assert got.result == "ok"
    assert got.consecutive_failures == 0


def test_circuit_breaker_trips_to_blocked(board):
    g = board.create(user_id="u1", title="flaky", max_retries=2)
    # first failure -> back to ready
    board.claim(g.id, "w", ttl_seconds=900)
    after1 = board.record_failure(g.id, error="boom1")
    assert after1.status == STATUS_READY
    assert after1.consecutive_failures == 1
    # second failure -> hits max_retries -> blocked
    board.claim(g.id, "w", ttl_seconds=900)
    after2 = board.record_failure(g.id, error="boom2")
    assert after2.status == STATUS_BLOCKED
    assert after2.consecutive_failures == 2
    kinds = [e["kind"] for e in board.events(g.id)]
    assert "gave_up" in kinds


def test_success_after_failure_resets_counter(board):
    g = board.create(user_id="u1", title="t", max_retries=3)
    board.claim(g.id, "w", ttl_seconds=900)
    board.record_failure(g.id, error="x")
    board.claim(g.id, "w", ttl_seconds=900)
    board.record_success(g.id, result="recovered")
    assert board.get(g.id).consecutive_failures == 0


def test_reclaim_stale_requeues_expired_claim(tmp_path):
    clk = _Clock()
    board = GoalBoard(str(tmp_path / "goals.db"), clock=clk)
    g = board.create(user_id="u1", title="t")
    board.claim(g.id, "w", ttl_seconds=100)
    assert board.get(g.id).status == STATUS_RUNNING
    # before TTL: not reclaimed
    clk.advance(50)
    assert board.reclaim_stale() == 0
    # after TTL: reclaimed to ready
    clk.advance(60)
    assert board.reclaim_stale() == 1
    assert board.get(g.id).status == STATUS_READY


def test_tenant_scoped_list_and_cancel(board):
    g1 = board.create(user_id="u1", title="a")
    board.create(user_id="u2", title="b")
    u1 = board.list(user_id="u1")
    assert [g.title for g in u1] == ["a"]
    # cancel scoped: wrong tenant can't cancel
    assert board.cancel(g1.id, user_id="u2") is False
    assert board.cancel(g1.id, user_id="u1") is True
    assert board.get(g1.id).status == STATUS_CANCELLED


def test_ready_ordered_by_priority(board):
    board.create(user_id="u1", title="low", priority=1)
    board.create(user_id="u1", title="high", priority=9)
    ready = board.ready(limit=10)
    assert ready[0].title == "high"
