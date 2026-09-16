"""043 W13 — the console routes that answered for the WRONG principal.

Six holes the capability inventory (§2D N1–N12) found, each tested for BOTH the
allowed and the denied principal:

1. ``GET /api/webgate/doctor`` built ``build_status_snapshot`` for
   ``webgate.local_owner_id()`` — the INSTANCE OWNER's health, goals, cron and
   money — and served it to any authenticated tenant.
2. ``POST /api/pfp/{generate,randomize,keep}`` re-rolled and one-way-LOCKED the
   instance avatar with no owner gate at all.
3. ``GET /api/webgate/{pause,halt}`` read the instance pause record with no
   ``Request`` — so a multitenant tenant with no identity still got an answer.
4. ``GET /api/session/{id}/debug`` dumped another tenant's session tree (paths,
   file inventory, feed samples) with no ownership check.
5. ``/admin`` denied a non-admin with an inline-JS ``alert()`` page at 403 while
   every sibling admin route redirects; ``/settings`` and ``/activity`` were
   registered outside the posture model — a multitenant page a tenant may not
   see must be 404 (absent), never 403 (present, denied).
6. Router mounts swallowed every failure, so a console could boot with its whole
   control plane missing and say nothing.
"""
import importlib
import types

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient


# --- 1. doctor ------------------------------------------------------------- #

def test_doctor_uses_the_caller_tenant(monkeypatch, tmp_path):
    import webview.pages as pages
    seen = {}
    monkeypatch.setattr(pages, "_effective_user_id", lambda req: "tenant-2")
    monkeypatch.setattr(pages, "_data_dir", lambda: str(tmp_path))

    def fake_snapshot(user_id, **kw):
        seen["uid"] = user_id
        raise RuntimeError("stop")

    monkeypatch.setattr("core.status_snapshot.build_status_snapshot", fake_snapshot)
    app = FastAPI()
    app.include_router(pages.router)
    TestClient(app).get("/api/webgate/doctor")
    assert seen["uid"] == "tenant-2"


def test_doctor_health_names_the_failure_rather_than_reporting_healthy(monkeypatch, tmp_path):
    import webview.pages as pages
    monkeypatch.setattr(pages, "_effective_user_id", lambda req: "tenant-2")
    monkeypatch.setattr(pages, "_data_dir", lambda: str(tmp_path))
    monkeypatch.setattr("core.status_snapshot.build_status_snapshot",
                        lambda user_id, **kw: (_ for _ in ()).throw(RuntimeError("boom")))
    app = FastAPI()
    app.include_router(pages.router)
    body = TestClient(app).get("/api/webgate/doctor").json()
    assert body["health"]["overall"] == "unavailable"


def test_doctor_denies_a_multitenant_caller_with_no_identity(monkeypatch, tmp_path):
    """``_effective_user_id`` is fail-closed — the doctor must not fall back to
    the instance owner's snapshot."""
    import webview.pages as pages
    monkeypatch.setenv("POLYROB_POSTURE", "multitenant")
    import webview.webgate as wg
    importlib.reload(wg)
    monkeypatch.setattr(pages, "_data_dir", lambda: str(tmp_path))
    app = FastAPI()
    app.include_router(pages.router)
    assert TestClient(app).get("/api/webgate/doctor").status_code == 403
    monkeypatch.delenv("POLYROB_POSTURE", raising=False)
    importlib.reload(wg)


# --- 2. pfp setup ---------------------------------------------------------- #

@pytest.fixture()
def _pfp_app(monkeypatch, tmp_path):
    import webview.pages as pages
    monkeypatch.setattr(pages, "_pfp_data_dir", lambda: str(tmp_path))
    app = FastAPI()
    app.include_router(pages.router)
    return TestClient(app)


def test_pfp_setup_refuses_a_multitenant_tenant(monkeypatch, _pfp_app):
    """The avatar is the INSTANCE's identity and `keep` is one-way — a tenant
    must never be able to re-roll or lock it."""
    monkeypatch.setenv("POLYROB_POSTURE", "multitenant")
    import webview.webgate as wg
    importlib.reload(wg)
    try:
        for path in ("/api/pfp/generate", "/api/pfp/randomize", "/api/pfp/keep"):
            assert _pfp_app.post(path).status_code == 403, path
    finally:
        monkeypatch.delenv("POLYROB_POSTURE", raising=False)
        importlib.reload(wg)


def test_pfp_setup_runs_on_the_owner_console(monkeypatch, _pfp_app):
    monkeypatch.setenv("POLYROB_POSTURE", "local")
    import webview.webgate as wg
    importlib.reload(wg)
    try:
        resp = _pfp_app.post("/api/pfp/generate")
        assert resp.status_code == 200
        assert resp.json()["ok"] in (True, False)  # ran; the store decides
    finally:
        monkeypatch.delenv("POLYROB_POSTURE", raising=False)
        importlib.reload(wg)


# --- 3. pause / halt reads -------------------------------------------------- #

def test_pause_read_resolves_the_caller(monkeypatch, tmp_path):
    import webview.pages as pages
    seen = []
    monkeypatch.setattr(pages, "_effective_user_id", lambda req: seen.append("called") or "u1")
    monkeypatch.setattr(pages, "_data_dir", lambda: str(tmp_path))
    app = FastAPI()
    app.include_router(pages.router)
    client = TestClient(app)
    assert client.get("/api/webgate/pause").status_code == 200
    assert client.get("/api/webgate/halt").status_code == 200
    assert len(seen) == 2


def test_pause_read_denies_a_multitenant_caller_with_no_identity(monkeypatch, tmp_path):
    import webview.pages as pages
    monkeypatch.setenv("POLYROB_POSTURE", "multitenant")
    import webview.webgate as wg
    importlib.reload(wg)
    monkeypatch.setattr(pages, "_data_dir", lambda: str(tmp_path))
    app = FastAPI()
    app.include_router(pages.router)
    try:
        assert TestClient(app).get("/api/webgate/pause").status_code == 403
        assert TestClient(app).get("/api/webgate/halt").status_code == 403
    finally:
        monkeypatch.delenv("POLYROB_POSTURE", raising=False)
        importlib.reload(wg)


# --- 4. session debug ------------------------------------------------------- #

def _server(monkeypatch, posture="local"):
    monkeypatch.delenv("WEBGATE_MULTITENANT", raising=False)
    monkeypatch.setenv("POLYROB_POSTURE", posture)
    monkeypatch.setenv("ENV", "development")
    import webview.webgate as wg
    importlib.reload(wg)
    import webview.server as srv
    importlib.reload(srv)
    return srv


@pytest.fixture(autouse=True)
def _restore_server(monkeypatch):
    yield
    for key in ("POLYROB_POSTURE", "WEBGATE_MULTITENANT", "ENV"):
        monkeypatch.delenv(key, raising=False)
    import webview.webgate as wg
    importlib.reload(wg)
    import webview.server as srv
    importlib.reload(srv)


def test_session_debug_denies_a_non_owner(monkeypatch):
    srv = _server(monkeypatch, "multitenant")
    client = TestClient(srv._fastapi)
    resp = client.get("/api/session/someone-elses-session/debug")
    assert resp.status_code in (401, 403), resp.status_code


def test_session_debug_answers_the_owner(monkeypatch):
    """The loopback operator OWNS every session, so the gate passes — whatever
    the session tree itself then says (a missing session is a 500 from pm(),
    which is pre-existing and not what this test is about)."""
    srv = _server(monkeypatch, "local")
    resp = TestClient(srv._fastapi).get("/api/session/s-1/debug")
    assert resp.status_code not in (401, 403), resp.text[:200]


# --- 5. posture-shaped denials ---------------------------------------------- #

def _posture_app(posture: str):
    """The admin/account pages alone, mounted for one posture — the auth
    middleware is not in the way, so the handler's own denial is observable."""
    from webview import posture_routes
    app = FastAPI()

    class _Templates:
        def TemplateResponse(self, request, template, ctx):
            from fastapi.responses import JSONResponse as _J
            return _J({"template": template, "is_admin": ctx["is_admin"]})

    posture_routes.mount(app, _Templates(), posture=posture)
    return app


def test_admin_redirects_a_non_admin_like_its_siblings():
    client = TestClient(_posture_app("multitenant"))
    for path in ("/admin", "/admin/users", "/admin/activity"):
        resp = client.get(path, follow_redirects=False)
        assert resp.status_code == 303, (path, resp.status_code)
        assert resp.headers["location"] == "/"
        assert "alert(" not in resp.text


def test_the_inline_admin_alert_page_is_gone():
    src = (__import__("pathlib").Path(__file__).resolve().parents[3]
           / "webview" / "server.py").read_text(encoding="utf-8")
    assert "alert('Admin access required')" not in src


def test_admin_pages_are_absent_outside_multitenant():
    for posture in ("local", "own_ops"):
        client = TestClient(_posture_app(posture))
        assert client.get("/admin").status_code == 404, posture
        assert client.get("/profile").status_code == 404, posture


# 043 §9 phase 4: the legacy /settings PAGE is deleted (its MCP/skills panels
# moved to the new Agent destination). It was never a posture row and is no longer
# registered under the legacy switch either, so the two /settings pins that lived
# here are gone with the page. /settings not being in posture_routes.PAGES is
# still guaranteed structurally (it was never added).


def test_activity_is_absent_not_denied_for_a_multitenant_tenant(monkeypatch):
    """A page a tenant may not see must not confirm its existence."""
    monkeypatch.setenv("POLYROB_POSTURE", "multitenant")
    import webview.activity as activity
    importlib.reload(activity)

    def _req(user_id, tier="standard", is_admin=False):
        return types.SimpleNamespace(
            state=types.SimpleNamespace(user_id=user_id, tier=tier, is_admin=is_admin))

    with pytest.raises(HTTPException) as exc:
        activity._require_activity_access(_req("tenant-b"))
    assert exc.value.status_code == 404

    activity._require_activity_access(_req("whoever", tier="admin"))   # allowed
    activity._require_activity_access(_req("anyone", is_admin=True))   # allowed


# --- 6. router mounts fail loud --------------------------------------------- #

def test_a_failed_router_mount_is_recorded(monkeypatch):
    srv = _server(monkeypatch, "own_ops")
    assert hasattr(srv, "UNMOUNTED_ROUTERS")
    assert isinstance(srv.UNMOUNTED_ROUTERS, list)


def test_the_doctor_payload_reports_unmounted_routers(monkeypatch, tmp_path):
    import webview.pages as pages
    import webview.server as srv
    monkeypatch.setattr(pages, "_effective_user_id", lambda req: "u1")
    monkeypatch.setattr(pages, "_data_dir", lambda: str(tmp_path))
    monkeypatch.setattr(srv, "UNMOUNTED_ROUTERS", ["knowledge: ImportError: boom"],
                        raising=False)
    app = FastAPI()
    app.include_router(pages.router)
    body = TestClient(app).get("/api/webgate/doctor").json()
    assert body["unmounted_routers"] == ["knowledge: ImportError: boom"]
