"""`/run` — list, pause, resume and stop ONE background run (core.run_control).

The ONE implementation behind Telegram `/run` and the terminal `/run` (070
decision 1, 2026-10-01): the rows come from the live-status reader, the control
goes through the durable per-session mailbox (``core.session_control``) that the
agent acknowledges at a step boundary.
"""
import asyncio
import json
import os
import sqlite3
import threading
import time

import pytest

from core import run_control
from core.session_control import SessionControl


def _registry(data_dir, rows):
    conn = sqlite3.connect(os.path.join(data_dir, "session_registry.db"))
    conn.execute("CREATE TABLE active_sessions (session_id TEXT, worker_pid INTEGER, "
                 "created_at TEXT, last_seen_at TEXT)")
    conn.executemany("INSERT INTO active_sessions VALUES (?,?,?,?)", rows)
    conn.commit()
    conn.close()


def _goals(data_dir, rows):
    conn = sqlite3.connect(os.path.join(data_dir, "goals.db"))
    conn.execute("CREATE TABLE goals (id TEXT, title TEXT, started_at REAL, session_id TEXT, "
                 "status TEXT, kind TEXT, user_id TEXT)")
    conn.executemany("INSERT INTO goals VALUES (?,?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()


def _session(root, user, sid, task, creator="cron"):
    d = os.path.join(root, user, sid)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "task.json"), "w") as fh:
        json.dump({"task": task, "creator": creator}, fh)
    return d


@pytest.fixture
def home(tmp_path):
    data = tmp_path / "data"
    roots = tmp_path / "sessions"
    data.mkdir()
    roots.mkdir()
    pid = os.getpid()
    _registry(str(data), [
        ("s-job", pid, "2026-10-01T10:00:00", "2026-10-01T10:05:00"),
        ("s-goal", pid, "2026-10-01T11:00:00", "2026-10-01T11:05:00"),
        ("s-chat", pid, "2026-10-01T09:00:00", "2026-10-01T09:05:00"),
        ("s-other-user", pid, "2026-10-01T09:00:00", "2026-10-01T09:05:00"),
    ])
    _goals(str(data), [("g1", "Safety monitor", 1.0, "s-goal", "running", "goal", "alice")])
    _session(str(roots), "alice", "s-job", "Check the treasury\nmore", creator="cron")
    _session(str(roots), "alice", "s-goal", "goal body", creator="goal")
    _session(str(roots), "alice", "s-chat", "hi", creator="telegram")
    _session(str(roots), "bob", "s-other-user", "not alice's")
    return str(data), str(roots)


def test_lists_runs_oldest_first_and_excludes_the_asking_chat(home):
    data, roots = home
    rows, readable = run_control.list_runs("alice", data, roots, exclude=("s-chat",))
    assert readable
    assert [r["session_id"] for r in rows] == ["s-job", "s-goal"]
    assert rows[0]["title"] == "Check the treasury"
    assert rows[1]["title"] == "Safety monitor"      # the goal title wins
    assert rows[1]["kind"] == "goal" and rows[0]["kind"] == "job"


def test_list_reply_numbers_the_runs(home):
    data, roots = home
    out = asyncio.run(run_control.run_reply("alice", data, roots, [], exclude=("s-chat",)))
    assert "1. " in out and "Check the treasury" in out
    assert "2. " in out and "Safety monitor" in out
    assert "/run stop 2" in out
    assert "not alice's" not in out


def test_nothing_running(tmp_path):
    data = tmp_path / "d"
    roots = tmp_path / "r"
    data.mkdir()
    roots.mkdir()
    _registry(str(data), [])
    out = asyncio.run(run_control.run_reply("alice", str(data), str(roots), []))
    assert out == run_control.NOTHING_RUNS


def test_unreadable_registry_is_not_nothing(tmp_path):
    out = asyncio.run(run_control.run_reply("alice", str(tmp_path), str(tmp_path), []))
    assert out == run_control.UNREADABLE


def test_bad_usage_and_bad_number(home):
    data, roots = home
    assert asyncio.run(run_control.run_reply("alice", data, roots, ["fly", "1"])) \
        .startswith("Usage: /run")
    out = asyncio.run(run_control.run_reply("alice", data, roots, ["stop", "9"],
                                            exclude=("s-chat",)))
    assert "There is no run 9" in out


def _ack_when_requested(directory, generation, state, stop):
    store = SessionControl(directory)
    while not stop.is_set():
        row = store.read()
        if row and row["acknowledged"] == "pending":
            store.acknowledge(generation, row["request"], state)
            return
        time.sleep(0.02)


@pytest.mark.parametrize("word,op,state,said", [
    ("pause", "pause", "paused", "Paused"),
    ("resume", "resume", "running", "Running again"),
    ("stop", "cancel", "cancelled", "Stopped"),
])
def test_control_writes_the_mailbox_and_reports_the_ack(home, word, op, state, said):
    data, roots = home
    directory = os.path.join(roots, "alice", "s-job")
    store = SessionControl(directory)
    generation = store.start()
    stop = threading.Event()
    t = threading.Thread(target=_ack_when_requested, args=(directory, generation, state, stop))
    t.start()
    try:
        out = asyncio.run(run_control.run_reply("alice", data, roots, [word, "1"],
                                                exclude=("s-chat",)))
    finally:
        stop.set()
        t.join()
    assert store.read()["request"] == op
    assert said in out and "Check the treasury" in out


def test_control_without_ack_says_it_waits_for_a_step(home, monkeypatch):
    data, roots = home
    directory = os.path.join(roots, "alice", "s-goal")
    SessionControl(directory).start()
    monkeypatch.setattr(run_control, "ACK_WAIT_S", 0.1)
    out = asyncio.run(run_control.run_reply("alice", data, roots, ["pause", "2"],
                                            exclude=("s-chat",)))
    assert "next step" in out


def test_a_run_with_no_live_control_is_named(home):
    data, roots = home
    out = asyncio.run(run_control.run_reply("alice", data, roots, ["stop", "1"],
                                            exclude=("s-chat",)))
    assert "cannot be controlled" in out


def test_session_id_prefix_works_too(home):
    data, roots = home
    directory = os.path.join(roots, "alice", "s-goal")
    SessionControl(directory).start()
    out = asyncio.run(run_control.run_reply("alice", data, roots, ["stop", "s-goal"],
                                            exclude=("s-chat",)))
    assert SessionControl(directory).read()["request"] == "cancel"
    assert "Safety monitor" in out
