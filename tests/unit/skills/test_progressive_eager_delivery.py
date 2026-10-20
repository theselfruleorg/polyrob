"""Review 2026-09-29 D5: under the defaults, PINNED skills reach the model.

With progressive disclosure ON and catalog include-all ON (both default), a rail
pin and a ``requires`` prerequisite only REORDERED the catalog. The safety
prerequisites (token-identity, pre-trade-check) were never delivered unless the
model chose to call ``load_skill``. 081 Q2: only a pin (and its prerequisites)
loads at session start; the rest is the short index.
"""
from pathlib import Path

import pytest

from core.skills import scopes
from agents.task.agent.skill_manager import (
    MAX_SKILL_INJECT_CHARS, MatchedSkill, SkillManager,
)


@pytest.fixture
def sm(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(scopes, "skills_data_home", lambda: tmp_path / "data")
    return SkillManager()


def _session(sm):
    matched = sm.get_pinned_skills(
        seeded_skill_ids=["treasury-trading"],
        tool_ids=["defi_trade", "defi_data", "task"])
    catalog = matched + [c for c in sm.get_catalog_skills(
        max_skills=50, tool_ids=["defi_trade", "defi_data", "task"])
        if c.skill_id not in {m.skill_id for m in matched}]
    return matched, catalog


def test_prerequisites_are_pinned_and_the_doctrine_that_does_not_fit_is_load_first(sm):
    matched, catalog = _session(sm)
    eager, load_first = sm.split_progressive(matched)
    eager_ids = [s.skill_id for s in eager]
    assert {"token-identity", "pre-trade-check"} <= set(eager_ids)
    # treasury-trading fits the budget alone (081 WS-4.4), not after its prerequisites.
    doctrine = len(next(m for m in matched if m.skill_id == "treasury-trading").content)
    assert doctrine <= MAX_SKILL_INJECT_CHARS
    assert doctrine + sum(len(s.content) for s in eager) > SkillManager.EAGER_INJECT_BUDGET_CHARS
    assert "treasury-trading" in load_first
    assert sum(len(s.content) for s in eager) <= SkillManager.EAGER_INJECT_BUDGET_CHARS

    text = sm.format_progressive(eager, catalog, load_first)
    assert '<skill id="token-identity">' in text
    assert '<skill id="pre-trade-check">' in text
    assert 'id="treasury-trading" — LOAD FIRST' in text
    # A pinned body is not listed again in the catalog.
    assert 'id="token-identity" —' not in text
    # The index still lists what was not pinned.
    assert "<skill-catalog>" in text


def test_budget_is_bounded_and_prerequisites_win_it():
    sm = SkillManager.__new__(SkillManager)
    big = MatchedSkill("parent", 1, ["seeded:preset"], "p" * 15000, trigger_type="seeded")
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


def test_construction_pins_only_the_pinned_skills():
    """Structural: construction reads the pins (no keyword matcher exists), the
    progressive branch goes through split_progressive and marks the pinned ids
    activated (their body is in the context)."""
    src = (Path(__file__).resolve().parents[3]
           / "agents/task/agent/core/construction.py").read_text(encoding="utf-8")
    assert "skill_manager.get_pinned_skills(" in src
    assert "split_progressive(_session_matched)" in src
    assert "format_progressive(" in src
    assert not hasattr(SkillManager, "get_skills_for_session")
    assert "_act.update(s.skill_id for s in _eager)" in src
