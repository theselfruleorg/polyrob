"""The discovery pack's skills (067 P3a): lead-research and web-scraping ship
in the pack with their rules and auto-activate like the builtins they were."""
import pytest

pytest.importorskip("polyrob_discovery")


def test_pack_skills_carry_their_rules_and_match():
    from agents.task.agent.skill_manager import SkillManager
    sm = SkillManager()
    sm._ensure_rules_loaded()
    assert {"lead-research", "web-scraping"} <= sm._pack_rule_ids
    assert sm.skill_rules["lead-research"]["priority"] == 6
    matched = sm.get_skills_for_session(tool_ids=["anysite"], task="find leads for a prospect list",
                                        available_actions=[], user_id=None)
    assert "lead-research" in [m.skill_id for m in matched]
    assert "Lead" in sm._load_skill_content("lead-research") or sm._load_skill_content("lead-research")


def test_the_skills_left_the_builtin_library():
    import json
    from pathlib import Path
    base = Path("data/prompts/skills")
    rules = json.loads((base / "rules.json").read_text())
    for sid in ("lead-research", "web-scraping"):
        assert sid not in rules and not (base / sid).exists()
