"""FIX 1 — the cold-start requeue must never rip a goal from a LIVE claim.

``requeue_running_on_boot`` selected EVERY ``status='running'`` row and flipped it
to ``ready`` with no predicate on the claim at all, while its siblings
(``reclaim_stale`` / ``hold_running``) both guard. It runs from every
``start_autonomy()`` — every worker process and every ``rob`` REPL under
``POLYROB_LOCAL`` + ``GOALS_ENABLED`` — so a SECOND process starting against the
same ``goals.db`` requeued a goal a FIRST, still-running process was executing:
the goal could be claimed and run a second time concurrently (for a manifest
stream leg, a second concurrent ``defi_trade`` grant), and the original run's
``record_success`` CAS (``WHERE status='running'``) then missed and degraded to a
stale completion, so real completed work was never recorded done.

Boundary: a row is requeued only when its claim is NOT live — no claim, an
EXPIRED claim, or a claim whose owning process is PROVABLY gone (same-host pid).
Anything else is left for ``reclaim_stale`` once the TTL lapses.
"""
import os
import subprocess
import sys

import pytest

from agents.task.goals.board import GoalBoard, STATUS_READY, STATUS_RUNNING


@pytest.fixture()
def board(tmp_path):
    return GoalBoard(str(tmp_path / "goals.db"))


def _dead_pid() -> int:
    """A pid that certainly does not exist (spawned, exited, reaped)."""
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


def test_live_claim_is_never_requeued(board):
    """The bug: a second process booting while the first still runs the goal."""
    g = board.create(user_id="u1", title="in flight on a LIVE worker")
    assert board.claim(g.id, f"goal-dispatch-{os.getpid()}", ttl_seconds=900) is not None

    assert board.requeue_running_on_boot() == 0, \
        "a goal held by a LIVE, unexpired claim must NOT be requeued at another process's boot"
    got = board.get(g.id)
    assert got.status == STATUS_RUNNING
    assert got.claim_lock == f"goal-dispatch-{os.getpid()}"


def test_dead_owner_process_is_requeued_without_failure_increment(board):
    """The documented purpose survives: a restart does NOT cost the goal a failure."""
    g = board.create(user_id="u1", title="orphaned by a restart")
    assert board.claim(g.id, f"goal-dispatch-{_dead_pid()}", ttl_seconds=900) is not None

    assert board.requeue_running_on_boot() == 1
    got = board.get(g.id)
    assert got.status == STATUS_READY
    assert got.claim_lock is None
    assert got.consecutive_failures == 0, \
        "a process restart is NOT the goal's failure"


def test_expired_claim_is_requeued(board):
    g = board.create(user_id="u1", title="claim TTL already lapsed")
    assert board.claim(g.id, "goal-dispatch-999999", ttl_seconds=1) is not None
    # Walk the board's clock past the TTL.
    board._now = lambda: __import__("time").time() + 3600

    assert board.requeue_running_on_boot() == 1
    assert board.get(g.id).status == STATUS_READY


def test_unparseable_live_claim_is_left_for_reclaim_stale(board):
    """An owner we cannot identify is treated as possibly-live (fail-safe)."""
    g = board.create(user_id="u1", title="claimed by an unknown worker")
    assert board.claim(g.id, "some-other-producer", ttl_seconds=900) is not None

    assert board.requeue_running_on_boot() == 0
    assert board.get(g.id).status == STATUS_RUNNING


def test_requeued_on_boot_event_is_preserved(board):
    g = board.create(user_id="u1", title="orphan")
    assert board.claim(g.id, f"goal-dispatch-{_dead_pid()}", ttl_seconds=900) is not None
    board.requeue_running_on_boot()
    kinds = [e["kind"] for e in board.events(g.id)]
    assert "requeued_on_boot" in kinds
