import pytest

from tools.cronjob_tools import CronScheduleAction
from cron.runner import make_agent_runner
from tests.unit.cron.test_runner_runloop_delivery import _job  # reuse the job factory


def test_the_agent_cannot_schedule_a_job_that_runs_nothing():
    """Prod 2026-11-18: the agent set wake_agent=False on its own announce job; the
    run was a $0 tick that did nothing and was recorded as done. The payload key
    stays for the model-free digest; the agent's tool no longer has the parameter."""
    assert "wake_agent" not in CronScheduleAction.model_fields
    with pytest.raises(Exception):
        CronScheduleAction(task="do the thing now", schedule="30m", wake_agent=False)


def test_extra_forbid_still_enforced():
    with pytest.raises(Exception):
        CronScheduleAction(task="do the thing now", schedule="30m", bogus=1)


@pytest.mark.asyncio
async def test_wake_agent_false_skips_agent(monkeypatch):
    monkeypatch.setenv("CRON_RUN_LOOP", "true")
    calls = {"create": 0, "run": 0}

    class _Agent:
        async def create_session(self, **kw):
            calls["create"] += 1
            return {"session_id": "s1"}

        async def run_session(self, *a, **k):
            calls["run"] += 1
            return "x"

    runner = make_agent_runner(_Agent())
    job = _job()
    job.payload = {**(job.payload or {}), "wake_agent": False}
    ok = await runner(job)
    assert ok is True                       # a $0 tick is a success
    assert calls == {"create": 0, "run": 0}  # the LLM path was never entered


@pytest.mark.asyncio
async def test_a_stored_agent_job_that_runs_nothing_fails(monkeypatch):
    """A row the agent made before 535523bf1 still holds wake_agent=False: it is a failed
    run (the failure limit then pauses it and tells the owner), never a silent done."""
    monkeypatch.setenv("CRON_RUN_LOOP", "true")

    class _Agent:
        async def create_session(self, **kw):
            raise AssertionError("the model must not run")

    runner = make_agent_runner(_Agent())
    job = _job(payload={"wake_agent": False, "armed_by": "agent"})
    assert await runner(job) is False
    assert job.run_note["outcome"] == "failed" and "runs nothing" in job.run_note["reason"]
