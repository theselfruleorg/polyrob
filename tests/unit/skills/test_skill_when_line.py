"""081 WS-4.3: the skill index line says WHEN to load a skill, in <= 60 chars.

Under 081 Q2 the model chooses a skill from the index, so the index line is the
routing text. The old line was the description cut at 160 chars, which often cut
the "use when" clause (review A-7). ``metadata.polyrob-when`` is that line.
"""
from pathlib import Path

import pytest

from agents.task.agent.skill_manager import MatchedSkill, SkillManager, catalog_line
from core.skills import scopes
from core.skills.frontmatter import (
    MAX_WHEN_CHARS, parse_frontmatter, validate_authored, validate_skill_content, when_line,
)
from modules.skills.writer import PROVENANCE_USER

ROOT = Path(__file__).resolve().parents[3]
SHIPPED = sorted([*ROOT.glob("data/prompts/skills/*/SKILL.md"),
                  *ROOT.glob("packs/*/polyrob_*/skills/*/SKILL.md")])


@pytest.mark.parametrize("md", SHIPPED, ids=[p.parent.name for p in SHIPPED])
def test_every_shipped_skill_says_when_to_load_it(md):
    meta, _ = parse_frontmatter(md.read_text(encoding="utf-8"))
    when = when_line(meta)
    assert when, f"{md.parent.name}: no metadata.polyrob-when"
    assert len(when) <= MAX_WHEN_CHARS, f"{md.parent.name}: {len(when)} chars"
    assert validate_authored(meta, md.parent.name) == []


def test_every_shipped_when_line_is_distinct():
    """The index line is the routing text: two skills with one line cannot be told apart."""
    seen = {}
    for md in SHIPPED:
        when = when_line(parse_frontmatter(md.read_text(encoding="utf-8"))[0]).lower()
        assert when not in seen, f"{md.parent.name} and {seen.get(when)} share {when!r}"
        seen[when] = md.parent.name


PACK_SKILLS = [p for p in SHIPPED if "/packs/" in p.as_posix()]


def _required_ids():
    from core.skills.catalog import rule_from_frontmatter
    out = set()
    for md in SHIPPED:
        rule = rule_from_frontmatter(parse_frontmatter(md.read_text(encoding="utf-8"))[0])
        out.update((rule or {}).get("requires") or [])
    return out


@pytest.mark.parametrize("md", PACK_SKILLS, ids=[p.parent.name for p in PACK_SKILLS])
def test_a_pack_skill_ranks_in_the_pack_band(md):
    """SKILL_AUTHORING_STANDARD 9c: a pack skill's priority is 5-7, so a pack never
    heads the index above the builtin library. A safety prerequisite (an id another
    skill ``requires``, e.g. ``crypto-trading-safety``) may rank higher."""
    md_meta = parse_frontmatter(md.read_text(encoding="utf-8"))[0].get("metadata") or {}
    priority = int(md_meta.get("polyrob-priority", "5"))
    if md.parent.name in _required_ids():
        return
    assert 5 <= priority <= 7, f"{md.parent.name}: priority {priority}"


def test_the_index_line_is_the_when_line():
    skill = MatchedSkill("defi-bridge", 2, ["catalog"], "body",
                         description="a long description " * 20, when="Moving funds between chains")
    assert catalog_line(skill) == '- id="defi-bridge" — Moving funds between chains'


def test_a_skill_without_one_falls_back_to_its_description():
    skill = MatchedSkill("x", 6, ["catalog"], "body", description="d" * 300)
    assert catalog_line(skill) == '- id="x" — ' + "d" * 160


def test_the_shipped_index_shows_the_when_lines():
    index = SkillManager().get_catalog_skills(tool_ids=["defi_trade"], max_skills=500)
    bridge = next(m for m in index if m.skill_id == "defi-bridge")
    assert catalog_line(bridge) == '- id="defi-bridge" — Moving funds between chains (bridge)'


LONG = "---\nname: mine\ndescription: d\nmetadata:\n  polyrob-when: '" + "w" * 61 + "'\n---\n# Mine\n" + "body " * 20


def test_a_long_when_line_is_refused_at_write():
    res = validate_skill_content("mine", LONG)
    assert not res.is_valid and "polyrob-when" in res.errors[0]
    ok = LONG.replace("w" * 61, "Calibrating a widget")
    assert validate_skill_content("mine", ok).is_valid


def test_the_agents_own_create_enforces_it(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(scopes, "skills_data_home", lambda: tmp_path / "data")
    sm = SkillManager()
    res = sm.create_skill("mine", LONG, user_id="u1", created_by=PROVENANCE_USER, pending=False)
    assert not res.ok and "polyrob-when" in " ".join(res.errors)
    res = sm.create_skill("mine", LONG.replace("w" * 61, "Calibrating a widget"), user_id="u1",
                          created_by=PROVENANCE_USER, pending=False)
    assert res.ok, res.errors
    index = sm.get_catalog_skills(user_id="u1", tool_ids=[], max_skills=500)
    assert catalog_line(next(m for m in index if m.skill_id == "mine")) == \
        '- id="mine" — Calibrating a widget'
