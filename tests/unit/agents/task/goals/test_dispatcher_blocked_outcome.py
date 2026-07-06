"""§3.1 dispatcher wiring — an agent-declared BLOCKED outcome must fail the goal.

A final message ending 'OUTCOME: BLOCKED — <need>' means the agent itself
concluded the goal cannot be met; _run_goal must route it to record_failure
(→ breaker → escalation rail) + immediate block, NEVER record_success or
self-wake. A normal outcome stays byte-identical to the legacy success path.
"""
import asyncio

from agents.task.goals.board import Goal, STATUS_BLOCKED, STATUS_READY
from agents.task.goals.dispatcher import GoalDispatcher


class _FakeBoard:
    def __init__(self):
        self.successes, self.failures, self.blocked, self.outcomes = [], [], [], []
        self._status = "running"

    def record_success(self, gid, session_id=None, result=None):
        self.successes.append(gid)
        self._status = "done"

    def record_failure(self, gid, error=None, session_id=None):
        self.failures.append((gid, error))
        self._status = STATUS_READY
        return Goal(id=gid, user_id="u1", title="t", status=STATUS_READY)

    def block_from_ready(self, gid, *, error):
        self.blocked.append((gid, error))
        self._status = STATUS_BLOCKED
        return True

    def get(self, gid):
        return Goal(id=gid, user_id="u1", title="t", status=self._status)

    def set_outcome(self, gid, outcome):
        self.outcomes.append((gid, outcome))
        return True

    def create_ask(self, **kw):
        return None


class _BlockedAgent:
    async def create_session(self, *, user_id, request):
        return {"id": "s1"}

    async def run_session(self, user_id, session_id):
        return "Drafted the tweet but cannot post.\nOUTCOME: BLOCKED — Twitter write is disabled"

    deliver_self_wake = None


def test_blocked_outcome_fails_and_blocks_instead_of_done():
    board = _FakeBoard()
    disp = GoalDispatcher(board, _BlockedAgent())
    goal = Goal(id="g1", user_id="u1", title="Post the announcement")
    asyncio.run(disp._run_goal(goal))
    assert not board.successes, "a BLOCKED declaration must never be recorded as success"
    assert board.failures and "Twitter write is disabled" in board.failures[0][1]
    assert board.blocked, "agent-declared BLOCKED must skip remaining retries and block now"


def test_blocked_outcome_is_recorded_on_the_goal():
    board = _FakeBoard()
    disp = GoalDispatcher(board, _BlockedAgent())
    goal = Goal(id="g1", user_id="u1", title="Post the announcement")
    asyncio.run(disp._run_goal(goal))
    assert board.outcomes and board.outcomes[0][1].startswith("BLOCKED")


def test_blocked_outcome_respects_owner_cancel():
    """If the owner cancelled mid-run (record_failure returns a non-ready row),
    the dispatcher must NOT force the row to blocked."""
    board = _FakeBoard()

    def _rf(gid, error=None, session_id=None):
        board.failures.append((gid, error))
        board._status = "cancelled"
        return Goal(id=gid, user_id="u1", title="t", status="cancelled")

    board.record_failure = _rf
    disp = GoalDispatcher(board, _BlockedAgent())
    goal = Goal(id="g1", user_id="u1", title="Post the announcement")
    asyncio.run(disp._run_goal(goal))
    assert not board.blocked, "owner cancel must win over the BLOCKED declaration"


def test_normal_outcome_still_records_success():
    class _GoodAgent:
        async def create_session(self, *, user_id, request):
            return {"id": "s2"}

        async def run_session(self, user_id, session_id):
            return "done\nOUTCOME: wrote project/a.md"

        deliver_self_wake = None

    board = _FakeBoard()
    disp = GoalDispatcher(board, _GoodAgent())
    goal = Goal(id="g2", user_id="u1", title="good goal")
    asyncio.run(disp._run_goal(goal))
    assert board.successes == ["g2"]
    assert not board.failures and not board.blocked
