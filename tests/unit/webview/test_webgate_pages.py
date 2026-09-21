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
    # 043 phase 5: pages.router carries the /api/webgate/* endpoints (and the one
    # surviving page, /pending). The WEBVIEW_UI legacy switch was removed.
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
# Goals endpoint — reuses GoalBoard.list_recent()/status_counts()/asks()
# (A16/B1: never GoalBoard.list() — the dispatcher's priority-ordered claim
# queue, forbidden as a "what is on my board" view per AGENTS.md).
# --------------------------------------------------------------------------- #

def test_goals_endpoint_calls_goalboard_list_recent(monkeypatch):
    client, pages = _router_client()
    monkeypatch.setattr(pages.AutonomyConfig, "goals_enabled", staticmethod(lambda: True))

    from agents.task.goals.board import Goal

    listed = {}

    class FakeBoard:
        def __init__(self, db_path, **kw):
            listed["db_path"] = db_path

        def list(self, *a, **k):
            raise AssertionError("GoalBoard.list() must never back a view (B1/B2)")

        def list_recent(self, *, user_id=None, statuses=None, limit=30):
            listed["user_id"] = user_id
            return [Goal(id="g1", user_id=user_id or "rob", title="ship it", status="ready")]

        def status_counts(self, *, user_id=None):
            listed["counts_user_id"] = user_id
            return {"ready": 1}

        def asks(self, *, user_id=None, status=None):
            listed["asks_status"] = status
            return []

    monkeypatch.setattr(pages, "GoalBoard", FakeBoard)
    r = client.get("/api/webgate/goals")
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is True
    assert body["goals"][0]["id"] == "g1"
    assert body["goals"][0]["title"] == "ship it"
    assert body["counts"] == {"ready": 1}
    assert body["asks"] == []
    assert listed["user_id"]  # tenant-scoped
    assert listed["db_path"].endswith("goals.db")
    assert listed["asks_status"] == "open"


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
# 030 D4 — silent-empty endpoints report an explicit ``error`` field
# --------------------------------------------------------------------------- #

def test_memory_endpoint_reports_search_error(monkeypatch):
    """A raising provider.search yields items:[] PLUS ``error`` (still 200) —
    a broken recall read must not masquerade as "no memories"."""
    client, pages = _router_client()

    class BoomProvider:
        async def search(self, query, *, user_id=None, session_id=None, limit=5, sort=None):
            raise RuntimeError("fts index corrupt")

    monkeypatch.setattr(pages, "_memory_provider", lambda: BoomProvider())
    r = client.get("/api/webgate/memory")
    assert r.status_code == 200
    body = r.json()
    assert body["items"] == [] and body["count"] == 0
    assert "fts index corrupt" in body["error"]


def test_memory_endpoint_no_error_field_on_success(monkeypatch):
    """The happy path stays shape-identical — no ``error`` key sneaks in."""
    client, pages = _router_client()

    class OkProvider:
        async def search(self, query, *, user_id=None, session_id=None, limit=5, sort=None):
            return "- alpha"

    monkeypatch.setattr(pages, "_memory_provider", lambda: OkProvider())
    body = client.get("/api/webgate/memory").json()
    assert "error" not in body


def test_cron_endpoint_reports_read_error(monkeypatch):
    """A raising cron store yields enabled:True, jobs:[] PLUS ``error``."""
    client, pages = _router_client()
    monkeypatch.setattr(pages, "_cron_enabled", lambda: True)
    monkeypatch.setattr(pages, "CronJobStore", _boom("cron.db unreadable"))
    r = client.get("/api/webgate/cron")
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is True and body["jobs"] == []
    assert "cron.db unreadable" in body["error"]


def test_goals_endpoint_reports_read_error(monkeypatch):
    """A raising goal board yields enabled:True, goals/counts/asks empty PLUS
    ``error`` — mirrors ``test_cron_endpoint_reports_read_error`` (B1/B2)."""
    client, pages = _router_client()
    monkeypatch.setattr(pages.AutonomyConfig, "goals_enabled", staticmethod(lambda: True))
    monkeypatch.setattr(pages, "GoalBoard", _boom("goals.db unreadable"))
    r = client.get("/api/webgate/goals")
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is True
    assert body["goals"] == [] and body["counts"] == {} and body["asks"] == []
    assert "goals.db unreadable" in body["error"]


# 043 §9 phase 4: the /memory PAGE (and its backend-error banner) is deleted —
# recall now lives on the new Agent destination's Memory tab over
# /api/webgate/memory + /api/webgate/memory/search (unchanged, tested below).


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


# 043 §9 phase 4: the /identity PAGE (and its avatar block / ui.show_avatar
# gating) is deleted — the persona/avatar now render on the new Agent
# destination's Identity tab over /api/webgate/identity + /pfp.json (the endpoint
# tests above stay). The read-only-endpoint invariant below is unchanged.


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
    body = r.json()
    # A failed build is NAMED (checks=None + checks_error), never a silent [].
    assert body["checks"] is not None, f"doctor_report failed in the endpoint: {body.get('checks_error')}"
    direct = doctor_report(dict(os.environ), local_absent_means_on=False)
    if body["checks"] != direct:  # pytest truncates the repr; keep the whole diff
        import json as _json
        with open("/tmp/iface-audit/doctor_diff.json", "w") as fh:
            _json.dump({"endpoint": body["checks"], "direct": direct}, fh, indent=1)
    assert body["checks"] == direct


# --------------------------------------------------------------------------- #
# Pages render 200 in single-user mode (via the real server, P1 reload pattern)
# --------------------------------------------------------------------------- #

# 043 §9 phase 4 deleted /memory, /identity and /system; A21 (2026-09-21)
# deleted /pending, the last legacy webgate PAGE. The five-destination shell is
# the console now, so that is what must render on the real server.
@pytest.mark.parametrize("path", ["/", "/inbox", "/work", "/money", "/agent"])
def test_pages_render_200_single_user(monkeypatch, path):
    server = _reload_server(monkeypatch, multitenant=False)
    client = TestClient(server._fastapi)
    assert client.get(path).status_code == 200


def test_api_endpoints_mounted_on_server(monkeypatch):
    """Every webgate endpoint is REACHABLE on the server app.

    This used to introspect ``server._fastapi.routes`` and look for ``.path``.
    Starlette 1.x restructured the router, so an ``include_router``-mounted path
    is no longer visible on the top-level route list — the introspection went
    silently blind while every endpoint still served 200. Asking the app for the
    route instead tests the property we actually care about (mounted AND
    routable) and cannot rot against a router-internals change.
    """
    server = _reload_server(monkeypatch, multitenant=False)
    client = TestClient(server._fastapi)
    for p in ("/api/webgate/memory", "/api/webgate/goals", "/api/webgate/cron",
              "/api/webgate/identity", "/api/webgate/doctor"):
        assert client.get(p).status_code != 404, f"{p} not mounted on _fastapi"


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
