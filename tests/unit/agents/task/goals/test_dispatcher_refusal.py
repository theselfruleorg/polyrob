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


def test_budget_halt_refusal_records_greppable_marker():
    """T1.1 validation fix (2026-07-23): a RUN_BUDGET_USD halt was recorded as
    the generic 'run did not complete (refusal or empty)' — indistinguishable
    on the board from any other refusal. The marker must prefix
    goals.last_failure_error, and it must NOT be labeled a provider outage
    (the halt text embeds dollar amounts that can satisfy the credit-death
    classifier, e.g. a $402 budget)."""

    class _BudgetAgent:
        async def create_session(self, *, user_id, request):
            return {"id": "s-budget"}

        async def run_session(self, user_id, session_id):
            return ("Session failed: run_budget_exhausted: session provider "
                    "spend $402.0000 reached RUN_BUDGET_USD $402.00; halting "
                    "before the next step")

        deliver_self_wake = None

    board = _FakeBoard()
    disp = GoalDispatcher(board, _BudgetAgent())
    goal = Goal(id="g-budget", user_id="u1", title="budget goal")
    asyncio.run(disp._run_goal(goal))
    assert not board.successes
    assert board.failures, "a budget halt must be recorded as failure"
    gid, error = board.failures[0]
    assert gid == "g-budget"
    assert error.startswith("run_budget_exhausted:"), error
    assert not error.startswith("llm_provider_exhausted"), (
        "a budget halt must never be labeled a provider outage: %s" % error)


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


# --- T2-01: a run that finished the loop but never called done() (max_steps
#     exhaustion / conversational drift) returns a non-refusal STATUS string that
#     looks like success. The board must record it as FAILURE, not success. ---

class _OrchAgent:
    """Task-agent whose run_session returns the generic success STATUS string, but
    whose resident orchestrator reveals whether the main agent actually called done()."""

    def __init__(self, done):
        self._done = done

    async def create_session(self, *, user_id, request):
        return {"id": "s3"}

    async def run_session(self, user_id, session_id):
        return "Session completed successfully"

    def get_orchestrator(self, session_id):
        done = self._done

        class _R:
            is_done = done

        class _A:
            _last_result = [_R()]
            _is_sub_agent = False

        class _O:
            agents = {"main": _A()}

        return _O()

    deliver_self_wake = None


def test_exhausted_run_without_done_is_recorded_as_failure():
    board = _FakeBoard()
    disp = GoalDispatcher(board, _OrchAgent(done=False))
    goal = Goal(id="g3", user_id="u1", title="post the announcement")
    asyncio.run(disp._run_goal(goal))
    assert board.failures, "a run that never called done() must be recorded as failure"
    assert not board.successes, "an exhausted run must NOT be recorded as success"


def test_genuine_done_run_is_recorded_as_success():
    board = _FakeBoard()
    disp = GoalDispatcher(board, _OrchAgent(done=True))
    goal = Goal(id="g4", user_id="u1", title="post the announcement")
    asyncio.run(disp._run_goal(goal))
    assert board.successes, "a run that called done() must be recorded as success"
    assert not board.failures
