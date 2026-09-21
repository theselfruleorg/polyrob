"""057 WS-B: a session-class input budget makes compaction REACHABLE.

Compaction fires at 85%/95% of max_input. With a 1,048,576-token model window
(or the prod TASK_MAX_INPUT_TOKENS=200000) those thresholds are 170k/190k while
a real goal history is 36-144k — compaction was not missing, it was unreachable.
"""
from unittest.mock import MagicMock

import pytest

import agents.task.agent.service  # noqa: F401 (import order)
from agents.task.agent.message_manager.service import MessageManager
from agents.task.agent.prompts import SystemPrompt
from agents.task.goals.autonomy_marker import mark_autonomous


def _mm(session_id, max_input=None):
    llm = MagicMock()
    llm.model_name = "gpt-4o"
    return MessageManager(
        llm=llm, task="t", action_descriptions="acts",
        system_prompt_class=SystemPrompt, max_input_tokens=max_input,
        session_id=session_id,
    )


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("AUTONOMOUS_CONTEXT_BUDGET_TOKENS", raising=False)
    monkeypatch.delenv("TASK_MAX_INPUT_TOKENS", raising=False)


def test_off_by_default(monkeypatch):
    mark_autonomous("acb-goal-1", "g1")
    assert _mm("acb-goal-1", 200000).max_input_tokens == 200000


def test_autonomous_session_keeps_the_model_limit_but_gauges_history(monkeypatch):
    """The budget drives the compaction gauge; it never clamps the safety limit
    (a clamped safe limit overflowed a full-rig run at step 1 on prod)."""
    monkeypatch.setenv("AUTONOMOUS_CONTEXT_BUDGET_TOKENS", "96000")
    mark_autonomous("acb-goal-2", "g2")
    mm = _mm("acb-goal-2", 200000)
    assert mm.max_input_tokens == 200000
    assert mm.safe_input_tokens > 96000
    assert mm._history_budget_tokens == 96000


def test_fixed_prefix_never_overflows_a_budgeted_run(monkeypatch):
    """Regression (prod 2026-09-20 04:40Z): 50k of tool schemas + 8k catalog +
    11k foundation with a 27k history must be SAFE and well under the 95%
    emergency line — the budget is about history, not the prefix."""
    monkeypatch.setenv("AUTONOMOUS_CONTEXT_BUDGET_TOKENS", "96000")
    mark_autonomous("acb-goal-5", "g5")
    mm = _mm("acb-goal-5", 200000)
    mm.history.total_tokens = 27000
    mm._self_context_tokens = 11000
    mm._tool_catalog_tokens = 8000
    mm.set_tool_schema_tokens(50000)
    check = mm.check_token_safety(raise_on_overflow=False)
    assert check["safe"], check
    assert mm.get_context_usage_percent() < 85
    # and the same run with a 90k history is at compaction time
    mm.history.total_tokens = 90000
    assert mm.get_context_usage_percent() >= 85   # (safety at this size is the MODEL's call)


def test_an_owner_chat_keeps_the_model_window(monkeypatch):
    monkeypatch.setenv("AUTONOMOUS_CONTEXT_BUDGET_TOKENS", "96000")
    mm = _mm("acb-chat-1", 200000)
    assert mm.max_input_tokens == 200000


def test_task_max_input_tokens_still_works(monkeypatch):
    monkeypatch.setenv("TASK_MAX_INPUT_TOKENS", "50000")
    mm = _mm("acb-chat-2", None)
    assert mm.max_input_tokens == 50000


def test_the_budget_never_RAISES_a_smaller_limit(monkeypatch):
    # A budget is a ceiling, not a target: a session already below it is left alone.
    monkeypatch.setenv("AUTONOMOUS_CONTEXT_BUDGET_TOKENS", "96000")
    mark_autonomous("acb-goal-3", "g3")
    assert _mm("acb-goal-3", 8000).max_input_tokens == 8000


def test_the_threshold_becomes_reachable(monkeypatch):
    """The point of the budget: a 90k history reads as >85% instead of ~45%."""
    monkeypatch.setenv("AUTONOMOUS_CONTEXT_BUDGET_TOKENS", "96000")
    mark_autonomous("acb-goal-4", "g4")
    mm = _mm("acb-goal-4", 200000)
    mm.history.total_tokens = 90000
    assert mm.get_context_usage_percent() >= 85
