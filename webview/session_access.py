"""Owner-only session reads; possession of a session URL is not a grant."""


def session_path_id(path):
    for prefix in ("/api/session/", "/session/"):
        if path.startswith(prefix):
            return path[len(prefix):].split("/", 1)[0]
    return None


def may_read_session_path(path, user_id, path_manager):
    session_id = session_path_id(path)
    if session_id is None:
        return True
    if not user_id or not session_id:
        return False
    try:
        clean_id = path_manager.clean_session_id(session_id)
        owner = path_manager.get_session_user(clean_id)
        return bool(owner) and owner == user_id
    except Exception:
        return False
