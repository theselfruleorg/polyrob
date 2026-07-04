"""Cross-worker session routing → HTTP (Item 6).

Translate a ``SessionRoute`` (``agents/task/session_route.py``) into an honest HTTP
outcome. The point is to stop **false-404ing** a session that simply lives in another
uvicorn worker: ``REMOTE`` becomes a ``409`` carrying ``owner_pid`` + ``Retry-After``,
so a sticky load balancer (or a human) can route the request to its owner.

With the default in-process registry every route is ``LOCAL`` or ``MISSING``, so
behaviour is unchanged until ``SESSION_REGISTRY_BACKEND=sqlite`` + ``workers>1``.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import HTTPException


def _remote_409(session_id: str, owner_pid: Optional[int]) -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={
            "session_id": session_id,
            "owner_pid": owner_pid,
            "detail": "session owned by another worker; enable sticky routing",
        },
        headers={"Retry-After": "1"},
    )


def route_to_http(route: Any, session_id: str) -> Any:
    """Full mapping: LOCAL → orchestrator; REMOTE → 409; MISSING → 404.

    Use at endpoints that REQUIRE a live in-process orchestrator.
    """
    if route.is_local:
        return route.orchestrator
    if route.is_remote:
        raise _remote_409(session_id, route.owner_pid)
    raise HTTPException(status_code=404, detail=f"Session {session_id} not found")


def guard_remote(agent: Any, session_id: str) -> Optional[Any]:
    """Raise 409 iff the session is owned by another worker; else return the route.

    LOCAL/MISSING pass through unchanged so an endpoint's own (DB-backed / resumable)
    fallback logic still runs. Tolerates a legacy agent without ``route_session``.
    """
    route_fn = getattr(agent, "route_session", None)
    if route_fn is None:
        return None
    route = route_fn(session_id)
    if route is not None and getattr(route, "is_remote", False):
        raise _remote_409(session_id, route.owner_pid)
    return route
