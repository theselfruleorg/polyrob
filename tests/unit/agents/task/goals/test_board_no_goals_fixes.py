"""Board primitives added by the 2026-08-29 "no goals" forensics.

- ``list_recent`` / ``status_counts``: the agent-facing ``goal_list`` and the
  Telegram ``/goals`` reply read ``board.list`` — a ``priority DESC, created_at
  ASC LIMIT 100`` window that, on a 409-row prod board, showed the OLDEST 100
  rows and zero recent legs (C7). These read newest-first, tenant-scoped, in SQL.
"""
import pytest

from agents.task.goals.board import GoalBoard, STATUS_DONE, STATUS_READY


@pytest.fixture
def board(tmp_path):
    return GoalBoard(str(tmp_path / "goals.db"))


def _done(board, uid, title, **kw):
    g = board.create(user_id=uid, title=title, force=True, **kw)
    board.claim(g.id, "w", ttl_seconds=60)
    board.record_success(g.id, result="ok", claim_token=board.get(g.id).claim_token)
    return g


# --- list_recent / status_counts ---------------------------------------------

def test_list_recent_returns_newest_first_regardless_of_priority(board):
    old_high = board.create(user_id="rob", title="old high priority", priority=9, force=True)
    new_low = board.create(user_id="rob", title="new low priority", priority=2, force=True)
    rows = board.list_recent(user_id="rob", limit=10)
    assert [g.id for g in rows] == [new_low.id, old_high.id]


def test_list_recent_filters_statuses_and_honours_limit(board):
    for i in range(5):
        _done(board, "rob", f"done {i}")
    live = board.create(user_id="rob", title="still ready", force=True)
    rows = board.list_recent(user_id="rob", statuses=(STATUS_READY,), limit=10)
    assert [g.id for g in rows] == [live.id]
    assert len(board.list_recent(user_id="rob", limit=3)) == 3


def test_list_recent_is_tenant_scoped(board):
    board.create(user_id="other", title="not yours", force=True)
    mine = board.create(user_id="rob", title="mine", force=True)
    assert [g.id for g in board.list_recent(user_id="rob", limit=10)] == [mine.id]


def test_status_counts_counts_every_row_not_a_window(board):
    for i in range(3):
        _done(board, "rob", f"done {i}")
    board.create(user_id="rob", title="ready one", force=True)
    board.create_ask(user_id="rob", what="an ask")  # not a goal
    assert board.status_counts(user_id="rob") == {STATUS_DONE: 3, STATUS_READY: 1}
