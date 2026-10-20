"""Skill rule integrity — the frontmatter is the ONLY metadata source (081 WS-4.2).

Before 081 a skill's triggers lived twice: in a ``rules.json`` index (what the
matcher loaded) and in the SKILL.md frontmatter (what a human edited). They
drifted, silently, and the drift disabled real doctrine: measured on prod
2026-09-12 with the trading rail granted, "should I hunt on robinhood chain
today" matched NO skill, because the index still carried old triggers. Now the
index is built from the frontmatter, so there is nothing to drift from; these
tests keep every shipped rule well-formed.

081 Q2: there are no keyword or pattern triggers. ``triggers`` carries only
``tool_ids`` — the domain a money playbook or a gated skill needs.
"""
from pathlib import Path

import pytest

from agents.task.agent.skill_manager import SkillManager, VALID_TOOL_IDS
from agents.task.tool_defaults import server_default_tools
from core.skills.catalog import rule_from_frontmatter
from core.skills.frontmatter import parse_frontmatter
from tests.support.skill_library import write_library

ROOT = Path(__file__).resolve().parents[4]
SKILLS_DIR = ROOT / "data" / "prompts" / "skills"
PACK_SKILL_DIRS = sorted(ROOT.glob("packs/*/polyrob_*/skills"))
_TRIGGER_KEYS = {"tool_ids"}


def _shipped():
    for root in [SKILLS_DIR, *PACK_SKILL_DIRS]:
        for md in sorted(root.glob("*/SKILL.md")):
            if not md.parent.name.startswith(("user_", ".")):
                yield md.parent.name, md


SHIPPED = list(_shipped())


def test_no_second_metadata_file_ships():
    """A shipped `rules.json` would be a second metadata source to drift from."""
    stray = [str(p.relative_to(ROOT)) for p in [SKILLS_DIR, *PACK_SKILL_DIRS]
             if (p / "rules.json").exists()]
    assert stray == []


def test_valid_tool_ids_cover_server_defaults():
    """VALID_TOOL_IDS must not warn on tools that ship in the server default set."""
    missing = [t for t in server_default_tools() if t not in VALID_TOOL_IDS]
    assert missing == [], f"server default tools missing from VALID_TOOL_IDS: {missing}"


@pytest.mark.parametrize("sid,md", SHIPPED, ids=[s for s, _ in SHIPPED])
def test_every_shipped_skill_declares_a_well_formed_rule(sid, md):
    meta, _ = parse_frontmatter(md.read_text(encoding="utf-8"))
    rule = rule_from_frontmatter(meta)
    assert rule is not None, f"{sid}: no metadata.polyrob-* rule in the frontmatter"
    triggers = rule["triggers"]
    assert set(triggers) <= _TRIGGER_KEYS, \
        f"{sid}: dead trigger keys {set(triggers) - _TRIGGER_KEYS} (no keyword matcher exists)"
    assert 1 <= rule["priority"] <= 10
    assert set(triggers.get("tool_ids") or []) <= VALID_TOOL_IDS, sid


def test_every_requires_names_a_shipped_skill():
    shipped = {sid for sid, _ in SHIPPED}
    dangling = {}
    for sid, md in SHIPPED:
        rule = rule_from_frontmatter(parse_frontmatter(md.read_text(encoding="utf-8"))[0]) or {}
        missing = [r for r in rule.get("requires") or [] if r not in shipped]
        if missing:
            dangling[sid] = missing
    assert dangling == {}


def test_the_prerequisites_still_ride_with_their_parents():
    """R5 of 081: `requires` is live on the seed path. The frontmatter carries it."""
    by_id = {sid: rule_from_frontmatter(parse_frontmatter(md.read_text(encoding="utf-8"))[0])
             for sid, md in SHIPPED}
    assert by_id["treasury-trading"]["requires"] == ["token-identity", "pre-trade-check"]
    assert by_id["hyperliquid-trading"]["requires"] == ["crypto-trading-safety"]
    assert by_id["polymarket-trading"]["requires"] == ["crypto-trading-safety"]


def test_a_gated_rule_loads_from_the_frontmatter(tmp_path):
    """`polyrob-auto-activate: 'false'` is a gated rule, kept in the index."""
    write_library(tmp_path, {"manual-skill": {"auto_activate": False,
                                              "triggers": {"tool_ids": ["browser"]}}})
    sm = SkillManager(skills_dir=tmp_path)
    sm._ensure_rules_loaded()
    assert sm.skill_rules["manual-skill"]["auto_activate"] is False


def test_a_plain_agentskills_body_is_not_a_rule(tmp_path):
    """A SKILL.md with no polyrob metadata is an index entry with no rule."""
    (tmp_path / "plain").mkdir()
    (tmp_path / "plain" / "SKILL.md").write_text(
        "---\nname: plain\ndescription: a plain skill\n---\n# Plain\nplain ghost body text\n")
    sm = SkillManager(skills_dir=tmp_path)
    sm._ensure_rules_loaded()
    assert "plain" not in sm.skill_rules
