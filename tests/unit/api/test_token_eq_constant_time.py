"""Security review 2026-09-23 (Low): the service-token compare is constant time."""
import hmac

from api import app as app_mod


def test_token_eq_matches_and_refuses():
    assert app_mod._token_eq("abc", "abc") is True
    assert app_mod._token_eq("abd", "abc") is False
    assert app_mod._token_eq(None, "abc") is False
    assert app_mod._token_eq("ключ", "abc") is False  # non-ASCII never raises


def test_token_eq_uses_compare_digest(monkeypatch):
    calls = []
    real = hmac.compare_digest
    monkeypatch.setattr(app_mod.hmac, "compare_digest",
                        lambda a, b: calls.append((a, b)) or real(a, b))
    app_mod._token_eq("x", "y")
    assert calls


def test_middleware_has_no_plain_token_equality():
    import inspect
    src = inspect.getsource(app_mod.fallback_auth_middleware)
    assert "provided_token != api_token" not in src
    assert "auth_header == api_token" not in src
