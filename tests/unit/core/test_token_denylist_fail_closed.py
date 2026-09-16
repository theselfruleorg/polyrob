import pytest

from core.token_denylist import TokenDenylist, jti_is_revoked


def test_failed_init_refuses_credentials(tmp_path):
    store = TokenDenylist(str(tmp_path))  # directory cannot be a SQLite database
    assert store.is_revoked("owner-session")


def test_failed_read_refuses_credentials(monkeypatch, tmp_path):
    store = TokenDenylist(str(tmp_path / "tokens.db"))

    def fail(*args, **kwargs):
        raise OSError("storage unavailable")

    monkeypatch.setattr("core.token_denylist.execute_retry", fail)
    assert store.is_revoked("owner-session")


def test_failed_logout_latches_store_closed(monkeypatch, tmp_path):
    store = TokenDenylist(str(tmp_path / "tokens.db"))
    with monkeypatch.context() as patch:
        def fail(*args, **kwargs):
            raise OSError("disk full")
        patch.setattr("core.token_denylist.execute_retry", fail)
        from core.token_denylist import RevocationUnavailable
        with pytest.raises(RevocationUnavailable):
            store.revoke("owner-session")
    assert store.is_revoked("owner-session")


def test_predicate_lookup_failure_refuses(monkeypatch):
    def fail():
        raise OSError("data home unavailable")
    monkeypatch.setattr("core.token_denylist.get_token_denylist", fail)
    assert jti_is_revoked({"jti": "owner-session"})
