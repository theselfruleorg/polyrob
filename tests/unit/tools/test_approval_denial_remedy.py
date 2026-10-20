"""2026-10-13 R4: a money-verb denial names the real levers, not the approvals.require pref."""
from core.money.classify import money_tool_ids
from tools.controller.approval import _denial_remedy


def test_money_verb_denial_does_not_point_at_the_pref():
    verb = sorted(money_tool_ids())[0]
    tail = _denial_remedy(verb)
    assert "approvals.require" not in tail
    assert "Spend limits" in tail


def test_other_verb_keeps_the_pref_advice():
    assert "approvals.require" in _denial_remedy("write_file")
