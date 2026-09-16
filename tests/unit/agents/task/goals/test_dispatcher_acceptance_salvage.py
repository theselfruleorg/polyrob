"""FIX 6 — a run that produced every declared deliverable is not a failure just
because it never called done().

`_run_goal` routed `run.done_called is False` straight to `_fail_run` BEFORE the
typed acceptance-check block, so a run that exhausted `max_steps` while PRODUCING
everything the goal declared was auto-failed anyway (open since the 2026-08-20
review, which proposed scoring by acceptance checks instead).

Boundary implemented:
  - `done_called is False` AND the goal declares `acceptance_checks` AND every
    check passes  -> success, recorded honestly ("no explicit done()").
  - any check failing, or NO checks declared -> the existing failure path,
    unchanged.
  - `done_called is None` (undeterminable) -> untouched legacy fall-through.
  - `run.all_actions_errored` still fails: nothing executed successfully, so the
    deliverables cannot be this run's work.
"""
import asyncio
from types import SimpleNamespace

import pytest

from agents.task.goals.board import Goal, STATUS_BLOCKED, STATUS_READY
from agents.task.goals.dispatcher import GoalDispatcher
from agents.task.runtime.run_outcome import RunOutcome

CHECKS = [{"type": "artifact", "path": "report.md"}]


class _FakeBoard:
    def __init__(self):
        self.successes, self.failures, self.outcomes, self.results = [], [], [], []
        self._status = "running"

    def record_success(self, gid, session_id=None, result=None):
        self.successes.append(gid)
        self.results.append(result)
        self._status = "done"

    def record_failure(self, gid, error=None, session_id=None):
        self.failures.append((gid, error))
        self._status = STATUS_READY
        return Goal(id=gid, user_id="u1", title="t", status=STATUS_READY)

    def block_from_ready(self, gid, *, error):
        self._status = STATUS_BLOCKED
        return True

    def get(self, gid):
        return Goal(id=gid, user_id="u1", title="t", status=self._status)

    def set_outcome(self, gid, outcome):
        self.outcomes.append((gid, outcome))
        return True

    def create_ask(self, **kw):
        return None


def _outcome(**kw) -> RunOutcome:
    base = dict(session_id="s-acc", status="Session completed successfully",
                steps=20, spend_usd=0.10, artifacts=[{"path": "report.md"}])
    base.update(kw)
    return RunOutcome(**base)


def _patch_run(monkeypatch, outcome):
    async def _fake(task_agent, *, user_id, request, autonomous=False, **_kw):
        # **_kw so a NEW kwarg on the real `run_task_to_outcome` (039 added
        # `goal_id`, to let an owner-queue ask name the goal it blocks) does
        # not fail 20 tests that never cared about it.
        return outcome
    monkeypatch.setattr("agents.task.goals.dispatcher._run_task_to_outcome", _fake)


def _patch_checks(monkeypatch, results):
    calls = []

    async def _run_checks(checks, **kw):
        calls.append(checks)
        return list(results)

    monkeypatch.setattr(
        "agents.task.runtime.acceptance_checks.run_acceptance_checks", _run_checks)
    return calls


def _dispatcher():
    board = _FakeBoard()
    agent = SimpleNamespace(deliver_self_wake=None, container=None)
    return board, GoalDispatcher(board, agent)


def _goal(**payload):
    return Goal(id="g-acc", user_id="u1", title="write the report",
                payload=dict(payload))


PASSING = [{"type": "artifact", "ok": True, "detail": "report.md exists"}]
FAILING = [{"type": "artifact", "ok": False, "detail": "report.md missing"}]


def test_no_done_but_all_acceptance_checks_pass_is_a_success(monkeypatch):
    board, disp = _dispatcher()
    _patch_run(monkeypatch, _outcome(done_called=False))
    calls = _patch_checks(monkeypatch, PASSING)

    asyncio.run(disp._run_goal(_goal(acceptance_checks=CHECKS)))

    assert board.successes == ["g-acc"], \
        "every declared deliverable exists — the missing done() is not a failure"
    assert not board.failures
    assert calls, "the acceptance checks must actually run"
    assert "done()" in (board.results[0] or ""), \
        "record the completion honestly: it finished without an explicit done()"


def test_the_checks_run_only_once(monkeypatch):
    board, disp = _dispatcher()
    _patch_run(monkeypatch, _outcome(done_called=False))
    calls = _patch_checks(monkeypatch, PASSING)
    asyncio.run(disp._run_goal(_goal(acceptance_checks=CHECKS)))
    assert len(calls) == 1, "acceptance checks may have side effects — never run them twice"


def test_no_done_with_a_failing_check_still_fails(monkeypatch):
    board, disp = _dispatcher()
    _patch_run(monkeypatch, _outcome(done_called=False))
    _patch_checks(monkeypatch, FAILING)
    asyncio.run(disp._run_goal(_goal(acceptance_checks=CHECKS)))
    assert not board.successes
    assert board.failures and "report.md missing" in board.failures[0][1]


def test_no_done_and_no_checks_declared_fails_exactly_as_before(monkeypatch):
    board, disp = _dispatcher()
    _patch_run(monkeypatch, _outcome(done_called=False))
    asyncio.run(disp._run_goal(_goal()))
    assert not board.successes
    assert board.failures
    assert "no done()" in board.failures[0][1]


def test_all_actions_errored_is_not_salvaged_by_passing_checks(monkeypatch):
    """The neighbouring invariant must not weaken: nothing executed successfully."""
    board, disp = _dispatcher()
    _patch_run(monkeypatch, _outcome(done_called=False, all_actions_errored=True))
    _patch_checks(monkeypatch, PASSING)
    asyncio.run(disp._run_goal(_goal(acceptance_checks=CHECKS)))
    assert not board.successes
    assert board.failures


def test_done_called_none_falls_through_unchanged(monkeypatch):
    """Undeterminable stays on the legacy path (success, checks still enforced)."""
    board, disp = _dispatcher()
    _patch_run(monkeypatch, _outcome(done_called=None))
    _patch_checks(monkeypatch, PASSING)
    asyncio.run(disp._run_goal(_goal(acceptance_checks=CHECKS)))
    assert board.successes == ["g-acc"]
    assert "done()" not in (board.results[0] or "") or "without" not in (board.results[0] or "")


def test_the_completion_judge_is_not_consulted_for_a_salvaged_run(monkeypatch):
    """There is no completion CLAIM to weigh against the evidence — the typed
    checks ARE the verdict."""
    board, disp = _dispatcher()
    _patch_run(monkeypatch, _outcome(done_called=False))
    _patch_checks(monkeypatch, PASSING)
    monkeypatch.setenv("GOAL_COMPLETION_JUDGE", "true")
    judged = []

    async def _judge(task_agent, session_id, goal, run):
        judged.append(goal.id)
        return "unmet", "no completion claim"

    monkeypatch.setattr("agents.task.goals.completion_judge.judge_run_outcome", _judge)
    asyncio.run(disp._run_goal(_goal(acceptance_checks=CHECKS)))
    assert judged == []
    assert board.successes == ["g-acc"]
