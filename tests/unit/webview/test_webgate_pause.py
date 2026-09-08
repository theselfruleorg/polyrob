"""031 T12: the web console pause endpoints over the one record."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("AUTONOMY_HALT", raising=False)
    monkeypatch.delenv("DATA_ROOT", raising=False)
    monkeypatch.setattr("core.runtime_paths.resolve_data_home", lambda: tmp_path)
    import webview.pages as pages
    monkeypatch.setattr(pages, "_effective_user_id", lambda req: "rob")
    monkeypatch.setattr(pages, "_data_dir", lambda: str(tmp_path))
    app = FastAPI()
    app.include_router(pages.router)
    return TestClient(app)


def test_pause_endpoints_roundtrip(client):
    assert client.get("/api/webgate/pause").json()["paused"] is False
    r = client.post("/api/webgate/pause", json={"scopes": ["social"], "duration_minutes": 30})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert "Paused social" in r.json()["message"]
    st = client.get("/api/webgate/pause").json()
    assert st["scopes"] == ["social"] and st["via"] == "webview" and st["until"]
    assert client.get("/api/webgate/halt").json()["halted"] is False
    r = client.post("/api/webgate/halt")
    assert r.json()["state"]["scopes"] == ["all"] and r.json()["halted"] is True
    r = client.post("/api/webgate/resume", json={})
    assert r.json()["ok"] is True and client.get("/api/webgate/pause").json()["paused"] is False


def test_scoped_resume_and_bad_body(client):
    client.post("/api/webgate/pause", json={"scopes": ["cron", "streams"]})
    r = client.post("/api/webgate/resume", json={"scopes": ["cron"]})
    assert r.json()["ok"] is True and r.json()["state"]["scopes"] == ["streams"]
    assert "still paused: streams" in r.json()["message"]
    assert client.post("/api/webgate/pause", json={"scopes": "cron"}).status_code == 400
    assert client.post("/api/webgate/pause", json={"duration_minutes": "soon"}).status_code == 400
