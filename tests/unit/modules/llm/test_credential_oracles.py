"""Credential-aware provider oracles (proposal 024, L1.5 — surface un-blinding).

Before this, every surface that asked "do we have a provider?" went through the
`*_with_keys` oracles, which can only see ENV KEYS. A store-backed credential
(an OAuth subscription seat, a borrowed login) served requests while `doctor`,
the banner, `model list` and the webview console all reported "missing" — L1 was
invisible.

Two properties carry the migration:

1. with `LLM_AUTH_STORE_ENABLED` off (the default), the new oracles return
   EXACTLY what the key oracles return — which is what makes swapping a call
   site over to them a no-op for every existing install;
2. with it on, a store credential is both visible and gating.
"""
import pytest


REAL = "sk-0123456789abcdef0123456789"


@pytest.fixture(autouse=True)
def clean_registry(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER_REGISTRY", "true")
    monkeypatch.setenv("LLM_CUSTOM_PROVIDERS", "")
    from modules.llm.provider_spec import reset_provider_registry_cache
    reset_provider_registry_cache()
    yield
    reset_provider_registry_cache()


# ---------------------------------------------------------------------------
# 1. Flag-off equivalence — the migration safety property
# ---------------------------------------------------------------------------
ENV_CASES = [
    {},
    {"OPENAI_API_KEY": REAL},
    {"DEEPSEEK_API_KEY": REAL},                    # present, never usable
    {"OPENAI_API_KEY": "short"},                   # malformed
    {"ANTHROPIC_API_KEY": "your-anthropic-key"},   # placeholder
    {"OPENAI_API_KEY": ""},                        # blank == absent
    {"OLLAMA_API_KEY": REAL},                      # a 024 T0 subscription row
    {"OPENROUTER_API_KEY": REAL, "GEMINI_API_KEY": REAL, "DEEPSEEK_API_KEY": REAL},
]


@pytest.mark.parametrize("env", ENV_CASES)
def test_credential_oracles_match_key_oracles_when_store_is_off(env, monkeypatch):
    monkeypatch.delenv("LLM_AUTH_STORE_ENABLED", raising=False)
    from modules.llm.profiles import (
        providers_with_credentials,
        providers_with_keys,
        usable_providers_with_credentials,
        usable_providers_with_keys,
    )
    assert providers_with_credentials(env) == providers_with_keys(env)
    assert usable_providers_with_credentials(env) == usable_providers_with_keys(env)


def test_status_separates_present_from_usable(monkeypatch):
    """"Configured but broken" and "absent" send the user to different fixes."""
    monkeypatch.delenv("LLM_AUTH_STORE_ENABLED", raising=False)
    from modules.llm.profiles import credential_status
    st = credential_status({"OPENAI_API_KEY": "short", "DEEPSEEK_API_KEY": REAL})

    assert st["openai"].present and not st["openai"].usable
    assert "malformed" in st["openai"].reason

    # deepseek's key is real; its DIRECT client is what's disabled
    assert st["deepseek"].present and not st["deepseek"].usable
    assert "OpenRouter" in st["deepseek"].reason

    assert not st["gemini"].present
    assert "GEMINI_API_KEY" in st["gemini"].reason


def test_status_never_carries_the_credential_value():
    """A status object flows to display surfaces; it must hold no secret."""
    from modules.llm.profiles import credential_status
    st = credential_status({"OPENAI_API_KEY": REAL})["openai"]
    assert REAL not in repr(st)
    assert not any(REAL == getattr(st, f) for f in st.__dataclass_fields__)


# ---------------------------------------------------------------------------
# 2. Store-backed credentials — what the key oracles could never see
# ---------------------------------------------------------------------------
@pytest.fixture
def auth_store(tmp_path, monkeypatch):
    """A live auth store with the tenancy preconditions satisfied."""
    path = tmp_path / "auth.json"
    monkeypatch.setenv("POLYROB_AUTH_STORE", str(path))
    monkeypatch.setenv("LLM_AUTH_STORE_ENABLED", "true")
    monkeypatch.setenv("POLYROB_LOCAL", "1")     # store rungs are local-only
    import core.llm_auth.store as store_mod
    from core.llm_auth.store import AuthStore

    store = AuthStore(str(path))
    monkeypatch.setattr(store_mod, "get_auth_store", lambda *a, **k: store)
    return store


def test_oauth_credential_is_visible_and_gating(auth_store, monkeypatch):
    """The whole point of L1.5: a connected account with NO env key counts."""
    from modules.llm.profiles import (
        credential_status,
        providers_with_credentials,
        providers_with_keys,
        usable_providers_with_credentials,
    )
    auth_store.set_provider("anthropic", {"access_token": "oauth-token-value"})

    env = {}   # no env key at all
    assert providers_with_keys(env) == []                      # old oracle: blind
    assert "anthropic" in providers_with_credentials(env)      # new: visible
    assert "anthropic" in usable_providers_with_credentials(env)
    st = credential_status(env)["anthropic"]
    assert st.source == "oauth" and st.usable


def test_expired_oauth_credential_is_present_but_not_usable(auth_store):
    """An expired seat must not gate as ready — it needs a reconnect, and
    'ready' would send the agent into a 401 loop instead."""
    from modules.llm.profiles import credential_status, usable_providers_with_credentials
    auth_store.set_provider(
        "anthropic", {"access_token": "stale", "expires_at": 1.0}   # long past
    )
    st = credential_status({})["anthropic"]
    assert st.present and not st.usable
    assert st.relogin_required
    assert "reconnect" in st.reason
    assert "anthropic" not in usable_providers_with_credentials({})


def test_exhausted_credential_is_routed_around(auth_store):
    """For a flat-rate seat, exhaustion is the NORMAL daily condition — the
    gating oracle must route around it rather than hammer a spent plan."""
    from modules.llm.profiles import credential_status, usable_providers_with_credentials
    auth_store.set_provider("anthropic", {"access_token": "tok"})
    auth_store.stamp_health("anthropic", "exhausted")
    st = credential_status({})["anthropic"]
    assert st.present and not st.usable and st.health == "exhausted"
    assert "anthropic" not in usable_providers_with_credentials({})
    # ...and the reason is renderable, so a surface can say WHY
    assert "exhausted" in st.reason


def test_env_key_still_wins_over_the_store(auth_store):
    """Rung order: an explicit env key is the operator's most direct statement."""
    from modules.llm.profiles import credential_status
    auth_store.set_provider("anthropic", {"access_token": "oauth"})
    st = credential_status({"ANTHROPIC_API_KEY": REAL})["anthropic"]
    assert st.source == "env" and st.usable


def test_non_owner_tenant_is_never_served_the_owner_seat(auth_store, monkeypatch):
    """§7.3 fail-closed tenancy: a correspondent must not inherit the owner's
    subscription. Pinned here because L1.5 is what put `user_id` on the path."""
    from modules.llm.profiles import credential_status
    auth_store.set_provider("anthropic", {"access_token": "oauth"})
    monkeypatch.setattr("core.instance.is_owner", lambda uid: False)
    st = credential_status({}, user_id="usr_someone_else")["anthropic"]
    assert not st.present and not st.usable


def test_unusable_but_present_explains_a_dead_box(auth_store):
    """So a 'no provider' message can say expired/exhausted/malformed instead
    of 'no key found', which is the wrong thing to go fix."""
    from modules.llm.profiles import unusable_but_present
    auth_store.set_provider("anthropic", {"access_token": "stale", "expires_at": 1.0})
    rows = unusable_but_present({"OPENAI_API_KEY": "short"})
    by_name = {r.provider: r for r in rows}
    assert "reconnect" in by_name["anthropic"].reason
    assert "malformed" in by_name["openai"].reason


# ---------------------------------------------------------------------------
# 3. Fail-open — a credential-layer fault must never blind the CLI
# ---------------------------------------------------------------------------
def test_resolver_failure_degrades_to_env_keys(monkeypatch):
    """Patched at the REAL seam (the core resolver), not at the wrapper that
    implements the fail-open — otherwise the test proves nothing."""
    import core.llm_auth.resolve as resolve_mod
    from modules.llm import profiles

    def boom(*a, **k):
        raise RuntimeError("credential layer down")

    monkeypatch.setattr(resolve_mod, "resolve_credential", boom)
    st = profiles.credential_status({"OPENAI_API_KEY": REAL})
    # still SEEN as configured (display keeps working)...
    assert st["openai"].present
    # ...but not asserted as ready, because nothing could confirm it
    assert not st["openai"].usable
