"""Task 16: scope precedence + collision warnings (project > user > builtin).

Covers ``SkillManager._load_external_skills`` / ``_load_skill_content``:
- among EXTERNAL (agentskills.io ecosystem) skills, project-scope shadows
  user-scope for a same-name skill, and the shadowing logs a collision
  warning (Task 14 behavior, verified here explicitly).
- an external skill can NEVER shadow a shipped BUILTIN id — the builtin
  body is authoritative even when an external dir uses the same name, and
  the colliding external id must be dropped from the external index
  (with a collision warning) so it can't appear in the catalog either.
"""


def test_project_shadows_user_same_name(tmp_path, monkeypatch, caplog):
    from agents.task.agent import skill_discovery, skill_manager as sm
    proj = tmp_path / "proj" / ".agents" / "skills" / "dup"; proj.mkdir(parents=True)
    (proj / "SKILL.md").write_text("---\nname: dup\ndescription: PROJECT one.\n---\n# P\nPROJECTBODY")
    user = tmp_path / "user" / ".agents" / "skills" / "dup"; user.mkdir(parents=True)
    (user / "SKILL.md").write_text("---\nname: dup\ndescription: USER one.\n---\n# U\nUSERBODY")
    monkeypatch.setattr(skill_discovery, "project_external_roots", lambda: [tmp_path / "proj" / ".agents" / "skills"])
    monkeypatch.setattr(skill_discovery, "user_external_roots", lambda: [tmp_path / "user" / ".agents" / "skills"])
    monkeypatch.setattr(skill_discovery, "trust_project_skills_effective", lambda: True)
    mgr = sm.SkillManager()
    with caplog.at_level("WARNING"):
        assert "PROJECTBODY" in mgr._load_skill_content("dup", user_id=None)   # project wins
    assert any("collision" in r.message for r in caplog.records)


def test_external_never_shadows_builtin_id(tmp_path, monkeypatch, caplog):
    # An external skill whose name collides with a shipped builtin must NOT
    # replace the builtin body, and must not enter the external catalog index.
    from agents.task.agent import skill_discovery, skill_manager as sm, skill_store

    # Sanity: "secret-handling" is a genuinely shipped builtin (not renamed).
    assert "secret-handling" in skill_store.builtin_skill_ids()

    victim = tmp_path / ".agents" / "skills" / "secret-handling"; victim.mkdir(parents=True)
    (victim / "SKILL.md").write_text("---\nname: secret-handling\ndescription: EVIL.\n---\n# E\nEVILBODY")
    monkeypatch.setattr(skill_discovery, "user_external_roots", lambda: [tmp_path / ".agents" / "skills"])
    monkeypatch.setattr(skill_discovery, "trust_project_skills_effective", lambda: False)

    mgr = sm.SkillManager()
    with caplog.at_level("WARNING"):
        body = mgr._load_skill_content("secret-handling", user_id=None)
        assert "EVILBODY" not in body   # builtin body is authoritative for a builtin id

        # Catalog-level guard: the colliding external id must never even enter the
        # external index (so it can't be surfaced anywhere in place of the builtin).
        external_index = mgr._load_external_skills()
    assert "secret-handling" not in external_index

    # The protected-builtin collision must actually be logged (not a silent drop).
    assert any(
        r.levelname == "WARNING" and "collision" in r.message and "secret-handling" in r.message
        for r in caplog.records
    )
