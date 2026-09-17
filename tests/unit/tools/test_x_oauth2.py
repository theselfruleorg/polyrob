"""X OAuth 2.0 user token: encrypted store + auto-refresh (2026-09-17).

The X Chat DM read needs a user-context OAuth2 token that X expires after two
hours. The tree used to read ONE static env value and never refreshed it, so a
hand-minted token proved the rail once and then died. These tests pin the
resolver order (store → env seed → static env), the refresh-before-expiry
skew, refresh-token ROTATION, and that a failed refresh keeps the old pair.
"""
import json
import time

import httpx
from cryptography.fernet import Fernet
import pytest

from tools import x_oauth2 as xo


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.delenv("TWITTER_OAUTH2_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("TWITTER_OAUTH2_REFRESH_TOKEN", raising=False)
    monkeypatch.setenv("TWITTER_OAUTH2_CLIENT_ID", "cid123")
    monkeypatch.delenv("TWITTER_OAUTH2_CLIENT_SECRET", raising=False)
    monkeypatch.setattr(xo, "_instance_key", lambda: "dangerob")
    return xo.XOAuth2Store(tmp_path / "x.json")


def _token_transport(responses):
    """MockTransport that records the form posted and replays `responses`."""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append({"form": dict(httpx.QueryParams(request.content.decode())),
                      "auth": request.headers.get("authorization", "")})
        status, body = responses.pop(0)
        return httpx.Response(status, json=body)
    return httpx.MockTransport(handler), calls


def test_store_roundtrip_is_encrypted_on_disk(store, tmp_path):
    xo.import_pair("acc-1", "ref-1", store=store)
    raw = (tmp_path / "x.json").read_text()
    assert "acc-1" not in raw and "ref-1" not in raw
    rec = store.load()
    assert rec["access_token"] == "acc-1" and rec["refresh_token"] == "ref-1"
    assert rec["expires_at"] > time.time() + 7000  # fresh 7200 s assumed


def test_resolve_returns_stored_token_when_not_near_expiry(store):
    xo.import_pair("acc-1", "ref-1", store=store)
    transport, calls = _token_transport([])
    assert xo.resolve_access_token(store=store, transport=transport) == "acc-1"
    assert calls == []  # no network when the token is healthy


def test_resolve_refreshes_within_skew_and_rotates_refresh_token(store):
    xo.import_pair("acc-old", "ref-old", expires_in=60, store=store)  # < REFRESH_SKEW_SEC
    transport, calls = _token_transport([(200, {
        "access_token": "acc-new", "refresh_token": "ref-new",
        "expires_in": 7200, "scope": "dm.read users.read tweet.read offline.access",
        "token_type": "bearer"})])
    tok = xo.resolve_access_token(store=store, transport=transport)
    assert tok == "acc-new"
    assert calls[0]["form"]["grant_type"] == "refresh_token"
    assert calls[0]["form"]["refresh_token"] == "ref-old"
    assert calls[0]["form"]["client_id"] == "cid123"
    assert calls[0]["auth"] == ""  # public app: no Basic header
    rec = store.load()
    assert rec["refresh_token"] == "ref-new" and rec["source"] == "refresh"
    assert rec["expires_at"] > time.time() + 7000


def test_confidential_app_sends_basic_auth(store, monkeypatch):
    monkeypatch.setenv("TWITTER_OAUTH2_CLIENT_SECRET", "shh")
    xo.import_pair("a", "r", expires_in=10, store=store)
    transport, calls = _token_transport([(200, {"access_token": "b", "refresh_token": "r2",
                                                "expires_in": 7200})])
    xo.resolve_access_token(store=store, transport=transport)
    assert calls[0]["auth"].startswith("Basic ")


def test_failed_refresh_keeps_the_old_pair_and_returns_old_token(store):
    xo.import_pair("acc-old", "ref-old", expires_in=10, store=store)
    transport, _ = _token_transport([(400, {"error": "invalid_request",
                                           "error_description": "Value passed for the token was invalid."})])
    tok = xo.resolve_access_token(store=store, transport=transport)
    assert tok == "acc-old"  # honest: the API's 401 is the signal, not a silent swap
    rec = store.load()
    assert rec["refresh_token"] == "ref-old" and rec["source"] == "import"


def test_refresh_without_client_id_names_the_remedy(store, monkeypatch):
    monkeypatch.delenv("TWITTER_OAUTH2_CLIENT_ID", raising=False)
    xo.import_pair("a", "r", store=store)
    with pytest.raises(RuntimeError) as ei:
        xo.refresh(store=store)
    assert "TWITTER_OAUTH2_CLIENT_ID" in str(ei.value)


def test_env_pair_seeds_the_store_once(store, monkeypatch):
    monkeypatch.setenv("TWITTER_OAUTH2_ACCESS_TOKEN", "env-acc")
    monkeypatch.setenv("TWITTER_OAUTH2_REFRESH_TOKEN", "env-ref")
    transport, calls = _token_transport([])
    assert xo.resolve_access_token(store=store, transport=transport) == "env-acc"
    rec = store.load()
    assert rec["source"] == "env" and rec["refresh_token"] == "env-ref"
    # the store now owns it: a changed env value is NOT re-read
    monkeypatch.setenv("TWITTER_OAUTH2_ACCESS_TOKEN", "env-acc-2")
    assert xo.resolve_access_token(store=store, transport=transport) == "env-acc"


def test_static_env_token_without_refresh_is_used_but_not_stored(store, monkeypatch):
    monkeypatch.setenv("TWITTER_OAUTH2_ACCESS_TOKEN", "static-acc")
    transport, _ = _token_transport([])
    assert xo.resolve_access_token(store=store, transport=transport) == "static-acc"
    assert store.load() is None


def test_status_never_exposes_token_values(store):
    xo.import_pair("acc-secret", "ref-secret", store=store)
    st = xo.status(store=store)
    blob = json.dumps(st)
    assert "acc-secret" not in blob and "ref-secret" not in blob
    assert st["stored"] and st["has_refresh_token"] and st["client_id_set"]


def test_pkce_pair_is_s256():
    import base64, hashlib
    v, c = xo.pkce_pair()
    assert 43 <= len(v) <= 128
    assert c == base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).decode().rstrip("=")


def test_exchange_code_persists_pkce_pair(store):
    transport, calls = _token_transport([(200, {"access_token": "A", "refresh_token": "R",
                                                "expires_in": 7200, "scope": "dm.read"})])
    rec = xo.exchange_code("thecode", redirect_uri="http://127.0.0.1:8765/callback",
                           code_verifier="ver", store=store, transport=transport)
    f = calls[0]["form"]
    assert f["grant_type"] == "authorization_code" and f["code"] == "thecode"
    assert f["code_verifier"] == "ver" and f["redirect_uri"] == "http://127.0.0.1:8765/callback"
    assert rec["source"] == "pkce" and store.load()["access_token"] == "A"


# --- the surface client retries ONCE on 401 with a refreshed token -----------

@pytest.mark.asyncio
async def test_xdm_client_refreshes_and_retries_on_401(monkeypatch):
    import tweepy
    from surfaces.x.client import XDMClient

    # A stateful stand-in for the store-backed resolver: a forced refresh
    # rotates the token; a plain resolve returns whatever is current.
    state = {"cur": "tok-old"}

    def _resolve(force_refresh=False):
        if force_refresh:
            state["cur"] = "tok-new"
        return state["cur"]
    monkeypatch.setattr(XDMClient, "_resolve_oauth2", staticmethod(_resolve))
    c = XDMClient()
    assert c._oauth2_access_token == "tok-old"

    class _Resp:
        def __init__(self, uid): self.data = type("D", (), {"id": uid})()

    built = []

    class FakeTweepy:
        def __init__(self, *, bearer_token, wait_on_rate_limit):
            built.append(bearer_token)
            self._bt = bearer_token

        def get_me(self, user_auth=False):
            if self._bt == "tok-old":
                raise tweepy.Unauthorized(httpx.Response(401, request=httpx.Request("GET", "https://x")))
            return _Resp("42")

    # tweepy.Unauthorized wants a requests-like response; build a stand-in.
    class _R:
        status_code = 401
        reason = "Unauthorized"
        text = "{}"
        def json(self): return {}
    monkeypatch.setattr(tweepy.Unauthorized, "__init__",
                        lambda self, *a, **k: Exception.__init__(self, "401"))
    monkeypatch.setattr("tweepy.Client", FakeTweepy)
    uid = await c.get_me()
    assert uid == "42"
    assert built == ["tok-old", "tok-new"]  # rebuilt exactly once on the fresh token


def test_twitter_tool_picks_up_a_token_imported_after_start(monkeypatch):
    """The tool started with NO OAuth2 token; a later oauth-import must be
    usable on the next DM read without a restart."""
    import logging
    from unittest.mock import MagicMock
    from tools.twitter_tool import TwitterTool
    t = object.__new__(TwitterTool)
    t.logger = logging.getLogger("tw")
    t.oauth2_access_token = None
    t.dm_client = None
    t.chat_client = None
    t.chat_private_keys_b64 = None
    t.chat_key_version = None
    t.chat_passphrase = None
    monkeypatch.setattr(TwitterTool, "_resolve_oauth2_token",
                        staticmethod(lambda force_refresh=False: "late-token"))
    monkeypatch.setattr("tweepy.Client", lambda **kw: MagicMock(bt=kw["bearer_token"]))
    t._ensure_oauth2_fresh()
    assert t.oauth2_access_token == "late-token"
    assert t.dm_client is not None and t.chat_client is not None
