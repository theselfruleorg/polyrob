"""Money-critical regression guard (Task 6).

The two GATED trading skills must remain non-auto-activating after the Task-2
frontmatter migration. A `bool("false") == True` mistake anywhere in the
frontmatter round-trip would silently arm live trading, so pin both the on-disk
`polyrob-auto-activate: 'false'` value AND that `parse_bool` reads it as False.
"""
from pathlib import Path

import pytest

from core.skills.frontmatter import parse_frontmatter, parse_bool

# 067 P4: both trading skills ship in the markets pack (its skills/).
_SKILLS = Path(pytest.importorskip("polyrob_markets").__file__).parent / "skills"
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


def test_gated_trading_skills_are_disabled_in_the_live_index():
    """The frontmatter IS the runtime gate since 081 WS-4.2: the index the matcher
    reads must keep both trading skills auto_activate=False."""
    from core.skills.catalog import load_builtin_rules
    rules = load_builtin_rules(_SKILLS)
    for sid in GATED:
        assert sid in rules, f"{sid}: missing from the index"
        assert rules[sid].get("auto_activate") is False, (
            f"{sid}: auto_activate must be False (the live gate) — "
            f"got {rules[sid].get('auto_activate')!r}"
        )
