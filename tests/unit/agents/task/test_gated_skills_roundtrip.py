"""Money-critical regression guard (Task 6).

The two GATED trading skills must remain non-auto-activating after the Task-2
frontmatter migration. A `bool("false") == True` mistake anywhere in the
frontmatter round-trip would silently arm live trading, so pin both the on-disk
`polyrob-auto-activate: 'false'` value AND that `parse_bool` reads it as False.
"""
import json
from pathlib import Path

from agents.task.agent.skill_frontmatter import parse_frontmatter, parse_bool

# repo-root anchored (not CWD-relative) so the test is location-robust
_SKILLS = Path(__file__).resolve().parents[4] / "data" / "prompts" / "skills"
GATED = ("polymarket-trading", "hyperliquid-trading")


def test_gated_trading_skills_stay_disabled_after_frontmatter_roundtrip():
    for sid in GATED:
        skill_md = _SKILLS / sid / "SKILL.md"
        assert skill_md.exists(), f"{sid}: SKILL.md missing at {skill_md}"
        meta, _ = parse_frontmatter(skill_md.read_text(encoding="utf-8"))
        val = (meta.get("metadata") or {}).get("polyrob-auto-activate")
        assert val == "false", f"{sid}: expected polyrob-auto-activate 'false', got {val!r}"
        # bool("false") is True in Python; parse_bool must NOT re-enable a money skill.
        assert parse_bool(val) is False, f"{sid}: parse_bool re-enabled a gated trading skill!"


def test_gated_trading_skills_are_disabled_in_rules_json():
    """The frontmatter value is currently NOT the runtime gate — gating is read from
    rules.json (frontmatter metadata is for interop/portability). So pin the fence on
    the ACTUAL live gate: rules.json must keep both trading skills auto_activate=false.
    (Final-review finding: the roundtrip guard above protects a value nothing consumes;
    THIS is the assertion that actually catches an accidental arming of live trading.)
    """
    rules = json.loads((_SKILLS / "rules.json").read_text(encoding="utf-8"))
    for sid in GATED:
        assert sid in rules, f"{sid}: missing from rules.json"
        assert rules[sid].get("auto_activate") is False, (
            f"{sid}: rules.json auto_activate must be False (the live gate) — "
            f"got {rules[sid].get('auto_activate')!r}"
        )
