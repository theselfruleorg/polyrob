"""058 follow-up — `polyrob update` perceives the agent's state and waits for
the wrap-up instead of refusing on sight.

The signals are the SAME ones scripts/deploy_when_idle.sh reads before a prod
deploy: a live human turn (`<data>/locks/turn.active`), a goal `running`, a
cron job `running` or due within the imminent window, the workspace turn lock,
a write-locked DB. An UNREADABLE store is busy, never idle (a reader deciding
whether to swap code under an agent may not guess). A resident server PROCESS
is a different fact: waiting does not end it, so it is reported separately."""
import json
import os
import sqlite3
import time
from pathlib import Path

import pytest

from cli.update import agent_state as st


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("POLYROB_WORKSPACE_LOCK_DIR", raising=False)
    (tmp_path / "locks").mkdir()
    return tmp_path


def _goals(home, statuses):
    db = home / "goals.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE goals (id TEXT, status TEXT, user_id TEXT)")
    con.executemany("INSERT INTO goals VALUES (?, ?, 'u')", [(f"g{i}", s) for i, s in enumerate(statuses)])
    con.commit(); con.close()


def _cron(home, rows):
    db = home / "cron.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE cron_jobs (id TEXT, status TEXT, enabled INTEGER, next_run_at TEXT)")
    con.executemany("INSERT INTO cron_jobs VALUES (?, ?, ?, ?)", rows)
    con.commit(); con.close()


def test_empty_home_is_idle(home):
    a = st.observe(home)
    assert a.idle and a.reasons == [] and a.unreadable == []


def test_live_turn_marker_is_busy_and_named(home):
    (home / "locks" / "turn.active").write_text(json.dumps(
        {"pid": os.getpid(), "kind": "owner_chat", "session_id": "abc123", "started": time.time() - 42}))
    a = st.observe(home)
    assert not a.idle
    # ``started`` is the key the real writer stamps (core.interactive_gate);
    # the age must render from it, not from a key nothing writes.
    assert any("owner_chat" in r and "abc123" in r and " s)" in r for r in a.reasons)


def test_marker_without_a_positive_pid_is_not_a_turn(home):
    # os.kill(0, 0) signals our own process group and succeeds; a pid-less
    # marker must read as "no turn" (the shell reader's `-gt 0`), not busy.
    (home / "locks" / "turn.active").write_text(json.dumps({"pid": 0, "kind": "owner_chat"}))
    assert st.observe(home).idle


def test_dead_pid_marker_is_not_a_turn(home):
    (home / "locks" / "turn.active").write_text(json.dumps({"pid": 999999999, "kind": "owner_chat"}))
    assert st.observe(home).idle


def test_running_goal_is_busy_done_goal_is_not(home):
    _goals(home, ["done", "ready"])
    assert st.observe(home).idle
    os.remove(home / "goals.db"); _goals(home, ["running"])
    a = st.observe(home)
    assert not a.idle and any("goal" in r for r in a.reasons)


def test_cron_running_or_imminent_is_busy(home):
    soon = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() + 30))
    later = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() + 3600))
    _cron(home, [("a", "scheduled", 1, later)])
    assert st.observe(home, cron_imminent_sec=120).idle
    os.remove(home / "cron.db"); _cron(home, [("a", "scheduled", 1, soon)])
    a = st.observe(home, cron_imminent_sec=120)
    assert not a.idle and any("due" in r for r in a.reasons)
    os.remove(home / "cron.db"); _cron(home, [("a", "running", 1, later)])
    assert any("running" in r for r in st.observe(home).reasons)


def test_unreadable_store_is_busy_not_idle(home):
    (home / "goals.db").write_bytes(b"not a database" * 10)
    a = st.observe(home)
    assert not a.idle and a.unreadable and "goals.db" in a.unreadable[0]


def test_wait_returns_when_the_agent_wraps_up(home, monkeypatch):
    _goals(home, ["running"])
    ticks = []

    def fake_sleep(_s):
        ticks.append(1)
        if len(ticks) == 2:
            os.remove(home / "goals.db"); _goals(home, ["done"])

    monkeypatch.setattr(st.time, "sleep", fake_sleep)
    seen = []
    a = st.wait_for_wrap_up(home, timeout=60, poll=1, on_tick=lambda act, waited: seen.append(waited))
    assert a.idle and len(ticks) == 2 and seen  # reported what it waited on


def test_wait_times_out_still_busy(home, monkeypatch):
    _goals(home, ["running"])
    clock = [0.0]
    monkeypatch.setattr(st.time, "sleep", lambda s: clock.__setitem__(0, clock[0] + s))
    monkeypatch.setattr(st.time, "monotonic", lambda: clock[0])
    a = st.wait_for_wrap_up(home, timeout=5, poll=1)
    assert not a.idle and a.waited >= 5
