from agents.task.templates import resolve_template_persona, seeded_skills_for, TEMPLATES


def test_persona_text_for_research():
    assert "research" in resolve_template_persona("research").lower()


def test_blank_persona_is_empty():
    assert resolve_template_persona("blank") == ""


def test_unknown_falls_back_to_general_persona():
    assert resolve_template_persona("nope") == resolve_template_persona("general")


def test_research_seeds_skills():
    skills = seeded_skills_for("research")
    assert "web-research" in skills


def test_general_seeds_no_skills():
    assert seeded_skills_for("general") == []


def test_seeded_skill_ids_exist_on_disk():
    """Every seeded skill id must have a SKILL.md (no dangling refs)."""
    import os
    base = "data/prompts/skills"
    for tpl in TEMPLATES.values():
        for sid in tpl.seeded_skills:
            assert os.path.isfile(os.path.join(base, sid, "SKILL.md")), \
                f"{tpl.name} seeds missing skill {sid}"


def test_seeded_skill_ids_have_rules_entry():
    """Every seeded skill id must have a rules.json entry.

    The runtime force-include path (get_skills_for_session) requires a rules
    entry to match; a SKILL.md-only skill is invisible at runtime.
    """
    import json
    import os
    rules_path = os.path.join("data", "prompts", "skills", "rules.json")
    with open(rules_path) as fh:
        rules = json.load(fh)
    for tpl in TEMPLATES.values():
        for sid in tpl.seeded_skills:
            assert sid in rules, (
                f"{tpl.name} seeds '{sid}' but rules.json has no entry for it; "
                f"the skill will be invisible at runtime."
            )
