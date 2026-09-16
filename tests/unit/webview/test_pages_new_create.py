"""043 A5 — creating a goal or a cron job from the console (``pages_new``).

The two new writers (``POST /api/webgate/goals``, ``POST /api/webgate/cron``)
route through the ONE owner-create SSOT (``core.owner_create``) with the OWNER's
grant: the tools named are written VERBATIM (never the agent's self-grant
allowlist), the write is tenant-scoped, and each carries the ``console_write``
audit row every mutating console route carries. The cron writer also passes
``via="webview"`` so Q2's service-level A29 audit names the surface.

⚠️ Reach, never policy: these tests name ``defi_trade`` to prove the grant is
unfiltered — a string in a DB row. Nothing here runs a money verb.
"""
import pytest


def _client(monkeypatch, tmp_path, user_id="u1"):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import webview.pages as pages
    import webview.pages_new as pages_new
    monkeypatch.setattr(pages, "_effective_user_id", lambda req: user_id)
    monkeypatch.setattr(pages, "_data_dir", lambda: str(tmp_path))
    app = FastAPI()
    app.include_router(pages_new.router)
    return TestClient(app)


def _telemetry(monkeypatch, tmp_path):
    monkeypatch.setenv("TELEMETRY_EVENT_LOG_PATH",
                       str(tmp_path / "telemetry_events.db"))
    monkeypatch.delenv("TELEMETRY_EVENT_LOG_ENABLED", raising=False)


# --- goal create ------------------------------------------------------------ #

def test_create_goal_writes_owner_tenant_and_unfiltered_tools(monkeypatch, tmp_path):
    from agents.task.goals.board import GoalBoard

    client = _client(monkeypatch, tmp_path, user_id="u1")
    r = client.post("/api/webgate/goals",
                    json={"title": "ship the widget",
                          "tools": ["defi_trade", "filesystem"]})
    assert r.status_code == 201, r.text
    gid = r.json()["id"]

    board = GoalBoard(str(tmp_path / "goals.db"))
    goal = board.get(gid, user_id="u1")
    assert goal is not None
    assert goal.user_id == "u1"
    # The money tool the agent's own goal_create would strip survives: owner grant.
    assert goal.payload.get("tools") == ["defi_trade", "filesystem"]


def test_create_goal_duplicate_returns_409(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path, user_id="u1")
    first = client.post("/api/webgate/goals", json={"title": "summarise the week"})
    assert first.status_code == 201, first.text
    dup = client.post("/api/webgate/goals", json={"title": "summarise the week"})
    assert dup.status_code == 409, dup.text
    assert dup.json()["ok"] is False


def test_create_goal_missing_title_returns_400(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path, user_id="u1")
    r = client.post("/api/webgate/goals", json={"title": "   "})
    assert r.status_code == 400
    assert r.json()["ok"] is False


def test_create_goal_bad_tools_shape_returns_400(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path, user_id="u1")
    r = client.post("/api/webgate/goals",
                    json={"title": "t", "tools": "defi_trade"})
    assert r.status_code == 400


def test_create_goal_writes_a_console_audit_row(monkeypatch, tmp_path):
    _telemetry(monkeypatch, tmp_path)
    from core.event_kinds import CONSOLE_GOAL_CREATE
    from core.event_log import get_event_log

    client = _client(monkeypatch, tmp_path, user_id="u1")
    r = client.post("/api/webgate/goals", json={"title": "audit me please"})
    assert r.status_code == 201, r.text
    rows = get_event_log().query(kind=CONSOLE_GOAL_CREATE)
    assert len(rows) == 1
    assert rows[0]["user_id"] == "u1"
    assert rows[0]["source"] == "webview"
    assert rows[0]["attrs"]["via"] == "webview"
    assert rows[0]["attrs"]["goal_id"] == r.json()["id"]


# --- cron create ------------------------------------------------------------ #

def test_create_cron_schedules_and_names_the_surface(monkeypatch, tmp_path):
    _telemetry(monkeypatch, tmp_path)
    from core.event_kinds import CONSOLE_CRON_CREATE, CRON_SCHEDULED
    from core.event_log import get_event_log
    from cron.jobs import CronJobStore
    from cron.service import CronService

    client = _client(monkeypatch, tmp_path, user_id="u1")
    r = client.post("/api/webgate/cron",
                    json={"task": "summarise the week", "schedule": "30m"})
    assert r.status_code == 201, r.text
    jid = r.json()["id"]

    svc = CronService(CronJobStore(str(tmp_path / "cron.db")))
    assert jid in {j.id for j in svc.list_jobs(user_id="u1")}

    # The console-action audit row.
    console_rows = get_event_log().query(kind=CONSOLE_CRON_CREATE)
    assert len(console_rows) == 1
    assert console_rows[0]["attrs"]["via"] == "webview"
    assert console_rows[0]["attrs"]["job_id"] == jid

    # Q2's service-level A29 audit, named to the webview surface.
    svc_rows = get_event_log().query(kind=CRON_SCHEDULED)
    assert len(svc_rows) == 1
    assert svc_rows[0]["attrs"]["via"] == "webview"


def test_create_cron_bad_schedule_returns_400(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path, user_id="u1")
    r = client.post("/api/webgate/cron",
                    json={"task": "do a real thing", "schedule": "not-a-schedule"})
    assert r.status_code == 400
    assert r.json()["ok"] is False


def test_create_cron_missing_task_returns_400(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path, user_id="u1")
    r = client.post("/api/webgate/cron", json={"schedule": "30m"})
    assert r.status_code == 400


def test_create_cron_missing_schedule_returns_400(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path, user_id="u1")
    r = client.post("/api/webgate/cron", json={"task": "do a real thing"})
    assert r.status_code == 400


# --- the routes actually mount past the method-aware clash check ------------- #

def test_create_routes_mount_beside_the_legacy_get_at_the_same_path(monkeypatch):
    """``pages.py`` serves ``GET /api/webgate/goals`` and ``GET /api/webgate/cron``.
    The path-only clash check would have dropped the new POSTs; the method-aware
    one keeps them. This asserts they survive a real ``mount`` beside the legacy
    reader routes."""
    from fastapi import FastAPI

    import webview.pages as pages
    import webview.pages_new as pages_new
    monkeypatch.delenv("WEBVIEW_UI", raising=False)
    app = FastAPI()
    app.include_router(pages.router)  # brings the GET /api/webgate/{goals,cron}
    pages_new.mount(app)

    http, _ = pages_new.served_method_paths(app)
    assert ("POST", "/api/webgate/goals") in http
    assert ("POST", "/api/webgate/cron") in http
    # the legacy GET readers are untouched
    assert ("GET", "/api/webgate/goals") in http
    assert ("GET", "/api/webgate/cron") in http
