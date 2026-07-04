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
frontmatter-free body. The catalog must also prefer the frontmatter
``description`` over a stale rules.json value when the two differ. Gating
(``auto_activate``/``priority``/``triggers``) must keep reading from
rules.json unchanged (money-critical trading-skill gate; a later task owns
that switch).
"""
import json
from pathlib import Path

import pytest

from agents.task.agent.skill_manager import SkillManager
from tools.controller._helpers import build_load_skill_result


RULES_DESCRIPTION = "rules.json description (stale)"
FRONTMATTER_DESCRIPTION = "frontmatter description (authoritative)"

# The frontmatter gating values DELIBERATELY DIVERGE from rules.json below so the
# gating test can actually discriminate: frontmatter says priority=9 /
# auto_activate=false, rules.json says priority=1 / auto_activate=true. Gating must
# read rules.json (the money-critical trading-skill gate), so a correct build keeps
# the skill active with priority 1; a regression that switched gating to frontmatter
# would drop it (auto_activate=false) or show priority 9.
SKILL_MD = f"""---
name: demo-skill
description: {FRONTMATTER_DESCRIPTION}
license: MIT
metadata:
  polyrob-priority: '9'
  polyrob-auto-activate: 'false'
---
# Demo Skill

Body content for the demo skill.
"""


@pytest.fixture
def skills_dir(tmp_path: Path) -> Path:
    d = tmp_path / "skills"
    d.mkdir()
    rules = {
        "demo-skill": {
            "triggers": {"keywords": ["demo"]},
            "priority": 1,
            "auto_activate": True,
            "description": RULES_DESCRIPTION,
        }
    }
    (d / "rules.json").write_text(json.dumps(rules))
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
    matched = sm.get_skills_for_session(task="run the demo", max_skills=5)
    assert matched, "expected demo-skill to trigger-match on 'demo'"
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

def test_catalog_surfaces_frontmatter_description_over_rules_json(skills_dir):
    sm = SkillManager(skills_dir=skills_dir)
    catalog = sm.get_catalog_skills()
    assert len(catalog) == 1
    entry = catalog[0]
    assert entry.description == FRONTMATTER_DESCRIPTION
    assert entry.description != RULES_DESCRIPTION


def test_format_skill_catalog_renders_frontmatter_description(skills_dir):
    sm = SkillManager(skills_dir=skills_dir)
    catalog = sm.get_catalog_skills()
    out = sm.format_skill_catalog(catalog)
    assert FRONTMATTER_DESCRIPTION in out
    assert RULES_DESCRIPTION not in out


def test_trigger_matched_skill_also_prefers_frontmatter_description(skills_dir):
    """get_skills_for_session's MatchedSkill.description feeds format_skill_catalog
    too (progressive disclosure merges matched + catalog-extra skills), so it must
    resolve the same way as get_catalog_skills — not just the catalog-only path."""
    sm = SkillManager(skills_dir=skills_dir)
    matched = sm.get_skills_for_session(task="run the demo", max_skills=5)
    assert len(matched) == 1
    assert matched[0].description == FRONTMATTER_DESCRIPTION


# --- Gating must NOT move off rules.json (out of scope for this task) -----

def test_gating_still_reads_from_rules_json(skills_dir):
    """Gating (auto_activate + priority) must come from rules.json, NOT frontmatter.

    This is the money-critical guard. The fixture DIVERGES the two sources
    (frontmatter: priority=9 / auto_activate=false; rules.json: priority=1 /
    auto_activate=true), and the assertions read the resulting MatchedSkill — the
    real gating OUTPUT — not the raw sm.skill_rules dict. So the test genuinely
    discriminates:
      * demo-skill IS matched  → auto_activate came from rules.json (true); had
        gating read frontmatter (false) the skill would drop out entirely.
      * matched priority == 1  → priority came from rules.json, not frontmatter's 9.
    If a later change points gating at frontmatter, this goes RED (skill missing or
    priority 9). A later task flips gating deliberately under its own test; this one
    must not.
    """
    sm = SkillManager(skills_dir=skills_dir)

    # get_skills_for_session path (trigger matching + priority sort)
    matched = sm.get_skills_for_session(task="run the demo", max_skills=5)
    ids = {m.skill_id for m in matched}
    assert "demo-skill" in ids, (
        "demo-skill must still activate — auto_activate must come from rules.json "
        "(true), not the frontmatter (false, which would drop it)"
    )
    skill = next(m for m in matched if m.skill_id == "demo-skill")
    assert skill.priority == 1, (
        f"priority must come from rules.json (1), not frontmatter (9); got {skill.priority}"
    )

    # get_catalog_skills path (auto_activate filter + priority) resolves the same way
    catalog = {m.skill_id: m for m in sm.get_catalog_skills()}
    assert "demo-skill" in catalog, (
        "catalog gating (auto_activate) must also read rules.json (true), not frontmatter"
    )
    assert catalog["demo-skill"].priority == 1

    # The raw rules dict is still the untouched source (defense-in-depth).
    sm._ensure_rules_loaded()
    rules = sm.skill_rules["demo-skill"]
    assert rules["priority"] == 1
    assert rules["auto_activate"] is True
    assert rules["triggers"] == {"keywords": ["demo"]}
