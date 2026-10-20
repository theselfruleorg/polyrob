"""P1 — Tier-0 standard skill library: each body exists and validates.

These are the minimum "basic required" skills a general agent must ship. Each must have
a loadable, valid SKILL.md (081 Q2: the model loads it from the index; no keyword match).
"""
import pytest

from agents.task.agent.skill_manager import SkillManager

TIER0 = ["presentation-creator", "web-research", "file-data-ops", "document-writing",
         "coding-workflow", "task-planning", "email-comms"]


@pytest.fixture(scope="module")
def sm():
    return SkillManager()


@pytest.mark.parametrize("skill_id", sorted(TIER0))
def test_tier0_body_present_and_valid(sm, skill_id):
    content = sm._load_skill_content(skill_id)
    assert content, f"{skill_id} has no loadable SKILL.md body"
    res = sm.validate_skill_content(skill_id, content)
    assert res.is_valid, f"{skill_id} body invalid: {res.errors}"


def test_tier0_skills_are_index_lines(sm):
    assert set(TIER0) <= {m.skill_id for m in sm.get_catalog_skills(tool_ids=[], max_skills=500)}


def test_tier0_rules_declare_known_tools(sm):
    """Every Tier-0 rule's declared tool_ids are in VALID_TOOL_IDS (no warn-spam)."""
    from agents.task.agent.skill_manager import VALID_TOOL_IDS
    sm._ensure_rules_loaded()
    for skill_id in TIER0:
        rule = sm.skill_rules.get(skill_id, {})
        for tid in rule.get("triggers", {}).get("tool_ids", []):
            assert tid in VALID_TOOL_IDS, f"{skill_id} declares unknown tool_id {tid}"
