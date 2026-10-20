"""A playbook for moving funds is not surfaced to a session that cannot move funds.

Measured on live prod 2026-09-12, with `defi_trade` absent from the toolset, a
"buy a token" task pinned 517 lines of execution doctrine for a rail the agent
structurally could not reach — the setup for telling the owner a shipped
capability does not exist.

081 Q2: no skill loads by keyword; the gate now decides which money playbooks
are lines in a session's skill INDEX (and which ``requires`` prerequisites a pin
pulls in). It applies only to a skill that declares a MONEY tool in
``triggers.tool_ids``, and ANY of that skill's declared tools satisfies it — so a
session holding read-only `defi_data` still sees the screens.
"""
from agents.task.agent.skill_manager import SkillManager


TRADING_RULES = {
    "treasury-trading": {"priority": 1, "auto_activate": True,
                         "triggers": {"tool_ids": ["defi_trade", "defi_data"]}},
    "web-research": {"priority": 5, "auto_activate": True,
                     "triggers": {"tool_ids": ["web_fetch"]}},
}


def _manager(monkeypatch, rules=None):
    sm = SkillManager()
    sm.skill_rules = dict(rules or TRADING_RULES)
    monkeypatch.setattr(sm, "_ensure_rules_loaded", lambda: None)
    monkeypatch.setattr(sm, "_load_skill_content", lambda sid, **kw: f"body of {sid}")
    monkeypatch.setattr(sm, "_load_user_rules", lambda uid: ({}, None))
    monkeypatch.setattr(sm, "_load_external_skills", lambda: {})
    return sm


def _index(sm, tools):
    return {m.skill_id for m in sm.get_catalog_skills(tool_ids=tools, max_skills=500)}


def test_money_playbook_hidden_when_no_declared_tool_is_loaded(monkeypatch):
    assert "treasury-trading" not in _index(_manager(monkeypatch), ["filesystem", "task"])


def test_money_playbook_listed_when_the_money_tool_is_granted(monkeypatch):
    assert "treasury-trading" in _index(_manager(monkeypatch),
                                        ["defi_trade", "defi_data", "task"])


def test_read_only_session_still_gets_the_screens(monkeypatch):
    """`defi_data` is read-only (no money capability)."""
    assert "treasury-trading" in _index(_manager(monkeypatch), ["defi_data", "task"])


def test_a_skill_with_no_money_tool_is_unaffected(monkeypatch):
    """The gate must not quietly hide every tool-declaring skill."""
    assert "web-research" in _index(_manager(monkeypatch), ["filesystem"])


def test_the_gate_reads_the_capability_table_not_a_hardcoded_list(monkeypatch):
    """Whatever `core.tool_capabilities` calls money is what is gated."""
    from core.tool_capabilities import ids_with
    assert "defi_trade" in ids_with("money")
    sm = _manager(monkeypatch, {"some-future-money-skill": {
        "priority": 1, "auto_activate": True, "triggers": {"tool_ids": ["hyperliquid"]}}})
    assert "some-future-money-skill" not in _index(sm, ["task"])
    assert "some-future-money-skill" in _index(sm, ["hyperliquid"])


def test_the_venue_safety_skill_follows_the_venue_tools():
    """`crypto-trading-safety` declares only venue tools, so an on-chain session
    does not see it; a venue session does."""
    sm = SkillManager()
    assert "crypto-trading-safety" in _index(sm, ["polymarket", "task"])
    onchain = _index(sm, ["defi_trade", "defi_data", "task"])
    assert "treasury-trading" in onchain and "crypto-trading-safety" not in onchain


def test_a_pin_loads_even_without_its_tools(monkeypatch):
    """A pin is explicit intent; only the index and `requires` are gated."""
    sm = _manager(monkeypatch)
    got = sm.get_pinned_skills(seeded_skill_ids=["treasury-trading"], tool_ids=["task"])
    assert [m.skill_id for m in got] == ["treasury-trading"]
