"""The planner is handed a starvation order; it does not choose which stream to serve."""
import pytest

from agents.task.goals.board import GoalBoard
from agents.task.goals.planner import (
    _starved_order_with_stats,
    build_planner_prompt,
)


def starvation_order(board, user_id, objectives):
    """Objective rows in service order — the ORDER half of the single board pass
    the prompt block renders. (The `starved_objectives` wrapper this used to call
    had no non-test caller and was dropped; the ordering it encoded is the
    behaviour under test and lives in `_starved_order_with_stats`.)"""
    return [o for o, _live, _last in
            _starved_order_with_stats(board, user_id, objectives)]


@pytest.fixture
def board(tmp_path):
    return GoalBoard(str(tmp_path / "goals.db"))


def test_objective_last_activity_reports_newest_child(board):
    o = board.create_objective(user_id="u1", title="A", force=True)
    board.create(user_id="u1", title="old", parent_id=o.id, force=True)
    assert board.objective_last_activity("u1")[o.id] > 0


def test_objective_last_activity_omits_childless_objectives(board):
    o = board.create_objective(user_id="u1", title="A", force=True)
    assert o.id not in board.objective_last_activity("u1")


def test_starved_objectives_puts_the_childless_stream_first(board):
    busy = board.create_objective(user_id="u1", title="busy", priority=9, force=True)
    board.create(user_id="u1", title="b1", parent_id=busy.id, force=True)
    starved = board.create_objective(user_id="u1", title="starved", priority=1, force=True)

    order = starvation_order(board, "u1", board.objectives(user_id="u1", status="active"))
    assert [o.id for o in order] == [starved.id, busy.id]


def test_starved_objectives_breaks_ties_by_oldest_activity(tmp_path):
    """Same live-child count on both objectives — the second sort leg (oldest
    activity first) must be the discriminator, not the first (live count) or
    the third (priority), which are equal here."""
    clock = {"t": 1000.0}
    b = GoalBoard(str(tmp_path / "clocked.db"), clock=lambda: clock["t"])

    older = b.create_objective(user_id="u1", title="older activity stream", force=True)
    b.create(user_id="u1", title="older activity child", parent_id=older.id, force=True)

    clock["t"] = 2000.0
    newer = b.create_objective(user_id="u1", title="newer activity stream", force=True)
    b.create(user_id="u1", title="newer activity child", parent_id=newer.id, force=True)

    order = starvation_order(b, "u1", b.objectives(user_id="u1", status="active"))
    assert [o.id for o in order] == [older.id, newer.id]


def test_starved_objectives_breaks_ties_by_priority_when_both_childless(board):
    """Same live-child count (zero, both childless) and same activity (absent
    for both, so both default to 0.0) — the third sort leg (highest priority
    first) must be the discriminator."""
    low = board.create_objective(user_id="u1", title="low priority stream",
                                  priority=1, force=True)
    high = board.create_objective(user_id="u1", title="high priority stream",
                                   priority=9, force=True)

    order = starvation_order(board, "u1", board.objectives(user_id="u1", status="active"))
    assert [o.id for o in order] == [high.id, low.id]


def test_prompt_names_the_starved_objectives_first(board):
    busy = board.create_objective(user_id="u1", title="busy stream", force=True)
    board.create(user_id="u1", title="b1", parent_id=busy.id, force=True)
    board.create_objective(user_id="u1", title="starved stream", force=True)

    prompt = build_planner_prompt(board, "u1", None)
    assert "SERVE THESE OBJECTIVES FIRST" in prompt
    head = prompt.split("SERVE THESE OBJECTIVES FIRST", 1)[1]
    assert head.index("starved stream") < head.index("busy stream")


def test_done_children_do_not_make_a_stream_look_saturated(board):
    """The block renders "N live goal(s)" from this count and sorts on it. A
    stream that has COMPLETED a lot of work has many non-cancelled children and
    would otherwise be pushed to the bottom of an order whose whole purpose is
    to find the stream that needs work."""
    finished = board.create_objective(user_id="u1", title="finished stream", force=True)
    for i in range(3):
        g = board.create(user_id="u1", title=f"finished work {i}",
                         parent_id=finished.id, force=True)
        assert board.claim(g.id, "w", ttl_seconds=60) is not None
        board.record_success(g.id, session_id="s", result="ok")
    busy = board.create_objective(user_id="u1", title="busy stream", force=True)
    board.create(user_id="u1", title="in flight work", parent_id=busy.id, force=True)

    order = _starved_order_with_stats(board, "u1",
                                      board.objectives(user_id="u1", status="active"))
    by_id = {o.id: live for o, live, _last in order}
    assert by_id[finished.id] == 0, "done children counted as in flight"
    assert [o.id for o, _l, _a in order] == [finished.id, busy.id]

    prompt = build_planner_prompt(board, "u1", None)
    block = prompt.split("SERVE THESE OBJECTIVES FIRST", 1)[1]
    assert "finished stream] — 0 live goal(s)" in block
