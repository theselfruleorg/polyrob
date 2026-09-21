"""057 WS-C — a deferral is a fact, and an owner turn in ANOTHER process is one.

A5: the dispatcher gated only on ``is_interactive_busy()``, an IN-PROCESS
counter. An owner turn live in the Telegram surface, the console or another
`rob` REPL was invisible to it, so a goal could start mutating the shared
workspace underneath a human turn — the same two-homes class 057 R3 names.

A6: a deferral was journald-only. It is now a ``goal_run deferred`` event with a
typed reason, emitted once per deferral EDGE (the ticker fires every 60 s).
"""
import asyncio

import pytest

from agents.task.goals.board import GoalBoard
from agents.task.goals.dispatcher import GoalDispatcher


@pytest.fixture
def evlog(tmp_path, monkeypatch):
    import core.event_log as el
    monkeypatch.setattr(el, "_INSTANCES", {})
    log = el.TelemetryEventLog(str(tmp_path / "te.db"))
    monkeypatch.setattr(el, "get_event_log", lambda *a, **k: log)
    return log


def _deferrals(log):
    return [r for r in log.query(kind="goal_run")
            if r["attrs"].get("outcome") == "deferred"]


@pytest.mark.asyncio
async def test_a_live_owner_turn_in_another_process_defers_dispatch(
        tmp_path, monkeypatch, evlog):
    monkeypatch.setenv("GOALS_ENABLED", "true")
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    board = GoalBoard(str(tmp_path / "goals.db"))
    board.create(user_id="rob", title="Round 15", priority=5)
    disp = GoalDispatcher(board, None, lock_path=str(tmp_path / "t.lock"))

    from core import interactive_gate as g
    g._busy_depth = 0
    # Written by ANOTHER process: this one's busy counter is zero, which is
    # exactly the gap — the in-process gate sees nothing.
    g._write_turn_marker("owner_chat", "s1")
    assert g.is_interactive_busy() is False
    try:
        assert await disp.dispatch_once() == 0
    finally:
        g._clear_turn_marker()

    rows = _deferrals(evlog)
    assert [r["attrs"]["reason"] for r in rows] == ["owner_turn"]
    assert board.get(board.ready(limit=1)[0].id).status == "ready", \
        "the goal waits; it is not failed or claimed"


@pytest.mark.asyncio
async def test_the_deferral_event_fires_once_per_edge_not_once_per_tick(
        tmp_path, monkeypatch, evlog):
    monkeypatch.setenv("GOALS_ENABLED", "true")
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    board = GoalBoard(str(tmp_path / "goals.db"))
    board.create(user_id="rob", title="Round 15", priority=5)
    disp = GoalDispatcher(board, None, lock_path=str(tmp_path / "t.lock"))

    from core import interactive_gate as g
    g._busy_depth = 0
    g._write_turn_marker("owner_chat", "s1")
    try:
        for _ in range(5):
            await disp.dispatch_once()
    finally:
        g._clear_turn_marker()

    assert len(_deferrals(evlog)) == 1, \
        "a 60 s ticker must not bury the event log under one stalled minute"


@pytest.mark.asyncio
async def test_an_imminent_cron_job_deferral_is_named_and_counted(
        tmp_path, monkeypatch, evlog):
    from datetime import datetime, timedelta

    from cron.jobs import CronJob, CronJobStore
    monkeypatch.setenv("GOALS_ENABLED", "true")
    monkeypatch.setenv("GOAL_DISPATCH_CRON_HEADROOM_SEC", "600")
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    store = CronJobStore(str(tmp_path / "cron.db"))
    store.add(CronJob(id="ops00001", task="LOG ROTATE", schedule_spec="every 2h",
                      user_id="rob", next_run_at=datetime.now() + timedelta(minutes=2),
                      payload={}))
    board = GoalBoard(str(tmp_path / "goals.db"))
    g0 = board.create(user_id="rob", title="Round 15", priority=5)
    disp = GoalDispatcher(board, None, lock_path=str(tmp_path / "t.lock"))

    from core import interactive_gate as g
    g._busy_depth = 0
    assert await disp.dispatch_once() == 0

    rows = _deferrals(evlog)
    assert len(rows) == 1
    assert rows[0]["attrs"]["reason"] == "headroom:ops00001"
    assert rows[0]["attrs"]["goal_id"] == g0.id
    assert rows[0]["attrs"]["ready"] == 1
