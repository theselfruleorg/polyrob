"""P1 — delegate_task policy + param model.

The role/depth gate is a pure function so it can be tested without building a
full Controller + orchestrator + SubAgentManager (mirrors how the pre_tool_call
hook seam was unit-tested).
"""
import pytest
from pydantic import ValidationError

from tools.controller.delegation import evaluate_delegation, DelegationDecision
from tools.controller.views import DelegateTaskAction


# --- DelegateTaskAction model -------------------------------------------------

def test_goal_only_is_valid():
    a = DelegateTaskAction(goal="research the polymarket BTC market in depth")
    assert a.goal and a.tasks is None
    assert a.role == "leaf"  # default: spawned child cannot recurse


def test_tasks_only_is_valid():
    a = DelegateTaskAction(tasks=[
        {"task": "scrape the first source page thoroughly"},
        {"task": "scrape the second source page thoroughly"},
    ])
    assert a.tasks and a.goal is None
    assert len(a.tasks) == 2


def test_goal_and_tasks_both_set_is_rejected():
    with pytest.raises(ValidationError):
        DelegateTaskAction(
            goal="some sufficiently long goal text here",
            tasks=[{"task": "a sufficiently long subtask one"},
                   {"task": "a sufficiently long subtask two"}],
        )


def test_neither_goal_nor_tasks_is_rejected():
    with pytest.raises(ValidationError):
        DelegateTaskAction()


def test_role_must_be_leaf_or_orchestrator():
    with pytest.raises(ValidationError):
        DelegateTaskAction(goal="a sufficiently long goal string", role="boss")


# --- evaluate_delegation policy ----------------------------------------------

def test_disabled_denies():
    d = evaluate_delegation(
        enabled=False, caller_is_sub_agent=False, caller_role="orchestrator",
        requested_child_role="leaf", max_depth=1,
    )
    assert isinstance(d, DelegationDecision)
    assert d.allowed is False
    assert "disabled" in d.reason.lower()


def test_main_orchestrator_may_delegate_child_clamped_to_leaf_at_depth1():
    d = evaluate_delegation(
        enabled=True, caller_is_sub_agent=False, caller_role="orchestrator",
        requested_child_role="orchestrator", max_depth=1,
    )
    assert d.allowed is True
    assert d.reason is None
    # depth-1 system: the child sits AT max depth, so it cannot itself recurse
    assert d.child_role == "leaf"


def test_leaf_caller_is_denied():
    d = evaluate_delegation(
        enabled=True, caller_is_sub_agent=True, caller_role="leaf",
        requested_child_role="leaf", max_depth=1,
    )
    assert d.allowed is False
    assert "depth" in d.reason.lower() or "leaf" in d.reason.lower()


def test_sub_agent_caller_denied_even_if_role_orchestrator_at_max_depth():
    # caller is already at depth 1; max_depth 1 => cannot go deeper
    d = evaluate_delegation(
        enabled=True, caller_is_sub_agent=True, caller_role="orchestrator",
        requested_child_role="leaf", max_depth=1, current_depth=1,
    )
    assert d.allowed is False


def test_deeper_tree_allows_orchestrator_child():
    # max_depth 2: a depth-0 orchestrator may spawn a depth-1 orchestrator child
    d = evaluate_delegation(
        enabled=True, caller_is_sub_agent=False, caller_role="orchestrator",
        requested_child_role="orchestrator", max_depth=2,
    )
    assert d.allowed is True
    assert d.child_role == "orchestrator"
