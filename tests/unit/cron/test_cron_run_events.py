"""cron_run events → durable event log (telemetry audit 2026-07-04)."""
from types import SimpleNamespace


def test_cron_ev_records_to_event_log(tmp_path, monkeypatch):
    import agents.task.telemetry.event_log as el
    monkeypatch.setattr(el, "_INSTANCES", {})
    test_log = el.TelemetryEventLog(str(tmp_path / "te.db"))
    monkeypatch.setattr(el, "get_event_log", lambda *a, **k: test_log)

    from cron import runner as cron_runner
    job = SimpleNamespace(id="job1", user_id="u1", task="do x")

    cron_runner._cron_ev(job, "started")
    cron_runner._cron_ev(job, "done", duration_s=2.0, spend_usd=0.01, steps=3)

    rows = test_log.query(kind="cron_run")
    assert len(rows) == 2
    outcomes = {r["attrs"]["outcome"] for r in rows}
    assert outcomes == {"started", "done"}
    done = [r for r in rows if r["attrs"]["outcome"] == "done"][0]
    assert done["attrs"]["steps"] == 3
    assert done["user_id"] == "u1"
    assert done["attrs"]["job_id"] == "job1"


def test_cron_ev_fail_open(monkeypatch):
    # No event log configured / broken import must not raise.
    from cron import runner as cron_runner
    job = SimpleNamespace(id="j", user_id="u", task="t")
    cron_runner._cron_ev(job, "started")  # should simply no-op if anything fails
