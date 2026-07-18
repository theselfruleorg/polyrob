"""§6.2 fail-closed: a MONEY-ENABLED autonomous run refuses to start unmetered.

The $-per-day budget cap reads usage_records; on a container without a
database_manager that ledger is empty, making the cap advisory on a live
mainnet wallet. Money tools + no metering = refuse the run with a clear error
(never a silent unmetered spend loop). Non-money runs are unaffected.
"""
import asyncio

from agents.task.goals.board import Goal, STATUS_READY
from agents.task.goals.dispatcher import GoalDispatcher


class _Board:
    def __init__(self):
        self.failures = []

    def record_failure(self, gid, error=None, session_id=None):
        self.failures.append((gid, error))
        return Goal(id=gid, user_id="u1", title="t", status=STATUS_READY)

    def create_ask(self, **kw):
        return None


class _NoDbContainer:
    def get_service(self, name):
        return None


class _Agent:
    container = _NoDbContainer()

    def __init__(self):
        self.created = False

    async def create_session(self, *, user_id, request):
        self.created = True
        return {"id": "s1"}

    async def run_session(self, user_id, session_id):
        return "Session completed successfully"


def test_unmetered_money_goal_is_refused():
    board = _Board()
    agent = _Agent()
    d = GoalDispatcher(board, agent)
    goal = Goal(id="g1", user_id="u1", title="invoice someone",
                payload={"tools": ["filesystem", "x402_invoice"]})
    asyncio.run(d._run_goal(goal))
    assert agent.created is False, "the run must not start"
    assert board.failures and "unmetered" in board.failures[0][1].lower()


def test_non_money_goal_runs_unmetered():
    board = _Board()
    agent = _Agent()
    d = GoalDispatcher(board, agent)
    goal = Goal(id="g2", user_id="u1", title="research",
                payload={"tools": ["filesystem", "task"]})
    asyncio.run(d._run_goal(goal))
    assert agent.created is True


def test_metered_money_goal_runs():
    class _Db:
        pass

    class _DbContainer:
        def get_service(self, name):
            return _Db() if name == "database_manager" else None

    board = _Board()
    agent = _Agent()
    agent.container = _DbContainer()
    d = GoalDispatcher(board, agent)
    goal = Goal(id="g3", user_id="u1", title="invoice someone",
                payload={"tools": ["x402_invoice"]})
    asyncio.run(d._run_goal(goal))
    assert agent.created is True
