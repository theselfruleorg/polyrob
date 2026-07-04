"""B7 — per-tenant scoping of the webgate-v1 pages (assessment gap 5).

Exploit-shaped regression test: under multitenant, the memory/goals/cron/identity
JSON endpoints in ``webview/pages.py`` must serve the AUTHENTICATED caller's
``request.state.user_id`` data, not the instance owner's (``webgate.local_owner_id()``).
Before the fix, every endpoint hardcoded ``webgate.local_owner_id()`` regardless of
posture, so an authenticated tenant B was served the owner's data — a cross-tenant
leak. This file also guards the Posture 0 / own_ops single-owner behavior stays
unchanged (no regression).
"""
import importlib
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def multitenant_pages_client(monkeypatch):
    monkeypatch.setenv("WEBGATE_MULTITENANT", "true")
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret")
    import webview.webgate as wg
    importlib.reload(wg)
    import webview.pages as pages
    importlib.reload(pages)
    import webview.server as srv
    importlib.reload(srv)
    return TestClient(srv._fastapi)


def _auth_cookie(user_id: str) -> dict:
    import jwt as pyjwt
    token = pyjwt.encode(
        {"sub": "0xabc", "user_id": user_id, "tier": "free", "role": "user"},
        "test-secret", algorithm="HS256",
    )
    return {"auth_token": token}


def test_memory_uses_caller_user_id_not_local_owner(multitenant_pages_client, monkeypatch):
    captured = {}

    async def fake_search(q, user_id, limit):
        captured["user_id"] = user_id
        return ""

    fake_provider = MagicMock()
    fake_provider.search = fake_search
    with patch("webview.pages._memory_provider", return_value=fake_provider):
        multitenant_pages_client.get(
            "/api/webgate/memory", cookies=_auth_cookie("tenant-42"),
        )
    assert captured["user_id"] == "tenant-42"


def test_goals_uses_caller_user_id_not_local_owner(multitenant_pages_client, monkeypatch):
    monkeypatch.setattr("webview.pages.AutonomyConfig.goals_enabled", lambda: True)
    with patch("webview.pages.GoalBoard") as MockBoard:
        MockBoard.return_value.list.return_value = []
        multitenant_pages_client.get(
            "/api/webgate/goals", cookies=_auth_cookie("tenant-42"),
        )
    MockBoard.return_value.list.assert_called_once_with(user_id="tenant-42")


def test_cron_uses_caller_user_id_not_local_owner(multitenant_pages_client, monkeypatch):
    monkeypatch.setattr("webview.pages._cron_enabled", lambda: True)
    with patch("webview.pages.CronService") as MockService:
        MockService.return_value.list_jobs.return_value = []
        multitenant_pages_client.get(
            "/api/webgate/cron", cookies=_auth_cookie("tenant-42"),
        )
    MockService.return_value.list_jobs.assert_called_once_with(user_id="tenant-42")


def test_identity_uses_caller_user_id_not_local_owner(multitenant_pages_client):
    with patch("webview.pages.load_self_doc") as mock_load_self:
        mock_load_self.return_value = None
        multitenant_pages_client.get(
            "/api/webgate/identity", cookies=_auth_cookie("tenant-42"),
        )
    args, kwargs = mock_load_self.call_args
    # load_self_doc(home, owner, instance_id) -- positional; owner is arg[1].
    assert args[1] == "tenant-42"


def test_unauthenticated_multitenant_request_is_401_not_leaked_to_handler(
    multitenant_pages_client,
):
    """B4's posture-aware auth gating already puts these pages behind auth in
    multitenant — an unauthenticated caller never reaches the handler at all
    (401 from the middleware), so the ``_effective_user_id`` fallback branch is
    unreachable via the API. Confirmed here so the next test's direct
    unit-check of the fallback isn't mistaken for a live route behavior.
    """
    resp = multitenant_pages_client.get("/api/webgate/memory")  # no cookie
    assert resp.status_code == 401


def test_effective_user_id_fails_closed_when_unauthenticated_in_multitenant(
    multitenant_pages_client,
):
    """Direct unit-check of the defense-in-depth branch inside
    ``_effective_user_id`` itself (unreachable through the gated route above,
    per the note in the previous test). Must fail CLOSED — never resolve to
    the instance owner's identity, even as a fallback."""
    from fastapi import HTTPException

    import webview.pages as pages

    class _FakeState:
        user_id = None

    class _FakeRequest:
        state = _FakeState()

    with pytest.raises(HTTPException) as exc_info:
        pages._effective_user_id(_FakeRequest())
    assert exc_info.value.status_code == 403


def test_effective_user_id_returns_caller_id_when_authenticated_in_multitenant(
    multitenant_pages_client,
):
    """Multitenant + present ``request.state.user_id`` returns the caller's id
    (not the owner's) — the non-fallback, expected path."""
    import webview.pages as pages

    class _FakeState:
        user_id = "tenant-42"

    class _FakeRequest:
        state = _FakeState()

    assert pages._effective_user_id(_FakeRequest()) == "tenant-42"


def test_effective_user_id_local_owner_fallback_for_non_multitenant(monkeypatch):
    """Local/own_ops posture (not multitenant): still returns
    ``local_owner_id()`` unconditionally — no regression to the single-owner
    branch."""
    import importlib

    monkeypatch.delenv("WEBGATE_MULTITENANT", raising=False)
    import webview.webgate as wg
    importlib.reload(wg)
    import webview.pages as pages
    importlib.reload(pages)

    class _FakeState:
        user_id = None

    class _FakeRequest:
        state = _FakeState()

    assert pages._effective_user_id(_FakeRequest()) == wg.local_owner_id()

    # restore multitenant module state for any subsequent tests in this file
    monkeypatch.setenv("WEBGATE_MULTITENANT", "true")
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret")
    importlib.reload(wg)
    importlib.reload(pages)
