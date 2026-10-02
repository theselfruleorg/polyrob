"""060 WS-5 — a rail pins its doctrine.

Prod 2026-09-21 (session 9d3067e0): the daily-X cron run keyword-matched
['self-deploy', 'polyrob-user-guide'] — neither about posting — while
`rh-reporting` and `x-engagement` stayed unloaded. `payload.skills` now reaches
SkillManager.get_skills_for_session(seeded_skill_ids=…) through the session
request; absent, the run is byte-identical to today's keyword matching.
"""
import logging
import types

import pytest

from core.config_policy.rigs import MAX_PINNED_SKILLS, pinned_skills
from cron.jobs import CronJob, CronJobStore
from cron.runner import make_agent_runner
from cron.service import CronService
from tools.cronjob_tools import CronJobTool, CronScheduleAction


@pytest.fixture(autouse=True)
def _clear_autonomy_marker():
	yield
	from agents.task.goals import autonomy_marker
	autonomy_marker._SESSIONS.clear()


def test_pinned_skills_cleans_the_payload():
	assert pinned_skills({}) == [] and pinned_skills(None) == []
	assert pinned_skills({"skills": "x-engagement"}) == []  # a string is not a list
	assert pinned_skills({"skills": [" a ", "a", "", 3, "b"]}) == ["a", "b"]
	many = [f"s{i}" for i in range(20)]
	assert len(pinned_skills({"skills": many})) == MAX_PINNED_SKILLS


class _TaskAgent:
	def __init__(self):
		self.request = None

	async def create_session(self, user_id, request):
		self.request = request
		return {"id": "s1"}

	async def run_session(self, user_id, session_id):
		return "done"


@pytest.mark.asyncio
@pytest.mark.parametrize("payload,expect", [
	({"skills": ["x-engagement", "rh-reporting"]}, ["x-engagement", "rh-reporting"]),
	({}, None),
])
async def test_the_cron_request_carries_the_pinned_skills(payload, expect):
	ta = _TaskAgent()
	job = CronJob(id="j", task="post the daily update", schedule_spec="1d", user_id="u1",
	              next_run_at=None, payload={"provider": "anthropic", **payload})
	assert await make_agent_runner(ta)(job) is True
	assert ta.request.get("skills") == expect
	if expect is None:
		assert "skills" not in ta.request  # byte-identical request when unpinned


@pytest.mark.asyncio
async def test_cronjob_schedule_stores_the_pinned_skills(tmp_path):
	t = object.__new__(CronJobTool)
	t._cron_service = CronService(CronJobStore(str(tmp_path / "cron.db")))
	ctx = types.SimpleNamespace(user_id="u1", role="orchestrator", is_sub_agent=False,
	                            metadata={}, session_id=None)
	res = await t.cronjob_schedule(
		CronScheduleAction(task="post the daily X update", schedule="1d",
		                   skills=["x-engagement", "x-engagement"]),
		execution_context=ctx)
	assert res.error is None
	assert t._cron_service.list_jobs(user_id="u1")[0].payload["skills"] == ["x-engagement"]


def test_session_request_and_config_accept_skills():
	from agents.task.config import TaskSessionConfig
	from agents.task.task_agent_support import SessionRequest
	assert SessionRequest(task="t", provider="p", model="m", skills=["a"]).skills == ["a"]
	assert TaskSessionConfig.from_dict({"skills": ["a"]}).skills == ["a"]
	assert TaskSessionConfig.defaults().skills is None


def test_construction_merges_rail_and_config_skills_and_reports_misses(monkeypatch):
	from agents.task.agent.core import construction as C
	events = []
	monkeypatch.setattr("core.event_log.emit", lambda kind, **kw: events.append((kind, kw)))
	agent = types.SimpleNamespace(
		orchestrator=types.SimpleNamespace(_rail_skill_ids=["x-engagement", "nope"],
		                                   user_id="u1", session_id="s1"),
		session_config=types.SimpleNamespace(skills=["rh-reporting", "x-engagement"]),
		logger=logging.getLogger("t"))
	ids = C.rail_skill_ids(agent)
	assert ids == ["x-engagement", "nope", "rh-reporting"]
	matched = [types.SimpleNamespace(skill_id="x-engagement"),
	           types.SimpleNamespace(skill_id="rh-reporting")]
	assert C.report_missing_rail_skills(agent, ids, matched) == ["nope"]
	assert events and events[0][0] == "rail_skill_missing"
	assert events[0][1]["attrs"]["missing"] == ["nope"]


def test_the_seed_seam_loads_a_pinned_skill_no_keyword_matches():
	"""The SkillManager seam the rail rides: a seed loads even when the task
	text matches nothing about it."""
	from agents.task.agent.skill_manager import get_skill_manager
	sm = get_skill_manager()
	ids = sm.get_skill_ids()
	assert ids, "no skills shipped?"
	target = ids[-1]
	got = sm.get_skills_for_session(tool_ids=[], task="zzqx unrelated words",
	                                available_actions=[], user_id=None,
	                                seeded_skill_ids=[target])
	assert target in [m.skill_id for m in got]


def test_the_rules_section_names_a_missed_pin(tmp_path, monkeypatch):
	monkeypatch.setenv("POLYROB_INSTANCE_ID", "rob")
	from core.status_rules import rules_section
	from core.status_snapshot import Section
	tele = Section(name="telemetry", data={"rows": [
		{"kind": "rail_skill_missing", "attrs": {"missing": ["nope"]}}]})
	sec = rules_section("u1", str(tmp_path), str(tmp_path / "cron.db"), tele)
	item = next(h for h in sec.health if h.key == "rail_skill_missing")
	assert "nope" in item.text and "did NOT load" in item.text


@pytest.mark.asyncio
@pytest.mark.parametrize("payload,expect", [
	({"skills": ["rh-reporting"]}, ["rh-reporting"]),
	({}, None),
])
async def test_the_goal_request_carries_the_pinned_skills(tmp_path, monkeypatch, payload, expect):
	import asyncio
	from agents.task.goals.board import GoalBoard
	from agents.task.goals.dispatcher import GoalDispatcher
	monkeypatch.setenv("GOALS_ENABLED", "true")
	monkeypatch.setenv("GOAL_MAX_CONCURRENT", "5")
	board = GoalBoard(str(tmp_path / "goals.db"))
	board.create(user_id="u1", title="report the week", payload={"tools": ["filesystem"], **payload})

	class _Agent(_TaskAgent):
		async def create_session(self, *, user_id, request):
			self.request = request
			return {"id": "sess-u1"}

		async def deliver_self_wake(self, *a, **k):
			return True

		def get_orchestrator(self, session_id):
			return None

	agent = _Agent()
	d = GoalDispatcher(board, agent)
	await d.dispatch_once()
	tasks = tuple(d._inflight)
	if tasks:
		await asyncio.wait_for(asyncio.gather(*tasks), timeout=3)
	assert agent.request.get("skills") == expect


def test_goal_create_schema_accepts_skills():
	from tools.goal_tools import GoalCreateAction
	assert GoalCreateAction(title="weekly report", skills=["rh-reporting"]).skills == ["rh-reporting"]
