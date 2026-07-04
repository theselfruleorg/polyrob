"""P5c — cron scheduler tick: run due jobs, reschedule, one-shot, cap, tick-lock."""
import asyncio
from datetime import datetime

import pytest

from cron.jobs import CronJob, CronJobStore
from cron.scheduler import CronScheduler, TickLock


def _store(tmp_path):
    return CronJobStore(str(tmp_path / "cron.db"))


def _job(**kw):
    base = dict(id="j1", task="t", schedule_spec="*/15 * * * *", user_id="u1",
                next_run_at=datetime(2026, 6, 6, 11, 0, 0), max_duration_seconds=180)
    base.update(kw)
    return CronJob(**base)


def _sched(store, tmp_path, runner):
    return CronScheduler(store, runner, lock_path=str(tmp_path / "tick.lock"))


NOW = datetime(2026, 6, 6, 12, 0, 0)


@pytest.mark.asyncio
async def test_recurring_job_runs_and_reschedules(tmp_path):
    s = _store(tmp_path)
    s.add(_job(schedule_spec="*/15 * * * *"))
    seen = []

    async def runner(job):
        seen.append(job.id)
        return True

    result = await _sched(s, tmp_path, runner).tick(now=NOW)
    assert result.ran == ["j1"] and seen == ["j1"]
    got = s.get("j1")
    assert got.status == "scheduled"
    assert got.next_run_at == datetime(2026, 6, 6, 12, 15, 0)  # next quarter hour
    assert got.last_run_at == NOW


@pytest.mark.asyncio
async def test_one_shot_success_marks_done(tmp_path):
    s = _store(tmp_path)
    s.add(_job(id="o", one_shot=True, schedule_spec="2026-06-06T11:00:00"))

    async def runner(job):
        return True

    await _sched(s, tmp_path, runner).tick(now=NOW)
    got = s.get("o")
    assert got.status == "done" and got.next_run_at is None


@pytest.mark.asyncio
async def test_one_shot_failure_marks_failed(tmp_path):
    s = _store(tmp_path)
    s.add(_job(id="o", one_shot=True, schedule_spec="2026-06-06T11:00:00"))

    async def runner(job):
        return False

    await _sched(s, tmp_path, runner).tick(now=NOW)
    assert s.get("o").status == "failed"


@pytest.mark.asyncio
async def test_runner_timeout_is_treated_as_failure(tmp_path):
    s = _store(tmp_path)
    s.add(_job(id="o", one_shot=True, schedule_spec="2026-06-06T11:00:00",
               max_duration_seconds=0))  # 0 => immediate timeout

    async def runner(job):
        await asyncio.sleep(1)
        return True

    await _sched(s, tmp_path, runner).tick(now=NOW)
    assert s.get("o").status == "failed"


@pytest.mark.asyncio
async def test_not_due_jobs_are_skipped(tmp_path):
    s = _store(tmp_path)
    s.add(_job(id="future", next_run_at=datetime(2026, 6, 6, 13, 0, 0)))
    ran = []

    async def runner(job):
        ran.append(job.id)
        return True

    result = await _sched(s, tmp_path, runner).tick(now=NOW)
    assert result.ran == [] and ran == []


@pytest.mark.asyncio
async def test_tick_skipped_when_lock_held(tmp_path):
    s = _store(tmp_path)
    s.add(_job())
    ran = []

    async def runner(job):
        ran.append(job.id)
        return True

    lock = TickLock(str(tmp_path / "tick.lock"))
    assert lock.acquire() is True
    try:
        result = await _sched(s, tmp_path, runner).tick(now=NOW)
        assert result.skipped_locked is True and result.ran == []
        assert ran == []
    finally:
        lock.release()


@pytest.mark.asyncio
async def test_tick_skipped_while_interactive_busy(tmp_path):
    """A human mid-turn in the REPL defers cron execution; jobs stay queued."""
    s = _store(tmp_path)
    s.add(_job())
    ran = []

    async def runner(job):
        ran.append(job.id)
        return True

    from core.interactive_gate import interactive_turn
    with interactive_turn():
        result = await _sched(s, tmp_path, runner).tick(now=NOW)
    assert result.skipped_busy is True
    assert result.ran == [] and ran == []      # due job NOT run while busy
    assert s.get("j1").status == "scheduled"    # left queued, untouched

    # idle again -> the due job runs
    result2 = await _sched(s, tmp_path, runner).tick(now=NOW)
    assert result2.ran == ["j1"] and ran == ["j1"]


def test_tick_lock_is_exclusive_then_reusable(tmp_path):
    p = str(tmp_path / "tick.lock")
    a, b = TickLock(p), TickLock(p)
    assert a.acquire() is True
    assert b.acquire() is False  # already held
    a.release()
    assert b.acquire() is True   # released -> acquirable
    b.release()
