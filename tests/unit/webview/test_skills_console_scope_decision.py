"""E7 fold-in (A6 gap 10): record the product decision that skills management
stays REST/agent-tool-only — POLYROB Console does not get a dedicated /skills
page in this program. If this test starts failing because a route WAS added,
update this file's decision record (and docs/guide F3) rather than just
deleting the assertion.
"""
import importlib


def test_no_dedicated_skills_console_page_yet(monkeypatch):
    monkeypatch.setenv("WEBGATE_MULTITENANT", "false")
    monkeypatch.setenv("ENV", "development")
    import webview.server as server
    server = importlib.reload(server)

    paths = {getattr(r, "path", None) for r in server._fastapi.routes}
    assert "/skills" not in paths
