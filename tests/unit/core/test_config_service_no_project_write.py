"""W8 (2026-10-10): no project-env write plumbing is left reachable.

polyrob does not load ./.polyrob/.env; set_value refuses scope="project". The
internal path helper must refuse it too, so a caller that bypasses set_value
cannot write a file nothing reads.
"""
import pytest


def test_internal_env_path_refuses_project_scope():
    from core.config_service import _env_path, home_env_path
    with pytest.raises(ValueError):
        _env_path("project")
    assert _env_path("global") == home_env_path()


def test_set_value_refuses_project_scope(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from core.config_service import set_value
    res = set_value("LOG_LEVEL", "DEBUG", scope="project", surface="local")
    assert not res.ok and res.outcome == "refused"
    assert not (tmp_path / ".polyrob" / ".env").exists()
