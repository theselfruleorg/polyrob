"""Autonomy liveness heartbeat (telemetry audit 2026-07-04, Phase 3).

The audit found NO automated 'is the loop alive' signal. TickerSupervisor now
records an autonomy_tick per running ticker so idle-but-alive is observable and a
dead ticker task is flagged.
"""
from types import SimpleNamespace


def test_emit_heartbeats_records_liveness(tmp_path, monkeypatch):
    import agents.task.telemetry.event_log as el
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
