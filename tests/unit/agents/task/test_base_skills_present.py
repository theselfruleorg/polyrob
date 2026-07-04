from pathlib import Path
import json

from agents.task.agent.skill_frontmatter import strip_frontmatter

BASE = Path(__file__).resolve().parents[4] / "data" / "prompts" / "skills"

def test_security_base_skills_have_bodies_and_rules():
    rules = json.loads((BASE / "rules.json").read_text())
    for sid in ("skill-authoring", "skill-security-review", "secret-handling"):
        assert sid in rules, f"{sid} missing from rules.json"
        body = (BASE / sid / "SKILL.md")
        assert body.exists(), f"{sid}/SKILL.md missing"
        # Bundled SKILL.md files now open with compliant YAML frontmatter (Task 2
        # migration); strip it before checking for the markdown heading, exactly
        # like production skill loading does (skill_manager.py's own frontmatter
        # handling before its markdown-structure check).
        assert strip_frontmatter(body.read_text()).lstrip().startswith("#")
