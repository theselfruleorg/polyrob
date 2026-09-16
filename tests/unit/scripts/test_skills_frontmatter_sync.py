"""The frontmatter sync must not throw away a skill's version.

`sync()` hardcoded `polyrob-version: "1"`, so running it rewrote every skill on
disk back to version 1 — observed live: a single run silently regressed
treasury-trading from 13 to 1 and polyrob-user-guide from 2 to 1. The version is
the only marker a reader has that a skill has been revised, and the script whose
job is to keep frontmatter consistent was erasing it.
"""
import importlib.util
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[3]


def _load():
    spec = importlib.util.spec_from_file_location(
        "skills_frontmatter_sync", ROOT / "scripts" / "skills_frontmatter_sync.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _skill(base, sid, version):
    d = base / sid
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        "---\n"
        f"name: {sid}\n"
        "description: d\n"
        "license: MIT\n"
        "metadata:\n"
        "  polyrob-priority: '1'\n"
        "  polyrob-auto-activate: 'true'\n"
        "  polyrob-triggers: '{}'\n"
        f"  polyrob-version: '{version}'\n"
        "---\n"
        "# body\n")
    return d / "SKILL.md"


def _rules(base, sid, **over):
    import json
    rule = {"triggers": {"keywords": ["k"]}, "priority": 1,
            "auto_activate": True, "description": "d"}
    rule.update(over)
    path = base / "rules.json"
    path.write_text(json.dumps({sid: rule}))
    return path


def test_an_existing_version_is_preserved(tmp_path):
    mod = _load()
    md = _skill(tmp_path, "a-skill", 13)
    mod.sync(_rules(tmp_path, "a-skill"), tmp_path)
    assert "polyrob-version: '13'" in md.read_text()


def test_a_version_in_rules_wins_over_the_file(tmp_path):
    mod = _load()
    md = _skill(tmp_path, "a-skill", 3)
    mod.sync(_rules(tmp_path, "a-skill", version=7), tmp_path)
    assert "polyrob-version: '7'" in md.read_text()


def test_a_skill_with_no_version_anywhere_defaults_to_one(tmp_path):
    mod = _load()
    d = tmp_path / "a-skill"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("# body only, no frontmatter\n")
    mod.sync(_rules(tmp_path, "a-skill"), tmp_path)
    assert "polyrob-version: '1'" in (d / "SKILL.md").read_text()


def test_sync_is_idempotent(tmp_path):
    mod = _load()
    md = _skill(tmp_path, "a-skill", 13)
    rules = _rules(tmp_path, "a-skill")
    mod.sync(rules, tmp_path)
    first = md.read_text()
    assert mod.sync(rules, tmp_path) == 0
    assert md.read_text() == first
