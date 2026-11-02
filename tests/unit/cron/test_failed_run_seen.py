"""A failed run is seen: its reason is stored, listed, and told to the owner once
(routines review 2026-10-29 §7/§14).

10-28 18:05Z: BOUNTY ANNOUNCE stopped at the step limit and nothing was posted.
The agent had dropped the job's ``deliver``; a failed run never echoes on any
sink anyway, so nobody told the owner, and the next owner turn saw only
"next 10-31".
"""
import asyncio
import types
from datetime import datetime

import pytest

from cron.delivery import failed_run_notice
from cron.jobs import CronJob, CronJobStore
from cron.scheduler import CronScheduler
from cron.service import CronService, last_outcome

NOW = datetime(2026, 10, 28, 18, 5)


def _sched(tmp_path, runner):
    store = CronJobStore(str(tmp_path / "cron.db"))
    return store, CronScheduler(store, runner, lock_path=str(tmp_path / "lock"))


def _add(store, payload=None):
    store.add(CronJob(id="job1", task="ANNOUNCE the round", schedule_spec="1h",
                      user_id="u1", next_run_at=datetime(2026, 10, 28, 18, 0),
                      payload=payload or {}))


def test_runner_reason_lands_on_the_row_and_the_owner_is_told(tmp_path):
    told = []

    async def runner(job):
        job.run_note = {"outcome": "failed", "reason": "stopped at the step limit (150/150)"}
        return False

    store, sch = _sched(tmp_path, runner)
    _add(store)

    async def hook(job, outcome, reason, at):
        told.append((job.id, outcome, reason))

    sch.set_failure_hook(hook)
    asyncio.run(sch._run_due(NOW))
    job = store.get("job1")
    assert job.payload["last_outcome"]["reason"] == "stopped at the step limit (150/150)"
    assert last_outcome(job) == "failed 10-28 18:05 (stopped at the step limit (150/150))"
    assert told == [("job1", "failed", "stopped at the step limit (150/150)")]


def test_a_successful_run_tells_nobody(tmp_path):
    told = []

    async def runner(job):
        return True

    store, sch = _sched(tmp_path, runner)
    _add(store)

    async def hook(*a):
        told.append(a)

    sch.set_failure_hook(hook)
    asyncio.run(sch._run_due(NOW))
    assert told == [] and store.get("job1").payload["last_outcome"]["outcome"] == "done"


def test_a_declared_blocked_run_is_recorded_as_blocked(tmp_path):
    async def runner(job):
        job.run_note = {"outcome": "blocked", "reason": "needs the owner's key"}
        return True

    store, sch = _sched(tmp_path, runner)
    _add(store)
    asyncio.run(sch._run_due(NOW))
    assert last_outcome(store.get("job1")) == "blocked 10-28 18:05 (needs the owner's key)"


def _job(**payload):
    return types.SimpleNamespace(id="bbbe8d3e1cd2", task="BOUNTY ANNOUNCE v2\nmore",
                                 payload=payload)


def test_notice_goes_out_with_no_deliver_and_names_the_reason():
    text = failed_run_notice(_job(), "failed", "stopped at the step limit", NOW)
    assert text and "bbbe8d3e" in text and "stopped at the step limit" in text
    assert "/job run bbbe8d3e" in text


def test_notice_is_once_per_failure_streak():
    prev = {"last_outcome": {"outcome": "failed", "at": "2026-10-27T18:05:00"}}
    assert failed_run_notice(_job(**prev), "failed", "x", NOW) is None


@pytest.mark.parametrize("payload", [{"actor": "agent"}, {"digest": True}])
def test_no_notice_for_an_agent_job_or_the_digest(payload):
    assert failed_run_notice(_job(**payload), "failed", "x", NOW) is None


def test_an_owner_job_whose_rig_the_agent_edited_still_tells_the_owner():
    # Review M1: a rig edit moves ``actor`` to the rig's writer; ``armed_by`` keeps
    # who owns the job, so its failures still reach the owner.
    job = _job(actor="agent", armed_by="owner_seat")
    assert failed_run_notice(job, "failed", "x", NOW)


def test_a_failed_one_shot_offers_no_retry_it_cannot_take():
    job = _job()
    job.one_shot = True
    text = failed_run_notice(job, "failed", "x", NOW)
    assert text and "/job show bbbe8d3e" in text and "/job run" not in text


def test_no_notice_for_a_done_or_skipped_run():
    assert failed_run_notice(_job(), "done", "", NOW) is None
    assert failed_run_notice(_job(), "skipped", "", NOW) is None


@pytest.mark.asyncio
async def test_cronjob_list_shows_the_last_outcome_and_the_delivery(tmp_path):
    from tools.cronjob_tools import CronJobTool, CronListAction
    t = object.__new__(CronJobTool)
    svc = CronService(CronJobStore(str(tmp_path / "cron.db")))
    t._cron_service = svc
    job = svc.schedule(task="ANNOUNCE the round", schedule_spec="1h", user_id="u1",
                       payload={"deliver": "telegram"})
    svc.store.record_outcome(job.id, "failed", NOW, reason="cut by the wall-clock cap (600s)")
    res = await t.cronjob_list(CronListAction(),
                               execution_context=types.SimpleNamespace(user_id="u1"))
    assert "last: failed 10-28 18:05 (cut by the wall-clock cap (600s))" in res.extracted_content
    assert "deliver: telegram" in res.extracted_content
