"""Owner-only session reads; possession of a session URL is not a grant.

ONE rule decides who may open a session, and every gate applies it: the auth
middleware (:func:`may_read_session_path`), the socket ``join_session`` and
``server._check_session_ownership`` (all via :func:`owner_may_open`).

* ``local`` — the loopback operator owns every session.
* ``own_ops`` — the instance has ONE owner (``webgate.local_owner_id()``) and
  that owner owns EVERY session, whatever identity tagged it (CLI ``local``,
  a room, a correspondent). Any other identity is refused.
* ``multitenant`` — strict: the caller must be the session's recorded owner.
"""


def session_path_id(path):
    for prefix in ("/api/session/", "/session/"):
        if path.startswith(prefix):
            return path[len(prefix):].split("/", 1)[0]
    return None


def owner_may_open(user_id, session_owner) -> bool:
    """The one session-open rule (see the module docstring). Fails closed."""
    from webview import webgate
    try:
        if not webgate.requires_owner_login():
            return True
        if not user_id:
            return False
        if webgate.is_own_ops():
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
        clean_id = path_manager.clean_session_id(session_id)
        return owner_may_open(user_id, path_manager.get_session_user(clean_id))
    except Exception:
        return False
