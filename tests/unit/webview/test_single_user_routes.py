"""Route mount-gate tests: single-user (flag OFF) vs multitenant (flag ON).

webview/server.py builds the FastAPI app + registers routes/routers at IMPORT
time, and reads WEBGATE_MULTITENANT at that point. So to exercise both modes
faithfully we set the env var and ``importlib.reload`` the module, then drive the
inner FastAPI sub-app (`server._fastapi`) with a TestClient. The public ASGI app
(`server.app`) is the Socket.IO wrapper around `_fastapi`.
"""
import importlib

import pytest
from fastapi.testclient import TestClient


def _reload_server(monkeypatch, multitenant: bool):
    monkeypatch.setenv("WEBGATE_MULTITENANT", "true" if multitenant else "false")
    # Keep auth middleware out of the way for plain reachability checks.
    monkeypatch.setenv("ENV", "development")
    import webview.server as server
    return importlib.reload(server)


def _route_paths(server) -> set:
    return {getattr(r, "path", None) for r in server._fastapi.routes}


# --------------------------------------------------------------------------- #
# Single-user (flag OFF): admin/signin/profile NOT registered; auth_router off.
# --------------------------------------------------------------------------- #

@pytest.fixture
def single_user(monkeypatch):
    return _reload_server(monkeypatch, multitenant=False)


def test_admin_404_in_single_user(single_user):
    client = TestClient(single_user._fastapi)
    assert client.get("/admin").status_code == 404


def test_signin_404_in_single_user(single_user):
    client = TestClient(single_user._fastapi)
    assert client.get("/signin").status_code == 404


def test_profile_logout_admin_routes_not_registered(single_user):
    paths = _route_paths(single_user)
    for gated in ("/signin", "/logout", "/profile", "/admin",
                  "/admin/users", "/admin/activity"):
        assert gated not in paths, f"{gated} should NOT be registered in single-user"


def test_auth_router_not_mounted_in_single_user(single_user):
    assert single_user.AUTH_ROUTER_MOUNTED is False
    paths = _route_paths(single_user)
    assert not any(p and p.startswith("/api/auth") for p in paths)


def test_task_router_and_settings_stay_mounted_single_user(single_user):
    # Core surfaces survive in both modes.
    assert single_user.TASK_ROUTER_MOUNTED is True
    assert "/settings" in _route_paths(single_user)


def test_index_reachable_without_auth_single_user(single_user):
    client = TestClient(single_user._fastapi)
    assert client.get("/").status_code == 200


def test_session_page_reachable_without_auth_single_user(single_user):
    client = TestClient(single_user._fastapi)
    assert client.get("/session/abc123").status_code == 200


# --------------------------------------------------------------------------- #
# Multitenant (flag ON): today's behavior — signin reachable, auth_router on.
# --------------------------------------------------------------------------- #

@pytest.fixture
def multitenant(monkeypatch):
    return _reload_server(monkeypatch, multitenant=True)


def test_signin_registered_in_multitenant(multitenant):
    assert "/signin" in _route_paths(multitenant)
    client = TestClient(multitenant._fastapi)
    assert client.get("/signin").status_code == 200


def test_auth_router_mounted_in_multitenant(multitenant):
    assert multitenant.AUTH_ROUTER_MOUNTED is True


def test_admin_route_registered_in_multitenant(multitenant):
    # Registered (handler enforces admin); the route exists (not a 404 page miss).
    assert "/admin" in _route_paths(multitenant)


@pytest.fixture(autouse=True)
def _restore_server(monkeypatch):
    """Leave the module in the default (single-user) state after the test file."""
    yield
    monkeypatch.delenv("WEBGATE_MULTITENANT", raising=False)
    import webview.server as server
    importlib.reload(server)
