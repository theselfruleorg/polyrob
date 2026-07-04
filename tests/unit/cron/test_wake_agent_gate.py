import pytest

from tools.cronjob_tools import CronScheduleAction
from cron.runner import make_agent_runner
from tests.unit.cron.test_runner_runloop_delivery import _job  # reuse the job factory


def test_wake_agent_field_defaults_true():
    a = CronScheduleAction(task="do the thing now", schedule="30m")
    assert a.wake_agent is True
    b = CronScheduleAction(task="do the thing now", schedule="30m", wake_agent=False)
    assert b.wake_agent is False


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
