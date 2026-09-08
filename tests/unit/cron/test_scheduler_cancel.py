"""031 T5: an owner pause cancels the in-flight cron run; the job is rescheduled."""
import asyncio

import pytest


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("core.runtime_paths.resolve_data_home", lambda: tmp_path)
    return tmp_path


@pytest.mark.asyncio
async def test_cancel_inflight_returns_job_to_scheduled(home, tmp_path):
    from cron.jobs import CronJob, CronJobStore
    from cron.scheduler import CronScheduler
    store = CronJobStore(str(tmp_path / "cron.db"))
    job = CronJob(id="j1", task="x", schedule_spec="30m", user_id="rob", next_run_at=None,
                  max_duration_seconds=60)
    store.add(job)
    started = asyncio.Event()

    async def runner(j):
        started.set()
        await asyncio.sleep(30)
        return True

    s = CronScheduler(store, runner, lock_path=str(tmp_path / "cron.tick.lock"))
    assert s.cancel_inflight() is False  # nothing running
    assert store.claim_for_run(job.id)
    t = asyncio.create_task(s._run_one(job))
    await started.wait()
    assert s.cancel_inflight() is False  # kind-aware: nothing is paused yet
    from core import autonomy_control as ac
    ac.pause(str(home), scopes=("cron",), via="test")
    assert s.cancel_inflight() is True
    assert await t is False
    assert s._pause_cancelled is True
    assert s._current is None


@pytest.mark.asyncio
async def test_run_due_reschedules_a_pause_cancelled_job(home, tmp_path):
    from datetime import datetime, timedelta
    from cron.jobs import CronJob, CronJobStore
    from cron.scheduler import CronScheduler
    store = CronJobStore(str(tmp_path / "cron.db"))
    due = datetime.now() - timedelta(minutes=1)
    store.add(CronJob(id="j1", task="x", schedule_spec="30m", user_id="rob", next_run_at=due,
                      max_duration_seconds=60))
    store.add(CronJob(id="j2", task="y", schedule_spec="30m", user_id="rob", next_run_at=due,
                      max_duration_seconds=60))
    ran = []
    sched = {}

    async def runner(j):
        ran.append(j.id)
        from core import autonomy_control as ac
        ac.pause(str(home), via="test")   # the owner pauses mid-run
        sched["s"].cancel_inflight()
        await asyncio.sleep(30)
        return True

    s = CronScheduler(store, runner, lock_path=str(tmp_path / "cron.tick.lock"))
    sched["s"] = s
    res = await s._run_due(datetime.now())
    assert ran == ["j1"]                       # the tick stops after the cancelled job
    assert res.ran == [] and res.failed == []  # a pause is not a failure
    assert store.get("j1").status == "scheduled"
    assert store.get("j2").status == "scheduled"


@pytest.mark.asyncio
async def test_digest_run_survives_a_cron_scoped_pause(home, tmp_path):
    from cron.jobs import CronJob, CronJobStore
    from cron.scheduler import CronScheduler
    from core import autonomy_control as ac
    store = CronJobStore(str(tmp_path / "cron.db"))
    job = CronJob(id="d1", task="digest", schedule_spec="30m", user_id="rob", next_run_at=None,
                  max_duration_seconds=60, payload={"digest": True})
    store.add(job)
    started = asyncio.Event()

    async def runner(j):
        started.set()
        await asyncio.sleep(0.2)
        return True

    s = CronScheduler(store, runner, lock_path=str(tmp_path / "cron.tick.lock"))
    t = asyncio.create_task(s._run_one(job))
    await started.wait()
    ac.pause(str(home), scopes=("cron",), via="test")
    assert s.cancel_inflight() is False   # the owner's own $0 report keeps running
    assert await t is True
