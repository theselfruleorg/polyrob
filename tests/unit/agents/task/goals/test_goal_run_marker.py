"""A goal run carries standing authority, the same as a cron job (CHAT-5): the
dispatcher records its text, so X posts, room moderation and self-service tool
loading read it. Money tools stay explicit-grant-only."""
import asyncio

from core import autonomy_marker as am
from agents.task.goals.board import Goal
from agents.task.goals.dispatcher import GoalDispatcher

from tests.unit.agents.task.goals.test_dispatcher_run_outcome import (
    _CaseStudyAgent, _FakeBoard,
)


class _ProbeAgent(_CaseStudyAgent):
    def __init__(self):
        super().__init__(done_text="Posted.\nOUTCOME: done")
        self.seen = []

    async def run_session(self, user_id, session_id):
        self.seen.append(am.job_task_for_session(session_id))
        return "Session completed successfully"


def _run(goal):
    agent = _ProbeAgent()
    asyncio.run(GoalDispatcher(_FakeBoard(), agent)._run_goal(goal))
    return agent.seen


def _cleanup(*gids):
    for gid in gids:
        am._GOALS.pop(gid, None)
    am._SESSIONS.pop("s43de", None)


def test_every_goal_run_is_recorded():
    try:
        for gid, payload in (("g-owner", {"owner_granted": True}),
                             ("g-agent", {"created_by_session_id": "s1"}),
                             ("g-bare", None)):
            seen = _run(Goal(id=gid, user_id="u1", title="Post the daily update on X",
                             payload=payload))
            assert seen == ["Post the daily update on X"], gid
    finally:
        _cleanup("g-owner", "g-agent", "g-bare")


def test_a_recorded_goal_session_loads_beyond_the_ceiling(monkeypatch):
    from agents.task import session_class
    from tools import goal_tools
    from tools.tool_disclosure import disclosure_ceiling
    from types import SimpleNamespace
    monkeypatch.setattr(session_class, "is_autonomous_session", lambda sid: True)
    monkeypatch.setattr(goal_tools, "allowed_self_goal_tools", lambda: frozenset({"web_fetch"}))
    am.mark_autonomous("goal-run", "g-o")
    am.note_goal("g-o", "Ship the weekly release")
    am.mark_autonomous("planner-run", None)
    try:
        assert disclosure_ceiling(SimpleNamespace(session_id="goal-run")) is None
        assert disclosure_ceiling(SimpleNamespace(session_id="planner-run")) is not None
    finally:
        for sid in ("goal-run", "planner-run"):
            am._SESSIONS.pop(sid, None)
        am._GOALS.pop("g-o", None)
