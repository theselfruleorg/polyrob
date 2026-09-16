"""A playbook for moving funds is not surfaced to a session that cannot move funds.

Measured on live prod 2026-09-12, with `defi_trade` absent from the toolset:

    task='buy a token'
      -> crypto-trading-safety (p1)
      -> treasury-trading     (p1)   # 517 lines of execution doctrine

`get_skills_for_session` required NO tool match — `tool_ids` only ADDED to a
skill's score, it was never a condition. So the agent read a sizing ladder,
screens and exit rules for a rail it structurally could not reach, which is the
setup for telling the owner a shipped capability does not exist.

The gate is deliberately narrow. It applies only to a skill that declares a
MONEY tool in `triggers.tool_ids`, and it is satisfied by ANY of that skill's
declared tools — so a session holding read-only `defi_data` still gets the
screens, and a granted run still gets the doctrine PINNED (not merely listed in
the catalog, where the agent would have to know to load it first — and the
instruction to do so lives inside the body it has not read).
"""
import pytest

from agents.task.agent.skill_manager import SkillManager


TRADING_RULES = {
    "treasury-trading": {
        "priority": 1,
        "auto_activate": True,
        "triggers": {
            "tool_ids": ["defi_trade", "defi_data"],
            "keywords": ["buy token", "portfolio"],
            "task_patterns": [],
            "action_names": [],
        },
    },
    "web-research": {
        "priority": 5,
        "auto_activate": True,
        "triggers": {
            "tool_ids": ["web_fetch"],
            "keywords": ["research"],
            "task_patterns": [],
            "action_names": [],
        },
    },
}


def _manager(monkeypatch):
    sm = SkillManager()
    sm.skill_rules = dict(TRADING_RULES)
    monkeypatch.setattr(sm, "_ensure_rules_loaded", lambda: None)
    monkeypatch.setattr(sm, "_load_skill_content", lambda sid, **kw: f"body of {sid}")
    monkeypatch.setattr(sm, "_load_user_rules", lambda uid: ({}, None))
    return sm


def _ids(matches):
    return {m.skill_id for m in matches}


def test_money_playbook_hidden_when_no_declared_tool_is_loaded(monkeypatch):
    """The live prod shape: the keyword matches, the rail is absent."""
    sm = _manager(monkeypatch)
    got = sm.get_skills_for_session(task="buy token", tool_ids=["filesystem", "task"])
    assert "treasury-trading" not in _ids(got)


def test_money_playbook_pinned_when_the_money_tool_is_granted(monkeypatch):
    """A granted goal run must still get the doctrine PINNED, not merely listed."""
    sm = _manager(monkeypatch)
    got = sm.get_skills_for_session(task="buy token",
                                    tool_ids=["defi_trade", "defi_data", "task"])
    assert "treasury-trading" in _ids(got)


def test_read_only_session_still_gets_the_screens(monkeypatch):
    """`defi_data` is read-only (no money capability). A session that can look at
    a market benefits from the screens even though it cannot trade."""
    sm = _manager(monkeypatch)
    got = sm.get_skills_for_session(task="portfolio", tool_ids=["defi_data", "task"])
    assert "treasury-trading" in _ids(got)


def test_a_skill_with_no_money_tool_is_unaffected(monkeypatch):
    """The gate must not quietly hide every tool-declaring skill."""
    sm = _manager(monkeypatch)
    got = sm.get_skills_for_session(task="research", tool_ids=["filesystem"])
    assert "web-research" in _ids(got), (
        "the gate is money-specific — a non-money skill still surfaces on a "
        "keyword match without its tool")


def test_the_gate_reads_the_capability_table_not_a_hardcoded_list(monkeypatch):
    """Whatever `core.tool_capabilities` calls money is what is gated, so a new
    money tool is covered the day it is classified."""
    from core.tool_capabilities import ids_with
    assert "defi_trade" in ids_with("money")
    sm = _manager(monkeypatch)
    sm.skill_rules = {
        "some-future-money-skill": {
            "priority": 1, "auto_activate": True,
            "triggers": {"tool_ids": ["hyperliquid"], "keywords": ["wager"],
                         "task_patterns": [], "action_names": []},
        }
    }
    assert "some-future-money-skill" not in _ids(
        sm.get_skills_for_session(task="wager", tool_ids=["task"]))
    assert "some-future-money-skill" in _ids(
        sm.get_skills_for_session(task="wager", tool_ids=["hyperliquid"]))


def test_the_venue_safety_skill_follows_the_venue_tools(monkeypatch):
    """`crypto-trading-safety` declares only venue tools (polymarket/hyperliquid)
    but generic trading keywords, so it used to fire on any "buy"/"trade" task —
    including on-chain ones it explicitly redirects away from ("On-chain treasury
    rail — read the `treasury-trading` skill instead"). Under the gate it follows
    its own declared rail. The cross-rail gates it describes (leaf/forged/taint)
    are enforced in code by tx_guard, not by this prose.
    """
    sm = SkillManager()
    venue = [m.skill_id for m in sm.get_skills_for_session(
        task="place a polymarket order", tool_ids=["polymarket", "task"])]
    assert "crypto-trading-safety" in venue

    onchain = [m.skill_id for m in sm.get_skills_for_session(
        task="buy a token", tool_ids=["defi_trade", "defi_data", "task"])]
    assert "treasury-trading" in onchain
    assert "crypto-trading-safety" not in onchain


def test_the_shipped_library_routes_the_queries_that_returned_nothing(monkeypatch):
    """Measured on live prod 2026-09-12 WITH the rail granted, every one of these
    matched zero skills, because rules.json (what the manager loads) was never
    updated when the SKILL.md body reached v7. Robinhood is the playbook's
    declared PRIMARY hunting ground and has its own 88-line section; `reconcile`
    was added after the agent published a false "book flat" to X.
    """
    sm = SkillManager()
    granted = ["defi_trade", "defi_data", "task"]
    for task in ("should I hunt on robinhood chain today",
                 "reconcile the ledger",
                 "bridge SOL to robinhood",
                 "check a solana memecoin"):
        ids = {m.skill_id for m in sm.get_skills_for_session(task=task, tool_ids=granted)}
        assert "treasury-trading" in ids, f"{task!r} reaches no trading doctrine"
