"""The goal self-wake targets the CREATING session, never the run session.

Prod 2026-08-24..28: the dispatcher woke the goal's own just-finished session
134 times; 126 of those turns closed as "self-wake is just the completion echo
of the goal I already finished this session" (9.25M input tokens). A session
that just produced the result has nothing to act on; the session that ASKED
for the work does.
"""
import asyncio

from agents.task.goals.board import Goal
from agents.task.goals.dispatcher import GoalDispatcher


class _Agent:
	def __init__(self):
		self.calls = []

	async def deliver_self_wake(self, session_id, user_id, text, metadata=None):
		self.calls.append((session_id, user_id, metadata))
		return True


def _dispatcher(agent):
	d = GoalDispatcher.__new__(GoalDispatcher)
	d.task_agent = agent
	d._completion_text = lambda goal, final: f"done: {final}"
	d._mark_episode_surfaced = lambda goal, sid: None
	return d


def _goal(payload=None):
	return Goal(id="g1", user_id="rob", title="t", body="b", payload=dict(payload or {}))


def test_run_session_is_never_woken_with_its_own_result():
	agent = _Agent()
	asyncio.run(_dispatcher(agent)._self_wake(_goal(), "run-1", "ok"))
	asyncio.run(_dispatcher(agent)._self_wake(
		_goal({"origin_session_id": "run-1"}), "run-1", "ok"))
	assert agent.calls == []


def test_distinct_origin_session_is_woken():
	agent = _Agent()
	asyncio.run(_dispatcher(agent)._self_wake(
		_goal({"origin_session_id": "chat-9"}), "run-1", "ok"))
	assert len(agent.calls) == 1
	sid, uid, meta = agent.calls[0]
	assert (sid, uid) == ("chat-9", "rob")
	assert meta["goal_id"] == "g1" and meta["run_session_id"] == "run-1"
