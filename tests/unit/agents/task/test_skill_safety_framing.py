"""P2-3 — skill prompts are advisory, not jailbreak-framed; no FORBIDDEN-without-fallback.

The legacy eager <skills> wrapper told the model skills "OVERRIDE your default behavior"
and "FAILURE TO FOLLOW = TASK FAILURE" — a poisoned/stale skill then reads as a hard
override. And person-analyzer FORBADE the working browser fallback. Both softened.
"""
from pathlib import Path

from agents.task.agent.skill_manager import SkillManager, MatchedSkill

SKILLS_DIR = Path(__file__).resolve().parents[4] / "data" / "prompts" / "skills"


def test_eager_prompt_is_advisory():
    sm = SkillManager()
    skill = MatchedSkill(skill_id="x", priority=1, match_reasons=["keyword:y"],
                         content="# X\nbody", description="d")
    out = sm.format_skills_for_prompt([skill])
    assert "FAILURE TO FOLLOW" not in out
    assert "OVERRIDE your default behavior" not in out


def test_no_shipped_skill_forbids_without_fallback():
    for skill_md in SKILLS_DIR.glob("*/SKILL.md"):
        text = skill_md.read_text()
        if "FORBIDDEN" in text:
            assert "fallback" in text.lower(), (
                f"{skill_md.parent.name} hard-FORBIDs without offering a fallback"
            )


def test_person_analyzer_offers_browser_fallback():
    text = (SKILLS_DIR / "person-analyzer" / "SKILL.md").read_text()
    assert "fallback" in text.lower()
