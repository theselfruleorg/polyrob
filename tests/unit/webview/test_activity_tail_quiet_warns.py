"""The live activity tail: a store error that is not "absent" still answers []
(it retries next tick) but is logged as a warning, once per table per period."""
import logging
import sqlite3

import webview.activity as act


def _locked_tail(tmp_path, monkeypatch):
    db = tmp_path / "goals.db"
    sqlite3.connect(db).execute("CREATE TABLE goal_events (id INTEGER PRIMARY KEY)").connection.commit()
    tail = act.SqliteTail(str(db), "goal_events")

    def _boom(sql, params=()):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(tail, "_query", _boom)
    monkeypatch.setattr(act, "_QUIET_WARNED", {})
    return tail


def test_a_live_tail_error_warns_once_per_period(tmp_path, monkeypatch, caplog):
    tail = _locked_tail(tmp_path, monkeypatch)
    clock = [1000.0]
    monkeypatch.setattr(act.time, "monotonic", lambda: clock[0])
    with caplog.at_level(logging.WARNING, logger=act.logger.name):
        assert tail.poll() == []
        assert tail.poll() == []
    warns = [r for r in caplog.records if "goal_events" in r.getMessage()]
    assert len(warns) == 1
    clock[0] += act._QUIET_WARN_PERIOD_S + 1
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger=act.logger.name):
        assert tail.poll() == []
    assert [r for r in caplog.records if "goal_events" in r.getMessage()]


def test_an_absent_table_stays_silent(tmp_path, monkeypatch, caplog):
    tail = act.SqliteTail(str(tmp_path / "missing.db"), "goal_events")
    monkeypatch.setattr(act, "_QUIET_WARNED", {})
    with caplog.at_level(logging.WARNING, logger=act.logger.name):
        assert tail.poll() == []
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
