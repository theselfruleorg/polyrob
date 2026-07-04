"""C3: fallback_auth_middleware was registered LAST, which (Starlette inserts each
add_middleware at index 0) made it the OUTERMOST layer — it ran before
AuthenticationMiddleware._validate_api_key, so self-service `rob_xxx` API keys were
rejected against the static API_AUTH_TOKEN and never reached the DB validator.

It must be registered FIRST so it becomes the INNERMOST layer and runs only after
the real auth middlewares have had a chance to authenticate the request.

Lower index in user_middleware == outermost == runs first.
"""
from api.app import create_app, fallback_auth_middleware


def _index_of_dispatch(user_mw, func):
    for i, m in enumerate(user_mw):
        opts = getattr(m, "kwargs", None) or getattr(m, "options", None) or {}
        if opts.get("dispatch") is func:
            return i
    return -1


def _index_of_cls(user_mw, cls):
    for i, m in enumerate(user_mw):
        if getattr(m, "cls", None) is cls:
            return i
    return -1


def test_fallback_auth_runs_inside_authentication_middleware(monkeypatch):
    # API_SECRET makes AuthenticationMiddleware (the DB-backed rob_xxx validator) register.
    monkeypatch.setenv("API_SECRET", "test-secret-for-authmw")
    from api.middleware import AuthenticationMiddleware

    app = create_app()
    mw = app.user_middleware

    fb = _index_of_dispatch(mw, fallback_auth_middleware)
    auth = _index_of_cls(mw, AuthenticationMiddleware)

    assert fb != -1, "fallback_auth_middleware not registered"
    assert auth != -1, "AuthenticationMiddleware not registered (API_SECRET set?)"
    # Fallback must be INNER (higher index) than AuthenticationMiddleware.
    assert fb > auth, (
        f"fallback (idx {fb}) must run inside AuthenticationMiddleware (idx {auth}); "
        "otherwise rob_xxx API keys never reach the DB validator"
    )
