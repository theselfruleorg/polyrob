"""056 WS1 — every `cron_run started` gets exactly one terminal event.

Prod 2026-09-17→19 (48 h): telemetry held started=175 but done+failed=105 — ~70
runs vanished from the ledger because `CronScheduler._run_one` emitted nothing on
the cap timeout, on an owner-pause cancel, or when a restart orphaned the row.
The rails looked healthier than they were and every "did it run?" answer needed
the journal. Now: `cut_by_cap`, `held` (pause) and `cut_by_restart` (reclaim) are
terminal outcomes, and the loops section renders the last outcome per job.
"""
import asyncio
from datetime import datetime

import pytest

from cron.jobs import CronJob, CronJobStore
from cron.scheduler import CronScheduler

NOW = datetime(2026, 9, 19, 5, 0, 0)


@pytest.fixture
def log(tmp_path, monkeypatch):
    import core.event_log as el
    monkeypatch.setattr(el, "_INSTANCES", {})
    test_log = el.TelemetryEventLog(str(tmp_path / "te.db"))
    monkeypatch.setattr(el, "get_event_log", lambda *a, **k: test_log)
    monkeypatch.setattr(el, "event_log_enabled", lambda: True)
    return test_log


def _job(**kw):
    base = dict(id="exit0001", task="EXIT RAIL", schedule_spec="0 * * * *", user_id="rob",
                next_run_at=datetime(2026, 9, 19, 4, 0, 0), max_duration_seconds=0)
    base.update(kw)
    return CronJob(**base)


def _outcomes(log, job_id="exit0001"):
    return [r["attrs"]["outcome"] for r in log.query(kind="cron_run")
            if r["attrs"].get("job_id") == job_id]


@pytest.mark.asyncio
async def test_cap_timeout_emits_cut_by_cap(tmp_path, log):
    store = CronJobStore(str(tmp_path / "cron.db"))
    store.add(_job())  # max_duration 0 => immediate timeout

    async def runner(job):
        await asyncio.sleep(1)
        return True

    await CronScheduler(store, runner, lock_path=str(tmp_path / "t.lock")).tick(now=NOW)
    outs = _outcomes(log)
    assert "cut_by_cap" in outs, outs
    row = [r for r in log.query(kind="cron_run") if r["attrs"].get("outcome") == "cut_by_cap"][0]
    assert row["attrs"]["cap_s"] == 0 and "duration_s" in row["attrs"]


@pytest.mark.asyncio
async def test_runner_exception_emits_failed(tmp_path, log):
    store = CronJobStore(str(tmp_path / "cron.db"))
    store.add(_job(max_duration_seconds=60))

    async def runner(job):
        raise RuntimeError("boom")

    await CronScheduler(store, runner, lock_path=str(tmp_path / "t.lock")).tick(now=NOW)
    outs = _outcomes(log)
    assert outs.count("failed") >= 1, outs
    row = [r for r in log.query(kind="cron_run") if r["attrs"].get("outcome") == "failed"][-1]
    assert row["attrs"]["reason"].startswith("RuntimeError")


def test_reclaim_returns_ids_and_emits_cut_by_restart(tmp_path, log):
    store = CronJobStore(str(tmp_path / "cron.db"))
    store.add(_job(max_duration_seconds=60))
    store.set_status("exit0001", "running")
    reclaimed = store.reclaim_stale_running()
    assert reclaimed == 1
    assert store.get("exit0001").status == "scheduled"
    assert "cut_by_restart" in _outcomes(log)


def test_loops_section_renders_last_outcome_per_job(tmp_path, log, monkeypatch):
    from core import status_snapshot as ss
    from cron import runner as cron_runner
    from types import SimpleNamespace
    store = CronJobStore(str(tmp_path / "cron.db"))
    store.add(_job(max_duration_seconds=1200))
    job = SimpleNamespace(id="exit0001", user_id="rob", task="EXIT RAIL")
    cron_runner._cron_ev(job, "started")
    cron_runner._cron_ev(job, "cut_by_cap", cap_s=1200, duration_s=1200.0)
    # Drive the section builder directly with the telemetry rows this log holds
    # (the same shape `_read_telemetry` produces: newest-first dicts with attrs).
    rows = log.query(kind="cron_run")
    tele = ss.Section(name="telemetry")
    tele.data["rows"] = rows
    sec = ss._loops_section("rob", str(tmp_path / "cron.db"), tele, NOW.timestamp(),
                            goals_db=str(tmp_path / "goals.db"), data_dir=str(tmp_path))
    joined = "\n".join(sec.lines)
    assert "EXIT RAIL" in joined and "cut_by_cap" in joined, joined
    keys = {h.key for h in sec.health}
    assert "rail_cut" in keys
