"""The discovery pack's skills (067 P3a): lead-research and web-scraping ship
in the pack with their rules and are index lines like the builtins they were."""
import pytest

pytest.importorskip("polyrob_discovery")


def test_pack_skills_carry_their_rules_and_are_listed():
    from agents.task.agent.skill_manager import SkillManager
    sm = SkillManager()
    sm._ensure_rules_loaded()
    assert {"lead-research", "web-scraping"} <= sm._pack_rule_ids
    assert sm.skill_rules["lead-research"]["priority"] == 6
    index = sm.get_catalog_skills(tool_ids=["anysite"], max_skills=500)
    assert "lead-research" in [m.skill_id for m in index]
    assert "Lead" in sm._load_skill_content("lead-research") or sm._load_skill_content("lead-research")


def test_the_skills_left_the_builtin_library():
    from pathlib import Path
    base = Path("data/prompts/skills")
    for sid in ("lead-research", "web-scraping"):
        assert not (base / sid).exists()
