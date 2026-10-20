"""080: a finished goal run writes ``payload.cost_usd`` for ``/goal show``."""
import logging
import os
from types import SimpleNamespace

from agents.task.goals.goal_run import GoalRunMixin


class _Board:
    def __init__(self, db_path):
        self.db_path = db_path
        self.merged = []

    def merge_payload(self, goal_id, updates):
        self.merged.append((goal_id, updates))
        return True


def _host(tmp_path):
    h = GoalRunMixin.__new__(GoalRunMixin)
    h.board = _Board(os.path.join(str(tmp_path), "goals.db"))
    return h


def _ctx(sid="s1"):
    return SimpleNamespace(session_id=sid, goal=SimpleNamespace(id="g1", session_id=None))


def test_cost_is_written_from_the_run_ledger(monkeypatch, tmp_path):
    import core.status_economics as econ
    seen = {}
    monkeypatch.setattr(econ, "run_cost_usd",
                        lambda sid, d=None: seen.update(sid=sid, d=d) or 0.42)
    h = _host(tmp_path)
    h._goal_run_record_cost(_ctx())
    assert h.board.merged == [("g1", {"cost_usd": 0.42})]
    assert seen == {"sid": "s1", "d": str(tmp_path)}


def test_unreadable_ledger_writes_nothing(monkeypatch, tmp_path):
    import core.status_economics as econ
    monkeypatch.setattr(econ, "run_cost_usd", lambda sid, d=None: None)
    h = _host(tmp_path)
    h._goal_run_record_cost(_ctx())
    assert h.board.merged == []


def test_a_failure_is_logged_not_raised(monkeypatch, tmp_path, caplog):
    import core.status_economics as econ

    def boom(sid, d=None):
        raise RuntimeError("ledger gone")
    monkeypatch.setattr(econ, "run_cost_usd", boom)
    h = _host(tmp_path)
    with caplog.at_level(logging.WARNING):
        h._goal_run_record_cost(_ctx())
    assert "could not record cost_usd" in caplog.text


def test_no_session_no_write(tmp_path):
    h = _host(tmp_path)
    h._goal_run_record_cost(_ctx(sid=None))
    assert h.board.merged == []
