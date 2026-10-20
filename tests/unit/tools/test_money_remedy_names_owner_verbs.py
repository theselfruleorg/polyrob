"""The money-gated remedy: the self-grant path, and propose_action for the owner."""
from tools.tool_disclosure import resolve_tool_status


def _money_remedy():
    st = resolve_tool_status("defi_trade", container=None, is_leaf=False,
                             loaded_ids=frozenset())
    assert st.status == "gated" and st.reason == "money"
    return st.remedy


def test_the_remedy_points_the_owner_case_at_propose_action():
    remedy = _money_remedy()
    assert "propose_action" in remedy
    # no hand-copied verb list and no dated story
    assert "/bridge" not in remedy and "2026-" not in remedy


def test_the_no_self_grant_warning_survives():
    """A goal the agent creates can never carry a money tool (unarmed regime)."""
    remedy = _money_remedy()
    assert "NEVER carry one" in remedy
    assert "goal_create strips money tools" in remedy


def test_under_the_armed_regime_the_remedy_says_a_goal_can_carry_it(monkeypatch):
    """When the self-goal ceiling holds the tool, the remedy names the real path."""
    monkeypatch.setattr("tools.goal_tools.allowed_self_goal_tools",
                        lambda: frozenset({"defi_trade", "filesystem"}))
    remedy = _money_remedy()
    assert "NEVER carry" not in remedy
    assert "rig='money_rail'" in remedy and "goal_create" in remedy
    assert "propose_action" in remedy
