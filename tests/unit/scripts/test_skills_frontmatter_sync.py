"""Regression test for scripts/skills_frontmatter_sync.py (P0 minor #3):
_atomic_write must write UTF-8 explicitly (not the platform locale default),
so a rules.json description containing non-ASCII text round-trips cleanly.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))
from skills_frontmatter_sync import _atomic_write, sync  # noqa: E402


def test_atomic_write_round_trips_utf8(tmp_path):
    target = tmp_path / "SKILL.md"
    text = "---\nname: x\ndescription: café — naïve résumé\n---\nbody\n"
    _atomic_write(target, text)
    assert target.read_text(encoding="utf-8") == text


def test_sync_preserves_non_ascii_description(tmp_path):
    base = tmp_path / "skills"
    skill_dir = base / "demo"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: demo\ndescription: old\n---\nbody text\n", encoding="utf-8"
    )
    rules_path = tmp_path / "rules.json"
    rules_path.write_text(
        '{"demo": {"description": "caf\\u00e9 \\u2014 na\\u00efve", "priority": 3, '
        '"auto_activate": true, "triggers": {}}}',
        encoding="utf-8",
    )
    changed = sync(rules_path, base)
    assert changed == 1
    new_text = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
    assert "café" in new_text
    assert "naïve" in new_text
