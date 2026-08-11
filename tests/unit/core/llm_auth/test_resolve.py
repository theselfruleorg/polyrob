"""resolve_credential precedence ladder + fail-closed tenancy (024 §4.1/§7.3)."""
from types import SimpleNamespace

import pytest

from core.llm_auth.resolve import (
    Credential,
    resolve_credential,
)
from core.llm_auth.store import AuthStore


def spec(env_key="X_API_KEY", auth_type="api_key"):
    return SimpleNamespace(env_key=env_key, auth_type=auth_type)


@pytest.fixture
def store(tmp_path):
    return AuthStore(tmp_path / "auth.json")


@pytest.fixture
def local_on(monkeypatch):
    monkeypatch.setenv("POLYROB_LOCAL", "1")
    monkeypatch.setenv("LLM_AUTH_STORE_ENABLED", "true")


class TestPrecedence:
    def test_env_key_first(self, store, local_on):
        store.set_provider("p", {"access_token": "oauth-tok"})
        cred = resolve_credential("p", spec(), env={"X_API_KEY": "env-key",
                                                    "LLM_AUTH_STORE_ENABLED": "true"},
                                  store=store)
        assert (cred.kind, cred.source, cred.value) == ("api_key", "env", "env-key")

    def test_env_key_respects_injected_validator(self, local_on, store):
        cred = resolve_credential(
            "p", spec(), env={"X_API_KEY": "short", "LLM_AUTH_STORE_ENABLED": "true"},
            key_validator=lambda v: len(str(v)) >= 20, store=store,
        )
        assert cred is None  # malformed env key, empty store → nothing

    def test_store_entry_second(self, store, local_on):
        store.set_provider("p", {"access_token": "oauth-tok", "expires_at": 9e12})
        cred = resolve_credential("p", spec(), env={"LLM_AUTH_STORE_ENABLED": "true"},
                                  store=store)
        assert (cred.kind, cred.source, cred.value) == ("bearer", "oauth", "oauth-tok")
        assert cred.health == "ok" and cred.relogin_required is False

    def test_expired_no_refresh_flags_relogin(self, store, local_on):
        store.set_provider("p", {"access_token": "tok", "expires_at": 1.0})
        cred = resolve_credential("p", spec(), env={"LLM_AUTH_STORE_ENABLED": "true"},
                                  store=store, now=100.0)
        assert cred.relogin_required is True

    def test_health_rides_credential(self, store, local_on):
        store.set_provider("p", {"access_token": "tok"})
        store.stamp_health("p", "exhausted", retry_after=9e12)
        cred = resolve_credential("p", spec(), env={"LLM_AUTH_STORE_ENABLED": "true"},
                                  store=store)
        assert cred.health == "exhausted"

    def test_lapsed_retry_after_heals(self, store, local_on):
        store.set_provider("p", {"access_token": "tok"})
        store.stamp_health("p", "rate_limited", retry_after=50.0)
        cred = resolve_credential("p", spec(), env={"LLM_AUTH_STORE_ENABLED": "true"},
                                  store=store, now=100.0)
        assert cred.health == "ok"

    def test_borrowed_third_and_gated(self, store, local_on):
        store.set_borrowed("p", {"access_token": "btok", "consented": True,
                                 "source": "codex_cli"})
        env = {"LLM_AUTH_STORE_ENABLED": "true"}
        assert resolve_credential("p", spec(), env=env, store=store) is None  # borrow off
        env["LLM_CREDENTIAL_BORROW"] = "true"
        cred = resolve_credential("p", spec(), env=env, store=store)
        assert (cred.source, cred.value) == ("borrowed", "btok")

    def test_auth_type_none_sentinel(self, store):
        cred = resolve_credential("ollama", spec(env_key=None, auth_type="none"),
                                  env={}, store=store)
        assert (cred.kind, cred.source, cred.value) == ("none", "none", None)

    def test_nothing_resolves_none(self, store):
        assert resolve_credential("p", spec(), env={}, store=store) is None


class TestFailClosedTenancy:
    def test_store_ignored_when_flag_off(self, store, monkeypatch):
        monkeypatch.setenv("POLYROB_LOCAL", "1")
        store.set_provider("p", {"access_token": "tok"})
        cred = resolve_credential("p", spec(), env={}, store=store)
        assert cred is None  # LLM_AUTH_STORE_ENABLED default OFF

    def test_store_ignored_on_server(self, store, monkeypatch):
        """No POLYROB_LOCAL → the store NEVER serves (an accidental server
        deploy degrades to env-key behavior, it can't share the owner's seat)."""
        monkeypatch.delenv("POLYROB_LOCAL", raising=False)
        monkeypatch.delenv("ROB_LOCAL", raising=False)
        store.set_provider("p", {"access_token": "tok"})
        cred = resolve_credential(
            "p", spec(), env={"LLM_AUTH_STORE_ENABLED": "true"}, store=store
        )
        assert cred is None


class TestRedaction:
    def test_repr_never_leaks_value(self):
        cred = Credential(kind="bearer", value="super-secret-token", provider="p",
                          source="oauth")
        assert "super-secret-token" not in repr(cred)
        assert "super-secret-token" not in str(cred)
        assert "redacted" in repr(cred)


class TestTenantGate:
    def test_non_owner_tenant_refused(self, store, local_on):
        """024 §7.3: a KNOWN non-owner tenant never receives a store credential,
        even on a POLYROB_LOCAL box with the store enabled."""
        store.set_provider("p", {"access_token": "tok"})
        env = {"LLM_AUTH_STORE_ENABLED": "true"}
        assert resolve_credential("p", spec(), env=env, store=store,
                                  user_id="stranger-123") is None

    def test_owner_context_none_served(self, store, local_on):
        store.set_provider("p", {"access_token": "tok"})
        cred = resolve_credential("p", spec(), env={"LLM_AUTH_STORE_ENABLED": "true"},
                                  store=store, user_id=None)
        assert cred is not None and cred.source == "oauth"


class TestExpirySemantics:
    def test_expired_with_refresh_token_still_flags_relogin(self, store, local_on):
        """Until L2 refresh flows exist, ANY expired token is relogin_required —
        a stale bearer must never be served as silently healthy (review M2)."""
        store.set_provider("p", {"access_token": "tok", "refresh_token": "r",
                                 "expires_at": 1.0})
        cred = resolve_credential("p", spec(), env={"LLM_AUTH_STORE_ENABLED": "true"},
                                  store=store, now=100.0)
        assert cred.relogin_required is True
