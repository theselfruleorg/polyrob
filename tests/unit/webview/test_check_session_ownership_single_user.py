"""_check_session_ownership short-circuits to the local owner in single-user mode."""
import importlib

import pytest


class _FakeState:
    pass


class _FakeRequest:
    """Minimal stand-in for a Starlette Request (no auth header)."""

    def __init__(self):
        self.state = _FakeState()
        self.headers = {}
        self.cookies = {}


def _reload_server(monkeypatch, multitenant: bool):
    monkeypatch.setenv("WEBGATE_MULTITENANT", "true" if multitenant else "false")
    monkeypatch.setenv("ENV", "development")
    import webview.server as server
    return importlib.reload(server)


def test_single_user_ownership_short_circuits_to_local_owner(monkeypatch):
    monkeypatch.setenv("POLYROB_LOCAL_OWNER", "rob")
    server = _reload_server(monkeypatch, multitenant=False)
    is_owner, current_user, session_owner = server._check_session_ownership(
        _FakeRequest(), "some-session-id"
    )
    assert is_owner is True
    assert current_user == "rob"
    assert session_owner == "rob"


def test_single_user_manual_auth_check_is_noop(monkeypatch):
    server = _reload_server(monkeypatch, multitenant=False)
    req = _FakeRequest()
    # Must not raise and must not populate auth state in single-user.
    server._manual_auth_check(req)
    assert not getattr(req.state, "authenticated", False)


def test_multitenant_ownership_unauthenticated_not_owner(monkeypatch):
    """Flag ON → legacy behavior: an unauthenticated request is not the owner."""
    server = _reload_server(monkeypatch, multitenant=True)

    # Stub the auth helpers so no real JWT/DB is needed.
    import utils.auth_utils as au
    monkeypatch.setattr(au, "is_authenticated", lambda request: False)

    is_owner, current_user, _ = server._check_session_ownership(
        _FakeRequest(), "some-session-id"
    )
    assert is_owner is False
    assert current_user is None


@pytest.fixture(autouse=True)
def _restore_server(monkeypatch):
    yield
    monkeypatch.delenv("WEBGATE_MULTITENANT", raising=False)
    monkeypatch.delenv("POLYROB_POSTURE", raising=False)
    import webview.server as server
    importlib.reload(server)


# ── H2b (B4-M): own_ops runs the real ownership check (requires_owner_login(),
# not is_multitenant()) instead of the single-user (True, owner, owner) bypass ──

def test_own_ops_ownership_authenticated_owner_allowed(monkeypatch):
    """own_ops: the authenticated owner reaches their own session (real check,
    not the bypass) because the owner-login JWT's user_id == local_owner_id()
    and the session they created is tagged with that same id."""
    server = _reload_server(monkeypatch, multitenant=False)
    monkeypatch.setenv("POLYROB_POSTURE", "own_ops")

    owner_id = server.webgate.local_owner_id()

    import utils.auth_utils as au
    monkeypatch.setattr(au, "is_authenticated", lambda request: True)
    monkeypatch.setattr(au, "get_authenticated_user_id", lambda request: owner_id)
    monkeypatch.setattr(type(server.pm()), "get_session_user", lambda self, session_id: owner_id)

    is_owner, current_user_id, session_owner_id = server._check_session_ownership(
        _FakeRequest(), "some-session-id"
    )
    assert is_owner is True
    assert current_user_id == owner_id
    assert session_owner_id == owner_id


def test_own_ops_ownership_unauthenticated_denied(monkeypatch):
    """own_ops: no auth token at all -> denied, not the single-user bypass."""
    server = _reload_server(monkeypatch, multitenant=False)
    monkeypatch.setenv("POLYROB_POSTURE", "own_ops")

    owner_id = server.webgate.local_owner_id()

    import utils.auth_utils as au
    monkeypatch.setattr(au, "is_authenticated", lambda request: False)
    monkeypatch.setattr(type(server.pm()), "get_session_user", lambda self, session_id: owner_id)

    is_owner, current_user_id, session_owner_id = server._check_session_ownership(
        _FakeRequest(), "some-session-id"
    )
    assert is_owner is False
    assert current_user_id is None


def test_own_ops_ownership_mismatched_user_denied(monkeypatch):
    """own_ops: authenticated but as a different id than the session owner ->
    denied (this is exactly the case the old is_multitenant() guard bypassed)."""
    server = _reload_server(monkeypatch, multitenant=False)
    monkeypatch.setenv("POLYROB_POSTURE", "own_ops")

    owner_id = server.webgate.local_owner_id()

    import utils.auth_utils as au
    monkeypatch.setattr(au, "is_authenticated", lambda request: True)
    monkeypatch.setattr(au, "get_authenticated_user_id", lambda request: "someone-else")
    monkeypatch.setattr(type(server.pm()), "get_session_user", lambda self, session_id: owner_id)

    is_owner, current_user_id, session_owner_id = server._check_session_ownership(
        _FakeRequest(), "some-session-id"
    )
    assert is_owner is False
    assert current_user_id == "someone-else"
    assert session_owner_id == owner_id


def test_own_ops_ownership_owner_allowed_on_cli_created_session(monkeypatch):
    """H2b regression: own_ops has exactly ONE owner. The authenticated owner
    must reach a session created via the CLI surface (hardcoded
    user_id="local", core/identity.py::LocalIdentity.resolve()), which never
    equals the own_ops owner-login id (webgate.local_owner_id(), default
    "rob"). A strict per-session string match here wrongly denies the owner
    on their own CLI-created sessions -- the single-owner model must allow it."""
    server = _reload_server(monkeypatch, multitenant=False)
    monkeypatch.setenv("POLYROB_POSTURE", "own_ops")

    owner_id = server.webgate.local_owner_id()
    assert owner_id != "local"  # sanity: the two identities really do differ

    import utils.auth_utils as au
    monkeypatch.setattr(au, "is_authenticated", lambda request: True)
    monkeypatch.setattr(au, "get_authenticated_user_id", lambda request: owner_id)
    monkeypatch.setattr(type(server.pm()), "get_session_user", lambda self, session_id: "local")

    is_owner, current_user_id, session_owner_id = server._check_session_ownership(
        _FakeRequest(), "cli-created-session-id"
    )
    assert is_owner is True
    assert current_user_id == owner_id
    assert session_owner_id == "local"


def test_multitenant_ownership_tenant_b_denied_tenant_a_session(monkeypatch):
    """multitenant: strict per-session ownership match is unchanged by H2b --
    tenant B must still be denied tenant A's session."""
    server = _reload_server(monkeypatch, multitenant=True)

    import utils.auth_utils as au
    monkeypatch.setattr(au, "is_authenticated", lambda request: True)
    monkeypatch.setattr(au, "get_authenticated_user_id", lambda request: "tenant-b")
    monkeypatch.setattr(type(server.pm()), "get_session_user", lambda self, session_id: "tenant-a")

    is_owner, current_user_id, session_owner_id = server._check_session_ownership(
        _FakeRequest(), "tenant-a-session-id"
    )
    assert is_owner is False
    assert current_user_id == "tenant-b"
    assert session_owner_id == "tenant-a"


def test_local_posture_ownership_unchanged_by_h2b(monkeypatch):
    """Posture 0 (local) must stay on the bypass: requires_owner_login() is False
    for local, same as is_multitenant() was — no behavior change here."""
    server = _reload_server(monkeypatch, multitenant=False)
    is_owner, current_user_id, session_owner_id = server._check_session_ownership(
        _FakeRequest(), "some-session-id"
    )
    assert is_owner is True
    assert current_user_id == session_owner_id == server.webgate.local_owner_id()
