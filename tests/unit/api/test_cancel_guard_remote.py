"""A32 — a cross-process Stop is an honest 409, not a silent no-op.

``POST /sessions/{id}/cancel`` used to call ``cancel_session_by_id`` directly.
That method self-authorizes against the session's own user_id but CANNOT reach a
session whose live orchestrator runs in another worker — so a console Stop on a
remote session returned success and cancelled nothing. This mirrors
``send_user_message``'s ``guard_remote`` (api/task_http_api.py): a REMOTE route
becomes a 409 carrying ``owner_pid`` + ``Retry-After`` BEFORE the session lookup,
while LOCAL/MISSING (and a legacy agent with no ``route_session``) pass straight
through to the existing logic, so nothing changes for the in-process registry.
"""
import pytest
from fastapi import HTTPException

import api.task_http_api as thp
from agents.task.session_route import SessionRoute, LOCAL, REMOTE


class _FakeState:
    def __init__(self, user_id):
        self.user_id = user_id


class _FakeRequest:
    def __init__(self, user_id):
        self.state = _FakeState(user_id)


class _RemoteAgent:
    """A session owned by ANOTHER worker: route_session → REMOTE."""

    def __init__(self, owner_pid=9191):
        self._owner_pid = owner_pid

    def route_session(self, session_id):
        return SessionRoute(status=REMOTE, owner_pid=self._owner_pid)

    async def get_session_by_id(self, session_id):  # pragma: no cover - must not run
        raise AssertionError("guard_remote must fire before the session lookup")

    async def cancel_session_by_id(self, session_id, force=False):  # pragma: no cover
        raise AssertionError("a remote session must never be cancelled locally")


class _LocalAgent:
    """A session owned by THIS worker: route_session → LOCAL, so cancel proceeds."""

    def __init__(self, owner="tenant-a"):
        self._owner = owner
        self.cancelled = []

    def route_session(self, session_id):
        return SessionRoute(status=LOCAL, orchestrator=object())

    async def get_session_by_id(self, session_id):
        return {"id": session_id, "user_id": self._owner, "status": "running"}

    async def cancel_session_by_id(self, session_id, force=False):
        self.cancelled.append(session_id)
        return True


class _LegacyAgent:
    """An agent with no ``route_session`` — guard_remote must be a no-op for it."""

    def __init__(self, owner="tenant-a"):
        self._owner = owner
        self.cancelled = []

    async def get_session_by_id(self, session_id):
        return {"id": session_id, "user_id": self._owner, "status": "running"}

    async def cancel_session_by_id(self, session_id, force=False):
        self.cancelled.append(session_id)
        return True


@pytest.mark.asyncio
async def test_cancel_remote_session_returns_409_with_owner_pid():
    agent = _RemoteAgent(owner_pid=9191)
    with pytest.raises(HTTPException) as ei:
        await thp.cancel_session("shared-session-id", _FakeRequest("tenant-a"), agent=agent)
    assert ei.value.status_code == 409
    assert ei.value.detail["owner_pid"] == 9191
    assert ei.value.detail["session_id"] == "shared-session-id"
    assert ei.value.headers.get("Retry-After")


@pytest.mark.asyncio
async def test_cancel_local_session_still_cancels():
    # "shared-session-id" survives clean_session_id_at_entry unchanged (the
    # ownership test relies on the same fact).
    agent = _LocalAgent("tenant-a")
    resp = await thp.cancel_session("shared-session-id", _FakeRequest("tenant-a"), agent=agent)
    assert resp.success is True
    assert agent.cancelled == ["shared-session-id"]


@pytest.mark.asyncio
async def test_cancel_legacy_agent_without_route_session_is_unaffected():
    agent = _LegacyAgent("tenant-a")
    resp = await thp.cancel_session("shared-session-id", _FakeRequest("tenant-a"), agent=agent)
    assert resp.success is True
    assert agent.cancelled == ["shared-session-id"]
