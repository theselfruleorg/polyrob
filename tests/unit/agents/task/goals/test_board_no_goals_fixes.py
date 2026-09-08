"""Board primitives added by the 2026-08-29 "no goals" forensics.

- ``list_recent`` / ``status_counts``: the agent-facing ``goal_list`` and the
  Telegram ``/goals`` reply read ``board.list`` — a ``priority DESC, created_at
  ASC LIMIT 100`` window that, on a 409-row prod board, showed the OLDEST 100
  rows and zero stream legs (C7). These read newest-first, tenant-scoped, in SQL.
- persisted planner outcome + once-per-stall escalation marker: the dispatcher
  kept ``_empty_planner_runs`` / ``_empty_pipeline_escalated`` in process memory,
  and 14 restarts in 36 h re-armed the owner push every time (C5).
- ``count_created_by_session``: how many goals a planner run actually queued.
- ``has_live_goals``: running/waiting/triage legs are work in flight, not an
  empty pipeline (C4).
- a spent-objective ask closes itself once the objective is no longer spent
  (converted to a stream, budget raised, children cancelled).
"""
import pytest

from agents.task.goals.board import (
    ASK_OBSOLETE, ASK_OPEN, GoalBoard, STATUS_DONE, STATUS_READY, STATUS_RUNNING,
    STATUS_WAITING,
)


@pytest.fixture
def board(tmp_path):
    return GoalBoard(str(tmp_path / "goals.db"))


def _done(board, uid, title, **kw):
    g = board.create(user_id=uid, title=title, force=True, **kw)
    board.claim(g.id, "w", ttl_seconds=60)
    board.record_success(g.id, result="ok")
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
    board.create_objective(user_id="rob", title="an objective")  # not a goal
    assert board.status_counts(user_id="rob") == {STATUS_DONE: 3, STATUS_READY: 1}


# --- has_live_goals / count_created_by_session --------------------------------

def test_has_live_goals_sees_running_and_waiting_work(board):
    assert board.has_live_goals(user_id="rob") is False
    head = board.create(user_id="rob", title="head leg", force=True)
    board.create(user_id="rob", title="tail leg", force=True, depends_on=[head.id])
    board.claim(head.id, "w", ttl_seconds=60)  # head running, tail waiting
    assert board.has_live_goals(user_id="rob") is True
    board.record_success(head.id, result="ok")  # tail auto-readies
    assert board.has_live_goals(user_id="rob") is True


def test_has_live_goals_ignores_done_and_blocked(board):
    _done(board, "rob", "finished")
    assert board.has_live_goals(user_id="rob") is False


def test_count_created_by_session_reads_the_origin_stamp(board):
    board.create(user_id="rob", title="from planner a", force=True,
                 payload={"origin_session_id": "plan-1"})
    board.create(user_id="rob", title="from planner b", force=True,
                 payload={"origin_session_id": "plan-1"})
    board.create(user_id="rob", title="from elsewhere", force=True,
                 payload={"origin_session_id": "plan-2"})
    board.create(user_id="rob", title="unstamped", force=True)
    assert board.count_created_by_session("plan-1") == 2
    assert board.count_created_by_session("nope") == 0


# --- persisted planner outcome + stall marker ---------------------------------

def test_consecutive_empty_planner_runs_counts_the_trailing_streak(board):
    assert board.consecutive_empty_planner_runs() == 0
    board.mark_planner_outcome(queued=0)
    board.mark_planner_outcome(queued=0)
    assert board.consecutive_empty_planner_runs() == 2
    board.mark_planner_outcome(queued=2)
    assert board.consecutive_empty_planner_runs() == 0
    board.mark_planner_outcome(queued=0)
    assert board.consecutive_empty_planner_runs() == 1


def test_empty_streak_started_at_is_the_first_empty_run_of_the_streak(board):
    clock = {"t": 1000.0}
    b = GoalBoard(board.db_path, clock=lambda: clock["t"])
    b.mark_planner_outcome(queued=1)
    clock["t"] = 2000.0
    b.mark_planner_outcome(queued=0)
    clock["t"] = 3000.0
    b.mark_planner_outcome(queued=0)
    assert b.empty_streak_started_at() == 2000.0
    b.mark_planner_outcome(queued=3)
    assert b.empty_streak_started_at() is None


def test_stall_escalation_marker_survives_a_new_board_instance(board):
    """The marker is a durable event, so a process restart cannot re-arm the push."""
    clock = {"t": 1000.0}
    b = GoalBoard(board.db_path, clock=lambda: clock["t"])
    b.mark_planner_outcome(queued=0)
    clock["t"] = 1100.0
    b.mark_planner_outcome(queued=0)
    assert b.stall_escalated_since(b.empty_streak_started_at()) is False
    clock["t"] = 1200.0
    b.mark_stall_escalated()
    fresh = GoalBoard(board.db_path, clock=lambda: 5000.0)  # "after restart"
    assert fresh.stall_escalated_since(fresh.empty_streak_started_at()) is True


def test_stall_marker_from_an_older_streak_does_not_count(board):
    clock = {"t": 1000.0}
    b = GoalBoard(board.db_path, clock=lambda: clock["t"])
    b.mark_planner_outcome(queued=0)
    b.mark_stall_escalated()               # escalated for streak #1
    clock["t"] = 2000.0
    b.mark_planner_outcome(queued=2)       # board refilled -> streak ends
    clock["t"] = 3000.0
    b.mark_planner_outcome(queued=0)       # streak #2 starts
    assert b.stall_escalated_since(b.empty_streak_started_at()) is False


# --- spent-objective ask closes itself --------------------------------------

def test_spent_ask_is_obsoleted_once_the_objective_becomes_a_stream(board, monkeypatch):
    monkeypatch.setenv("OBJECTIVE_GOAL_BUDGET", "2")
    obj = board.create_objective(user_id="rob", title="Promote in public")
    for i in range(2):
        board.create(user_id="rob", title=f"child {i}", parent_id=obj.id, force=True)
    assert board.escalate_spent_objectives(user_id="rob")
    (ask,) = board.asks(user_id="rob", status=ASK_OPEN)

    board.merge_payload(obj.id, {"stream_id": "promote"})  # adopted by the manifest
    closed = board.escalate_spent_objectives(user_id="rob")

    assert closed == []
    assert board.asks(user_id="rob", status=ASK_OPEN) == []
    reopened = board.get(ask.id)
    assert reopened.status == ASK_OBSOLETE
    assert (reopened.payload or {}).get("obsolete_reason") == "objective_no_longer_spent"


def test_spent_ask_stays_open_while_the_objective_is_still_spent(board, monkeypatch):
    monkeypatch.setenv("OBJECTIVE_GOAL_BUDGET", "1")
    obj = board.create_objective(user_id="rob", title="Ship software")
    board.create(user_id="rob", title="only child", parent_id=obj.id, force=True)
    board.escalate_spent_objectives(user_id="rob")
    board.escalate_spent_objectives(user_id="rob")
    assert len(board.asks(user_id="rob", status=ASK_OPEN)) == 1
