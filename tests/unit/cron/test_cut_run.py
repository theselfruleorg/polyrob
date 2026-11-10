"""107 WS-3: a run that a crash or a shutdown cut does not repeat an outward effect."""
import asyncio
from datetime import datetime, timedelta

import pytest

from core.sqlite_util import execute_retry
from cron.jobs import CronJob, CronJobStore
from cron.scheduler import CronScheduler, cut_run_may_repeat

NOW = datetime(2026, 11, 7, 12, 0)


@pytest.fixture
def rig(tmp_path):
    store = CronJobStore(str(tmp_path / "cron.db"))
    ran, lines = [], []

    async def runner(job):
        ran.append(job.id)
        return True

    sch = CronScheduler(store, runner, lock_path=str(tmp_path / "lock"))

    async def line(text):
        lines.append(text)

    sch.set_notice_hook(line)
    return store, sch, ran, lines


def _orphan(store, *, payload, one_shot=False):
    store.add(CronJob(id="job1", task="POST the update", schedule_spec="1h", user_id="u1",
                      next_run_at=NOW, one_shot=one_shot, payload=payload))
    store.claim_for_run("job1")
    execute_retry(store.db_path, "UPDATE cron_jobs SET lease_until=1 WHERE id=?", ("job1",))


READ_ONLY = {"read_verb": "defi_data.x"}
OUTWARD = {"tools": ["twitter"]}


def test_a_read_with_no_model_turn_runs_again(rig):
    store, sch, ran, lines = rig
    _orphan(store, payload={"wake_agent": False})
    asyncio.run(sch._run_due(NOW))
    assert ran == ["job1"] and lines == []
    assert "cut_run" not in store.get("job1").payload


def test_a_model_run_with_a_narrow_list_does_not_repeat(rig):
    # A run can load a tool on demand, so its list does not prove it did nothing outward.
    store, sch, ran, lines = rig
    _orphan(store, payload={"tools": ["filesystem"]})
    asyncio.run(sch._run_due(NOW))
    assert ran == [] and len(lines) == 1


def test_a_job_with_an_outward_tool_goes_to_its_next_slot_and_tells(rig):
    store, sch, ran, lines = rig
    _orphan(store, payload=OUTWARD)
    asyncio.run(sch._run_due(NOW))
    job = store.get("job1")
    assert ran == []
    assert job.status == "scheduled" and job.next_run_at == NOW + timedelta(hours=1)
    assert job.payload["last_outcome"]["outcome"] == "cut"
    assert job.payload["last_outcome"]["reason"] == "cut by a restart"
    assert "cut_run" not in job.payload
    assert len(lines) == 1 and "/job run job1" in lines[0]
    asyncio.run(sch._run_due(NOW))
    assert len(lines) == 1


def test_a_one_time_job_with_an_outward_tool_ends_as_failed(rig):
    store, sch, ran, lines = rig
    _orphan(store, payload=OUTWARD, one_shot=True)
    asyncio.run(sch._run_due(NOW))
    job = store.get("job1")
    assert ran == [] and job.status == "failed" and not job.enabled
    assert "will not run again" in lines[0]


def test_a_money_job_does_not_repeat(rig):
    store, sch, ran, lines = rig
    _orphan(store, payload={"tools": ["defi_trade"]})
    asyncio.run(sch._run_due(NOW))
    assert ran == [] and len(lines) == 1


def test_a_shutdown_cut_takes_the_same_path(rig, tmp_path):
    store, _, _, lines = rig
    store.add(CronJob(id="job1", task="POST it", schedule_spec="1h", user_id="u1",
                      next_run_at=NOW, payload=OUTWARD))
    started = []

    async def slow(job):
        started.append(job.id)
        await asyncio.sleep(60)
        return True

    sch = CronScheduler(store, slow, lock_path=str(tmp_path / "lock2"))

    async def line(text):
        lines.append(text)

    sch.set_notice_hook(line)

    async def scenario():
        tick = asyncio.create_task(sch._run_due(NOW))
        await asyncio.sleep(0.05)
        tick.cancel()
        with pytest.raises(asyncio.CancelledError):
            await tick

    asyncio.run(scenario())
    assert started == ["job1"] and store.get("job1").payload["cut_run"] == "restart"
    asyncio.run(sch._run_due(NOW))
    assert started == ["job1"] and len(lines) == 1


def test_the_rule_for_each_kind_of_job():
    job = lambda p: CronJob(id="j", task="t", schedule_spec="1h", user_id="u", next_run_at=NOW,
                            payload=p)
    assert cut_run_may_repeat(job({"digest": True}))
    assert cut_run_may_repeat(job({"read_verb": "defi_data.x"}))
    assert cut_run_may_repeat(job({"wake_agent": False}))
    assert not cut_run_may_repeat(job({"write_verb": "agent_nft.x"}))
    assert not cut_run_may_repeat(job({"tools": ["filesystem"]}))
    assert not cut_run_may_repeat(job({}))


def test_an_unreadable_cut_list_does_not_stop_the_tick(rig, monkeypatch):
    store, sch, ran, _ = rig
    store.add(CronJob(id="job1", task="SCAN", schedule_spec="1h", user_id="u1", next_run_at=NOW))

    def boom():
        raise RuntimeError("malformed JSON")

    monkeypatch.setattr(store, "cut_runs", boom)
    asyncio.run(sch._run_due(NOW))
    assert ran == ["job1"]


def test_a_cut_does_not_hide_the_next_real_failure(rig):
    from cron.delivery import failed_run_notice
    store, sch, _, _ = rig
    _orphan(store, payload=OUTWARD)
    asyncio.run(sch._run_due(NOW))
    assert failed_run_notice(store.get("job1"), "failed", "why", NOW)


def test_a_pause_cut_names_the_pause(rig):
    store, sch, _, lines = rig
    store.add(CronJob(id="job1", task="POST it", schedule_spec="1h", user_id="u1",
                      next_run_at=NOW, payload=OUTWARD))
    store.mark_cut_run("job1", by="pause")
    asyncio.run(sch._run_due(NOW))
    assert "was cut by an owner pause" in lines[0]
    assert store.get("job1").payload["last_outcome"]["reason"] == "cut by an owner pause"


def test_a_pause_and_resume_drop_an_unsettled_cut(rig):
    # Before: the mark outlived the pause, so the first tick after a resume days later
    # skipped the fresh slot and said the job "was cut by a restart".
    store, sch, ran, lines = rig
    store.add(CronJob(id="job1", task="POST it", schedule_spec="1h", user_id="u1",
                      next_run_at=NOW, payload=OUTWARD))
    store.mark_cut_run("job1")
    assert store.pause("job1", user_id=None, note={"by": "owner"})
    assert "cut_run" not in store.get("job1").payload
    execute_retry(store.db_path,
                  "UPDATE cron_jobs SET payload=json_set(payload, '$.cut_run', 1) WHERE id=?",
                  ("job1",))
    assert store.resume("job1", user_id=None, next_run_at=NOW)
    assert "cut_run" not in store.get("job1").payload
    asyncio.run(sch._run_due(NOW))
    assert ran == ["job1"] and lines == []


def test_a_settled_cut_does_not_offer_job_run_while_jobs_are_paused(rig, monkeypatch):
    # Before: the line offered "/job run … runs it now" while `/pause cron` held every run.
    from types import SimpleNamespace
    import core.autonomy_control as ac
    store, sch, _, lines = rig
    _orphan(store, payload=OUTWARD)
    monkeypatch.setattr(ac, "allows", lambda kind: SimpleNamespace(allowed=False, reason="p"))
    asyncio.run(sch._run_due(NOW))
    assert "/job run" not in lines[0]
    assert "runs after /resume" in lines[0]


def test_a_settled_cut_offers_job_run_when_jobs_may_run(rig):
    store, sch, _, lines = rig
    _orphan(store, payload=OUTWARD)
    asyncio.run(sch._run_due(NOW))
    assert "/job run" in lines[0]
