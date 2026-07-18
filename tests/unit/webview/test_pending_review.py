"""Pending self-evolution review — /pending + /api/webgate/pending* (parity G5).

The 2026-07-12 UI-surface review: CLI (`polyrob owner pending/promote/reject`),
REPL (`/pending`) and Telegram (`/pending /approve /reject`) can all act on the
agent's quarantined proposals, but the webview could only LIST pending skills
(knowledge section) with no approve/reject — a web-only owner could approve
nothing. These endpoints ride the SAME aggregator every other surface uses
(``core.self_evolution.list_pending/show/promote/reject``), so a decision made
here is byte-identical to one made on the CLI.

Exercised against the REAL pref-change pipeline (no mocks): propose → list →
promote/reject → observe preferences.toml.
"""
import importlib

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _client(monkeypatch, tmp_path, user_id="u1"):
    import webview.pages as pages
    monkeypatch.setattr(pages, "_effective_user_id", lambda req: user_id)
    monkeypatch.setattr(pages, "_data_dir", lambda: str(tmp_path))
    app = FastAPI()
    app.include_router(pages.router)
    return TestClient(app), pages


def _propose(tmp_path, user_id="u1", key="budget.wallet_daily_usd", value=2.5):
    from core.instance import resolve_instance_id
    from core.prefs import propose_pref_change
    ok, pid = propose_pref_change(user_id, key, value, tmp_path,
                                  resolve_instance_id())
    assert ok, pid
    return pid


def test_pending_list_shows_proposed_pref_change(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path)
    pid = _propose(tmp_path)
    r = client.get("/api/webgate/pending")
    assert r.status_code == 200
    items = r.json()["items"]
    pref_items = [it for it in items if it["kind"] == "pref_change"]
    assert [it["id"] for it in pref_items] == [pid]
    assert pref_items[0]["preview"]


def test_pending_show_returns_full_body(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path)
    pid = _propose(tmp_path)
    r = client.get(f"/api/webgate/pending/pref_change/{pid}")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert "budget.wallet_daily_usd" in body["body"]


def test_promote_applies_the_pref(monkeypatch, tmp_path):
    from core.prefs import load_preferences
    client, _ = _client(monkeypatch, tmp_path)
    pid = _propose(tmp_path)
    r = client.post(f"/api/webgate/pending/pref_change/{pid}/promote")
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert load_preferences(tmp_path, "u1").get("budget.wallet_daily_usd") == 2.5
    # queue drains
    assert client.get("/api/webgate/pending").json()["items"] == []


def test_reject_archives_without_applying(monkeypatch, tmp_path):
    from core.prefs import load_preferences
    client, _ = _client(monkeypatch, tmp_path)
    pid = _propose(tmp_path)
    r = client.post(f"/api/webgate/pending/pref_change/{pid}/reject")
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert load_preferences(tmp_path, "u1").get("budget.wallet_daily_usd") is None
    assert client.get("/api/webgate/pending").json()["items"] == []


def test_promote_unknown_item_is_ok_false_not_500(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path)
    r = client.post("/api/webgate/pending/pref_change/nope/promote")
    assert r.status_code == 200
    assert r.json()["ok"] is False


def test_read_only_blocks_promote_and_reject_allows_list(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path)
    pid = _propose(tmp_path)
    monkeypatch.setenv("WEBVIEW_READ_ONLY", "true")
    assert client.get("/api/webgate/pending").status_code == 200
    assert client.post(f"/api/webgate/pending/pref_change/{pid}/promote").status_code == 403
    assert client.post(f"/api/webgate/pending/pref_change/{pid}/reject").status_code == 403


def test_decisions_are_tenant_scoped(monkeypatch, tmp_path):
    """A tenant identity resolved by _effective_user_id can only act on ITS
    pending items — u2 promoting u1's proposal is a no-op miss, not a write."""
    from core.prefs import load_preferences
    pid = _propose(tmp_path, user_id="u1")
    client, _ = _client(monkeypatch, tmp_path, user_id="u2")
    r = client.post(f"/api/webgate/pending/pref_change/{pid}/promote")
    assert r.status_code == 200 and r.json()["ok"] is False
    assert load_preferences(tmp_path, "u1").get("budget.wallet_daily_usd") is None


def test_pending_page_renders_200(monkeypatch):
    monkeypatch.setenv("WEBGATE_MULTITENANT", "false")
    monkeypatch.setenv("ENV", "development")
    import webview.server as server
    server = importlib.reload(server)
    client = TestClient(server._fastapi)
    r = client.get("/pending")
    assert r.status_code == 200
    assert "Pending review" in r.text


@pytest.fixture(autouse=True)
def _restore_server():
    yield
    import webview.server as server
    importlib.reload(server)
