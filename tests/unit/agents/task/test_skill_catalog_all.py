"""Progressive disclosure should be able to expose ALL available skills.

Regression for the live S9 gap: a generic session trigger-matched zero skills, so
the agent got NO catalog and could not discover/load any skill. get_catalog_skills
returns every auto-activatable skill (capped) so load_skill can reach them.
"""
from agents.task.agent.skill_manager import SkillManager


def test_get_catalog_skills_returns_all_available_skills():
    sm = SkillManager()
    catalog = sm.get_catalog_skills()
    ids = {s.skill_id for s in catalog}
    # the exact skill the live S9 run could not find
    assert "project-analyzer" in ids
    # only skills with an actual body file are catalogued (can't load_skill a bodyless rule)
    assert len(catalog) >= 3
    for s in catalog:
        assert s.skill_id
        assert s.content  # body preloaded so load_skill serves without disk re-read


def test_get_catalog_skills_is_bounded():
    sm = SkillManager()
    catalog = sm.get_catalog_skills(max_skills=2)
    assert len(catalog) <= 2


def test_polyrob_user_guide_visible_in_catalog_with_its_description():
    """owner-UX P3 T2: the polyrob-user-guide builtin skill must actually be
    reachable via the catalog the model sees, not just present as a rules.json
    entry with a bodiless/orphaned SKILL.md."""
    sm = SkillManager()
    catalog = sm.get_catalog_skills()
    ids = {s.skill_id for s in catalog}
    assert "polyrob-user-guide" in ids

    text = sm.format_skill_catalog(catalog)
    assert 'id="polyrob-user-guide"' in text
    # one-line description (frontmatter, agentskills.io source of truth) renders
    assert "The map of what POLYROB is" in text
