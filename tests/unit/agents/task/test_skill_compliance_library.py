# tests/unit/agents/task/test_skill_compliance_library.py
from pathlib import Path
from agents.task.agent.skill_frontmatter import parse_frontmatter

SKILLS = Path("data/prompts/skills")
NAME_OK = __import__("re").compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")

def _skill_dirs():
    return [d for d in SKILLS.iterdir() if d.is_dir() and (d / "SKILL.md").exists()
            and not d.name.startswith((".", "user_"))]

def test_every_bundled_skill_has_compliant_frontmatter():
    for d in _skill_dirs():
        meta, body = parse_frontmatter((d / "SKILL.md").read_text())
        assert meta.get("name") == d.name, f"{d.name}: name!=dir ({meta.get('name')!r})"
        assert NAME_OK.match(d.name) and len(d.name) <= 64
        desc = meta.get("description", "")
        assert 1 <= len(desc) <= 1024, f"{d.name}: bad description len {len(desc)}"
        assert meta.get("license") == "MIT"
        md = meta.get("metadata") or {}
        assert all(isinstance(v, str) for v in md.values()), f"{d.name}: non-str metadata"
        assert body.strip(), f"{d.name}: empty body after frontmatter"
