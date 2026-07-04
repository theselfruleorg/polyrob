"""MT-1 hard ratchet: the multi-tenant server NEVER becomes project-root.

Claim #4's only real defense is "project-root mode is installed onto the global
pm() only inside build_cli_container, and the server never calls it." That is a
convention enforced in one function — pin it with tests so a future import can't
silently collapse every tenant into one folder.
"""

import pathlib

import pytest


@pytest.fixture(autouse=True)
def _reset_pm():
    from agents.task.path import reset_path_manager
    reset_path_manager()
    yield
    reset_path_manager()


def test_server_path_manager_is_per_session_even_with_project_env(monkeypatch, tmp_path):
    # build_bot constructs the manager via get_path_manager(data_root=...) with NO
    # project-root args. Setting POLYROB_PROJECT_DIR must NOT make it project-root.
    monkeypatch.setenv("POLYROB_PROJECT_DIR", str(tmp_path / "proj"))
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path / "data"))
    from agents.task.path import get_path_manager
    server_pm = get_path_manager(data_root=str(tmp_path / "data" / "task"))
    assert server_pm.is_project_root_workspace is False


def test_two_tenants_resolve_to_different_roots(tmp_path):
    from agents.task.path import get_path_manager
    server_pm = get_path_manager(data_root=str(tmp_path / "task"))
    a = server_pm.get_workspace_dir("sess-aaa", user_id="tenant_a")
    b = server_pm.get_workspace_dir("sess-bbb", user_id="tenant_b")
    assert a != b
    assert "tenant_a" in str(a) and "tenant_b" in str(b)


def test_server_modules_do_not_import_cli_container():
    # Static ratchet: api/server code must never reach build_cli_container or
    # set_path_manager (the only sanctioned project-root installer is the CLI).
    repo = pathlib.Path(__file__).resolve().parents[3]
    offenders = []
    for sub in ("api",):
        for p in (repo / sub).rglob("*.py"):
            text = p.read_text(encoding="utf-8", errors="ignore")
            if "build_cli_container" in text or "set_path_manager" in text:
                offenders.append(str(p.relative_to(repo)))
    assert offenders == [], f"server code must not install a project-root pm(): {offenders}"
