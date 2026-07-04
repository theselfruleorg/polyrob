"""Task 15: project-scope discovery trust gate + git-root walk.

Covers ``skill_discovery.trust_project_skills_effective()`` (server fail-closed,
local-default-ON, local-opt-out) and ``skill_discovery.project_external_roots()``
(CWD -> git-root walk, most-local first).
"""


def test_project_trust_is_fail_closed_off_on_server(monkeypatch):
    from agents.task.agent import skill_discovery
    from agents.task import constants
    monkeypatch.setattr(constants, "local_mode_enabled", lambda: False)      # server
    monkeypatch.setenv("POLYROB_TRUST_PROJECT_SKILLS", "true")               # even if someone sets it
    assert skill_discovery.trust_project_skills_effective() is False


def test_project_trust_on_for_local_operator_by_default(monkeypatch):
    from agents.task.agent import skill_discovery
    from agents.task import constants
    monkeypatch.setattr(constants, "local_mode_enabled", lambda: True)
    monkeypatch.delenv("POLYROB_TRUST_PROJECT_SKILLS", raising=False)
    assert skill_discovery.trust_project_skills_effective() is True


def test_local_operator_can_opt_out(monkeypatch):
    from agents.task.agent import skill_discovery
    from agents.task import constants
    monkeypatch.setattr(constants, "local_mode_enabled", lambda: True)
    monkeypatch.setenv("POLYROB_TRUST_PROJECT_SKILLS", "false")
    assert skill_discovery.trust_project_skills_effective() is False


def test_project_roots_walk_to_git_root(tmp_path, monkeypatch):
    from agents.task.agent import skill_discovery
    repo = tmp_path / "repo"; (repo / ".git").mkdir(parents=True)
    (repo / ".agents" / "skills").mkdir(parents=True)
    sub = repo / "a" / "b"; sub.mkdir(parents=True)
    monkeypatch.chdir(sub)
    roots = skill_discovery.project_external_roots()
    assert (repo / ".agents" / "skills").resolve() in [r.resolve() for r in roots]
