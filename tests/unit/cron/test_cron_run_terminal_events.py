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
    base = dict(id="exit0001", task="EXIT RAIL", schedule_spec="0 * * * *", user_id="alice",
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
    store.claim_for_run("exit0001")
    from core.sqlite_util import execute_retry
    execute_retry(store.db_path, "UPDATE cron_jobs SET lease_until=1 WHERE id=?",
                  ("exit0001",))                       # 098 MO-09: the lease ran out
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
    job = SimpleNamespace(id="exit0001", user_id="alice", task="EXIT RAIL")
    cron_runner._cron_ev(job, "started")
    cron_runner._cron_ev(job, "cut_by_cap", cap_s=1200, duration_s=1200.0)
    # Drive the section builder directly with the telemetry rows this log holds
    # (the same shape `_read_telemetry` produces: newest-first dicts with attrs).
    rows = log.query(kind="cron_run")
    tele = ss.Section(name="telemetry")
    tele.data["rows"] = rows
    sec = ss._loops_section("alice", str(tmp_path / "cron.db"), tele, NOW.timestamp(),
                            goals_db=str(tmp_path / "goals.db"), data_dir=str(tmp_path))
    joined = "\n".join(sec.lines)
    assert "EXIT RAIL" in joined and "cut_by_cap" in joined, joined
    keys = {h.key for h in sec.health}
    assert "rail_cut" in keys


@pytest.mark.asyncio
async def test_a_cancelled_tick_gives_the_row_back_at_once(tmp_path, log):
    """098 review: a cancel that is not an owner pause (shutdown, /autonomy off)
    requeues the row; it stayed `running` until its lease ran out."""
    store = CronJobStore(str(tmp_path / "cron.db"))
    store.add(_job(max_duration_seconds=60))
    started = asyncio.Event()

    async def runner(job):
        started.set()
        await asyncio.sleep(30)
        return True

    t = asyncio.create_task(
        CronScheduler(store, runner, lock_path=str(tmp_path / "t.lock")).tick(now=NOW))
    await asyncio.wait_for(started.wait(), 5)
    t.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t
    assert store.get("exit0001").status == "scheduled"


# --- 116 W2.1: done() after every action errored ------------------------------

def _agent_job(**kw):
    base = dict(id="err00001", task="read the inbox", schedule_spec="30m", user_id="alice",
                next_run_at=None, one_shot=True, skip_memory=True,
                max_duration_seconds=180, payload=kw.pop("payload", {}), created_at=None)
    base.update(kw)
    return CronJob(**base)


class _Agent:
    async def create_session(self, *, user_id, request):
        return {"id": "sess-err"}


async def _run_all_errored(tmp_path, monkeypatch, *, errored=True, payload=None):
    from agents.task.runtime.run_outcome import RunOutcome
    from cron.runner import make_agent_runner
    monkeypatch.setenv("CRON_RUN_LOOP", "true")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "absent" / "bot.db"))

    async def fake_run(*a, **k):
        return RunOutcome(session_id="sess-err", status="ok", done_called=True,
                          done_text="nothing new", user_messages=[],
                          all_actions_errored=errored)
    monkeypatch.setattr("cron.runner._run_task_to_outcome", fake_run)
    job = _agent_job(payload=payload or {})
    ok = await make_agent_runner(_Agent(), data_dir=str(tmp_path))(job)
    return ok, job


def _last_terminal(log, job_id="err00001"):
    rows = [r for r in log.query(kind="cron_run")
            if r["attrs"].get("job_id") == job_id and r["attrs"].get("outcome") != "started"]
    return rows[-1]["attrs"]


@pytest.mark.asyncio
async def test_all_errored_done_is_recorded_only_in_dry_mode(tmp_path, monkeypatch, log):
    import cron.runner as cr
    assert cr.ALL_ERRORED_FAILS is False      # the dry day: record, do not fail
    ok, job = await _run_all_errored(tmp_path, monkeypatch,
                                     payload={"expect": {"idle_ok": True}})
    a = _last_terminal(log)
    assert ok is True and a["outcome"] == "done"
    assert a["all_errored_verdict"] is True
    assert a["verified"] is True              # dry mode leaves idle_ok as it was
    assert job.run_note["outcome"] == "done"


@pytest.mark.asyncio
async def test_all_errored_done_fails_once_switched_on(tmp_path, monkeypatch, log):
    import cron.runner as cr
    monkeypatch.setattr(cr, "ALL_ERRORED_FAILS", True)
    ok, job = await _run_all_errored(tmp_path, monkeypatch,
                                     payload={"expect": {"idle_ok": True}})
    a = _last_terminal(log)
    assert ok is False and a["outcome"] == "failed"
    assert a["reason"] == cr.ALL_ERRORED_REASON
    assert a["all_errored_verdict"] is True
    assert a["verified"] is False             # idle_ok no longer verifies such a run
    assert job.run_note == {"outcome": "failed", "reason": cr.ALL_ERRORED_REASON,
                            "report": job.run_note["report"]}


@pytest.mark.asyncio
async def test_a_clean_done_carries_no_all_errored_verdict(tmp_path, monkeypatch, log):
    ok, _job_ = await _run_all_errored(tmp_path, monkeypatch, errored=False)
    a = _last_terminal(log)
    assert ok is True and a["outcome"] == "done"
    assert "all_errored_verdict" not in a
