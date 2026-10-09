"""An OWNER-authored goal run carries the owner's standing authority, the same
as an owner-authored cron job (CHAT-5): the dispatcher records it, so X posts
and self-service tool loading treat it like the owner's cron job. An agent-
authored or unstamped goal is never recorded (AGT-14 stays closed), and money
tools stay explicit-grant-only."""
import asyncio

from agents.task.goals import autonomy_marker as am
from agents.task.goals.board import Goal
from agents.task.goals.dispatcher import GoalDispatcher, _owner_authored_goal

from tests.unit.agents.task.goals.test_dispatcher_run_outcome import (
    _CaseStudyAgent, _FakeBoard,
)


class _ProbeAgent(_CaseStudyAgent):
    def __init__(self):
        super().__init__(done_text="Posted.\nOUTCOME: done")
        self.seen = []

    async def run_session(self, user_id, session_id):
        self.seen.append(am.owner_job_task_for_session(session_id))
        return "Session completed successfully"


def _run(goal):
    agent = _ProbeAgent()
    asyncio.run(GoalDispatcher(_FakeBoard(), agent)._run_goal(goal))
    return agent.seen


def _cleanup(*gids):
    for gid in gids:
        am._OWNER_GOALS.pop(gid, None)
    am._SESSIONS.pop("s43de", None)


def test_owner_authored_goal_run_is_recorded_as_the_owners():
    try:
        seen = _run(Goal(id="g-owner", user_id="u1", title="Post the daily update on X",
                         payload={"authored_by": "owner"}))
        assert seen == ["Post the daily update on X"]
    finally:
        _cleanup("g-owner")


def test_seat_granted_goal_is_owner_authored():
    assert _owner_authored_goal({"owner_granted": True, "stream": "x"})


def test_agent_authored_or_unstamped_goal_is_never_recorded():
    try:
        assert _run(Goal(id="g-agent", user_id="u1", title="post",
                         payload={"authored_by": "agent"})) == [None]
        assert _run(Goal(id="g-legacy", user_id="u1", title="post",
                         payload={"created_by_session_id": "s1"})) == [None]
        assert _run(Goal(id="g-bare", user_id="u1", title="post")) == [None]
    finally:
        _cleanup("g-agent", "g-legacy", "g-bare")


def test_goal_rewritten_by_the_agent_loses_its_record():
    """goal_update by a non-owner turn stamps authored_by=agent; the next run
    must drop the earlier owner record, not reuse it."""
    try:
        _run(Goal(id="g-flip", user_id="u1", title="t", payload={"authored_by": "owner"}))
        assert "g-flip" in am._OWNER_GOALS
        assert _run(Goal(id="g-flip", user_id="u1", title="t",
                         payload={"authored_by": "agent"})) == [None]
        assert "g-flip" not in am._OWNER_GOALS
    finally:
        _cleanup("g-flip")


def test_owner_goal_session_loads_beyond_ceiling_but_money_stays_gated(monkeypatch):
    from agents.task import session_class
    from tools import goal_tools
    from tools.tool_disclosure import disclosure_ceiling
    from types import SimpleNamespace
    monkeypatch.setattr(session_class, "is_autonomous_session", lambda sid: True)
    monkeypatch.setattr(goal_tools, "allowed_self_goal_tools", lambda: frozenset({"web_fetch"}))
    am.mark_autonomous("owner-goal-run", "g-o")
    am.note_owner_goal("g-o", "Ship the weekly release")
    am.mark_autonomous("agent-goal-run", "g-a")
    try:
        assert disclosure_ceiling(SimpleNamespace(session_id="owner-goal-run")) is None
        assert disclosure_ceiling(SimpleNamespace(session_id="agent-goal-run")) is not None
    finally:
        for sid in ("owner-goal-run", "agent-goal-run"):
            am._SESSIONS.pop(sid, None)
        am._OWNER_GOALS.pop("g-o", None)
