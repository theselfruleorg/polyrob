"""Task 0.2 — GoalDispatcher must treat run_session refusal strings as failure.

A refusal string (e.g. "No active session found") is truthy, so the bare
``if final:`` in _run_goal recorded it as SUCCESS and self-woke a follow-up turn.
This test pins the corrected behaviour: refusal → record_failure, no self-wake.
"""
import asyncio
import pytest

from agents.task.goals.dispatcher import GoalDispatcher
from agents.task.goals.board import Goal


class _FakeBoard:
    def __init__(self):
        self.successes, self.failures = [], []

    def record_success(self, gid, session_id=None, result=None):
        self.successes.append(gid)

    def record_failure(self, gid, error=None, session_id=None):
        self.failures.append((gid, error))


class _FakeAgent:
    """Agent whose run_session returns a known refusal string."""

    async def create_session(self, *, user_id, request):
        return {"id": "s1"}

    async def run_session(self, user_id, session_id):
        return "No active session found"

    deliver_self_wake = None  # no self-wake capability wired


def test_refusal_string_is_recorded_as_failure_not_success():
    board = _FakeBoard()
    disp = GoalDispatcher(board, _FakeAgent())
    goal = Goal(id="g1", user_id="u1", title="test goal")
    asyncio.run(disp._run_goal(goal))
    assert board.failures, "a run_session refusal string must be recorded as failure"
    assert not board.successes, "a run_session refusal string must NOT be recorded as success"
    assert board.failures[0][0] == "g1"


def test_genuine_result_is_recorded_as_success():
    """Sanity: a real result string still goes to record_success (regression guard)."""

    class _GoodAgent:
        async def create_session(self, *, user_id, request):
            return {"id": "s2"}

        async def run_session(self, user_id, session_id):
            return "Task complete: wrote the report."

        deliver_self_wake = None

    board = _FakeBoard()
    disp = GoalDispatcher(board, _GoodAgent())
    goal = Goal(id="g2", user_id="u1", title="good goal")
    asyncio.run(disp._run_goal(goal))
    assert board.successes, "a genuine result must be recorded as success"
    assert not board.failures, "a genuine result must NOT be recorded as failure"
