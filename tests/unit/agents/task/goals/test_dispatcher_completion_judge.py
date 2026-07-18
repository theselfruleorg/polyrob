"""§3.2 dispatcher wiring — the completion judge gates record_success.

Decision matrix pinned here (no string-matching side channels — owner
directive: capability/platform knowledge lives in agent memory/skills):
- judge OFF or no acceptance -> legacy success path, judge never called
- verdict 'unmet'            -> record_failure (normal breaker retries)
- verdict 'met'/'unclear'    -> success (never block on uncertainty)
"""
import asyncio

from agents.task.goals.board import Goal, STATUS_BLOCKED, STATUS_READY
from agents.task.goals.dispatcher import GoalDispatcher


class _FakeBoard:
    def __init__(self):
        self.successes, self.failures, self.blocked, self.asks = [], [], [], []
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
        return True

    def create_ask(self, **kw):
        self.asks.append(kw)
        return None


class _Agent:
    async def create_session(self, *, user_id, request):
        return {"id": "s1"}

    async def run_session(self, user_id, session_id):
        return "OUTCOME: posted the announcement"

    def get_orchestrator(self, session_id):
        return None

    deliver_self_wake = None


def _goal(acceptance="a live tweet URL"):
    payload = {"acceptance": acceptance} if acceptance else {}
    return Goal(id="g1", user_id="u1", title="Post announcement", payload=payload)


def _run(monkeypatch, *, judge_flag, verdict=None, acceptance="a live tweet URL"):
    board = _FakeBoard()
    disp = GoalDispatcher(board, _Agent())
    monkeypatch.setenv("GOAL_COMPLETION_JUDGE", "true" if judge_flag else "false")

    calls = {"judge": 0}

    async def _fake_judge(task_agent, session_id, goal, final, **kw):
        calls["judge"] += 1
        return (verdict or "unclear", "test reason")

    import agents.task.goals.completion_judge as cj
    monkeypatch.setattr(cj, "judge_run_outcome", _fake_judge)

    asyncio.run(disp._run_goal(_goal(acceptance)))
    return board, calls


def test_judge_off_is_zero_behavior_change(monkeypatch):
    board, calls = _run(monkeypatch, judge_flag=False, verdict="unmet")
    assert board.successes == ["g1"]
    assert calls["judge"] == 0


def test_no_acceptance_still_judges_when_on(monkeypatch):
    """§4.3 re-basing: the evidence judge runs for every autonomous completion —
    acceptance prose is an optional sharpener, no longer the gate."""
    board, calls = _run(monkeypatch, judge_flag=True, verdict="unmet", acceptance=None)
    assert not board.successes
    assert calls["judge"] == 1


def test_unmet_verdict_records_failure_with_reason(monkeypatch):
    board, calls = _run(monkeypatch, judge_flag=True, verdict="unmet")
    assert not board.successes
    assert calls["judge"] == 1
    assert board.failures and "completion judge" in board.failures[0][1]
    assert not board.blocked, "judge-unmet keeps normal breaker retries (no immediate block)"


def test_met_and_unclear_verdicts_pass(monkeypatch):
    for v in ("met", "unclear"):
        board, _ = _run(monkeypatch, judge_flag=True, verdict=v)
        assert board.successes == ["g1"], f"verdict {v} must pass"
        assert not board.failures
