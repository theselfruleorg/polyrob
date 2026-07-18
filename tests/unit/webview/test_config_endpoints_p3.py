"""P3 (proposal 018): webgate config endpoints over core.config_service."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _client(monkeypatch, tmp_path, user_id="u1", posture="local",
            read_only=False):
    import webview.pages as pages
    from webview import webgate
    monkeypatch.setattr(pages, "_effective_user_id", lambda req: user_id)
    monkeypatch.setattr(pages, "_data_dir", lambda: str(tmp_path))
    monkeypatch.setattr(webgate, "posture", lambda: posture)
    monkeypatch.setattr(webgate, "read_only", lambda: read_only)
    app = FastAPI()
    app.include_router(pages.router)
    return TestClient(app)


@pytest.fixture(autouse=True)
def _cwd(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("POLYROB_HOME", str(tmp_path / "home"))


def test_config_search_spans_namespaces(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    body = c.get("/api/webgate/config", params={"query": "wallet"}).json()
    keys = [s["key"] for s in body["settings"]]
    assert "budget.wallet_daily_usd" in keys
    assert any(k.startswith("WALLET_") for k in keys)


def test_config_explain_masks_secrets(monkeypatch, tmp_path):
    monkeypatch.setenv("ANYSITE_API_KEY", "sk-supersecretvalue")
    c = _client(monkeypatch, tmp_path)
    body = c.get("/api/webgate/config/ANYSITE_API_KEY/explain").json()
    assert "supersecret" not in str(body)
    assert body["secret"] is True
    assert c.get("/api/webgate/config/nope.nothing/explain").status_code == 404


def test_config_patch_pref_and_flag(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    r = c.patch("/api/webgate/config/goals.daily_quota", json={"value": "3"})
    assert r.status_code == 200 and r.json()["outcome"] == "written"
    # guarded pref without confirm -> queued (202), with confirm -> written
    r2 = c.patch("/api/webgate/config/budget.wallet_daily_usd",
                 json={"value": "5"})
    assert r2.status_code == 202 and r2.json()["outcome"] == "queued"
    r3 = c.patch("/api/webgate/config/budget.wallet_daily_usd",
                 json={"value": "5", "confirm": True})
    assert r3.status_code == 200
    # flag write lands in the project env file, restart-effective
    r4 = c.patch("/api/webgate/config/GOALS_ENABLED", json={"value": "on"})
    assert r4.status_code == 200
    assert "GOALS_ENABLED=on" in (tmp_path / ".polyrob" / ".env").read_text()


def test_flag_write_denied_off_owner_postures(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path, posture="multitenant")
    r = c.patch("/api/webgate/config/GOALS_ENABLED", json={"value": "on"})
    assert r.status_code == 403
    # prefs still writable in multitenant (tenant-scoped store)
    r2 = c.patch("/api/webgate/config/goals.daily_quota", json={"value": "3"})
    assert r2.status_code == 200


def test_read_only_blocks_all_writes(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path, read_only=True)
    r = c.patch("/api/webgate/config/goals.daily_quota", json={"value": "3"})
    assert r.status_code == 403
