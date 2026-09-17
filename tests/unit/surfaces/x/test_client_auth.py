"""X DM client must distinguish OAuth2 user tokens from app bearer tokens."""
from types import SimpleNamespace

import pytest

from surfaces.x.client import XDMClient


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
