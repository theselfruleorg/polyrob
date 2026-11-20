"""Goal create is one transaction (proposal 125 S17, B-H8).

The row, its ``created`` event and its dependency edges were separate writes: an
edge write that failed left a goal with no edges — ``waiting`` on nothing, or
``ready`` while its prerequisite was still open.
"""
import sqlite3

import pytest

from modules.goals.board import DuplicateGoalError, GoalBoard


def test_a_failed_edge_write_leaves_no_goal_row(tmp_path):
    board = GoalBoard(str(tmp_path / "goals.db"))
    dep = board.create(user_id="u", title="prepare the data")
    conn = sqlite3.connect(board.db_path)
    conn.execute("CREATE TRIGGER no_edges BEFORE INSERT ON goal_edges "
                 "BEGIN SELECT RAISE(ABORT, 'edge refused'); END")
    conn.commit()
    conn.close()
    with pytest.raises(sqlite3.DatabaseError):
        board.create(user_id="u", title="publish the report", depends_on=[dep.id])
    assert [g.id for g in board.list(user_id="u")] == [dep.id]
    n = sqlite3.connect(board.db_path).execute(
        "SELECT COUNT(*) FROM goal_events WHERE goal_id != ?", (dep.id,)).fetchone()[0]
    assert n == 0


def test_create_with_deps_writes_row_edges_and_events(tmp_path):
    board = GoalBoard(str(tmp_path / "goals.db"))
    dep = board.create(user_id="u", title="prepare the data")
    g = board.create(user_id="u", title="publish the report", depends_on=[dep.id])
    assert board.get(g.id).status == "waiting"
    kinds = [e["kind"] for e in board.events(g.id)]
    assert kinds == ["created", "waiting_on_deps"]


def test_a_duplicate_writes_no_row_and_records_the_rejection(tmp_path, monkeypatch):
    monkeypatch.setenv("GOAL_DEDUP_THRESHOLD", "0.8")
    board = GoalBoard(str(tmp_path / "goals.db"))
    first = board.create(user_id="u", title="write the weekly report")
    with pytest.raises(DuplicateGoalError):
        board.create(user_id="u", title="write the weekly report")
    assert len(board.list(user_id="u")) == 1
    assert "dedup_rejected" in [e["kind"] for e in board.events(first.id)]
