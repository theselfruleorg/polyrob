"""P5c — cron scheduler tick: run due jobs, reschedule, one-shot, cap, tick-lock."""
import asyncio
from datetime import datetime, timedelta

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


@pytest.mark.asyncio
async def test_an_interval_counts_from_the_end_of_a_run_behind_a_long_job(tmp_path, monkeypatch):
    """Jobs run one at a time: a 30m job behind a 20 minute job ended 22 minutes into
    the tick and was due again 8 minutes later."""
    import cron.scheduler as sched_mod
    clock = {"t": 1000.0}
    monkeypatch.setattr(sched_mod.time, "monotonic", lambda: clock["t"])
    s = CronJobStore(str(tmp_path / "cron.db"))
    long_job = CronJob(id="long", task="t", schedule_spec="every day 09:00", user_id="u",
                       next_run_at=NOW - timedelta(minutes=2))
    behind = CronJob(id="behind", task="t", schedule_spec="30m", user_id="u",
                     next_run_at=NOW - timedelta(minutes=1))
    s.add(long_job)
    s.add(behind)

    async def runner(job):
        clock["t"] += 1200 if job.id == "long" else 120
        return True

    await _sched(s, tmp_path, runner).tick(now=NOW)
    got = s.get("behind", user_id="u")
    assert got.last_run_at == NOW + timedelta(minutes=22)
    assert got.next_run_at == NOW + timedelta(minutes=52)


def test_tick_lock_is_never_stolen_from_a_live_holder_by_age(tmp_path):
    # Before: a lock file older than the stale limit was unlinked and re-created by the
    # next worker, so a live long tick lost its lock and its jobs could run twice.
    import os
    import time
    p = str(tmp_path / "tick.lock")
    a, b = TickLock(p), TickLock(p)
    assert a.acquire() is True
    old = time.time() - 3600
    os.utime(p, (old, old))
    assert b.acquire() is False
    a.release()
    assert b.acquire() is True
    b.release()


def test_tick_lock_left_by_a_dead_holder_is_free(tmp_path):
    # The kernel drops a flock with its holder: a file left on disk holds nothing.
    p = tmp_path / "tick.lock"
    p.write_text("12345")
    lock = TickLock(str(p))
    assert lock.acquire() is True
    lock.release()


@pytest.mark.asyncio
async def test_one_shot_held_by_an_owner_pause_keeps_its_slot(tmp_path):
    # Before: a one-shot reminder skipped by `/pause cron` was written done and
    # disabled — it never ran and nobody was told.
    s = _store(tmp_path)
    s.add(_job(id="o", one_shot=True, schedule_spec="2026-06-06T11:00:00"))

    async def runner(job):
        job.run_note = {"outcome": "skipped", "reason": "the owner paused jobs", "hold": True}
        return True

    await _sched(s, tmp_path, runner).tick(now=NOW)
    got = s.get("o")
    assert got.status == "scheduled" and got.enabled
    assert got.next_run_at == datetime(2026, 6, 6, 11, 0, 0)


@pytest.mark.asyncio
async def test_one_shot_quiet_skip_without_a_hold_stays_final(tmp_path):
    s = _store(tmp_path)
    s.add(_job(id="o", one_shot=True, schedule_spec="2026-06-06T11:00:00"))

    async def runner(job):
        job.run_note = {"outcome": "skipped", "reason": "nothing changed"}
        return True

    await _sched(s, tmp_path, runner).tick(now=NOW)
    assert s.get("o").status == "done"
