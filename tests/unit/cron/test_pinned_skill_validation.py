"""081 WS-5.4: one pin validator for job and goal writes (the lane gate's), and a
pinned skill that is gone is named at the top of the run, never dropped silently."""
import types

import pytest

from core import cron_lanes
from core.cron_lanes import LaneStopped, UnknownPinnedSkill, check_pinned_skills
from cron.jobs import CronJob, CronJobStore
from cron.runner import make_agent_runner
from cron.service import CronService

REAL = "web-research"  # a shipped builtin


def test_a_known_pin_passes_and_an_unknown_one_is_refused():
    check_pinned_skills({"skills": [REAL]}, user_id="u1")
    check_pinned_skills({}, user_id="u1")
    with pytest.raises(UnknownPinnedSkill) as e:
        check_pinned_skills({"skills": [REAL, "no-such-skill"]}, user_id="u1")
    assert "`no-such-skill`" in str(e.value) and isinstance(e.value, LaneStopped)


def test_a_failed_lookup_refuses(monkeypatch):
    def _boom(*a, **k):
        raise OSError("disk")
    monkeypatch.setattr("core.skills.scopes.external_index", _boom)
    with pytest.raises(UnknownPinnedSkill, match="could not check"):
        check_pinned_skills({"skills": [REAL]}, user_id="u1")


def test_schedule_refuses_an_unknown_pin_and_stores_nothing(tmp_path):
    svc = CronService(CronJobStore(str(tmp_path / "cron.db")))
    with pytest.raises(UnknownPinnedSkill):
        svc.schedule(task="research", schedule_spec="1d", user_id="u1",
                     payload={"skills": ["no-such-skill"]})
    assert svc.list_jobs(user_id="u1") == []
    assert svc.schedule(task="research", schedule_spec="1d", user_id="u1",
                        payload={"skills": [REAL]}).payload["skills"] == [REAL]


@pytest.mark.asyncio
async def test_goal_tool_refuses_an_unknown_pin(tmp_path):
    from tools.goal_tools import GoalCreateAction, GoalTool
    t = object.__new__(GoalTool)
    t._resolve_board = lambda: pytest.fail("nothing may be stored")
    ctx = types.SimpleNamespace(user_id="u1", role="orchestrator", is_sub_agent=False,
                                metadata={}, session_id=None)
    res = await t.goal_create(GoalCreateAction(title="weekly report", skills=["no-such-skill"]),
                              execution_context=ctx)
    assert res.error and "no-such-skill" in res.error


class _TaskAgent:
    request = None

    async def create_session(self, user_id, request):
        self.request = request
        return {"id": "s1"}

    async def run_session(self, user_id, session_id):
        return "done"


@pytest.mark.asyncio
async def test_a_missing_pin_is_named_at_the_top_of_the_run(monkeypatch):
    ta = _TaskAgent()
    job = CronJob(id="j", task="post the daily update", schedule_spec="1d", user_id="u1",
                  next_run_at=None, payload={"provider": "anthropic",
                                             "skills": [REAL, "gone-skill"]})
    assert await make_agent_runner(ta)(job) is True
    first = ta.request["task"].splitlines()[0]
    assert first.startswith("[IMPORTANT:") and "gone-skill" in first and REAL not in first


@pytest.mark.asyncio
async def test_a_run_whose_pins_resolve_has_no_notice():
    ta = _TaskAgent()
    job = CronJob(id="j", task="post the daily update", schedule_spec="1d", user_id="u1",
                  next_run_at=None, payload={"provider": "anthropic", "skills": [REAL]})
    assert await make_agent_runner(ta)(job) is True
    assert "[IMPORTANT" not in ta.request["task"]
    assert cron_lanes.missing_skills_notice({}, user_id="u1") == ""
