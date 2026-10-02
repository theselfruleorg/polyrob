"""067 P6 / plan T4.2: `allowed-tools` is recorded and surfaced as an ADVISORY
line on the loaded body — not enforced (no per-turn narrowing hook exists)."""
import json
from pathlib import Path

from agents.task.agent.skill_allowed_tools import advisory_line, apply, parse_allowed_tools
from agents.task.agent.skill_manager import SkillManager


def _mgr(tmp_path: Path, bodies: dict) -> SkillManager:
    (tmp_path / "rules.json").write_text(json.dumps(
        {sid: {"priority": 5, "auto_activate": True} for sid in bodies}))
    for sid, text in bodies.items():
        (tmp_path / sid).mkdir()
        (tmp_path / sid / "SKILL.md").write_text(text)
    return SkillManager(skills_dir=tmp_path)


def test_parse_allowed_tools_shapes():
    assert parse_allowed_tools({"allowed-tools": "Bash Read  Bash"}) == ["Bash", "Read"]
    assert parse_allowed_tools({"allowed-tools": ["web_fetch", "browser"]}) == ["web_fetch", "browser"]
    assert parse_allowed_tools({"allowed-tools": "a, b"}) == ["a", "b"]
    assert parse_allowed_tools({}) == [] and parse_allowed_tools({"allowed-tools": 3}) == []


def test_apply_leaves_body_alone_without_declaration():
    assert apply({"name": "x"}, "# body") == ("# body", [])


def test_loaded_body_carries_advisory_and_is_recorded(tmp_path):
    sm = _mgr(tmp_path, {
        "narrow": "---\nname: narrow\ndescription: d\nallowed-tools: web_fetch browser\n---\n# Narrow\nDo it.",
        "plain": "---\nname: plain\ndescription: d\n---\n# Plain",
    })
    body = sm._load_skill_content("narrow")
    assert body.startswith("# Narrow") and advisory_line(["web_fetch", "browser"]) in body
    assert "not enforce" in body
    assert sm.skill_allowed_tools == {"narrow": ["web_fetch", "browser"]}
    assert sm._load_skill_content("plain") == "# Plain"
    assert "plain" not in sm.skill_allowed_tools
