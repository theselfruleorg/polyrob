"""Autonomy liveness heartbeat (telemetry audit 2026-07-04, Phase 3).

The audit found NO automated 'is the loop alive' signal. ``emit_loop_heartbeats``
records an autonomy_tick per running ticker so idle-but-alive is observable and a
dead ticker task is flagged.
"""
import asyncio
from types import SimpleNamespace

import pytest


def test_emit_heartbeats_records_liveness(tmp_path, monkeypatch):
    import core.event_log as el
    monkeypatch.setattr(el, "_INSTANCES", {})
    log = el.TelemetryEventLog(str(tmp_path / "te.db"))
    monkeypatch.setattr(el, "get_event_log", lambda *a, **k: log)

    from core.tickers import emit_loop_heartbeats
    # Fake ticker tasks: 'cron' alive, 'goal' dead.
    emit_loop_heartbeats([("cron", SimpleNamespace(done=lambda: False)),
                          ("goal", SimpleNamespace(done=lambda: True))])

    ticks = log.query(kind="autonomy_tick")
    by_loop = {t["attrs"]["loop"]: t["attrs"] for t in ticks}
    assert by_loop["cron"]["alive"] is True
    assert by_loop["goal"]["alive"] is False
    assert by_loop["goal"]["reason"] == "task_exited"


def test_emit_heartbeats_fail_open_no_tasks():
    from core.tickers import emit_loop_heartbeats
    emit_loop_heartbeats([])  # no tasks, must not raise


# --- 2026-08-28 (status SSOT D13): AutonomyHandles — the class the API
# lifespan, the REPL and the Telegram surface actually start their loops with —
# never emitted a heartbeat; prod had 16k telemetry rows and zero autonomy_tick.

def test_autonomy_handles_emit_heartbeats(tmp_path, monkeypatch):
    import core.event_log as el
    monkeypatch.setattr(el, "_INSTANCES", {})
    log = el.TelemetryEventLog(str(tmp_path / "te.db"))
    monkeypatch.setattr(el, "get_event_log", lambda *a, **k: log)

    from polyrob.app.autonomy_runtime import AutonomyHandles
    h = AutonomyHandles()
    h._entries = [("cron", SimpleNamespace(done=lambda: False), None),
                  ("goals", SimpleNamespace(done=lambda: True), None)]
    h.emit_heartbeats()
    by_loop = {t["attrs"]["loop"]: t["attrs"] for t in log.query(kind="autonomy_tick")}
    assert by_loop["cron"]["alive"] is True
    assert by_loop["goals"]["alive"] is False and by_loop["goals"]["reason"] == "task_exited"


@pytest.mark.asyncio
async def test_autonomy_handles_start_a_heartbeat_task(monkeypatch):
    from polyrob.app.autonomy_runtime import AutonomyHandles

    class _Ticker:
        async def run_forever(self, stop_event):
            await stop_event.wait()

    h = AutonomyHandles()
    beats = []
    monkeypatch.setattr(h, "emit_heartbeats", lambda: beats.append(1))
    h._add("cron", _Ticker())
    await asyncio.sleep(0)  # let the heartbeat task run its first beat
    assert h._hb_task is not None
    assert beats, "the first heartbeat is emitted immediately on start"
    await h.stop()
    assert h._hb_task is None


# --- 116 W4.1 / W4.2: liveness measures work ---------------------------------

def _hb(ts=1000.0, **kw):
    base = {"ts": ts, "alive": True, "pid": None, "tracked": True, "proc_gone": False,
            "tick_ok_at": ts - 10, "tick_running_since": None, "tick_errors": 0,
            "tick_error": "", "interval_s": 60.0}
    base.update(kw)
    return base


def test_the_verdict_reads_completed_ticks_not_the_task_object():
    from core.status_loops import loop_verdict
    now, stale = 1010.0, 900.0
    assert loop_verdict(None, now, stale)[0] == "silent"
    assert loop_verdict(_hb(), now, stale)[0] == "alive"
    assert loop_verdict(_hb(alive=False), now, stale)[0] == "dead"
    assert loop_verdict(_hb(ts=10.0), now, stale)[0] == "dead"            # stale
    assert loop_verdict(_hb(proc_gone=True, pid=4), now, stale)[0] == "dead"
    state, detail = loop_verdict(_hb(tick_errors=3, tick_error="KeyError: 'x'"), now, stale)
    assert state == "failing" and "KeyError" in detail
    assert loop_verdict(_hb(tick_ok_at=1000.0 - 4000), now, stale)[0] == "stuck"
    assert loop_verdict(_hb(tick_ok_at=1000.0 - 4000, tick_running_since=990.0),
                        now, stale)[0] == "alive"                         # a long run is work
    assert loop_verdict(_hb(tick_running_since=1000.0 - 7 * 3600), now, stale)[0] == "stuck"
    # A heartbeat from a build that records no ticks is judged by task and age.
    assert loop_verdict(_hb(tracked=False, tick_ok_at=None), now, stale)[0] == "alive"


def test_a_dead_process_cannot_mask_or_be_masked(monkeypatch):
    from core import status_loops as sl
    monkeypatch.setattr(sl, "_pid_alive", lambda pid: pid == 2)
    rows = [  # newest first: a REPL (pid 1, now gone) wrote last
        {"ts": 300.0, "attrs": {"loop": "cron", "alive": True, "pid": 1}},
        {"ts": 200.0, "attrs": {"loop": "cron", "alive": True, "pid": 2}},
        {"ts": 300.0, "attrs": {"loop": "curator", "alive": True, "pid": 1}},
    ]
    latest = sl.latest_heartbeats(rows)
    assert latest["cron"]["pid"] == 2 and not latest["cron"]["proc_gone"]
    assert latest["curator"]["proc_gone"]
    assert sl.loop_verdict(latest["curator"], 310.0, 900.0)[0] == "dead"


@pytest.mark.asyncio
async def test_a_ticker_records_its_ticks_and_the_heartbeat_carries_them():
    from core.tickers import IntervalTicker, heartbeat_attrs
    calls = {"n": 0}

    async def tick():
        calls["n"] += 1
        if calls["n"] >= 2:
            raise KeyError("boom")

    stop = asyncio.Event()
    task = asyncio.create_task(IntervalTicker(tick, 0).run_forever(stop))
    while calls["n"] < 4:
        await asyncio.sleep(0)
    attrs = heartbeat_attrs("cron", task)
    stop.set()
    await task
    assert attrs["alive"] is True                       # the task object exists…
    assert attrs["tick_errors"] >= 3 and "KeyError" in attrs["tick_error"]   # …and does no work
    assert attrs["tick_ok_at"] is not None


def test_the_headline_counts_with_the_same_verdict_and_never_prints_zero_of_unknown():
    from core.status_render import pause_headline_from
    live = {"running_goals": 0, "running_cron": 0, "started_loops": ["cron", "goals"],
            "loops_alive": {"cron": True, "goals": False}, "loops_checked": True,
            "turn": {"kind": "owner_chat", "started": 0}}
    line = pause_headline_from({}, live)
    assert "loops alive 1/2" in line and "owner turn live since 00:00 UTC" in line
    live.update({"loops_alive": {}, "loops_checked": False, "turn": None})
    assert "loops alive ?" in pause_headline_from({}, live)


def test_a_seat_with_autonomy_off_in_its_env_does_not_print_idle_over_a_live_service(monkeypatch):
    from core import status_loops as sl
    from core.config_policy import AutonomyConfig
    monkeypatch.setattr(AutonomyConfig, "cron_enabled", staticmethod(lambda: False))
    monkeypatch.setattr(AutonomyConfig, "goals_enabled", staticmethod(lambda: False))
    assert sl.autonomy_idle_reason() == "autonomy is off"
    assert sl.autonomy_idle_reason(["curator"]) == "autonomy is off"
    assert sl.autonomy_idle_reason(["cron", "curator"]) is None
