"""Review 2026-09-29 D5: under the defaults, matched skills reach the model.

With progressive disclosure ON and catalog include-all ON (both default), a
trigger match, a rail seed and a ``requires`` prerequisite only REORDERED the
catalog. The safety prerequisites (token-identity, pre-trade-check) were never
delivered unless the model chose to call ``load_skill``.
"""
from pathlib import Path

import pytest

from agents.task.agent import skill_store
from agents.task.agent.skill_manager import (
    MAX_SKILL_INJECT_CHARS, MatchedSkill, SkillManager,
)


@pytest.fixture
def sm(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(skill_store, "skills_data_home", lambda: tmp_path / "data")
    return SkillManager()


def _session(sm):
    matched = sm.get_skills_for_session(
        task="buy token PNL for the buyback",
        tool_ids=["defi_trade", "defi_data", "task"],
        available_actions=["preferences", "agent_status"])
    catalog = matched + [c for c in sm.get_catalog_skills(
        max_skills=50, tool_ids=["defi_trade", "defi_data", "task"])
        if c.skill_id not in {m.skill_id for m in matched}]
    return matched, catalog


def test_prerequisites_are_pinned_and_the_oversized_doctrine_is_load_first(sm):
    matched, catalog = _session(sm)
    eager, load_first = sm.split_progressive(matched)
    eager_ids = [s.skill_id for s in eager]
    assert {"token-identity", "pre-trade-check"} <= set(eager_ids)
    # treasury-trading's body is bigger than the whole budget.
    assert len(next(m for m in matched if m.skill_id == "treasury-trading").content) \
        > MAX_SKILL_INJECT_CHARS
    assert "treasury-trading" in load_first
    assert sum(len(s.content) for s in eager) <= SkillManager.EAGER_INJECT_BUDGET_CHARS

    text = sm.format_progressive(eager, catalog, load_first)
    assert '<skill id="token-identity">' in text
    assert '<skill id="pre-trade-check">' in text
    assert 'id="treasury-trading" — LOAD FIRST' in text
    # A pinned body is not listed again in the catalog.
    assert 'id="token-identity" —' not in text
    # The catalog still lists what did not match.
    assert "<skill-catalog>" in text


def test_budget_is_bounded_and_prerequisites_win_it():
    sm = SkillManager.__new__(SkillManager)
    big = MatchedSkill("parent", 1, ["keyword:x"], "p" * 15000, trigger_type="keyword")
    pre = MatchedSkill("prereq", 2, ["requires:parent"], "q" * 8000,
                       trigger_type="prerequisite")
    eager, load_first = SkillManager.split_progressive(sm, [big, pre], budget=20000)
    assert [s.skill_id for s in eager] == ["prereq"]
    assert load_first == {"parent"}


def test_catalog_without_load_first_is_unchanged(sm):
    skills = [MatchedSkill("a", 1, ["catalog"], "body", description="does a")]
    text = sm.format_skill_catalog(skills)
    assert "LOAD FIRST" not in text
    assert '- id="a" — does a' in text


def test_construction_pins_matched_skills_under_progressive_disclosure():
    """Structural: the progressive branch goes through split_progressive and
    marks the pinned ids activated (their body is in the context)."""
    src = (Path(__file__).resolve().parents[3]
           / "agents/task/agent/core/construction.py").read_text(encoding="utf-8")
    assert "split_progressive(_session_matched)" in src
    assert "format_progressive(" in src
    assert "_act.update(s.skill_id for s in _eager)" in src
