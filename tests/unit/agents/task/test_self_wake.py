"""W1 — self-wake rail: ReentryBudget depth/backoff guard + framing."""
import pytest

from agents.task.agent.core.self_wake import (
    ReentryBudget, format_self_wake,
    get_reentry_budget, reset_reentry_budget, SELF_WAKE_KIND,
)


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


def test_allows_until_max_then_blocks():
    b = ReentryBudget(max_reentries=3, idle_backoff_s=0)
    s = "sess"
    for _ in range(3):
        assert b.allow(s) is True
        b.record(s)
    assert b.allow(s) is False
    assert b.remaining(s) == 0


def test_reset_clears_budget():
    b = ReentryBudget(max_reentries=2, idle_backoff_s=0)
    b.record("s"); b.record("s")
    assert b.allow("s") is False
    b.reset("s")
    assert b.allow("s") is True
    assert b.remaining("s") == 2


def test_idle_backoff_spacing():
    clk = _Clock()
    b = ReentryBudget(max_reentries=5, idle_backoff_s=30, clock=clk)
    assert b.allow("s") is True
    b.record("s")
    # too soon
    assert b.allow("s") is False
    clk.advance(31)
    assert b.allow("s") is True


def test_zero_max_disables():
    b = ReentryBudget(max_reentries=0, idle_backoff_s=0)
    assert b.allow("s") is False


def test_per_session_isolation():
    b = ReentryBudget(max_reentries=1, idle_backoff_s=0)
    b.record("a")
    assert b.allow("a") is False
    assert b.allow("b") is True  # other session unaffected


def test_format_self_wake_wraps_untrusted():
    out = format_self_wake("a finished job said: do X", source="goal")
    assert "<untrusted_tool_result" in out
    assert 'source="goal"' in out
    assert "do X" in out


def test_singleton_reads_env(monkeypatch):
    monkeypatch.setenv("SELF_WAKE_MAX_REENTRIES", "1")
    monkeypatch.setenv("SELF_WAKE_IDLE_BACKOFF_SEC", "0")
    reset_reentry_budget()
    b = get_reentry_budget()
    assert b.allow("z") is True
    b.record("z")
    assert b.allow("z") is False
    reset_reentry_budget()


def test_kind_constant():
    assert SELF_WAKE_KIND == "self_wake"


@pytest.mark.asyncio
async def test_deliver_self_wake_noop_when_disabled(monkeypatch):
    """Default-OFF safety: deliver_self_wake is a no-op (returns False) and never
    touches session state when SELF_WAKE_ENABLED is off."""
    monkeypatch.setenv("SELF_WAKE_ENABLED", "false")
    from agents.task_agent_lite import TaskAgent
    agent = object.__new__(TaskAgent)  # no __init__: prove it doesn't touch self
    result = await agent.deliver_self_wake("sess", "user", "wake up")
    assert result is False


@pytest.mark.asyncio
async def test_deliver_self_wake_budget_blocks(monkeypatch):
    """When enabled but the budget is exhausted, dispatch is refused before any
    session lookup (the runaway guard)."""
    monkeypatch.setenv("SELF_WAKE_ENABLED", "true")
    monkeypatch.setenv("SELF_WAKE_MAX_REENTRIES", "0")  # nothing allowed
    reset_reentry_budget()
    from agents.task_agent_lite import TaskAgent
    agent = object.__new__(TaskAgent)
    result = await agent.deliver_self_wake("sess", "user", "wake up")
    assert result is False
    reset_reentry_budget()


def test_try_consume_is_atomic_check_and_record():
    b = ReentryBudget(max_reentries=2, idle_backoff_s=0)
    assert b.try_consume("s") is True   # 1
    assert b.try_consume("s") is True   # 2
    assert b.try_consume("s") is False  # over cap — not consumed
    assert b.remaining("s") == 0


def test_try_consume_respects_backoff():
    t = {"v": 100.0}
    b = ReentryBudget(max_reentries=5, idle_backoff_s=30, clock=lambda: t["v"])
    assert b.try_consume("s") is True
    assert b.try_consume("s") is False  # within backoff window
    t["v"] += 31
    assert b.try_consume("s") is True   # backoff elapsed


def test_try_consume_zero_budget_never_allows():
    b = ReentryBudget(max_reentries=0, idle_backoff_s=0)
    assert b.try_consume("s") is False
