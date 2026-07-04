"""Item 6 — cross-worker session routing → HTTP.

A SessionRoute is translated to an honest HTTP outcome: LOCAL proceeds, REMOTE
becomes 409 + owner_pid + Retry-After (NOT a silent false-404), MISSING is 404.
``guard_remote`` only upgrades REMOTE (so an endpoint's own DB-backed/resumable
fallback still runs for LOCAL/MISSING).
"""
import pytest
from fastapi import HTTPException

from agents.task.session_route import SessionRoute, LOCAL, REMOTE, MISSING
from api.session_routing import route_to_http, guard_remote


def test_local_returns_orchestrator():
    obj = object()
    assert route_to_http(SessionRoute(status=LOCAL, orchestrator=obj), "s1") is obj


def test_remote_raises_409_with_owner_pid():
    with pytest.raises(HTTPException) as ei:
        route_to_http(SessionRoute(status=REMOTE, owner_pid=4242), "s1")
    assert ei.value.status_code == 409
    assert ei.value.detail["owner_pid"] == 4242
    assert ei.value.detail["session_id"] == "s1"
    assert ei.value.headers.get("Retry-After")


def test_missing_raises_404():
    with pytest.raises(HTTPException) as ei:
        route_to_http(SessionRoute(status=MISSING), "s1")
    assert ei.value.status_code == 404


class _FakeAgent:
    def __init__(self, route):
        self._route = route

    def route_session(self, sid):
        return self._route


def test_guard_remote_raises_on_remote():
    with pytest.raises(HTTPException) as ei:
        guard_remote(_FakeAgent(SessionRoute(status=REMOTE, owner_pid=7)), "s1")
    assert ei.value.status_code == 409
    assert ei.value.detail["owner_pid"] == 7


def test_guard_remote_passes_local():
    route = guard_remote(_FakeAgent(SessionRoute(status=LOCAL, orchestrator=object())), "s1")
    assert route.is_local


def test_guard_remote_passes_missing():
    route = guard_remote(_FakeAgent(SessionRoute(status=MISSING)), "s1")
    assert route.is_missing


def test_guard_remote_tolerates_agent_without_route_session():
    class _Old:
        pass
    assert guard_remote(_Old(), "s1") is None  # no route_session -> no-op
