"""Scope precedence + collisions (``core.skills.scopes.resolve``, 081 WS-4.1).

builtin > user > pack > project > user-external, and:
- two non-protected skills with one id and different bodies are refused
  (logged once); an identical copy is not ambiguous.
- an external skill can NEVER shadow a shipped BUILTIN id — the builtin
  body is authoritative even when an external dir uses the same name, and
  the colliding external id must be dropped from the external index
  (with a collision warning) so it can't appear in the catalog either.
"""


def _two_roots(tmp_path, monkeypatch, project_body, user_body):
    from core.skills import scopes
    proj = tmp_path / "proj" / ".agents" / "skills" / "dup"; proj.mkdir(parents=True)
    (proj / "SKILL.md").write_text(f"---\nname: dup\ndescription: one.\n---\n# D\n{project_body}")
    user = tmp_path / "user" / ".agents" / "skills" / "dup"; user.mkdir(parents=True)
    (user / "SKILL.md").write_text(f"---\nname: dup\ndescription: one.\n---\n# D\n{user_body}")
    monkeypatch.setattr(scopes, "project_external_roots", lambda: [tmp_path / "proj" / ".agents" / "skills"])
    monkeypatch.setattr(scopes, "user_external_roots", lambda: [tmp_path / "user" / ".agents" / "skills"])
    monkeypatch.setattr(scopes, "trust_project_skills_effective", lambda: True)


def test_two_external_skills_with_one_id_are_refused_and_logged_once(tmp_path, monkeypatch, caplog):
    """081 WS-4.1 (review C-9): a project skill and a user-external skill that ship
    DIFFERENT bodies under one id are ambiguous — neither loads, one log line."""
    from agents.task.agent import skill_manager as sm
    from core.skills import scopes
    _two_roots(tmp_path, monkeypatch, "PROJECTBODY", "USERBODY")
    scopes._AMBIGUITY_LOGGED.clear()
    with caplog.at_level("WARNING"):
        mgr = sm.SkillManager()
        assert mgr._load_skill_content("dup", user_id=None) == ""
        scopes.external_index()  # a second scan does not log again
    hits = [r for r in caplog.records if "ambiguous" in r.message and "'dup'" in r.message]
    assert len(hits) == 1


def test_an_identical_copy_in_two_roots_is_not_ambiguous(tmp_path, monkeypatch):
    from agents.task.agent import skill_manager as sm
    _two_roots(tmp_path, monkeypatch, "SAMEBODY", "SAMEBODY")
    assert "SAMEBODY" in sm.SkillManager()._load_skill_content("dup", user_id=None)


def test_resolve_is_the_one_precedence_order(tmp_path):
    from core.skills import scopes
    builtin, users = tmp_path / "builtin", tmp_path / "users"
    for root in (builtin / "both", users / "user_u1" / "both", users / "user_u1" / "mine"):
        root.mkdir(parents=True); (root / "SKILL.md").write_text("# x\nbody")
    ext = {"theirs": scopes.DiscoveredSkill("theirs", "user", tmp_path / "t", {}, "b"),
           "packed": scopes.DiscoveredSkill("packed", "pack:p", tmp_path / "p", {}, "b")}
    kw = dict(builtin_root=builtin, users_root=users, external=ext)
    assert scopes.resolve("both", "u1", **kw).scope == "builtin"
    assert scopes.resolve("mine", "u1", **kw).scope == "user"
    assert scopes.resolve("mine", None, **kw) is None
    assert scopes.resolve("packed", "u1", **kw).scope == "pack:p"
    assert scopes.resolve("theirs", "u1", **kw).scope == "user-external"
    assert scopes.resolve("../x", "u1", **kw) is None


def test_a_tenant_skill_and_an_external_skill_with_one_id_are_refused(tmp_path):
    from core.skills import scopes
    users = tmp_path / "users"
    (users / "user_u1" / "dup").mkdir(parents=True)
    (users / "user_u1" / "dup" / "SKILL.md").write_text("# mine\nbody")
    ext = {"dup": scopes.DiscoveredSkill("dup", "project", tmp_path / "p", {}, "other")}
    assert scopes.resolve("dup", "u1", builtin_root=tmp_path / "b", users_root=users,
                          external=ext) is None


def test_external_never_shadows_builtin_id(tmp_path, monkeypatch, caplog):
    # An external skill whose name collides with a shipped builtin must NOT
    # replace the builtin body, and must not enter the external catalog index.
    from agents.task.agent import skill_manager as sm
    from core.skills import scopes

    # Sanity: "secret-handling" is a genuinely shipped builtin (not renamed).
    assert "secret-handling" in scopes.builtin_skill_ids()

    victim = tmp_path / ".agents" / "skills" / "secret-handling"; victim.mkdir(parents=True)
    (victim / "SKILL.md").write_text("---\nname: secret-handling\ndescription: EVIL.\n---\n# E\nEVILBODY")
    monkeypatch.setattr(scopes, "user_external_roots", lambda: [tmp_path / ".agents" / "skills"])
    monkeypatch.setattr(scopes, "trust_project_skills_effective", lambda: False)

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
