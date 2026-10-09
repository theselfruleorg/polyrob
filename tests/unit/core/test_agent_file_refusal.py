"""H11/H12 (security analysis 2026-09-23): the ONE agent-file deny seam."""
import os

import pytest

from core.path_safety import (PROJECT_CONTEXT_FILENAMES, agent_file_refusal,
                              data_home_refusal)


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    # A data home far from the test workspaces unless a test says otherwise.
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path / "elsewhere-home"))


def test_context_filenames_match_the_loader():
    from agents.task.agent.core.project_context import _CONTEXT_FILENAMES
    assert {n.lower() for n in _CONTEXT_FILENAMES} == set(PROJECT_CONTEXT_FILENAMES)


@pytest.mark.parametrize("rel", [".git/config", ".git/hooks/pre-commit",
                                 "sub/.git/config", ".GIT/config"])
def test_git_segment_refused_for_read_and_write(tmp_path, rel):
    for write in (True, False):
        assert agent_file_refusal(str(tmp_path / rel), str(tmp_path), write=write)


@pytest.mark.parametrize("rel", [".agents/skills/x/SKILL.md", ".claude/skills/y/SKILL.md",
                                 "pkg/.claude/skills/z/run.sh", ".Claude/Skills/a.md"])
def test_skill_dirs_refused_for_write_only(tmp_path, rel):
    assert agent_file_refusal(str(tmp_path / rel), str(tmp_path), write=True)
    assert agent_file_refusal(str(tmp_path / rel), str(tmp_path), write=False) is None


@pytest.mark.parametrize("name", ["AGENTS.md", "CLAUDE.md", "polyrob.md", "POLYROB.md",
                                  ".cursorrules", "agents.md"])
def test_project_context_at_root_refused_for_write(tmp_path, name):
    assert agent_file_refusal(str(tmp_path / name), str(tmp_path), write=True)
    assert agent_file_refusal(str(tmp_path / name), str(tmp_path), write=False) is None


def test_project_context_name_in_a_subdir_is_allowed(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert agent_file_refusal(str(tmp_path / "docs" / "AGENTS.md"), str(tmp_path),
                              write=True) is None


def test_project_context_in_cwd_ancestor_refused(tmp_path, monkeypatch):
    sub = tmp_path / "a" / "b"
    sub.mkdir(parents=True)
    monkeypatch.chdir(sub)
    # A workspace ABOVE the cwd: the upward context walk reaches tmp_path/a.
    assert agent_file_refusal(str(tmp_path / "a" / "CLAUDE.md"), str(tmp_path), write=True)


def test_symlink_to_skill_dir_refused(tmp_path):
    skills = tmp_path / ".claude" / "skills"
    skills.mkdir(parents=True)
    (tmp_path / "innocent").symlink_to(skills)
    assert agent_file_refusal(str(tmp_path / "innocent" / "evil.md"), str(tmp_path),
                              write=True)


def test_ordinary_file_allowed(tmp_path):
    assert agent_file_refusal(str(tmp_path / "src" / "app.py"), str(tmp_path), write=True) is None


def test_local_shape_data_home_inside_workspace_refused(tmp_path, monkeypatch):
    home = tmp_path / ".polyrob"
    (home / "sessions").mkdir(parents=True)
    monkeypatch.setenv("POLYROB_DATA_DIR", str(home))
    for rel in ("goals.db", "sessions/u/sessions/s/message_history.json", "skills/x/SKILL.md"):
        for write in (True, False):
            assert agent_file_refusal(str(home / rel), str(tmp_path), write=write), rel
    assert agent_file_refusal(str(tmp_path / "notes.md"), str(tmp_path), write=True) is None


def test_server_shape_workspace_under_data_home_allowed(tmp_path, monkeypatch):
    home = tmp_path / "var-lib-polyrob"
    ws = home / "sessions" / "u" / "sessions" / "s" / "workspace"
    ws.mkdir(parents=True)
    monkeypatch.setenv("POLYROB_DATA_DIR", str(home))
    assert agent_file_refusal(str(ws / "report.md"), str(ws), write=True) is None
    # A sibling session file is still the data home.
    assert data_home_refusal(str(home / "sessions" / "u" / "sessions" / "s" /
                                 "message_history.json"), str(ws))


def test_symlink_into_data_home_refused(tmp_path, monkeypatch):
    home = tmp_path / ".polyrob"
    home.mkdir()
    (home / "cron.db").write_text("x")
    monkeypatch.setenv("POLYROB_DATA_DIR", str(home))
    ws = tmp_path / "ws"
    ws.mkdir()
    os.symlink(home / "cron.db", ws / "link.db")
    assert agent_file_refusal(str(ws / "link.db"), str(ws), write=False)


def test_extra_home_is_honoured(tmp_path):
    other = tmp_path / "cfg-data"
    other.mkdir()
    assert agent_file_refusal(str(other / "x.json"), str(tmp_path), write=False,
                              extra_homes=[str(other)])


@pytest.mark.parametrize("relative", [".husky/pre-commit", ".mcp.json", ".pre-commit-config.yaml",
                                      "lefthook.yml", ".claude/settings.json", ".claude/settings.local.json"])
def test_hook_manager_and_agent_settings_writes_refused(tmp_path, relative):
    from core.path_safety import agent_file_refusal
    assert agent_file_refusal(str(tmp_path / relative), str(tmp_path), write=True)
