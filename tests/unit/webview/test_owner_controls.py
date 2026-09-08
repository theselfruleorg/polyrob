"""030 WS-C C3 — the webview owner control plane.

Kill switch (halt/resume), goal write verbs, cron cancel, correspondent
reject, and the Config page. Every endpoint is thin plumbing over the SAME
primitives the CLI (`polyrob owner …`), the REPL (`/halt` …) and Telegram
call — these tests exercise the REAL stores (tmp data home), mirroring the
harness of test_pending_review.py / test_config_endpoints_p3.py.
"""
from core.autonomy_control import PAUSE_FILENAME as _PAUSE_FILENAME  # 031: the one record
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


# --------------------------------------------------------------------------- #
# Kill switch — halt/resume roundtrip against a tmp data home
# --------------------------------------------------------------------------- #

@pytest.fixture()
def _halt_env(tmp_path, monkeypatch):
    # POLYROB_DATA_DIR pins BOTH the write bases and the runtime predicate
    # (AutonomyConfig.autonomy_halted) to tmp — nothing touches the real home.
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("AUTONOMY_HALT", raising=False)
    monkeypatch.delenv("DATA_ROOT", raising=False)
    return tmp_path


def test_halt_resume_roundtrip(monkeypatch, tmp_path, _halt_env):
    client, _ = _client(monkeypatch, tmp_path)

    state = client.get("/api/webgate/halt").json()
    assert state["halted"] is False

    r = client.post("/api/webgate/halt")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["effective"] is True
    assert (tmp_path / _PAUSE_FILENAME).exists()
    assert "HALTED" in body["message"] or "Paused everything" in body["message"]
    assert client.get("/api/webgate/halt").json()["halted"] is True

    r2 = client.post("/api/webgate/resume")
    assert r2.status_code == 200
    body2 = r2.json()
    assert body2["ok"] is True
    assert "RESUMED" in body2["message"]
    assert not (tmp_path / _PAUSE_FILENAME).exists()
    assert client.get("/api/webgate/halt").json()["halted"] is False


def test_resume_without_halt_is_honest(monkeypatch, tmp_path, _halt_env):
    client, _ = _client(monkeypatch, tmp_path)
    body = client.post("/api/webgate/resume").json()
    assert body["ok"] is True
    assert "was not paused" in body["message"]


def test_resume_reports_env_halt_honestly(monkeypatch, tmp_path, _halt_env):
    """AUTONOMY_HALT set in the env also halts — resume must never claim
    RESUMED while the runtime still reports autonomy as halted (the same
    honest verified-state semantics as `polyrob owner resume`)."""
    client, _ = _client(monkeypatch, tmp_path)
    client.post("/api/webgate/halt")
    monkeypatch.setenv("AUTONOMY_HALT", "1")
    body = client.post("/api/webgate/resume").json()
    assert body["ok"] is False
    assert "RESUMED" not in body["message"]
    assert "AUTONOMY_HALT" in body["message"]
    assert body["still_halted"] is True and body["env_halt"] is True


def test_halt_refused_on_multitenant_posture(monkeypatch, tmp_path, _halt_env):
    """Instance-wide controls need the OWNER console — an authenticated
    multitenant tenant must never halt the whole instance (same posture rule
    as the env-flag config writes)."""
    from webview import webgate
    client, _ = _client(monkeypatch, tmp_path)
    monkeypatch.setattr(webgate, "posture", lambda: "multitenant")
    assert client.post("/api/webgate/halt").status_code == 403
    assert client.post("/api/webgate/resume").status_code == 403


# --------------------------------------------------------------------------- #
# Goal write verbs — tenant-scoped transitions over the REAL GoalBoard
# --------------------------------------------------------------------------- #

def _make_goal(tmp_path, user_id="u1", title="ship the widget"):
    from agents.task.goals.board import GoalBoard
    board = GoalBoard(str(tmp_path / "goals.db"))
    goal = board.create(user_id=user_id, title=title)
    return board, goal


def test_goal_verb_tenant_scoping(monkeypatch, tmp_path):
    """board.get() is NOT tenant-scoped — the endpoint must resolve the id
    within the CALLER's own goals: another tenant's goal id 404s and the row
    is untouched."""
    board, goal = _make_goal(tmp_path, user_id="u1")
    client, _ = _client(monkeypatch, tmp_path, user_id="u2")
    r = client.post(f"/api/webgate/goals/{goal.id}/pause")
    assert r.status_code == 404
    assert board.get(goal.id).status == "ready"


def test_goal_pause_resume_retry_cancel(monkeypatch, tmp_path):
    board, goal = _make_goal(tmp_path, user_id="u1")
    client, _ = _client(monkeypatch, tmp_path, user_id="u1")

    r = client.post(f"/api/webgate/goals/{goal.id}/pause")
    assert r.status_code == 200 and r.json()["ok"] is True
    assert board.get(goal.id).status == "blocked"

    r = client.post(f"/api/webgate/goals/{goal.id}/resume")
    assert r.status_code == 200 and r.json()["status"] == "ready"
    assert board.get(goal.id).status == "ready"

    # retry is only valid from blocked — the transition table refuses honestly
    r = client.post(f"/api/webgate/goals/{goal.id}/retry")
    assert r.status_code == 409
    assert board.get(goal.id).status == "ready"

    r = client.post(f"/api/webgate/goals/{goal.id}/cancel")
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    assert board.get(goal.id).status == "cancelled"


def test_goal_unknown_verb_is_400(monkeypatch, tmp_path):
    _, goal = _make_goal(tmp_path, user_id="u1")
    client, _ = _client(monkeypatch, tmp_path, user_id="u1")
    assert client.post(f"/api/webgate/goals/{goal.id}/explode").status_code == 400


# --------------------------------------------------------------------------- #
# Cron cancel — tenant-scoped over the REAL CronService store
# --------------------------------------------------------------------------- #

def _make_cron_job(tmp_path, user_id="u1"):
    from cron.jobs import CronJobStore
    from cron.service import CronService
    svc = CronService(CronJobStore(str(tmp_path / "cron.db")))
    return svc, svc.schedule(task="summarise the week", schedule_spec="30m",
                             user_id=user_id)


def test_cron_cancel_tenant_scoping(monkeypatch, tmp_path):
    svc, job = _make_cron_job(tmp_path, user_id="u1")
    client, _ = _client(monkeypatch, tmp_path, user_id="u2")
    assert client.post(f"/api/webgate/cron/{job.id}/cancel").status_code == 404
    assert {j.id for j in svc.list_jobs(user_id="u1")} == {job.id}


def test_cron_cancel_happy_path(monkeypatch, tmp_path):
    svc, job = _make_cron_job(tmp_path, user_id="u1")
    client, _ = _client(monkeypatch, tmp_path, user_id="u1")
    r = client.post(f"/api/webgate/cron/{job.id}/cancel")
    assert r.status_code == 200 and r.json()["ok"] is True
    statuses = {j.id: j.status for j in svc.list_jobs(user_id="u1")}
    assert statuses.get(job.id) in (None, "cancelled")


def test_cron_cancel_unknown_id_404(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path, user_id="u1")
    assert client.post("/api/webgate/cron/nope/cancel").status_code == 404


# --------------------------------------------------------------------------- #
# Correspondent reject — the registry decision verb, wired (was refused)
# --------------------------------------------------------------------------- #

def _seed_pending_correspondent(monkeypatch, tmp_path, user_id="u1"):
    import webview.pages as pages
    monkeypatch.setattr(pages, "_data_dir", lambda: str(tmp_path))
    registry = pages._webgate_correspondent_registry()
    state = registry.seed(surface="email", address="a@x.com", session_id="s1",
                          user_id=user_id, provenance="owner",
                          require_approval=True)
    assert state == "pending"
    return registry


def test_correspondent_reject_flips_the_row(monkeypatch, tmp_path):
    registry = _seed_pending_correspondent(monkeypatch, tmp_path)
    client, _ = _client(monkeypatch, tmp_path, user_id="u1")

    # listed as pending before the decision
    items = client.get("/api/webgate/pending").json()["items"]
    assert any(it["kind"] == "correspondent" and it["id"] == "email:a@x.com"
               for it in items)

    r = client.post("/api/webgate/pending/correspondent/email:a@x.com/reject")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert "rejected" in body["message"]

    rows = registry.list(user_id="u1")
    assert rows and rows[0]["state"] == "expired"
    # gone from the pending listing, and no longer routable
    items = client.get("/api/webgate/pending").json()["items"]
    assert not any(it["kind"] == "correspondent" for it in items)
    assert registry.resolve(surface="email", address="a@x.com") is None


def test_correspondent_reject_is_tenant_scoped(monkeypatch, tmp_path):
    registry = _seed_pending_correspondent(monkeypatch, tmp_path, user_id="u1")
    client, _ = _client(monkeypatch, tmp_path, user_id="u2")
    r = client.post("/api/webgate/pending/correspondent/email:a@x.com/reject")
    assert r.status_code == 200 and r.json()["ok"] is False
    assert registry.list(user_id="u1")[0]["state"] == "pending"


def test_correspondent_reject_unknown_is_ok_false(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path, user_id="u1")
    r = client.post("/api/webgate/pending/correspondent/email:nope@x.com/reject")
    assert r.status_code == 200 and r.json()["ok"] is False


def test_correspondent_approve_still_promotes(monkeypatch, tmp_path):
    registry = _seed_pending_correspondent(monkeypatch, tmp_path)
    client, _ = _client(monkeypatch, tmp_path, user_id="u1")
    r = client.post("/api/webgate/pending/correspondent/email:a@x.com/promote")
    assert r.status_code == 200 and r.json()["ok"] is True
    assert registry.list(user_id="u1")[0]["state"] == "active"


# --------------------------------------------------------------------------- #
# Read-only mode refuses EVERY new mutation (server-side, not just hidden UI)
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("method,path", [
    ("POST", "/api/webgate/halt"),
    ("POST", "/api/webgate/resume"),
    ("POST", "/api/webgate/goals/some-goal/pause"),
    ("POST", "/api/webgate/goals/some-goal/cancel"),
    ("POST", "/api/webgate/cron/some-job/cancel"),
    ("POST", "/api/webgate/invoices/some-invoice/settle"),
])
def test_read_only_refuses_every_new_mutation(monkeypatch, tmp_path, method, path):
    client, _ = _client(monkeypatch, tmp_path)
    monkeypatch.setenv("WEBVIEW_READ_ONLY", "true")
    r = client.request(method, path)
    assert r.status_code == 403, f"{method} {path} -> {r.status_code}"
    assert "read-only" in str(r.json()).lower()


def test_read_only_still_allows_reads(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path)
    monkeypatch.setenv("WEBVIEW_READ_ONLY", "true")
    assert client.get("/api/webgate/halt").status_code == 200
    assert client.get("/api/webgate/goals").status_code == 200
    assert client.get("/api/webgate/cron").status_code == 200


# --------------------------------------------------------------------------- #
# Page routes render (bare pages-router app, like the other page tests)
# --------------------------------------------------------------------------- #

def test_config_page_renders(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path)
    r = client.get("/config")
    assert r.status_code == 200
    assert 'id="config-root"' in r.text
    # page JS is an EXTERNAL file (the CSP is being tightened) — never inline
    assert "/static/js/pages/config.js" in r.text


def test_autonomy_page_carries_killswitch_and_external_js(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path)
    r = client.get("/autonomy")
    assert r.status_code == 200
    assert 'id="halt-panel"' in r.text
    assert "/static/js/pages/autonomy.js" in r.text


def test_finance_page_carries_invoices_section(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path)
    r = client.get("/finance")
    assert r.status_code == 200
    assert 'id="invoices-section"' in r.text
    assert "/static/js/pages/invoices.js" in r.text


def test_layout_nav_links_config_page(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path)
    assert 'href="/config"' in client.get("/config").text


def test_config_page_via_real_server(monkeypatch):
    """The full server mounts the pages router — /config must render there too."""
    monkeypatch.setenv("WEBGATE_MULTITENANT", "false")
    monkeypatch.setenv("ENV", "development")
    import webview.server as server
    server = importlib.reload(server)
    client = TestClient(server._fastapi)
    r = client.get("/config")
    assert r.status_code == 200
    assert 'id="config-root"' in r.text


@pytest.fixture(autouse=True)
def _restore_server():
    yield
    import webview.server as server
    importlib.reload(server)
