"""Socket join limits for the console (070 W0.17).

Before this, every page cost TWO counts (the activity room and the session
room) against 10 per IP per minute — and behind nginx every visitor is
``127.0.0.1`` — after which the server disconnected the socket and the head
showed "Rate limit exceeded" until a reload that hit the same wall. Now:

* a socket counts ONCE (:func:`join_allowed`), however many rooms it joins,
  and only a join that passed the limit marks it;
* the key is the owner when the socket has one (``u:<user>``), else the IP;
* the limit is :data:`JOIN_LIMIT_PER_MIN` per minute;
* a refusal is a machine code (:func:`refusal`) and the socket stays connected,
  so the client can join again after ``retry_after`` seconds.
"""
from __future__ import annotations

from typing import Callable, Mapping, Optional

JOIN_LIMIT_PER_MIN = 30

_counted: set = set()


def limit_key(sid: str, environ: Optional[Mapping], socket_user: Mapping) -> str:
    """``u:<owner>`` when the socket carries an identity, else ``ip:<address>``."""
    user = socket_user.get(sid) if socket_user else None
    if user:
        return f"u:{user}"
    return "ip:" + str((environ or {}).get("REMOTE_ADDR", "unknown"))


def join_allowed(sid: str, check: Callable[[], bool]) -> bool:
    """True if this join may proceed. A socket already counted is free; else
    ``check()`` spends one count and the socket is marked ONLY when it passes
    (WS2 — marking first let a refused socket re-join at once, unlimited)."""
    if sid in _counted:
        return True
    if not check():
        return False
    _counted.add(sid)
    return True


def forget(sid: str) -> None:
    """Drop a disconnected socket."""
    _counted.discard(sid)


def refusal(code: str, retry_after: Optional[int] = None) -> dict:
    """The ``error`` payload: a code and when to retry, never prose."""
    return {"code": code, "retry_after": retry_after}
