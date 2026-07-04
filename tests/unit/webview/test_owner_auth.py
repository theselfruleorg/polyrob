import importlib
import pytest
from argon2 import PasswordHasher


@pytest.fixture
def owner_auth(monkeypatch):
    for k in ("POLYROB_OWNER_USERNAME", "POLYROB_OWNER_PASSWORD_HASH", "JWT_SECRET_KEY"):
        monkeypatch.delenv(k, raising=False)
    import webview.owner_auth as oa
    return importlib.reload(oa)


def _hash(pw: str) -> str:
    return PasswordHasher().hash(pw)


def test_owner_credentials_not_configured_by_default(owner_auth):
    assert owner_auth.owner_credentials_configured() is False


def test_owner_credentials_configured_when_both_set(owner_auth, monkeypatch):
    monkeypatch.setenv("POLYROB_OWNER_USERNAME", "op")
    monkeypatch.setenv("POLYROB_OWNER_PASSWORD_HASH", _hash("s3cret"))
    assert owner_auth.owner_credentials_configured() is True


def test_verify_owner_password_correct(owner_auth, monkeypatch):
    monkeypatch.setenv("POLYROB_OWNER_USERNAME", "op")
    monkeypatch.setenv("POLYROB_OWNER_PASSWORD_HASH", _hash("s3cret"))
    assert owner_auth.verify_owner_password("op", "s3cret") is True


def test_verify_owner_password_wrong_password(owner_auth, monkeypatch):
    monkeypatch.setenv("POLYROB_OWNER_USERNAME", "op")
    monkeypatch.setenv("POLYROB_OWNER_PASSWORD_HASH", _hash("s3cret"))
    assert owner_auth.verify_owner_password("op", "wrong") is False


def test_verify_owner_password_wrong_username(owner_auth, monkeypatch):
    monkeypatch.setenv("POLYROB_OWNER_USERNAME", "op")
    monkeypatch.setenv("POLYROB_OWNER_PASSWORD_HASH", _hash("s3cret"))
    assert owner_auth.verify_owner_password("someone_else", "s3cret") is False


def test_verify_owner_password_unconfigured_returns_false_not_raise(owner_auth):
    assert owner_auth.verify_owner_password("op", "s3cret") is False


def test_verify_owner_password_malformed_hash_returns_false_not_raise(owner_auth, monkeypatch):
    monkeypatch.setenv("POLYROB_OWNER_USERNAME", "op")
    monkeypatch.setenv("POLYROB_OWNER_PASSWORD_HASH", "not-a-valid-argon2-hash")
    assert owner_auth.verify_owner_password("op", "s3cret") is False


def _spy_on_hasher_verify(owner_auth, monkeypatch):
    """argon2's PasswordHasher uses __slots__ (no instance __dict__), so an
    instance attribute can't be monkeypatched directly. Patch the class
    attribute instead with a plain MagicMock (not a descriptor, so it is
    NOT auto-bound with `self` on instance access — mirrors how the real
    call site `self._hasher.verify(hash_, password)` invokes it) whose
    side_effect delegates to the real bound method captured before patching.
    """
    from unittest.mock import MagicMock

    real_bound_verify = owner_auth._hasher.verify

    def _dispatch(hash_, password):
        return real_bound_verify(hash_, password)

    spy = MagicMock(side_effect=_dispatch)
    monkeypatch.setattr(owner_auth.PasswordHasher, "verify", spy)
    return spy


def test_verify_owner_password_runs_argon2_on_bad_username(owner_auth, monkeypatch):
    """No fast-path: a wrong USERNAME must still burn an argon2 verify (against
    the dummy hash), not return before argon2 ever runs. Deterministic (spies
    on the call), not a flaky wall-clock timing assertion."""
    monkeypatch.setenv("POLYROB_OWNER_USERNAME", "op")
    monkeypatch.setenv("POLYROB_OWNER_PASSWORD_HASH", _hash("s3cret"))

    spy = _spy_on_hasher_verify(owner_auth, monkeypatch)

    assert owner_auth.verify_owner_password("someone_else", "s3cret") is False
    assert spy.call_count == 1


def test_verify_owner_password_runs_argon2_on_bad_password(owner_auth, monkeypatch):
    """Same call shape for a correct username + wrong password, so both
    failure modes cost identical argon2 work (proving no username-based
    fast-path remains)."""
    monkeypatch.setenv("POLYROB_OWNER_USERNAME", "op")
    monkeypatch.setenv("POLYROB_OWNER_PASSWORD_HASH", _hash("s3cret"))

    spy = _spy_on_hasher_verify(owner_auth, monkeypatch)

    assert owner_auth.verify_owner_password("op", "wrong") is False
    assert spy.call_count == 1


def test_issue_owner_session_cookie_contract_shape(owner_auth, monkeypatch):
    import jwt as pyjwt
    from fastapi import Response

    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret")
    monkeypatch.setenv("POLYROB_OWNER_USERNAME", "op")
    monkeypatch.setenv("POLYROB_OWNER_PASSWORD_HASH", _hash("s3cret"))

    resp = Response()
    token = owner_auth.issue_owner_session_cookie(resp)

    decoded = pyjwt.decode(token, "test-secret", algorithms=["HS256"])
    assert decoded["role"] == "owner"
    assert decoded["tier"] == "admin"
    assert decoded.get("payment_method") is None
    assert decoded["user_id"]  # non-empty — webgate.local_owner_id()

    set_cookie_header = resp.headers.get("set-cookie", "")
    assert "auth_token=" in set_cookie_header
    assert "httponly" in set_cookie_header.lower()
    assert "samesite=lax" in set_cookie_header.lower()


def test_issue_owner_session_cookie_raises_without_jwt_secret(owner_auth, monkeypatch):
    from fastapi import Response
    monkeypatch.delenv("JWT_SECRET_KEY", raising=False)
    with pytest.raises(RuntimeError):
        owner_auth.issue_owner_session_cookie(Response())
