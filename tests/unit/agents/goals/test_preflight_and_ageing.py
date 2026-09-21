"""057 WS-C — do not start work a rail will pre-empt; let a yielded goal climb.

B10 (056 WS5 2b, never built until now): the headroom rule bounds a goal's START
by a FIXED window and knows nothing about how long THAT goal takes. Prod
2026-09-19 started a 14-step goal three minutes before the hourly rail: the goal
lost its run and the rail waited anyway.

B11: a goal pre-empted repeatedly queues behind fresh work forever (one prod
goal yielded 6 times in one night).
"""
from datetime import datetime, timedelta

import pytest

from agents.task.goals.board import GoalBoard
from agents.task.goals.dispatcher import GoalDispatcher
from agents.task.goals.preflight import (crossing_rail, expected_run_seconds,
                                         next_preempting_rail, p95)

NOW = datetime(2026, 9, 20, 13, 0, 0)


def _cron(tmp_path, *, when, payload):
    from cron.jobs import CronJob, CronJobStore
    store = CronJobStore(str(tmp_path / "cron.db"))
    store.add(CronJob(id="exit0001", task="EXIT RAIL", schedule_spec="0 * * * *",
                      user_id="rob", next_run_at=when, payload=payload))
    return store


def test_p95_is_nearest_rank_not_interpolated():
    assert p95([]) is None
    assert p95([10.0]) == 10.0
    assert p95([1.0, 2.0, 30.0]) == 30.0, \
        "with three samples the honest p95 is the slowest one"


def test_next_preempting_rail_skips_a_non_preempting_job(tmp_path):
    _cron(tmp_path, when=NOW + timedelta(minutes=5), payload={"priority": "ops"})
    assert next_preempting_rail(str(tmp_path), NOW) is None


def test_next_preempting_rail_names_the_money_rail(tmp_path):
    _cron(tmp_path, when=NOW + timedelta(minutes=5), payload={"priority": "money"})
    hit = next_preempting_rail(str(tmp_path), NOW)
    assert hit is not None and hit[0] == "exit0001"


def test_an_unmeasured_goal_uses_the_fallback_and_says_so(tmp_path):
    est, measured = expected_run_seconds("g-new", 14, fallback_step_sec=45,
                                         max_run_sec=1800, data_dir=str(tmp_path))
    assert est == 14 * 45 and measured is False


def test_the_estimate_is_clamped_by_the_wall_clock_cap(tmp_path):
    est, _ = expected_run_seconds("g-new", 60, fallback_step_sec=45,
                                  max_run_sec=600, data_dir=str(tmp_path))
    assert est == 600, "a goal cannot be expected to run past its own hard cap"


def test_a_goal_that_would_run_into_the_rail_is_named(tmp_path):
    _cron(tmp_path, when=NOW + timedelta(minutes=3), payload={"priority": "money"})
    assert crossing_rail("g-14", 14, data_dir=str(tmp_path), now=NOW,
                         fallback_step_sec=45, max_run_sec=1800) == "exit0001"


def test_a_short_goal_clears_the_rail(tmp_path):
    _cron(tmp_path, when=NOW + timedelta(minutes=30), payload={"priority": "money"})
    assert crossing_rail("g-2", 2, data_dir=str(tmp_path), now=NOW,
                         fallback_step_sec=45, max_run_sec=1800) is None


def test_an_absent_cron_store_never_refuses(tmp_path):
    """A pre-flight that refuses on ignorance stops the board."""
    assert crossing_rail("g-14", 14, data_dir=str(tmp_path / "nope"), now=NOW,
                         fallback_step_sec=45, max_run_sec=1800) is None


def test_measured_history_beats_the_fallback(tmp_path, monkeypatch):
    import core.event_log as el
    monkeypatch.setattr(el, "_INSTANCES", {})
    log = el.TelemetryEventLog(str(tmp_path / "telemetry_events.db"))
    monkeypatch.setattr(el, "get_event_log", lambda *a, **k: log)
    # Two prior runs at ~5 s/step — far under the 45 s fallback.
    for _ in range(2):
        log.record("goal_run", user_id="rob", source="goal",
                   attrs={"goal_id": "g-fast", "outcome": "done",
                          "steps": 10, "duration_sec": 50.0})
    est, measured = expected_run_seconds("g-fast", 14, fallback_step_sec=45,
                                         max_run_sec=1800, data_dir=str(tmp_path))
    assert measured is True and est == pytest.approx(14 * 5.0)


def test_a_run_with_no_duration_is_not_counted_as_fast(tmp_path, monkeypatch):
    import core.event_log as el
    monkeypatch.setattr(el, "_INSTANCES", {})
    log = el.TelemetryEventLog(str(tmp_path / "telemetry_events.db"))
    monkeypatch.setattr(el, "get_event_log", lambda *a, **k: log)
    log.record("goal_run", user_id="rob", source="goal",
               attrs={"goal_id": "g-old", "outcome": "done", "steps": 10})
    est, measured = expected_run_seconds("g-old", 14, fallback_step_sec=45,
                                         max_run_sec=1800, data_dir=str(tmp_path))
    assert measured is False and est == 14 * 45


@pytest.mark.asyncio
async def test_preflight_defers_the_claim_and_emits_the_reason(
        tmp_path, monkeypatch):
    import core.event_log as el
    monkeypatch.setattr(el, "_INSTANCES", {})
    log = el.TelemetryEventLog(str(tmp_path / "te.db"))
    monkeypatch.setattr(el, "get_event_log", lambda *a, **k: log)
    monkeypatch.setenv("GOALS_ENABLED", "true")
    monkeypatch.setenv("GOAL_PREFLIGHT_ENABLED", "true")
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    from cron.jobs import CronJob, CronJobStore
    store = CronJobStore(str(tmp_path / "cron.db"))
    store.add(CronJob(id="exit0001", task="EXIT RAIL", schedule_spec="0 * * * *",
                      user_id="rob",
                      next_run_at=datetime.now() + timedelta(minutes=3),
                      payload={"priority": "money"}))
    board = GoalBoard(str(tmp_path / "goals.db"))
    g = board.create(user_id="rob", title="Long build", priority=5,
                     payload={"max_steps": 20})
    disp = GoalDispatcher(board, None, lock_path=str(tmp_path / "t.lock"))
    from core import interactive_gate as ig
    ig._busy_depth = 0

    assert await disp.dispatch_once() == 0
    assert board.get(g.id).status == "ready", "not claimed, not failed"
    rows = [r for r in log.query(kind="goal_run")
            if r["attrs"].get("outcome") == "deferred"]
    assert [r["attrs"]["reason"] for r in rows] == ["preflight:exit0001"]


def test_yield_ageing_lifts_a_repeatedly_preempted_goal(tmp_path):
    board = GoalBoard(str(tmp_path / "goals.db"))
    old = board.create(user_id="rob", title="Yielded six times", priority=3)
    fresh = board.create(user_id="rob", title="Fresh", priority=5)
    for _ in range(6):
        board._event(old.id, "yielded", {"job_id": "exit0001"})

    assert [g.id for g in board.ready(limit=2)] == [fresh.id, old.id], \
        "with ageing off the order is untouched"
    assert [g.id for g in board.ready(limit=2, yield_ageing=1)] == [old.id, fresh.id]
    assert board.yield_count(old.id) == 6
