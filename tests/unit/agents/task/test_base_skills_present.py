from pathlib import Path

from core.skills.frontmatter import strip_frontmatter

BASE = Path(__file__).resolve().parents[4] / "data" / "prompts" / "skills"

def test_security_base_skills_have_bodies_and_rules():
    from core.skills.catalog import load_builtin_rules
    rules = load_builtin_rules(BASE)
    for sid in ("skill-authoring", "skill-security-review", "secret-handling"):
        assert sid in rules, f"{sid} declares no rule in its frontmatter"
        body = (BASE / sid / "SKILL.md")
        assert body.exists(), f"{sid}/SKILL.md missing"
        # Bundled SKILL.md files now open with compliant YAML frontmatter (Task 2
        # migration); strip it before checking for the markdown heading, exactly
        # like production skill loading does (skill_manager.py's own frontmatter
        # handling before its markdown-structure check).
        assert strip_frontmatter(body.read_text()).lstrip().startswith("#")
