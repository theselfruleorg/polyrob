"""Autonomy liveness heartbeat (telemetry audit 2026-07-04, Phase 3).

The audit found NO automated 'is the loop alive' signal. TickerSupervisor now
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

    from core.tickers import TickerSupervisor
    sup = TickerSupervisor()
    # Inject fake ticker tasks: 'cron' alive, 'goal' dead.
    sup._tasks = {
        "cron": (SimpleNamespace(done=lambda: False), None),
        "goal": (SimpleNamespace(done=lambda: True), None),
    }

    sup._emit_heartbeats()

    ticks = log.query(kind="autonomy_tick")
    by_loop = {t["attrs"]["loop"]: t["attrs"] for t in ticks}
    assert by_loop["cron"]["alive"] is True
    assert by_loop["goal"]["alive"] is False
    assert by_loop["goal"]["reason"] == "task_exited"


def test_emit_heartbeats_fail_open_no_tasks():
    from core.tickers import TickerSupervisor
    sup = TickerSupervisor()
    sup._emit_heartbeats()  # no tasks, must not raise


# --- 2026-08-28 (status SSOT D13): AutonomyHandles — the class the API
# lifespan, the REPL and the Telegram surface actually start their loops with —
# never emitted a heartbeat; prod had 16k telemetry rows and zero autonomy_tick.

def test_autonomy_handles_emit_heartbeats(tmp_path, monkeypatch):
    import core.event_log as el
    monkeypatch.setattr(el, "_INSTANCES", {})
    log = el.TelemetryEventLog(str(tmp_path / "te.db"))
    monkeypatch.setattr(el, "get_event_log", lambda *a, **k: log)

    from core.autonomy_runtime import AutonomyHandles
    h = AutonomyHandles()
    h._entries = [("cron", SimpleNamespace(done=lambda: False), None),
                  ("goals", SimpleNamespace(done=lambda: True), None)]
    h.emit_heartbeats()
    by_loop = {t["attrs"]["loop"]: t["attrs"] for t in log.query(kind="autonomy_tick")}
    assert by_loop["cron"]["alive"] is True
    assert by_loop["goals"]["alive"] is False and by_loop["goals"]["reason"] == "task_exited"


@pytest.mark.asyncio
async def test_autonomy_handles_start_a_heartbeat_task(monkeypatch):
    from core.autonomy_runtime import AutonomyHandles

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
