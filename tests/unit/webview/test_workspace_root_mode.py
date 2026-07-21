"""QW-3 (2026-07-19, assessment §3.3): the webview must browse the SAME
workspace root the agent writes to. With POLYROB_PROJECT_DIR set the agent
process runs pm() in project-root mode (core/bootstrap), but the webview's
startup pm() install ignored it — the file browser showed the EMPTY per-session
dir while artifacts sat in the project dir. Single-tenant postures only:
multitenant must NEVER share one project root across tenants.
"""
import importlib


def _kwargs(monkeypatch, posture, project_dir):
    import webview.webgate as webgate
    monkeypatch.setattr(webgate, "posture", lambda: posture)
    if project_dir is None:
        monkeypatch.delenv("POLYROB_PROJECT_DIR", raising=False)
    else:
        monkeypatch.setenv("POLYROB_PROJECT_DIR", str(project_dir))
    from webview.server import _workspace_mode_kwargs
    return _workspace_mode_kwargs()


def test_own_ops_with_project_dir_gets_project_root_mode(monkeypatch, tmp_path):
    kw = _kwargs(monkeypatch, "own_ops", tmp_path / "project")
    assert kw["workspace_is_project_root"] is True
    assert str(tmp_path / "project") in kw["project_root"]


def test_local_posture_with_project_dir_gets_project_root_mode(monkeypatch, tmp_path):
    kw = _kwargs(monkeypatch, "local", tmp_path / "project")
    assert kw["workspace_is_project_root"] is True


def test_multitenant_never_gets_project_root_mode(monkeypatch, tmp_path):
    kw = _kwargs(monkeypatch, "multitenant", tmp_path / "project")
    assert kw == {}


def test_no_project_dir_keeps_legacy_kwargs(monkeypatch):
    kw = _kwargs(monkeypatch, "own_ops", None)
    assert kw == {}


def test_workspace_file_guard_refuses_credential_shaped(tmp_path):
    """Review Minor #9: under project-root mode a top-level .env would be
    servable — the file endpoints must refuse credential-shaped names."""
    from pathlib import Path
    from webview.server import _served_file_refusal
    assert _served_file_refusal(Path(tmp_path / ".env")) is not None
    assert _served_file_refusal(Path(tmp_path / "polyrob.env")) is not None
    assert _served_file_refusal(Path(tmp_path / "report.md")) is None
