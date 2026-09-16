"""Console posture authorization after cryptographic session verification."""


def require_session_identity(claims):
    """A valid customer/admin token is not an own_ops owner credential.

    Single-owner page readers resolve their tenant from the configured owner,
    so this check must precede request-state population on every entry point.
    Multitenant readers retain their existing per-caller authorization.
    """
    import jwt
    from webview import webgate

    try:
        if not webgate.is_own_ops():
            return
        uid = claims.get("user_id")
        if isinstance(uid, str) and uid and uid == webgate.local_owner_id():
            return
    except Exception:
        pass
    raise jwt.InvalidTokenError("console_owner_mismatch")
