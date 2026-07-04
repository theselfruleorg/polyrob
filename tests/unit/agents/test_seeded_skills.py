"""Tests for seeded-skill force-include in SkillManager.get_skills_for_session (Task 5)."""
from agents.task.agent.skill_manager import SkillManager


def test_seeded_skill_included_without_trigger():
    sm = SkillManager()
    # A generic task that matches no specific skill triggers.
    matched = sm.get_skills_for_session(
        tool_ids=["filesystem", "task"],
        task="hello",
        seeded_skill_ids=["web-research"],
    )
    ids = {m.skill_id for m in matched}
    assert "web-research" in ids


def test_seeded_skill_not_duplicated():
    sm = SkillManager()
    matched = sm.get_skills_for_session(
        tool_ids=["filesystem", "task"],
        task="please research and cite sources about x",
        seeded_skill_ids=["web-research"],
    )
    ids = [m.skill_id for m in matched]
    assert ids.count("web-research") == 1


def test_no_seed_is_byte_identical():
    sm = SkillManager()
    a = sm.get_skills_for_session(tool_ids=["filesystem"], task="hi")
    b = sm.get_skills_for_session(tool_ids=["filesystem"], task="hi", seeded_skill_ids=None)
    assert [m.skill_id for m in a] == [m.skill_id for m in b]


def test_unknown_seeded_id_skipped():
    """An unknown seeded skill id must be skipped (fail-open, never crash)."""
    sm = SkillManager()
    matched = sm.get_skills_for_session(
        tool_ids=["filesystem"],
        task="hello",
        seeded_skill_ids=["nonexistent-skill-xyzzy"],
    )
    ids = {m.skill_id for m in matched}
    assert "nonexistent-skill-xyzzy" not in ids


def test_multiple_seeds_all_included():
    """Multiple seeded skills are all force-included."""
    sm = SkillManager()
    matched = sm.get_skills_for_session(
        tool_ids=["filesystem"],
        task="hello",
        seeded_skill_ids=["web-research", "task-planning"],
    )
    ids = {m.skill_id for m in matched}
    # Both must be present regardless of trigger matching or max_skills cap
    assert "web-research" in ids
    assert "task-planning" in ids
