"""P2 webgate v1 pages — read-only Memory/Autonomy/Identity/System.

Each JSON endpoint REUSES the underlying service (memory provider / GoalBoard /
CronService / core.instance / doctor_report) — it does NOT reimplement it. These
tests mock the service at the ``webview.pages`` seam and assert the endpoint
delegates to it (the proof of reuse). Page routes render 200 via the real
``webview.server._fastapi`` in single-user mode (the P1 reload pattern).
"""
import importlib

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _router_client():
    """A bare app hosting ONLY the pages router — fast, isolated, monkeypatchable."""
    import webview.pages as pages
    app = FastAPI()
    app.include_router(pages.router)
    return TestClient(app), pages


def _reload_server(monkeypatch, multitenant=False):
    monkeypatch.setenv("WEBGATE_MULTITENANT", "true" if multitenant else "false")
    monkeypatch.setenv("ENV", "development")
    import webview.server as server
    return importlib.reload(server)


# --------------------------------------------------------------------------- #
# Memory endpoint — reuses the MemoryProvider.search()
# --------------------------------------------------------------------------- #

def test_memory_endpoint_calls_provider_search(monkeypatch):
    client, pages = _router_client()
    calls = {}

    class FakeProvider:
        async def search(self, query, *, user_id=None, session_id=None, limit=5, sort=None):
            calls["query"] = query
            calls["user_id"] = user_id
            calls["limit"] = limit
            return "- alpha finding\n- beta finding"

    monkeypatch.setattr(pages, "_memory_provider", lambda: FakeProvider())
    r = client.get("/api/webgate/memory", params={"q": "alpha", "limit": 7})
    assert r.status_code == 200
    body = r.json()
    assert body["items"] == ["alpha finding", "beta finding"]
    assert body["count"] == 2
    assert body["mode"] == "search"
    # Proof of reuse: the real provider.search was called with the tenant owner + args.
    assert calls["query"] == "alpha"
    assert calls["limit"] == 7
    assert calls["user_id"]  # tenant-scoped to the local owner


def test_memory_endpoint_browse_when_empty_query(monkeypatch):
    client, pages = _router_client()
    seen = {}

    class FakeProvider:
        async def search(self, query, *, user_id=None, session_id=None, limit=5, sort=None):
            seen["query"] = query
            return ""

    monkeypatch.setattr(pages, "_memory_provider", lambda: FakeProvider())
    r = client.get("/api/webgate/memory")
    assert r.status_code == 200
    body = r.json()
    assert body["items"] == [] and body["count"] == 0
    assert body["mode"] == "browse"
    assert seen["query"] == ""  # browse-recent shape


def test_memory_endpoint_fail_open_no_provider(monkeypatch):
    client, pages = _router_client()
    monkeypatch.setattr(pages, "_memory_provider", lambda: None)
    r = client.get("/api/webgate/memory")
    assert r.status_code == 200
    assert r.json()["items"] == []


# --------------------------------------------------------------------------- #
# Goals endpoint — reuses GoalBoard.list()
# --------------------------------------------------------------------------- #

def test_goals_endpoint_calls_goalboard_list(monkeypatch):
    client, pages = _router_client()
    monkeypatch.setattr(pages.AutonomyConfig, "goals_enabled", staticmethod(lambda: True))

    from agents.task.goals.board import Goal

    listed = {}

    class FakeBoard:
        def __init__(self, db_path, **kw):
            listed["db_path"] = db_path

        def list(self, *, user_id=None, status=None, limit=100):
            listed["user_id"] = user_id
            return [Goal(id="g1", user_id=user_id or "rob", title="ship it", status="ready")]

    monkeypatch.setattr(pages, "GoalBoard", FakeBoard)
    r = client.get("/api/webgate/goals")
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is True
    assert body["goals"][0]["id"] == "g1"
    assert body["goals"][0]["title"] == "ship it"
    assert listed["user_id"]  # tenant-scoped
    assert listed["db_path"].endswith("goals.db")


def test_goals_endpoint_disabled_flag(monkeypatch):
    client, pages = _router_client()
    monkeypatch.setattr(pages.AutonomyConfig, "goals_enabled", staticmethod(lambda: False))
    # Must NOT touch GoalBoard when disabled.
    monkeypatch.setattr(pages, "GoalBoard", _boom("GoalBoard touched while GOALS disabled"))
    r = client.get("/api/webgate/goals")
    assert r.status_code == 200
    assert r.json() == {"enabled": False, "goals": []}


# --------------------------------------------------------------------------- #
# Cron endpoint — reuses CronService.list_jobs()
# --------------------------------------------------------------------------- #

def test_cron_endpoint_calls_cronservice_list(monkeypatch):
    client, pages = _router_client()
    monkeypatch.setattr(pages, "_cron_enabled", lambda: True)

    from cron.jobs import CronJob

    seen = {}

    class FakeService:
        def list_jobs(self, user_id=None):
            seen["user_id"] = user_id
            return [CronJob(id="c1", task="ping", schedule_spec="30m",
                            user_id=user_id or "rob", next_run_at=None)]

    monkeypatch.setattr(pages, "CronJobStore", lambda path: ("store", path))
    monkeypatch.setattr(pages, "CronService", lambda store: FakeService())
    r = client.get("/api/webgate/cron")
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is True
    assert body["jobs"][0]["id"] == "c1"
    assert body["jobs"][0]["task"] == "ping"
    assert seen["user_id"]


def test_cron_endpoint_disabled_flag(monkeypatch):
    client, pages = _router_client()
    monkeypatch.setattr(pages, "_cron_enabled", lambda: False)
    monkeypatch.setattr(pages, "CronService", _boom("CronService touched while CRON disabled"))
    r = client.get("/api/webgate/cron")
    assert r.status_code == 200
    assert r.json() == {"enabled": False, "jobs": []}


# --------------------------------------------------------------------------- #
# Identity endpoint — reuses core.instance, READ-ONLY (no write path)
# --------------------------------------------------------------------------- #

def test_identity_endpoint_reuses_core_instance(monkeypatch):
    client, pages = _router_client()
    monkeypatch.setattr(pages, "load_self_context", lambda home: "SOUL TEXT")
    monkeypatch.setattr(pages, "load_self_doc", lambda home, uid, iid: "SELF TEXT")
    monkeypatch.setattr(pages, "resolve_instance_id", lambda: "rob")
    r = client.get("/api/webgate/identity")
    assert r.status_code == 200
    body = r.json()
    assert body["soul"] == "SOUL TEXT"
    assert body["self"] == "SELF TEXT"
    assert body["instance_id"] == "rob"
    assert body["owner"]


def test_identity_endpoint_null_when_absent(monkeypatch):
    client, pages = _router_client()
    monkeypatch.setattr(pages, "load_self_context", lambda home: "")
    monkeypatch.setattr(pages, "load_self_doc", lambda home, uid, iid: "")
    r = client.get("/api/webgate/identity")
    assert r.status_code == 200
    body = r.json()
    assert body["soul"] is None
    assert body["self"] is None


def test_identity_page_shows_avatar_by_default(monkeypatch, tmp_path):
    """No preferences.toml at all -> fail-open default True -> avatar block renders."""
    client, pages = _router_client()
    monkeypatch.setattr(pages, "_data_dir", lambda: str(tmp_path))
    monkeypatch.setattr(pages, "_effective_user_id", lambda request: "u1")
    r = client.get("/identity")
    assert r.status_code == 200
    assert 'id="agent-avatar"' in r.text


def test_identity_page_hides_avatar_when_pref_false(monkeypatch, tmp_path):
    """Task 8: ui.show_avatar=false (owner-UX prefs) removes the avatar block
    (and its probe script) from the rendered identity page entirely."""
    from core import prefs
    client, pages = _router_client()
    monkeypatch.setattr(pages, "_data_dir", lambda: str(tmp_path))
    monkeypatch.setattr(pages, "_effective_user_id", lambda request: "u1")
    ok, err = prefs.write_preference(tmp_path, "u1", "ui.show_avatar", False, "rob")
    assert ok, err
    r = client.get("/identity")
    assert r.status_code == 200
    assert 'id="agent-avatar"' not in r.text
    assert "/pfp.json" not in r.text


def test_identity_has_no_write_path(monkeypatch):
    """Read-only in v1: no POST/PUT/DELETE/PATCH on /api/webgate/identity."""
    _, pages = _router_client()
    write_methods = set()
    for route in pages.router.routes:
        path = getattr(route, "path", "")
        if path == "/api/webgate/identity":
            write_methods |= (getattr(route, "methods", set()) & {"POST", "PUT", "DELETE", "PATCH"})
    assert write_methods == set(), f"identity must be read-only, found {write_methods}"


# --------------------------------------------------------------------------- #
# Doctor endpoint — reuses cli.commands.doctor.doctor_report
# --------------------------------------------------------------------------- #

def test_doctor_endpoint_reuses_doctor_report(monkeypatch):
    client, pages = _router_client()
    sentinel = ["POLYROB doctor — resolved env: development", "provider keys:"]
    monkeypatch.setattr(pages, "doctor_report", lambda env, **kw: list(sentinel))
    r = client.get("/api/webgate/doctor")
    assert r.status_code == 200
    body = r.json()
    assert body["checks"] == sentinel
    assert "instance_id" in body
    assert "version" in body
    assert "memory_backend" in body
    assert "provider" in body and "model" in body


def test_doctor_endpoint_matches_real_report():
    """Without mocking, the endpoint returns the SAME content doctor_report produces.

    The endpoint resolves in server-process context (POLYROB_LOCAL absent means
    OFF — no CLI setdefault happens in the webview; P0-4)."""
    import os
    from cli.commands.doctor import doctor_report
    client, _pages = _router_client()
    r = client.get("/api/webgate/doctor")
    assert r.status_code == 200
    assert r.json()["checks"] == doctor_report(dict(os.environ), local_absent_means_on=False)


# --------------------------------------------------------------------------- #
# Pages render 200 in single-user mode (via the real server, P1 reload pattern)
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("path", ["/memory", "/autonomy", "/identity", "/system"])
def test_pages_render_200_single_user(monkeypatch, path):
    server = _reload_server(monkeypatch, multitenant=False)
    client = TestClient(server._fastapi)
    assert client.get(path).status_code == 200


def test_api_endpoints_mounted_on_server(monkeypatch):
    server = _reload_server(monkeypatch, multitenant=False)
    paths = {getattr(r, "path", None) for r in server._fastapi.routes}
    for p in ("/api/webgate/memory", "/api/webgate/goals", "/api/webgate/cron",
              "/api/webgate/identity", "/api/webgate/doctor",
              "/memory", "/autonomy", "/identity", "/system"):
        assert p in paths, f"{p} not mounted on _fastapi"


# --------------------------------------------------------------------------- #
# utilities
# --------------------------------------------------------------------------- #

def _boom(msg):
    def _raise(*a, **k):
        raise AssertionError(msg)
    return _raise


@pytest.fixture(autouse=True)
def _restore_server(monkeypatch):
    yield
    monkeypatch.delenv("WEBGATE_MULTITENANT", raising=False)
    import webview.server as server
    importlib.reload(server)
