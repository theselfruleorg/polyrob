from pathlib import Path
from agents.task.agent.skill_discovery import discover_skills, DiscoveredSkill


def _write(d: Path, name: str, fm: str, body: str = "# body\ncontent"):
    p = d / name; p.mkdir(parents=True)
    (p / "SKILL.md").write_text(fm + body); return p


def test_discovers_frontmatter_only_external_skill(tmp_path):
    _write(tmp_path, "3d-modeling",           # digit-leading name = spec-valid, strict-rejected
           "---\nname: 3d-modeling\ndescription: Model 3D things. Use when modeling.\n---\n")
    found = discover_skills(tmp_path, "user")
    assert [s.skill_id for s in found] == ["3d-modeling"]
    assert isinstance(found[0], DiscoveredSkill) and found[0].scope == "user"
    assert found[0].body.strip().startswith("# body")   # frontmatter stripped


def test_skips_skill_with_no_description_but_keeps_valid_sibling(tmp_path):
    _write(tmp_path, "good", "---\nname: good\ndescription: ok\n---\n")
    _write(tmp_path, "bad",  "---\nname: bad\n---\n")            # no description -> error -> skip
    ids = {s.skill_id for s in discover_skills(tmp_path, "user")}
    assert ids == {"good"}


def test_scan_is_bounded_and_ignores_noise_dirs(tmp_path):
    _write(tmp_path, "real", "---\nname: real\ndescription: d\n---\n")
    (tmp_path / "node_modules" / "pkg").mkdir(parents=True)
    (tmp_path / ".git").mkdir()
    ids = {s.skill_id for s in discover_skills(tmp_path, "user", max_count=2000)}
    assert ids == {"real"}


def test_max_count_bounds_directories_visited_not_just_skills_found(tmp_path, monkeypatch):
    # 30 sibling directories, NONE containing SKILL.md, so the old "seen"
    # counter (incremented only on a matched skill) never advances and the
    # walk processes every directory regardless of max_count. Instrument
    # Path.iterdir to count how many directories actually got expanded.
    for i in range(30):
        (tmp_path / f"noise_{i}").mkdir()
    calls = {"n": 0}
    orig_iterdir = Path.iterdir

    def counting_iterdir(self):
        calls["n"] += 1
        return orig_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", counting_iterdir)
    discover_skills(tmp_path, "user", max_count=5)
    assert calls["n"] <= 5, (
        f"expected at most 5 directories processed (max_count bound), got {calls['n']}"
    )


def test_external_skill_is_catalog_visible_and_loadable(tmp_path, monkeypatch):
    from agents.task.agent import skill_discovery, skill_manager as sm
    ext = tmp_path / ".agents" / "skills" / "widget"
    ext.mkdir(parents=True)
    (ext / "SKILL.md").write_text("---\nname: widget\ndescription: Do widgets.\n---\n# W\nBODY")
    monkeypatch.setattr(skill_discovery, "user_external_roots", lambda: [tmp_path / ".agents" / "skills"])
    monkeypatch.setattr(skill_discovery, "trust_project_skills_effective", lambda: False, raising=False)
    mgr = sm.SkillManager()
    assert any(c["id"] == "widget" for c in mgr.get_catalog_skills())
    assert "BODY" in mgr._load_skill_content("widget", user_id=None)
