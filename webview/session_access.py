"""Owner-only session reads; possession of a session URL is not a grant.

ONE rule decides who may open a session, and every gate applies it: the auth
middleware (:func:`may_read_session_path`), the socket ``join_session`` and
``server._check_session_ownership`` (all via :func:`owner_may_open`).

* ``local`` / ``own_ops`` — the instance has ONE owner (``webgate.local_owner_id()``) and
  that owner owns EVERY session, whatever identity tagged it (CLI ``local``,
  a room, a correspondent). Any other identity is refused.
* ``multitenant`` — strict: the caller must be the session's recorded owner.
"""


from agents.task.path import is_reserved_session_id  # noqa: F401 — WEB-1, one rule


def session_path_id(path):
    for prefix in ("/api/session/", "/session/"):
        if path.startswith(prefix):
            return path[len(prefix):].split("/", 1)[0]
    return None


def owner_may_open(user_id, session_owner) -> bool:
    """The one session-open rule (see the module docstring). Fails closed."""
    from webview import webgate
    try:
        if not user_id:
            return False
        if webgate.is_owner_console():
            return user_id == webgate.local_owner_id()
        return bool(session_owner) and user_id == session_owner
    except Exception:
        return False


def may_read_session_path(path, user_id, path_manager):
    session_id = session_path_id(path)
    if session_id is None:
        return True
    if not user_id or not session_id:
        return False
    try:
        from agents.task.path import require_canonical_session_id
        clean_id = require_canonical_session_id(session_id, path_manager=path_manager)
        return owner_may_open(user_id, path_manager.get_session_user(clean_id))
    except Exception:
        return False


def session_room(session_id: str) -> str:
    """Session streams never share a Socket.IO room with global or private streams."""
    return "session:" + session_id


def http_session_id(session_id: str, path_manager) -> str:
    from agents.task.path import require_canonical_session_id
    from fastapi import HTTPException
    from webview.copy import t
    try:
        return require_canonical_session_id(session_id, path_manager=path_manager)
    except ValueError as exc:
        raise HTTPException(400, t("chat.invalid_session")) from exc
