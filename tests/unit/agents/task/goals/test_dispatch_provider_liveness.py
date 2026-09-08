"""Goal dispatch pauses only when NOTHING can serve — not when the default dies.

From 2026-08-17 19:35Z prod dispatched zero goals: the default provider
(zai-coding) was credit-dead, so the whole dispatch gate returned 0 on every
tick. Whether a SECOND credentialed provider was alive was never asked.

A per-goal provider pin gets the same treatment as a cron pin: prefer it, but do
not stop working forever because a month-old preference died.
"""
import pytest

import core.runtime_config as rc
from agents.task.goals.dispatcher import GoalDispatcher


@pytest.fixture
def two_providers(monkeypatch):
    monkeypatch.setattr(rc, "usable_providers_with_credentials",
                        lambda env=None: ["zai-coding", "openrouter"], raising=False)


def _dead(*names):
    dead = set(names)
    return lambda provider=None: (bool(dead) if provider is None else provider in dead)


def test_dispatch_is_not_paused_while_a_live_provider_remains(two_providers, monkeypatch):
    monkeypatch.setattr(rc, "_sentinel_active", _dead("zai-coding"), raising=False)
    assert GoalDispatcher.dispatch_blocked_by_providers() is False


def test_dispatch_pauses_when_every_provider_is_dead(two_providers, monkeypatch):
    import core.credit_sentinel as cs
    monkeypatch.setattr(rc, "_sentinel_active",
                        _dead("zai-coding", "openrouter"), raising=False)
    # Pausing needs a genuinely tripped latch, not merely an empty live set.
    monkeypatch.setattr(cs, "credit_sentinel_active", lambda provider=None: True,
                        raising=False)
    assert GoalDispatcher.dispatch_blocked_by_providers() is True


def test_a_goals_dead_provider_pin_reroutes(two_providers, monkeypatch):
    monkeypatch.setattr(rc, "_sentinel_active", _dead("zai-coding"), raising=False)
    assert rc.resolve_live_provider("zai-coding") == "openrouter"


# --- 2026-08-18 intel finding: _run_goal must not spend on a goal whose own
# effective provider resolution comes back empty (nothing can serve it, even
# though the once-per-tick dispatch_blocked_by_providers gate had passed
# earlier). Goal 872943753c2e burned ~$0.31 across 2 guaranteed-402 attempts
# against a pinned-dead OpenRouter before this fix. ------------------------

import asyncio

from agents.task.goals.dispatcher import GoalDispatcher
from agents.task.goals.board import Goal


class _FakeBoard:
    def __init__(self):
        self.successes, self.failures = [], []

    def record_success(self, gid, session_id=None, result=None):
        self.successes.append(gid)

    def record_failure(self, gid, error=None, session_id=None):
        self.failures.append((gid, error))


class _SpendTrackingAgent:
    """create_session/run_session record a call — used to prove NO spend
    happens when nothing can serve the goal's own provider."""

    def __init__(self):
        self.called = False

    async def create_session(self, *, user_id, request):
        self.called = True
        return {"id": "should-not-run"}

    async def run_session(self, user_id, session_id):
        self.called = True
        return "should not have run"

    deliver_self_wake = None


def test_run_goal_skips_without_spending_when_its_own_provider_cannot_serve(
        two_providers, monkeypatch):
    """Both zai-coding (default) and openrouter (this goal's pin) are dead —
    resolve_live_provider must return None for this goal, and _run_goal must
    record a failure WITHOUT ever calling create_session/run_session."""
    monkeypatch.setattr(rc, "_sentinel_active",
                        _dead("zai-coding", "openrouter"), raising=False)
    monkeypatch.setattr(rc, "resolve_default_provider",
                        lambda: ("zai-coding", "glm-5"), raising=False)

    board = _FakeBoard()
    agent = _SpendTrackingAgent()
    disp = GoalDispatcher(board, agent)
    goal = Goal(id="g-dead-pin", user_id="u1", title="pinned goal",
                payload={"provider": "openrouter"})
    asyncio.run(disp._run_goal(goal))

    assert not agent.called, "must not spend an attempt when nothing can serve this goal"
    assert not board.successes
    assert board.failures, "must record a failure instead of silently doing nothing"
    gid, error = board.failures[0]
    assert gid == "g-dead-pin"
    assert error.startswith("llm_provider_exhausted:"), error
    assert "openrouter" in error and "zai-coding" in error


def test_run_goal_still_runs_when_the_default_is_live(two_providers, monkeypatch):
    """Regression guard: the fix must not block the healthy case — an
    unpinned goal with a live default provider still runs normally."""
    monkeypatch.setattr(rc, "_sentinel_active", lambda provider=None: False, raising=False)
    monkeypatch.setattr(rc, "resolve_default_provider",
                        lambda: ("zai-coding", "glm-5"), raising=False)

    board = _FakeBoard()
    agent = _SpendTrackingAgent()
    disp = GoalDispatcher(board, agent)
    goal = Goal(id="g-healthy", user_id="u1", title="normal goal")
    asyncio.run(disp._run_goal(goal))

    assert agent.called, "a goal with a live provider must still run"
