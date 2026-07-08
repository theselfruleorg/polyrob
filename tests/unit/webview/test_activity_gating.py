"""Task 4 — /activity page + backfill API posture gating (webview/activity.py).

Access model: local = open; own_ops = owner cookie (middleware) suffices;
multitenant = admin tier or instance owner ONLY (a global cross-tenant stream
must never be tenant-visible); flag off = 404 everywhere.
"""
import importlib
import json
import types

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient

_ENV_KEYS = (
    "POLYROB_POSTURE", "WEBGATE_MULTITENANT", "WEBGATE_HOST", "WEBGATE_PORT",
    "JWT_SECRET_KEY", "POLYROB_OWNER_USERNAME", "POLYROB_OWNER_PASSWORD_HASH",
    "ENVIRONMENT", "WEBVIEW_ACTIVITY_ENABLED",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    yield


def _reload_webview():
    import webview.webgate as wg
    importlib.reload(wg)
    import webview.owner_auth as oa
    importlib.reload(oa)
    import webview.server as srv
    importlib.reload(srv)
    return srv


def _client(monkeypatch, posture, owner_creds=False):
    monkeypatch.setenv("POLYROB_POSTURE", posture)
    if owner_creds:
        monkeypatch.setenv("JWT_SECRET_KEY", "test-secret")
        monkeypatch.setenv("POLYROB_OWNER_USERNAME", "op")
        monkeypatch.setenv("POLYROB_OWNER_PASSWORD_HASH", PasswordHasher().hash("s3cret"))
        monkeypatch.setenv("ENVIRONMENT", "development")
    srv = _reload_webview()
    return TestClient(srv._fastapi)


def test_local_activity_page_and_backfill_open(monkeypatch):
    client = _client(monkeypatch, "local")
    page = client.get("/activity")
    assert page.status_code == 200
    assert 'id="activity-stream"' in page.text
    back = client.get("/api/activity/backfill")
    assert back.status_code == 200
    assert isinstance(back.json()["events"], list)


def test_own_ops_unauthenticated_denied(monkeypatch):
    client = _client(monkeypatch, "own_ops", owner_creds=True)
    page = client.get("/activity", follow_redirects=False)
    assert page.status_code in (302, 303, 307, 401)
    back = client.get("/api/activity/backfill")
    assert back.status_code == 401


def test_own_ops_owner_cookie_allowed(monkeypatch):
    import re
    client = _client(monkeypatch, "own_ops", owner_creds=True)
    page = client.get("/owner-login")
    match = re.search(r'name="csrf_token" value="([0-9a-f]+)"', page.text)
    login = client.post("/owner-login",
                        data={"username": "op", "password": "s3cret",
                              "csrf_token": match.group(1) if match else ""},
                        follow_redirects=False)
    assert login.status_code in (302, 303)
    page = client.get("/activity")
    assert page.status_code == 200
    back = client.get("/api/activity/backfill")
    assert back.status_code == 200


def test_flag_off_is_404_even_in_local(monkeypatch):
    monkeypatch.setenv("WEBVIEW_ACTIVITY_ENABLED", "false")
    client = _client(monkeypatch, "local")
    assert client.get("/activity").status_code == 404
    assert client.get("/api/activity/backfill").status_code == 404


def test_multitenant_tenant_denied_owner_allowed(monkeypatch):
    """Direct gate check — a plain authenticated tenant must NOT see the
    global stream; admin tier and the instance owner may."""
    monkeypatch.setenv("POLYROB_POSTURE", "multitenant")
    from fastapi import HTTPException
    import webview.activity as activity
    importlib.reload(activity)

    def _req(user_id, tier="standard", is_admin=False):
        state = types.SimpleNamespace(user_id=user_id, tier=tier, is_admin=is_admin)
        return types.SimpleNamespace(state=state)

    with pytest.raises(HTTPException) as exc:
        activity._require_activity_access(_req("tenant-b"))
    assert exc.value.status_code == 403

    activity._require_activity_access(_req("whoever", tier="admin"))  # no raise
    activity._require_activity_access(_req("anyone", is_admin=True))  # no raise

    import webview.webgate as wg
    activity._require_activity_access(_req(wg.local_owner_id()))  # instance owner


def test_cold_backfill_merges_sources(monkeypatch, tmp_path):
    """With an empty hub, backfill seeds from DB tails + recent feed files."""
    import sqlite3
    import webview.activity as activity

    # goals.db with one goal event
    goals = tmp_path / "goals.db"
    con = sqlite3.connect(goals)
    con.execute("""CREATE TABLE goal_events (id INTEGER PRIMARY KEY AUTOINCREMENT,
                   goal_id TEXT, kind TEXT, payload TEXT, created_at REAL)""")
    con.execute("INSERT INTO goal_events (goal_id, kind, payload, created_at)"
                " VALUES ('g1','created','{}',10.0)")
    con.commit(); con.close()

    # session feed file
    feed = tmp_path / "sessions" / "rob" / "s-1" / "feed"
    feed.mkdir(parents=True)
    (feed / "000001_session_start.json").write_text(
        json.dumps({"type": "session_start", "_ts_ms": 11000, "_seq": 1,
                    "data": {"task": "demo"}}))

    monkeypatch.setattr(activity, "_data_dir", lambda: str(tmp_path))
    monkeypatch.setattr(activity, "_sessions_data_root", lambda: str(tmp_path / "sessions"))

    events = activity._cold_backfill(50)
    kinds = {e["kind"] for e in events}
    assert "goal_created" in kinds
    assert "session_start" in kinds
    assert events == sorted(events, key=lambda e: e["ts"])
