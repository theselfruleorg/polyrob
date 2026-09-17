"""Encrypted X Chat API client contract tests (no network)."""
import sys
from types import SimpleNamespace

import pytest

from surfaces.x.chat_client import XChatAPIError, XChatClient


class _Response:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def json(self):
        return self.payload

    async def text(self):
        return str(self.payload)


class _Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, params=None):
        self.calls.append((url, params))
        return self.responses.pop(0)


@pytest.mark.asyncio
async def test_lists_chat_inbox_with_participant_expansion():
    session = _Session([_Response({
        "data": [{"id": "1-2", "participant_ids": ["1", "2"]}],
        "meta": {"next_token": "n"},
    })])
    client = XChatClient("user-token", session=session)

    result = await client.get_conversations(max_results=25)

    assert result["data"][0]["id"] == "1-2"
    url, params = session.calls[0]
    assert url.endswith("/2/chat/conversations")
    assert params["max_results"] == 25
    assert "participant_ids" in params["expansions"]


@pytest.mark.asyncio
async def test_raw_chat_events_are_never_presented_as_plaintext():
    session = _Session([_Response({
        "data": [{"id": "e1", "encoded_event": "ciphertext", "sender_id": "2"}],
        "meta": {"next_token": "next", "conversation_key_events": ["key-event"]},
    })])
    client = XChatClient("user-token", session=session,
                         private_keys_b64="", passphrase="")

    result = await client.read_conversation("1:2")

    assert result["events"][0]["encoded_event"] == "ciphertext"
    assert result["decryption"]["status"] == "not_configured"
    assert "messages" not in result
    assert session.calls[0][0].endswith("/chat/conversations/1-2/events")


@pytest.mark.asyncio
async def test_chat_http_error_is_explicit():
    session = _Session([_Response({"detail": "missing dm.read"}, status=403)])
    client = XChatClient("user-token", session=session)

    with pytest.raises(XChatAPIError, match="403.*dm.read"):
        await client.get_conversations()


@pytest.mark.asyncio
async def test_key_blob_decrypts_key_changes_and_messages(monkeypatch):
    calls = {}

    class _Chat:
        def __init__(self):
            pass

        def import_keys(self, blob, version, /):
            calls["import"] = (blob, version)

        def set_identity(self, user_id, version):
            calls["identity"] = (user_id, version)

        def set_cache_keys(self, enabled):
            calls["cache"] = enabled

        def set_signing_keys(self, keys):
            calls["signing"] = keys

        def decrypt_events(self, events):
            calls["events"] = events
            return {"messages": [{"event": {"type": "Message", "content": {
                "content_type": "Text", "text": "hello"}}}], "errors": {}}

    monkeypatch.setitem(sys.modules, "chat_xdk", SimpleNamespace(Chat=_Chat))
    key_record = {"public_key_version": "7", "public_key": "identity",
                  "signing_public_key": "signing",
                  "identity_public_key_signature": "signature"}
    session = _Session([
        _Response({"data": [{"encoded_event": "message"}],
                   "meta": {"conversation_key_events": ["key-change"]}}),
        _Response({"data": {"id": "1"}}),
        _Response({"data": [key_record]}),
        _Response({"data": [key_record]}),
        _Response({"data": [key_record]}),
    ])
    client = XChatClient("user-token", session=session,
                         private_keys_b64="c2VjcmV0", key_version="7")

    result = await client.read_conversation("1-2", participant_ids=["2"])

    assert result["decryption"]["status"] == "ok"
    assert result["messages"][0]["event"]["content"]["text"] == "hello"
    assert calls["import"] == (b"secret", "7")
    assert calls["events"] == ["key-change", "message"]
