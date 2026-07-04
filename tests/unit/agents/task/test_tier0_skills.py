"""P1 — Tier-0 standard skill library: each body exists, validates, and trigger-matches.

These are the minimum "basic required" skills a general agent must ship. Each must have
a loadable, valid SKILL.md and a representative task that activates it.
"""
import pytest

from agents.task.agent.skill_manager import SkillManager

# id -> a representative task string that SHOULD activate the skill
TIER0 = {
    "presentation-creator": "make me a slideshow about our roadmap",
    "web-research": "research the latest on small modular reactors and cite sources",
    "file-data-ops": "parse this csv and extract the rows where status is open",
    "document-writing": "draft a report summarizing the findings",
    "coding-workflow": "fix the bug in the auth handler and run tests",
    "task-planning": "how should i approach building this feature, break it down",
    "email-comms": "draft an email to the team and follow up",
}


@pytest.fixture(scope="module")
def sm():
    return SkillManager()


@pytest.mark.parametrize("skill_id", sorted(TIER0))
def test_tier0_body_present_and_valid(sm, skill_id):
    content = sm._load_skill_content(skill_id)
    assert content, f"{skill_id} has no loadable SKILL.md body"
    res = sm.validate_skill_content(skill_id, content)
    assert res.is_valid, f"{skill_id} body invalid: {res.errors}"


@pytest.mark.parametrize("skill_id,task", sorted(TIER0.items()))
def test_tier0_trigger_matches(sm, skill_id, task):
    matched = sm.get_skills_for_session(task=task, max_skills=20)
    assert skill_id in {m.skill_id for m in matched}, (
        f"task {task!r} did not activate {skill_id}; got {[m.skill_id for m in matched]}"
    )


def test_tier0_rules_declare_known_tools(sm):
    """Every Tier-0 rule's declared tool_ids are in VALID_TOOL_IDS (no warn-spam)."""
    from agents.task.agent.skill_manager import VALID_TOOL_IDS
    sm._ensure_rules_loaded()
    for skill_id in TIER0:
        rule = sm.skill_rules.get(skill_id, {})
        for tid in rule.get("triggers", {}).get("tool_ids", []):
            assert tid in VALID_TOOL_IDS, f"{skill_id} declares unknown tool_id {tid}"
