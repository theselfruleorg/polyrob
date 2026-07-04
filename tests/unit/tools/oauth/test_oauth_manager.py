"""Item 4 — OAuth manager + generic provider (library-only, no live endpoints)."""
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
