"""Task 13 (Phase 3 R3) — `usage_summary` must be blocked while a session is
correspondent-tainted, same reasoning as `agent_status` (info-disclosure of
cost/invoice data to a forged correspondent).

Kept as its own small file (rather than added to
tests/unit/agents/task/test_correspondent_gate.py) because that file has
unrelated in-flight edits from another session on this shared tree.
"""
from agents.task.agent.core.correspondent_gate import (
    HIGH_IMPACT_TOOLS,
    is_high_impact,
    make_correspondent_gate_hook,
)


def test_usage_summary_action_is_high_impact_by_name():
    assert is_high_impact("usage_summary")
    assert "usage_summary" in HIGH_IMPACT_TOOLS


def test_gate_blocks_usage_summary_when_tainted():
    hook = make_correspondent_gate_hook(lambda: True)
    reason = hook("usage_summary", {}, None)
    assert reason and "correspondent" in reason.lower()


def test_gate_allows_usage_summary_when_not_tainted():
    hook = make_correspondent_gate_hook(lambda: False)
    assert hook("usage_summary", {}, None) is None
