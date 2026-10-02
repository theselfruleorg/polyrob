"""WS7: the workspace file routes refused real files by SUBSTRING
(`windows`, `c:`, `/home/`, `/.`). The rule is now `..`/absolute + resolved
containment only — one helper (webview/workspace_paths.py) for both routes."""
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch, tmp_path):
    from webview import server
    ws = tmp_path / "ws"
    (ws / "docs").mkdir(parents=True)
    (ws / "docs" / "windows-setup.md").write_text("win")
    (ws / "site" / ".well-known").mkdir(parents=True)
    (ws / "site" / ".well-known" / "security.txt").write_text("sec")
    (ws / "users" / "home").mkdir(parents=True)
    (ws / "users" / "home" / "notes.md").write_text("home")
    (ws / "abc:def.txt").write_text("colon")
    (tmp_path / "outside.txt").write_text("secret")
    (ws / "link.txt").symlink_to(tmp_path / "outside.txt")
    monkeypatch.setattr(server, "pm", lambda: SimpleNamespace(
        clean_session_id=lambda sid: sid, get_session_user=lambda sid: "owner",
        get_workspace_dir=lambda sid, user_id: ws))
    monkeypatch.setattr(server, "_served_file_refusal", lambda p: False)
    app = FastAPI()
    for ep in (server.api_workspace_file, server.api_workspace_serve):
        app.router.routes.append(next(r for r in server._fastapi.routes
                                      if getattr(r, "endpoint", None) is ep))
    return TestClient(app)


@pytest.mark.parametrize("rel,body", [
    ("docs/windows-setup.md", "win"), ("site/.well-known/security.txt", "sec"),
    ("users/home/notes.md", "home"), ("abc:def.txt", "colon")])
def test_real_files_are_served(client, rel, body):
    r = client.get("/api/session/s/workspace/file", params={"path": rel})
    assert r.status_code == 200 and r.text == body
    r = client.get(f"/api/session/s/workspace/serve/{rel}")
    assert r.status_code == 200 and r.text == body


@pytest.mark.parametrize("rel", ["../outside.txt", "docs/../../outside.txt", "/etc/passwd",
                                 "%2e%2e/outside.txt", "..\\outside.txt", "C:/x", "link.txt"])
def test_escapes_are_refused(client, rel):
    r = client.get("/api/session/s/workspace/file", params={"path": rel})
    assert r.status_code in (403, 404) and "secret" not in r.text


def test_helper_rules(tmp_path):
    from webview.workspace_paths import OutsideWorkspace, resolve_in_workspace
    (tmp_path / "a").mkdir()
    assert resolve_in_workspace(tmp_path, "a/x.md") == (tmp_path / "a" / "x.md").resolve()
    for bad in ("..", "a/../../b", "/abs", "\\\\unc", "%2e%2e/b", "D:\\x", "a\x00b"):
        with pytest.raises(OutsideWorkspace):
            resolve_in_workspace(tmp_path, bad)


def test_a_binary_download_still_names_the_file(client, tmp_path):
    (tmp_path / "ws" / "data.bin").write_bytes(b"\x00\x01")
    r = client.get("/api/session/s/workspace/file", params={"path": "data.bin"})
    assert r.status_code == 200 and "data.bin" in r.headers["content-disposition"]
