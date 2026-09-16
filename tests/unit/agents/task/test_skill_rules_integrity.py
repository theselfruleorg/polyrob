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


def test_rules_json_triggers_match_the_skill_frontmatter():
    """`rules.json` is what SkillManager LOADS; SKILL.md frontmatter is what a
    human edits. They drifted, silently, and the drift disabled real doctrine.

    Measured on live prod 2026-09-12 with the trading rail fully granted:
    "should I hunt on robinhood chain today" -> NO SKILLS, "reconcile the
    ledger" -> NO SKILLS, "bridge SOL to robinhood" -> NO SKILLS. The
    treasury-trading body had reached v7 with a Robinhood section, a Solana
    section and a reconcile step; rules.json still carried the v-something
    triggers, so none of it was reachable by name.

    Note `scripts/skills_frontmatter_sync.py` syncs rules.json -> frontmatter and
    hardcodes `polyrob-version: "1"`, so it is a one-time normalizer, NOT a
    maintenance tool — running it resets every skill's version. Edit both, or fix
    the script first.
    """
    import json
    import re
    from pathlib import Path

    base = Path(__file__).resolve().parents[4] / "data" / "prompts" / "skills"
    rules = json.loads((base / "rules.json").read_text())

    drifted = {}
    for sid, rule in rules.items():
        md = base / sid / "SKILL.md"
        if not md.exists():
            continue
        m = re.search(r"polyrob-triggers: '(.+?)'\n", md.read_text(), re.S)
        if not m:
            continue
        fm = json.loads(m.group(1))
        for key in ("keywords", "task_patterns", "action_names", "tool_ids"):
            if sorted(fm.get(key) or []) != sorted(rule.get("triggers", {}).get(key) or []):
                drifted.setdefault(sid, []).append(key)

    assert not drifted, (
        "SKILL.md frontmatter and rules.json triggers disagree — rules.json is "
        f"what actually loads, so the frontmatter half is dead: {drifted}")
