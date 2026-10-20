"""Task 5 — close the frontmatter-leak regression introduced by Task 2.

Task 2 added YAML frontmatter (``---\\nname: ...\\n---``) to every bundled
``data/prompts/skills/*/SKILL.md``, but ``SkillManager`` read bodies RAW
(``skill_file.read_text()`` with no stripping), so the frontmatter block leaked
into every LLM-served surface: the full-body ``<skills>`` injection
(``format_skills_for_prompt``), the on-demand ``load_skill`` result
(``build_load_skill_result``), and the ``<skill-catalog>`` description text
(``get_catalog_skills``/``format_skill_catalog``) — plus skewed the
``content_len``/``estimated_tokens`` accounting in ``validate_skill_content``.

This is the regression lock: frontmatter must be stripped once, at the
``_load_skill_content`` source, so every downstream consumer naturally sees a
frontmatter-free body. Since 081 WS-4.2 the frontmatter is also the ONLY
metadata source: description, triggers, priority and the auto-activate gate.
"""
from pathlib import Path

import pytest

from agents.task.agent.skill_manager import SkillManager
from tools.controller._helpers import build_load_skill_result


FRONTMATTER_DESCRIPTION = "frontmatter description (authoritative)"

SKILL_MD = f"""---
name: demo-skill
description: {FRONTMATTER_DESCRIPTION}
license: MIT
metadata:
  polyrob-priority: '1'
  polyrob-auto-activate: 'true'
  polyrob-triggers: '{{"tool_ids":["browser"]}}'
---
# Demo Skill

Body content for the demo skill.
"""


@pytest.fixture(autouse=True)
def _no_pack_skills(monkeypatch):
    """The catalog counts below are of THIS fixture's library: an installed
    pack's skills (067 P3, e.g. the discovery pack) are not part of it."""
    from core.skills import scopes
    monkeypatch.setattr(scopes, "pack_scopes", lambda: [])


@pytest.fixture
def skills_dir(tmp_path: Path) -> Path:
    d = tmp_path / "skills"
    d.mkdir()
    skill_dir = d / "demo-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(SKILL_MD)
    return d


# --- Site 1: _load_skill_content is the strip source -----------------------

def test_loaded_body_has_no_frontmatter(skills_dir):
    sm = SkillManager(skills_dir=skills_dir)
    content = sm._load_skill_content("demo-skill")
    assert content, "expected a non-empty body"
    assert "---" not in content
    assert "polyrob-priority" not in content
    assert "license: MIT" not in content
    assert content.lstrip().startswith("# Demo Skill")


# --- Site 2: format_skills_for_prompt (full-body <skills> injection) -------

def test_format_skills_for_prompt_has_no_frontmatter_leak(skills_dir):
    sm = SkillManager(skills_dir=skills_dir)
    matched = sm.get_pinned_skills(seeded_skill_ids=["demo-skill"])
    assert matched, "expected the pinned demo-skill"
    out = sm.format_skills_for_prompt(matched)
    assert "polyrob-priority" not in out
    assert "license: MIT" not in out
    assert "# Demo Skill" in out


# --- Site 3: build_load_skill_result (on-demand load_skill tool) -----------

def test_build_load_skill_result_has_no_frontmatter_leak(skills_dir):
    sm = SkillManager(skills_dir=skills_dir)
    catalog = sm.get_catalog_skills()
    session_skills = {s.skill_id: s for s in catalog}
    result = build_load_skill_result(session_skills, "demo-skill")
    assert result.error is None
    assert "polyrob-priority" not in result.extracted_content
    assert "license: MIT" not in result.extracted_content
    assert "# Demo Skill" in result.extracted_content


# --- Site 4: validate_skill_content's content_len/estimated_tokens ---------

def test_validate_skill_content_len_excludes_frontmatter(skills_dir):
    sm = SkillManager(skills_dir=skills_dir)
    content = sm._load_skill_content("demo-skill")
    raw_len = len((skills_dir / "demo-skill" / "SKILL.md").read_text())
    # The body handed to validate_skill_content must be shorter than the raw
    # on-disk file (which still carries the frontmatter block) — otherwise
    # content_len/estimated_tokens are computed on frontmatter-inflated text.
    assert len(content) < raw_len
    res = sm.validate_skill_content("demo-skill", content)
    assert res.is_valid, f"unexpected validation errors: {res.errors}"


# --- Catalog description: frontmatter is authoritative ---------------------

def test_catalog_surfaces_the_frontmatter_description(skills_dir):
    sm = SkillManager(skills_dir=skills_dir)
    catalog = sm.get_catalog_skills()
    assert len(catalog) == 1
    assert catalog[0].description == FRONTMATTER_DESCRIPTION


def test_format_skill_catalog_renders_frontmatter_description(skills_dir):
    sm = SkillManager(skills_dir=skills_dir)
    catalog = sm.get_catalog_skills()
    out = sm.format_skill_catalog(catalog)
    assert FRONTMATTER_DESCRIPTION in out


def test_pinned_skill_also_prefers_frontmatter_description(skills_dir):
    """A pin's MatchedSkill.description feeds format_skill_catalog too (the
    index merges pins + index entries), so it resolves like get_catalog_skills."""
    sm = SkillManager(skills_dir=skills_dir)
    matched = sm.get_pinned_skills(seeded_skill_ids=["demo-skill"])
    assert len(matched) == 1
    assert matched[0].description == FRONTMATTER_DESCRIPTION


# --- Gating reads the frontmatter (081 WS-4.2) -----------------------------

def test_gating_reads_the_frontmatter(skills_dir):
    """auto_activate, priority and triggers come from the SKILL.md frontmatter —
    the one metadata source — on both the pin path and the catalog path."""
    sm = SkillManager(skills_dir=skills_dir)
    skill = next(m for m in sm.get_pinned_skills(seeded_skill_ids=["demo-skill"])
                 if m.skill_id == "demo-skill")
    assert skill.priority == 1
    assert sm.get_catalog_skills()[0].priority == 1
    sm._ensure_rules_loaded()
    rule = sm.skill_rules["demo-skill"]
    assert rule["auto_activate"] is True and rule["triggers"] == {"tool_ids": ["browser"]}

    gated = SKILL_MD.replace("polyrob-auto-activate: 'true'", "polyrob-auto-activate: 'false'")
    (skills_dir / "demo-skill" / "SKILL.md").write_text(gated)
    sm.reload_rules()
    assert "demo-skill" not in {m.skill_id for m in sm.get_catalog_skills(tool_ids=[])}
