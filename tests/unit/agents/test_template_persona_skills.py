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


def test_social_seeds_discovery_and_account_communication_skills():
    skills = seeded_skills_for("social")
    assert {"social-discovery", "x-engagement"} <= set(skills)


#: Seeds whose skill ships in a pack (067 P3), and the pack's import name. With
#: the pack absent the seed is simply not matched at runtime.
PACK_SKILLS = {"lead-research": "polyrob_discovery", "web-scraping": "polyrob_discovery"}


def _skill_sources():
    """(SKILL.md roots, merged rules): the builtin library plus every loaded pack."""
    import json
    import os
    from pathlib import Path
    from core.packs.state import skill_dirs
    base = Path("data/prompts/skills")
    roots = [base]
    rules = json.loads((base / "rules.json").read_text())
    for _pack_id, root in skill_dirs():
        roots.append(Path(root))
        extra = Path(root) / "rules.json"
        if extra.is_file():
            rules = {**json.loads(extra.read_text()), **rules}
    return roots, rules


def _pack_absent(sid):
    import importlib.util
    mod = PACK_SKILLS.get(sid)
    return mod is not None and importlib.util.find_spec(mod) is None


def test_seeded_skill_ids_exist_on_disk():
    """Every seeded skill id must have a SKILL.md (no dangling refs)."""
    roots, _ = _skill_sources()
    for tpl in TEMPLATES.values():
        for sid in tpl.seeded_skills:
            if _pack_absent(sid):
                continue
            assert any((r / sid / "SKILL.md").is_file() for r in roots), \
                f"{tpl.name} seeds missing skill {sid}"


def test_seeded_skill_ids_have_rules_entry():
    """Every seeded skill id must have a rules.json entry (builtin or its pack's).

    The runtime force-include path (get_skills_for_session) requires a rules
    entry to match; a SKILL.md-only skill is invisible at runtime.
    """
    _, rules = _skill_sources()
    for tpl in TEMPLATES.values():
        for sid in tpl.seeded_skills:
            if _pack_absent(sid):
                continue
            assert sid in rules, (
                f"{tpl.name} seeds '{sid}' but no rules.json has an entry for it; "
                f"the skill will be invisible at runtime."
            )
