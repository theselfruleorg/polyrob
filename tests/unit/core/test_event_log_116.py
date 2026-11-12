"""116 W3.1 / W3.3 / W3.5 / W1.4: telemetry cannot fail in silence, a read is
a read, and a write never holds its caller for the full retry ladder."""
import logging
import os
import sqlite3
import time

import pytest

from core import event_log as el
from core.sqlite_util import execute_retry


def test_a_dead_log_warns_counts_the_dropped_rows_and_is_tried_again(tmp_path, caplog, monkeypatch):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    bad = str(blocker / "telemetry_events.db")      # the parent is a file: init fails
    monkeypatch.setattr(el, "_INSTANCES", {})
    before = el.dropped_rows()["n"]
    with caplog.at_level(logging.WARNING, logger=el.logger.name):
        log = el.get_event_log(bad)
    assert not log._ready and "init failed" in caplog.text
    log.record("x")
    assert el.dropped_rows()["n"] == before + 1
    assert el.get_event_log(bad) is log                       # inside the retry window
    log.init_failed_at = time.time() - el.INIT_RETRY_SEC - 1
    assert el.get_event_log(bad) is not log                   # not cached for life


def test_a_failed_insert_is_counted_and_loud(tmp_path, caplog, monkeypatch):
    log = el.TelemetryEventLog(str(tmp_path / "t.db"))
    monkeypatch.setitem(el._drops, "warned_at", 0.0)
    before = el.dropped_rows()["n"]

    def boom(*a, **k):
        raise sqlite3.OperationalError("disk I/O error")
    monkeypatch.setattr(el, "execute_retry", boom)
    with caplog.at_level(logging.WARNING, logger=el.logger.name):
        log.record("x")
    got = el.dropped_rows()
    assert got["n"] == before + 1 and "disk I/O error" in got["last_error"]
    assert "telemetry row dropped" in caplog.text


def test_the_read_handle_changes_nothing(tmp_path, monkeypatch):
    path = str(tmp_path / "telemetry_events.db")
    conn = sqlite3.connect(path)         # an OLD file: no effect column, no index
    conn.execute("CREATE TABLE telemetry_events (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                 "ts REAL NOT NULL, kind TEXT NOT NULL, user_id TEXT NOT NULL DEFAULT '', "
                 "session_id TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT '', "
                 "attrs TEXT NOT NULL DEFAULT '{}')")
    conn.execute("INSERT INTO telemetry_events (ts, kind) VALUES (1.0, 'a')")
    conn.commit()
    conn.close()
    schema = sqlite3.connect(path).execute("SELECT sql FROM sqlite_master").fetchall()

    log = el.open_event_log(str(tmp_path))
    assert log.read_only and not log._has_effect
    assert [r["kind"] for r in log.query()] == ["a"] and log.query()[0]["effect"] == ""
    log.record("b")                                           # a read handle never writes
    assert log.prune(older_than_ts=time.time()) == 0
    assert len(log.query()) == 1
    assert sqlite3.connect(path).execute("SELECT sql FROM sqlite_master").fetchall() == schema


def test_query_filters_kinds_and_strict_raises(tmp_path, monkeypatch):
    log = el.TelemetryEventLog(str(tmp_path / "t.db"))
    for k in ("a", "b", "c", "a"):
        log.record(k)
    assert sorted(r["kind"] for r in log.query(kinds=("a", "c"))) == ["a", "a", "c"]
    assert log.query(kinds=()) == []

    def boom(*a, **k):
        raise sqlite3.OperationalError("database disk image is malformed")
    monkeypatch.setattr(log, "_run", boom)
    assert log.query() == []
    with pytest.raises(sqlite3.OperationalError):
        log.query(strict=True)


def test_a_write_deadline_bounds_a_contended_insert(tmp_path):
    path = str(tmp_path / "busy.db")
    execute_retry(path, "CREATE TABLE t (x)")
    holder = sqlite3.connect(path, isolation_level=None)
    holder.execute("BEGIN EXCLUSIVE")
    try:
        t0 = time.monotonic()
        with pytest.raises(sqlite3.OperationalError):
            execute_retry(path, "INSERT INTO t VALUES (1)", deadline_sec=0.4)
        assert time.monotonic() - t0 < 1.5
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert execute_retry(path, "INSERT INTO t VALUES (1)", deadline_sec=0.4) == 1
