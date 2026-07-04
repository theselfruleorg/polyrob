"""E8 (A6 gap 4) — LIVE cross-tenant IDOR on api/task_http_api.py session
endpoints. Every endpoint here trusted the path parameter's session_id/user_id
with ZERO comparison to the authenticated caller's identity: tenant B could
read AND WRITE into tenant A's session, and hijack tenant A's active-session
pointer, just by knowing/guessing a session_id / user_id string. Fix mirrors
the ALREADY-correct pattern at the workspace/upload endpoint
(api/task_http_api.py:1554-1564): session_owner != caller -> 403.
"""
import pytest
from fastapi import HTTPException

import api.task_http_api as thp
from api.models import UserMessage as UserMessageRequest


class _FakeState:
    def __init__(self, user_id):
        self.user_id = user_id


class _FakeRequest:
    def __init__(self, user_id):
        self.state = _FakeState(user_id)


class _FakeAgent:
    """Minimal stand-in for TaskAgent exposing only what these 4 endpoints touch."""

    def __init__(self, session_owner="tenant-a"):
        self._session = {
            "id": "shared-session-id", "user_id": session_owner,
            "status": "running", "task": "do the thing",
            "created_at": "2026-07-02T00:00:00", "config": {}, "metadata": {},
        }
        self.active_sessions = {}
        self.user_sessions = {}

        self.cancelled = []

    async def get_session_by_id(self, session_id):
        return dict(self._session) if session_id == "shared-session-id" else None

    async def cancel_session_by_id(self, session_id, force=False):
        self.cancelled.append(session_id)
        return True

    def get_orchestrator(self, session_id):
        return None  # not active → queue-status falls to final-status branch


# ── the ownership helper itself ────────────────────────────────────────────

def test_require_session_owner_allows_matching_owner():
    thp._require_session_owner(_FakeRequest("tenant-a"), "tenant-a")  # must not raise


def test_require_session_owner_denies_mismatch():
    with pytest.raises(HTTPException) as ei:
        thp._require_session_owner(_FakeRequest("tenant-b"), "tenant-a")
    assert ei.value.status_code == 403


def test_require_session_owner_allows_when_no_owner_recorded():
    thp._require_session_owner(_FakeRequest("tenant-b"), None)  # must not raise


# ── POST /sessions/{id}/messages ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_cross_tenant_cannot_write_message_into_another_tenants_session():
    agent = _FakeAgent(session_owner="tenant-a")
    attacker_req = _FakeRequest(user_id="tenant-b")
    msg = UserMessageRequest(text="inject some guidance")

    with pytest.raises(HTTPException) as ei:
        await thp.send_user_message("shared-session-id", msg, attacker_req, agent=agent)
    assert ei.value.status_code == 403


# ── GET /sessions/{id} ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cross_tenant_cannot_read_another_tenants_session_status():
    agent = _FakeAgent(session_owner="tenant-a")
    attacker_req = _FakeRequest(user_id="tenant-b")
    with pytest.raises(HTTPException) as ei:
        await thp.get_session_status("shared-session-id", attacker_req, agent=agent)
    assert ei.value.status_code == 403


@pytest.mark.asyncio
async def test_owner_can_still_read_own_session_status():
    agent = _FakeAgent(session_owner="tenant-a")
    owner_req = _FakeRequest(user_id="tenant-a")
    resp = await thp.get_session_status("shared-session-id", owner_req, agent=agent)
    assert resp.user_id == "tenant-a"


# ── GET/POST /users/{user_id}/... ──────────────────────────────────────────

# ── POST /sessions/{id}/cancel (IDOR) ──────────────────────────────────────

@pytest.mark.asyncio
async def test_cross_tenant_cannot_cancel_another_tenants_session():
    agent = _FakeAgent(session_owner="tenant-a")
    attacker_req = _FakeRequest(user_id="tenant-b")
    with pytest.raises(HTTPException) as ei:
        await thp.cancel_session("shared-session-id", attacker_req, agent=agent)
    assert ei.value.status_code == 403
    assert agent.cancelled == []  # never reached the cancel


@pytest.mark.asyncio
async def test_owner_can_cancel_own_session():
    agent = _FakeAgent(session_owner="tenant-a")
    owner_req = _FakeRequest(user_id="tenant-a")
    resp = await thp.cancel_session("shared-session-id", owner_req, agent=agent)
    assert resp.success is True
    assert agent.cancelled == ["shared-session-id"]


# ── GET /sessions/{id}/queue-status (IDOR) ─────────────────────────────────

@pytest.mark.asyncio
async def test_cross_tenant_cannot_read_another_tenants_queue_status():
    agent = _FakeAgent(session_owner="tenant-a")
    attacker_req = _FakeRequest(user_id="tenant-b")
    with pytest.raises(HTTPException) as ei:
        await thp.get_queue_status("shared-session-id", attacker_req, agent=agent)
    assert ei.value.status_code == 403


@pytest.mark.asyncio
async def test_owner_can_read_own_queue_status():
    agent = _FakeAgent(session_owner="tenant-a")
    agent._session["status"] = "completed"  # no orchestrator → final-status branch
    owner_req = _FakeRequest(user_id="tenant-a")
    resp = await thp.get_queue_status("shared-session-id", owner_req, agent=agent)
    assert resp["session_completed"] is True


@pytest.mark.asyncio
async def test_cross_tenant_cannot_list_another_users_sessions():
    agent = _FakeAgent()
    attacker_req = _FakeRequest(user_id="tenant-b")
    with pytest.raises(HTTPException) as ei:
        await thp.get_user_sessions("tenant-a", attacker_req, agent=agent)
    assert ei.value.status_code == 403


@pytest.mark.asyncio
async def test_cross_tenant_cannot_hijack_another_users_active_session():
    agent = _FakeAgent()
    attacker_req = _FakeRequest(user_id="tenant-b")
    with pytest.raises(HTTPException) as ei:
        await thp.switch_active_session(
            "tenant-a", {"session_id": "evil-session"}, attacker_req, agent=agent
        )
    assert ei.value.status_code == 403
