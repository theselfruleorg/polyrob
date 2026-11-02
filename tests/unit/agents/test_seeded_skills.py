"""Pinned skills (081 Q2): a persona seed or a rail pin loads at session start."""
from agents.task.agent.skill_manager import SkillManager


def _ids(pins, tools=("filesystem", "task")):
    return [m.skill_id for m in SkillManager().get_pinned_skills(
        seeded_skill_ids=pins, tool_ids=list(tools))]


def test_a_pinned_skill_loads():
    assert _ids(["web-research"]) == ["web-research"]


def test_a_pin_named_twice_loads_once():
    assert _ids(["web-research", "web-research"]) == ["web-research"]


def test_no_pin_loads_nothing():
    assert _ids(None) == [] and _ids([]) == []


def test_unknown_pinned_id_skipped():
    """An unknown pinned id is skipped (fail-open, never crash)."""
    assert _ids(["nonexistent-skill-xyzzy"]) == []


def test_multiple_pins_all_load():
    assert set(_ids(["web-research", "task-planning"])) == {"web-research", "task-planning"}


def test_a_pin_is_marked_seeded():
    got = SkillManager().get_pinned_skills(seeded_skill_ids=["web-research"])
    assert got[0].trigger_type == "seeded" and got[0].match_reasons == ["seeded:preset"]


def test_a_pinned_user_copy_with_no_rules_row_still_loads(tmp_path, monkeypatch):
    """095 B0 preserves a moving builtin as a user-scope copy with no rules.json
    row. A pin is explicit intent: the copy serves from its own frontmatter, the
    same existence test the lane check uses (never a silent skip)."""
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    skill = tmp_path / "skills" / "user_u1" / "preserved-playbook"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: preserved-playbook\ndescription: A preserved playbook.\n---\n\nDo the work.\n")
    got = SkillManager().get_pinned_skills(seeded_skill_ids=["preserved-playbook"],
                                           user_id="u1")
    assert [m.skill_id for m in got] == ["preserved-playbook"]
    assert "Do the work." in got[0].content
