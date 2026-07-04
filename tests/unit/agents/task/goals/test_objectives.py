import os
import sqlite3

import pytest

from agents.task.goals.board import (
    GoalBoard, KIND_GOAL, KIND_OBJECTIVE,
    OBJ_ACTIVE, OBJ_PAUSED, OBJ_DROPPED,
)


@pytest.fixture
def board(tmp_path):
    return GoalBoard(str(tmp_path / "goals.db"))


def test_create_objective_active_and_kind(board):
    o = board.create_objective(user_id="rob", title="Get 100k followers on X")
    assert o.kind == KIND_OBJECTIVE
    assert o.status == OBJ_ACTIVE


def test_goal_default_kind_is_goal(board):
    g = board.create(user_id="rob", title="Draft a thread")
    assert g.kind == KIND_GOAL


def test_ready_never_returns_objectives(board):
    board.create_objective(user_id="rob", title="Earn money")
    g = board.create(user_id="rob", title="Draft a thread")
    ids = [x.id for x in board.ready(limit=10)]
    assert ids == [g.id]


def test_ready_guard_even_if_objective_status_forced_to_ready(board):
    o = board.create_objective(user_id="rob", title="Earn money")
    # simulate corruption / bad manual edit
    import core.sqlite_util as squ
    squ.execute_retry(board.db_path, "UPDATE goals SET status='ready' WHERE id=?", (o.id,))
    assert board.ready(limit=10) == []


def test_objectives_list_and_status_filter(board):
    a = board.create_objective(user_id="rob", title="A")
    b = board.create_objective(user_id="rob", title="B")
    board.set_objective_status(b.id, OBJ_PAUSED)
    active = board.objectives(user_id="rob", status=OBJ_ACTIVE)
    assert [o.id for o in active] == [a.id]
    assert {o.id for o in board.objectives(user_id="rob")} == {a.id, b.id}


def test_set_objective_status_validates(board):
    o = board.create_objective(user_id="rob", title="A")
    g = board.create(user_id="rob", title="g")
    assert board.set_objective_status(o.id, OBJ_DROPPED) is True
    with pytest.raises(ValueError):
        board.set_objective_status(o.id, "running")  # goal-only status
    assert board.set_objective_status(g.id, OBJ_PAUSED) is False  # not an objective


def test_children(board):
    o = board.create_objective(user_id="rob", title="A")
    g1 = board.create(user_id="rob", title="g1", parent_id=o.id)
    g2 = board.create(user_id="rob", title="g2 totally different words", parent_id=o.id)
    assert [c.id for c in board.children(o.id)] == [g1.id, g2.id]


def test_migration_adds_kind_to_prekind_db(tmp_path):
    db = str(tmp_path / "goals.db")
    conn = sqlite3.connect(db)
    # minimal pre-kind schema (subset of prod columns, no `kind`)
    conn.executescript(
        """
        CREATE TABLE goals (
            id TEXT PRIMARY KEY, user_id TEXT NOT NULL, title TEXT NOT NULL,
            body TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'ready',
            priority INTEGER NOT NULL DEFAULT 5, parent_id TEXT,
            claim_lock TEXT, claim_expires REAL,
            consecutive_failures INTEGER NOT NULL DEFAULT 0,
            max_retries INTEGER NOT NULL DEFAULT 2, last_failure_error TEXT,
            session_id TEXT, result TEXT, payload TEXT NOT NULL DEFAULT '{}',
            created_at REAL NOT NULL, started_at REAL, completed_at REAL,
            last_heartbeat_at REAL
        );
        """
    )
    conn.execute(
        "INSERT INTO goals (id,user_id,title,created_at) VALUES ('old1','rob','legacy',1.0)")
    conn.commit(); conn.close()
    b = GoalBoard(db)  # must not raise; must migrate
    g = b.get("old1")
    assert g.kind == KIND_GOAL


def test_cancel_refuses_objectives(board):
    o = board.create_objective(user_id="rob", title="Uncancellable mission")
    assert board.cancel(o.id) is False
    assert board.get(o.id).status == "active"
