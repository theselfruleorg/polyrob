"""X DM client must distinguish OAuth2 user tokens from app bearer tokens."""
import pytest

pytest.importorskip("polyrob_x")

from types import SimpleNamespace

import pytest

from polyrob_x.surface.client import XDMClient


class _Client:
    def __init__(self):
        self.calls = []

    def get_direct_message_events(self, **kwargs):
        self.calls.append(("read", kwargs))
        event = SimpleNamespace(data={
            "id": "1", "event_type": "MessageCreate", "text": "inbound",
            "sender_id": "42", "dm_conversation_id": "42-99",
        })
        return SimpleNamespace(data=[event], meta={})

    def create_direct_message(self, **kwargs):
        self.calls.append(("send", kwargs))
        return SimpleNamespace(data={"dm_event_id": "2"})


@pytest.mark.asyncio
async def test_oauth2_user_token_reads_with_bearer_user_context():
    client = XDMClient({"oauth2_access_token": "user-token"})
    fake = _Client()
    client._client = fake
    assert client.has_credentials is True
    assert client.auth_mode == "oauth2_user"
    result = await client.get_dm_events()
    assert result["events"][0]["text"] == "inbound"
    assert fake.calls[0][1]["user_auth"] is False


@pytest.mark.asyncio
async def test_oauth1_user_credentials_remain_supported():
    client = XDMClient({
        "api_key": "k", "api_secret": "s",
        "access_token": "t", "access_token_secret": "ts",
    })
    fake = _Client()
    client._client = fake
    assert client.has_credentials is True
    assert client.auth_mode == "oauth1_user"
    await client.get_dm_events()
    assert fake.calls[0][1]["user_auth"] is True


@pytest.mark.asyncio
async def test_oauth2_user_token_sends_with_same_user_context():
    client = XDMClient({"oauth2_access_token": "user-token"})
    fake = _Client()
    client._client = fake
    result = await client.send_dm("42", "hello")
    assert result["dm_event_id"] == "2"
    assert fake.calls[0] == ("send", {
        "participant_id": "42", "text": "hello", "user_auth": False})


def test_app_bearer_is_not_accepted_as_dm_user_auth(monkeypatch):
    # Isolate from a sibling test (or a loaded .env) that left user-context
    # TWITTER_* values in os.environ — the client reads the env as a fallback.
    for var in ("TWITTER_API_KEY", "TWITTER_API_SECRET_KEY", "TWITTER_ACCESS_TOKEN",
                "TWITTER_ACCESS_TOKEN_SECRET", "TWITTER_OAUTH2_ACCESS_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("TWITTER_BEARER_TOKEN", "app-only")
    client = XDMClient({})
    assert client.has_credentials is False


# --- 401: OAuth1 fallback + back-off instead of a WARNING per poll (P1-2) ------

class _Unauth(Exception):
    pass


def _patch_unauth(monkeypatch):
    import tweepy
    monkeypatch.setattr(tweepy, "Unauthorized", _Unauth)


class _Refusing:
    def __init__(self):
        self.calls = 0

    def get_direct_message_events(self, **kwargs):
        self.calls += 1
        raise _Unauth("401")


@pytest.mark.asyncio
async def test_oauth2_401_falls_back_to_oauth1_keys(monkeypatch):
    _patch_unauth(monkeypatch)
    monkeypatch.setattr(XDMClient, "_resolve_oauth2", staticmethod(lambda force_refresh=False, refresh=True: "dead"))
    client = XDMClient({"api_key": "k", "api_secret": "s",
                        "access_token": "t", "access_token_secret": "ts"})
    assert client.auth_mode == "oauth2_user"
    refusing = _Refusing()
    oauth1 = _Client()
    monkeypatch.setattr(client, "_tweepy",
                        lambda: refusing if client._oauth2_access_token else oauth1)
    result = await client.get_dm_events()
    assert result["events"][0]["text"] == "inbound"
    assert refusing.calls == 1
    assert client.auth_mode == "oauth1_user"
    assert oauth1.calls[0][1]["user_auth"] is True       # re-signed, not the stale False


@pytest.mark.asyncio
async def test_unfixable_401_backs_off_without_calling_x(monkeypatch, caplog):
    import logging
    from polyrob_x.surface.client import XRateLimited, XUnauthorized
    _patch_unauth(monkeypatch)
    for var in ("TWITTER_API_KEY", "TWITTER_API_SECRET_KEY", "TWITTER_ACCESS_TOKEN",
                "TWITTER_ACCESS_TOKEN_SECRET"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(XDMClient, "_resolve_oauth2", staticmethod(lambda force_refresh=False, refresh=True: "dead"))
    client = XDMClient({})
    refusing = _Refusing()
    client._client = refusing
    monkeypatch.setattr(client, "_tweepy", lambda: refusing)
    with caplog.at_level(logging.WARNING, logger="polyrob_x.surface.client"):
        with pytest.raises(XUnauthorized) as ei:
            await client.get_dm_events()
        assert isinstance(ei.value, XRateLimited)           # the poller backs off to reset_at
        first_reset = ei.value.reset_at
        assert first_reset > __import__("time").time() + 200
        assert "/x login" in str(ei.value)
        with pytest.raises(XUnauthorized):
            await client.get_dm_events()                     # inside the back-off: no network
    assert refusing.calls == 1
    assert len([r for r in caplog.records if "refused the login" in r.getMessage()]) == 1
