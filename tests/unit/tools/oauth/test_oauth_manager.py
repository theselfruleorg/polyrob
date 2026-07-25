"""Item 4 — OAuth manager + generic provider (library-only, no live endpoints)."""
import asyncio
import time

import pytest

from tools.mcp.security import MCPEncryption
from tools.oauth import (
    GenericOAuth2Provider,
    OAuthError,
    OAuthManager,
    OAuthProvider,
    OAuthToken,
)


def _manager():
    """Manager with a fresh temp Fernet key + in-memory store."""
    enc = MCPEncryption(key=MCPEncryption.generate_key())
    return OAuthManager(encryption=enc, store={})


class _MockProvider(OAuthProvider):
    name = "mock"

    def __init__(self):
        self.refresh_calls = 0

    def authorize_url(self, *, state=None, redirect_uri=None):
        return "https://auth.example/authorize"

    async def exchange_code(self, code, *, redirect_uri=None):
        return OAuthToken(access_token="initial", refresh_token="r0")

    async def refresh(self, token):
        self.refresh_calls += 1
        return OAuthToken(
            access_token=f"refreshed-{self.refresh_calls}",
            refresh_token=token.refresh_token,
            expires_at=time.time() + 3600,
        )


class _SlowMockProvider(_MockProvider):
    """Like _MockProvider, but `refresh()` yields the loop mid-flight (an
    ``asyncio.sleep``) so a concurrency test can reliably force two callers
    to overlap inside the refresh window, regardless of scheduling order."""

    def __init__(self, delay: float = 0.02):
        super().__init__()
        self._delay = delay

    async def refresh(self, token):
        await asyncio.sleep(self._delay)
        return await super().refresh(token)


# --- encrypted store round-trip ----------------------------------------------

def test_encrypted_round_trip_store_load():
    mgr = _manager()
    mgr.register(_MockProvider())
    token = OAuthToken(access_token="abc123", refresh_token="r0", expires_at=time.time() + 3600)
    mgr.store_token("user1", "mock", token)
    # stored blob is ciphertext, not plaintext
    blob = mgr._store[("user1", "mock")]
    assert b"abc123" not in blob
    loaded = mgr.load_token("user1", "mock")
    assert loaded.access_token == "abc123"
    assert loaded.refresh_token == "r0"


# --- get_token behaviour -----------------------------------------------------

@pytest.mark.asyncio
async def test_get_token_returns_cached_valid_token():
    mgr = _manager()
    prov = _MockProvider()
    mgr.register(prov)
    mgr.store_token("u", "mock", OAuthToken(access_token="good", refresh_token="r0", expires_at=time.time() + 3600))
    token = await mgr.get_token("u", "mock")
    assert token.access_token == "good"
    assert prov.refresh_calls == 0  # not refreshed


@pytest.mark.asyncio
async def test_expired_token_triggers_refresh():
    mgr = _manager()
    prov = _MockProvider()
    mgr.register(prov)
    mgr.store_token("u", "mock", OAuthToken(access_token="stale", refresh_token="r0", expires_at=time.time() - 10))
    token = await mgr.get_token("u", "mock")
    assert prov.refresh_calls == 1
    assert token.access_token.startswith("refreshed-")
    # refreshed token persisted
    assert mgr.load_token("u", "mock").access_token == token.access_token


@pytest.mark.asyncio
async def test_no_stored_token_raises():
    mgr = _manager()
    mgr.register(_MockProvider())
    with pytest.raises(OAuthError):
        await mgr.get_token("nobody", "mock")


def test_unknown_provider_raises():
    mgr = _manager()
    with pytest.raises(OAuthError):
        mgr.get_provider("ghost")


# --- generic provider (injected http, no live endpoint) ----------------------

@pytest.mark.asyncio
async def test_generic_provider_exchange_and_refresh():
    posts = []

    async def fake_post(url, data):
        posts.append(data)
        if data["grant_type"] == "authorization_code":
            return {"access_token": "AT1", "refresh_token": "RT1", "expires_in": 3600, "token_type": "Bearer"}
        return {"access_token": "AT2", "expires_in": 3600}  # refresh: no new refresh_token

    prov = GenericOAuth2Provider(
        "generic",
        {
            "client_id": "cid",
            "client_secret": "secret",
            "auth_url": "https://auth.example/authorize",
            "token_url": "https://auth.example/token",
            "scopes": ["read", "write"],
            "redirect_uri": "https://app/cb",
        },
        http_post=fake_post,
    )

    tok = await prov.exchange_code("the-code")
    assert tok.access_token == "AT1" and tok.refresh_token == "RT1"
    assert tok.expires_at and tok.expires_at > time.time()

    refreshed = await prov.refresh(tok)
    assert refreshed.access_token == "AT2"
    # refresh token reused since provider didn't rotate it
    assert refreshed.refresh_token == "RT1"
    assert posts[-1]["grant_type"] == "refresh_token"


def test_generic_provider_authorize_url():
    prov = GenericOAuth2Provider(
        "generic",
        {
            "client_id": "cid",
            "auth_url": "https://auth.example/authorize",
            "token_url": "https://auth.example/token",
            "scopes": ["read"],
            "redirect_uri": "https://app/cb",
        },
    )
    url = prov.authorize_url(state="xyz")
    assert url.startswith("https://auth.example/authorize?")
    assert "client_id=cid" in url and "state=xyz" in url and "scope=read" in url


# --- T2.4 review fast-follow: refresh is serialized per (user_id, provider) ----

@pytest.mark.asyncio
async def test_concurrent_get_token_calls_collapse_to_one_refresh():
    """Two coroutines both observe the SAME expired token and both call
    get_token concurrently — without the lock this would fire the provider's
    refresh endpoint twice (and, on a real IdP, burn a single-use
    refresh_token on the second, losing call). With the lock, exactly one
    provider refresh happens; the loser returns the winner's fresh token."""
    mgr = _manager()
    prov = _SlowMockProvider()
    mgr.register(prov)
    mgr.store_token("u", "mock", OAuthToken(access_token="stale", refresh_token="r0", expires_at=time.time() - 10))

    results = await asyncio.gather(
        mgr.get_token("u", "mock"),
        mgr.get_token("u", "mock"),
    )

    assert prov.refresh_calls == 1
    assert results[0].access_token == results[1].access_token == "refreshed-1"
    assert mgr.load_token("u", "mock").access_token == "refreshed-1"


@pytest.mark.asyncio
async def test_concurrent_force_refresh_calls_collapse_to_one_refresh():
    """Same collapse guarantee for force_refresh — the shape the 401-retry
    callback (tools/mcp/oauth_bridge.py::make_auth_refresh_callback) drives,
    reacting to two concurrent 401s on the same (user_id, provider)."""
    mgr = _manager()
    prov = _SlowMockProvider()
    mgr.register(prov)
    # A token that still looks locally VALID (force_refresh must refresh it
    # anyway — the server just said 401 despite our clock thinking it's fine).
    mgr.store_token("u", "mock", OAuthToken(access_token="looks-valid", refresh_token="r0", expires_at=time.time() + 3600))

    results = await asyncio.gather(
        mgr.force_refresh("u", "mock"),
        mgr.force_refresh("u", "mock"),
    )

    assert prov.refresh_calls == 1
    assert results[0].access_token == results[1].access_token == "refreshed-1"


@pytest.mark.asyncio
async def test_concurrent_get_token_and_force_refresh_collapse_to_one_refresh():
    """The two call sites that can race in production (a natural expiry
    refresh via get_token, and a 401-triggered force_refresh) also collapse
    to one provider call when they land on the same (user_id, provider)."""
    mgr = _manager()
    prov = _SlowMockProvider()
    mgr.register(prov)
    mgr.store_token("u", "mock", OAuthToken(access_token="stale", refresh_token="r0", expires_at=time.time() - 10))

    results = await asyncio.gather(
        mgr.get_token("u", "mock"),
        mgr.force_refresh("u", "mock"),
    )

    assert prov.refresh_calls == 1
    assert results[0].access_token == results[1].access_token == "refreshed-1"


@pytest.mark.asyncio
async def test_different_provider_keys_are_not_serialized_against_each_other():
    """The lock is per-(user_id, provider) — unrelated keys must not block
    each other (a global lock would be a correctness AND latency bug)."""
    mgr = _manager()
    prov_a = _SlowMockProvider(delay=0.05)
    prov_b = _SlowMockProvider(delay=0.05)
    mgr.register(prov_a)
    # second provider needs its own `name` to key separately
    prov_b.name = "mock2"
    mgr.register(prov_b)
    mgr.store_token("u", "mock", OAuthToken(access_token="stale-a", refresh_token="ra", expires_at=time.time() - 10))
    mgr.store_token("u", "mock2", OAuthToken(access_token="stale-b", refresh_token="rb", expires_at=time.time() - 10))

    start = time.monotonic()
    await asyncio.gather(
        mgr.get_token("u", "mock"),
        mgr.get_token("u", "mock2"),
    )
    elapsed = time.monotonic() - start

    assert prov_a.refresh_calls == 1
    assert prov_b.refresh_calls == 1
    # Ran concurrently (~0.05s), not serialized end-to-end (~0.1s).
    assert elapsed < 0.09


@pytest.mark.asyncio
async def test_force_refresh_no_stored_token_raises():
    mgr = _manager()
    mgr.register(_MockProvider())
    with pytest.raises(OAuthError):
        await mgr.force_refresh("nobody", "mock")


# --- T2.4 review fast-follow: token-endpoint error responses are sanitized -----

@pytest.mark.asyncio
async def test_missing_access_token_error_never_leaks_response_values():
    async def fake_post(url, data):
        return {
            "error": "invalid_grant",
            "error_description": "the refresh token is expired",
            "client_secret_echo": "SUPER-SECRET-DO-NOT-LEAK",
            "partial_token": "eyJhbGciOiJIUzI1NiJ9.leaked",
        }

    prov = GenericOAuth2Provider(
        "generic",
        {
            "client_id": "cid",
            "client_secret": "secret",
            "auth_url": "https://auth.example/authorize",
            "token_url": "https://auth.example/token",
        },
        http_post=fake_post,
    )

    with pytest.raises(OAuthError) as exc_info:
        await prov.exchange_code("the-code")

    msg = str(exc_info.value)
    # allowlisted, human-readable OAuth2 fields ARE surfaced
    assert "invalid_grant" in msg
    assert "the refresh token is expired" in msg
    # every other value is NEVER surfaced — only key names
    assert "SUPER-SECRET-DO-NOT-LEAK" not in msg
    assert "eyJhbGciOiJIUzI1NiJ9.leaked" not in msg
    assert "client_secret_echo" in msg  # key name is fine
    assert "partial_token" in msg


@pytest.mark.asyncio
async def test_missing_access_token_with_no_error_fields_still_safe():
    async def fake_post(url, data):
        return {"unexpected_field": "some-value-that-must-not-leak"}

    prov = GenericOAuth2Provider(
        "generic",
        {
            "client_id": "cid",
            "auth_url": "https://auth.example/authorize",
            "token_url": "https://auth.example/token",
        },
        http_post=fake_post,
    )

    with pytest.raises(OAuthError) as exc_info:
        await prov.exchange_code("the-code")

    msg = str(exc_info.value)
    assert "some-value-that-must-not-leak" not in msg
    assert "unexpected_field" in msg
    assert "no access_token" in msg
