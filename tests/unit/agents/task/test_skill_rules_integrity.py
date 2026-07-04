"""P0 — skill rules ↔ body integrity (drift guard).

A rule in rules.json with ``auto_activate:true`` but no on-disk ``SKILL.md`` body is a
SILENT failure: get_skills_for_session matches it, _load_skill_content returns "", and
the match is dropped with only a WARNING log. These tests are the regression lock for
that whole class of drift.
"""
import json
from pathlib import Path

from agents.task.agent.skill_manager import SkillManager, VALID_TOOL_IDS
from agents.task.tool_defaults import server_default_tools


SKILLS_DIR = Path(__file__).resolve().parents[4] / "data" / "prompts" / "skills"


def test_no_orphan_rules():
    """Every auto_activate system rule must have a readable SKILL.md body."""
    rules = json.loads((SKILLS_DIR / "rules.json").read_text())
    orphans = [
        sid for sid, r in rules.items()
        if r.get("auto_activate", True) and not (SKILLS_DIR / sid / "SKILL.md").exists()
    ]
    assert orphans == [], f"orphan rules (auto_activate, no body): {orphans}"


def test_valid_tool_ids_cover_server_defaults():
    """VALID_TOOL_IDS must not warn on tools that ship in the server default set."""
    missing = [t for t in server_default_tools() if t not in VALID_TOOL_IDS]
    assert missing == [], f"server default tools missing from VALID_TOOL_IDS: {missing}"


def _make_manager(tmp_path: Path, rules: dict, bodies: dict) -> SkillManager:
    (tmp_path / "rules.json").write_text(json.dumps(rules))
    for sid, body in bodies.items():
        d = tmp_path / sid
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(body)
    return SkillManager(skills_dir=tmp_path)


def test_bodiless_auto_activate_rule_is_pruned(tmp_path):
    """A rule with auto_activate but no body is pruned from skill_rules at load."""
    sm = _make_manager(
        tmp_path,
        rules={
            "real-skill": {"auto_activate": True, "triggers": {"keywords": ["real"]}},
            "ghost-skill": {"auto_activate": True, "triggers": {"keywords": ["ghost"]}},
        },
        bodies={"real-skill": "# Real\nbody"},
    )
    sm._ensure_rules_loaded()
    assert "real-skill" in sm.skill_rules
    assert "ghost-skill" not in sm.skill_rules, "bodiless auto_activate rule should be pruned"


def test_pruned_orphan_never_matches(tmp_path):
    """After pruning, a task that would have matched the orphan loads nothing for it."""
    sm = _make_manager(
        tmp_path,
        rules={"ghost-skill": {"auto_activate": True, "triggers": {"keywords": ["ghost"]}}},
        bodies={},
    )
    matched = sm.get_skills_for_session(task="please ghost this")
    assert all(m.skill_id != "ghost-skill" for m in matched)


def test_inactive_bodiless_rule_not_pruned(tmp_path):
    """A non-auto_activate rule with no body is left alone (not an orphan footgun)."""
    sm = _make_manager(
        tmp_path,
        rules={"manual-skill": {"auto_activate": False, "triggers": {"keywords": ["x"]}}},
        bodies={},
    )
    sm._ensure_rules_loaded()
    assert "manual-skill" in sm.skill_rules
