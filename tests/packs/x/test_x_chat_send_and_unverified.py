"""X Chat: unverified-sender reads, key-map retry, and the encrypted send (no network).

2026-10-04: X moved DMs to encrypted X Chat. The owner's key record carried no
identity_public_key_signature, so chatxdk refused the whole thread; and the
agent's only send was the obsolete plaintext endpoint.
"""
import pytest

pytest.importorskip("polyrob_x")

import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from polyrob_x.x_chat_client import (XChatAPIError, XChatClient,
                                     XChatRecipientUnavailable)

ME, PEER = "100", "200"


class _Response:
    def __init__(self, payload, status=200):
        self.payload, self.status = payload, status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return None

    async def json(self):
        return self.payload

    async def text(self):
        return str(self.payload)


class _Session:
    """Routes by URL suffix; records every call."""

    def __init__(self, routes):
        self.routes, self.calls, self.posts = routes, [], []

    def _match(self, url):
        for suffix, resp in self.routes.items():
            if url.endswith(suffix):
                return resp() if callable(resp) else resp
        raise AssertionError(f"unrouted {url}")

    def get(self, url, params=None):
        self.calls.append(url)
        return self._match(url)

    def post(self, url, json=None):
        self.posts.append((url, json))
        return self._match(url)


def _key(version="7", signature="sig"):
    row = {"public_key_version": version, "public_key": "identity",
           "signing_public_key": "signing"}
    if signature is not None:
        row["identity_public_key_signature"] = signature
    return row


class _Chat:
    """A chatxdk stand-in that records what the client hands it."""
    instances = []

    def __init__(self, config=None):
        self.signing = None
        self.reject_unverified = True
        self.batch_errors = {}
        self.encrypted = None
        _Chat.instances.append(self)

    def unlock(self, pin, /):
        pass

    def set_identity(self, uid, version):
        self.identity = (uid, version)

    def set_cache_keys(self, on):
        pass

    def set_reject_unverified(self, reject):
        self.reject_unverified = reject

    def set_signing_keys(self, keys):
        for k in keys:
            for field, value in k.items():
                assert isinstance(value, str), f"{field} must be a str for chatxdk"
        self.signing = keys

    def decrypt_events(self, events):
        msgs = [{"event": {"type": "Message", "sender_id": PEER, "verified": True,
                           "content": {"text": f"ok:{e}"}}, "original_b64": e}
                for i, e in enumerate(events)
                if str(i) not in self.batch_errors and e.startswith("msg")]
        return {"messages": msgs, "errors": dict(self.batch_errors),
                "conversation_keys": {"keys": {}, "latest_version": ""}}

    def extract_conversation_keys(self, events):
        return {"keys": {"5": b"k5", "11": b"k11"}, "latest_version": "11"}

    def decrypt_event(self, event, keys):
        assert keys, "retry must pass the extracted key map"
        return {"type": "Message", "sender_id": PEER, "verified": False,
                "content": {"text": f"retried:{event}"}}

    def encrypt_message(self, cid, text, *, conversation_key=None,
                        conversation_key_version=None):
        self.encrypted = (cid, text, conversation_key, conversation_key_version)
        return SimpleNamespace(message_id="m-1", encrypted_content="ENC",
                               encoded_event_signature="SIG")

    def verify_key_binding(self, identity, signing, signature):
        return signature == "sig"

    def prepare_conversation_key_change(self, inputs):
        self.prepared_inputs = inputs
        return {"conversation_id": f"{ME}:{PEER}", "conversation_key": b"fresh",
                "conversation_key_version": "99",
                "participant_keys": [{"user_id": PEER, "encrypted_key": "EK",
                                      "public_key_version": "7"}],
                "action_signatures": [{"message_id": "a1",
                                       "encoded_message_event_detail": "D",
                                       "signature": "S", "signature_version": "1",
                                       "public_key_version": "7"}]}

    def get_public_keys(self):
        return SimpleNamespace(signing="MY-SIGNING", identity="i", version="7")


@pytest.fixture
def chat(monkeypatch):
    _Chat.instances.clear()
    monkeypatch.setitem(sys.modules, "chat_xdk", SimpleNamespace(Chat=_Chat))
    return _Chat


def _client(routes):
    session = _Session(routes)
    client = XChatClient("tok", session=session, private_keys_b64="",
                         passphrase="pin")
    return client, session


def _common(peer_key=None, events=None, key_events=("kc-1",)):
    peer_key = peer_key if peer_key is not None else _key(signature=None)
    return {
        "/users/me": _Response({"data": {"id": ME}}),
        f"/users/{ME}/public_keys": lambda: _Response({"data": [
            dict(_key(), juicebox_config={"c": 1})]}),
        f"/users/{PEER}/public_keys": lambda: _Response({"data": [peer_key]}),
        "/events": lambda: _Response({
            "data": events if events is not None else [
                {"id": "e1", "sender_id": PEER, "encoded_event": "msg-1",
                 "conversation_id": f"{PEER}:{ME}", "created_at": "t"}],
            "meta": {"conversation_key_events": list(key_events)}}),
        "/messages": _Response({"data": {"ok": True}}),
        "/keys": _Response({"data": {"conversation_id": f"{ME}-{PEER}"}}),
    }


@pytest.mark.asyncio
async def test_unsigned_key_row_is_passed_as_strings_and_unverified_allowed(chat):
    client, _ = _client(_common())
    result = await client.read_conversation(f"{PEER}-{ME}", participant_ids=[PEER])
    sdk = chat.instances[0]
    peer_rows = [k for k in sdk.signing if k["user_id"] == PEER]
    assert peer_rows[0]["identity_public_key_signature"] == ""
    assert sdk.reject_unverified is False
    assert result["decryption"]["status"] == "ok"


@pytest.mark.asyncio
async def test_batch_failure_is_retried_with_extracted_keys_and_marked(chat):
    orig = _Chat.__init__

    def _init(self, config=None):
        orig(self, config)
        self.batch_errors = {"1": "no matching key"}

    chat.__init__ = _init
    try:
        client, _ = _client(_common())
        result = await client.read_conversation(f"{PEER}-{ME}", participant_ids=[PEER])
    finally:
        chat.__init__ = orig
    assert result["decryption"]["status"] == "ok"
    assert result["decryption"]["errors"] == {}
    texts = [m["event"]["content"]["text"] for m in result["messages"]]
    assert "retried:msg-1" in texts
    retried = [m for m in result["messages"] if "retried" in m["event"]["content"]["text"]]
    assert "UNVERIFIED" in retried[0]["trust"]
    # ciphertext is not handed back once decrypted
    assert all("encoded_event" not in e for e in result["events"])


@pytest.mark.asyncio
async def test_send_into_existing_thread_uses_newest_key(chat):
    orig = _Chat.__init__

    def _init(self, config=None):
        orig(self, config)
        self.batch_errors = {"1": "no matching key"}

    chat.__init__ = _init
    try:
        client, session = _client(_common())
        sent = await client.send_message(PEER, "hello")
    finally:
        chat.__init__ = orig
    sdk = chat.instances[0]
    assert sdk.encrypted == (f"{PEER}:{ME}", "hello", b"k11", "11")
    url, body = session.posts[-1]
    assert url.endswith(f"/chat/conversations/{PEER}-{ME}/messages")
    assert body == {"message_id": "m-1", "encoded_message_create_event": "ENC",
                    "encoded_message_event_signature": "SIG"}
    assert sent["new_conversation"] is False
    assert not any(u.endswith("/keys") for u, _ in session.posts)


@pytest.mark.asyncio
async def test_first_contact_registers_a_conversation_key(chat):
    routes = _common(peer_key=_key(signature="sig"), events=[], key_events=())
    client, session = _client(routes)
    sent = await client.send_message(PEER, "hi")
    keys_url, keys_body = session.posts[0]
    assert keys_url.endswith(f"/chat/conversations/{PEER}/keys")
    assert keys_body["conversation_participant_keys"] == [
        {"user_id": PEER, "encrypted_conversation_key": "EK", "public_key_version": "7"}]
    sig = keys_body["action_signatures"][0]
    assert sig["message_event_signature"]["signing_public_key"] == "MY-SIGNING"
    assert chat.instances[0].encrypted[2:] == (b"fresh", "99")
    assert session.posts[1][0].endswith(f"/chat/conversations/{ME}-{PEER}/messages")
    assert sent["new_conversation"] is True


@pytest.mark.asyncio
async def test_first_contact_refuses_an_unverifiable_recipient_key(chat):
    routes = _common(peer_key=_key(signature=None), events=[], key_events=())
    client, session = _client(routes)
    with pytest.raises(XChatRecipientUnavailable, match="cannot be verified"):
        await client.send_message(PEER, "hi")
    assert session.posts == []


@pytest.mark.asyncio
async def test_existing_thread_without_a_readable_key_is_not_rotated(chat):
    def _no_keys(self, events):
        return {"keys": {}, "latest_version": ""}

    orig_init, orig_extract = _Chat.__init__, _Chat.extract_conversation_keys

    def _init(self, config=None):
        orig_init(self, config)
        self.batch_errors = {"1": "no matching key"}

    chat.__init__, chat.extract_conversation_keys = _init, _no_keys
    try:
        client, session = _client(_common())
        with pytest.raises(XChatAPIError, match="refusing to rotate"):
            await client.send_message(PEER, "hi")
    finally:
        chat.__init__, chat.extract_conversation_keys = orig_init, orig_extract
    assert session.posts == []


# --- the tool ---------------------------------------------------------------

def _tool(monkeypatch):
    import logging
    from tools.base_tool import ToolStatus
    from polyrob_x.twitter_tool import TwitterTool
    monkeypatch.setenv("TWITTER_ENABLED", "true")
    monkeypatch.setenv("TWITTER_REQUIRE_APPROVAL", "false")
    t = object.__new__(TwitterTool)
    t.logger = logging.getLogger("tw-chat-send")
    t.name = "twitter"
    t._status = ToolStatus.HEALTHY
    t._error_message = None
    t._enabled = True
    t._initialized = True
    t._container = MagicMock()
    t._services = {}
    t.client = MagicMock()
    t.dm_client = MagicMock()
    t.api_v1 = MagicMock()
    t._write_times = []
    t._dm_times = []
    t.oauth2_access_token = "tok"
    t._oauth1_available = True
    monkeypatch.setattr(TwitterTool, "_ensure_oauth2_fresh",
                        lambda self, force_refresh=False: None)
    t.chat_client = MagicMock()
    t.chat_client.can_decrypt = True
    return t


@pytest.mark.asyncio
async def test_twitter_dm_sends_through_x_chat(monkeypatch):
    from polyrob_x.twitter_tool import TwitterDMAction
    t = _tool(monkeypatch)
    t.chat_client.send_message = AsyncMock(return_value={
        "conversation_id": "1-2", "message_id": "m-9", "key_version": "3",
        "new_conversation": False})
    res = await t.twitter_dm(TwitterDMAction(recipient="123456", text="hi"))
    assert res.error is None, res.error
    assert "encrypted X Chat" in res.extracted_content
    t.chat_client.send_message.assert_awaited_once_with("123456", "hi")
    t.dm_client.create_direct_message.assert_not_called()
    t.client.create_direct_message.assert_not_called()


@pytest.mark.asyncio
async def test_twitter_dm_falls_back_only_for_an_unreachable_recipient(monkeypatch):
    from polyrob_x.twitter_tool import TwitterDMAction
    t = _tool(monkeypatch)
    t.chat_client.send_message = AsyncMock(
        side_effect=XChatRecipientUnavailable("user 123456 has no registered X Chat keys"))
    t.dm_client.create_direct_message.return_value = MagicMock(data={})
    res = await t.twitter_dm(TwitterDMAction(recipient="123456", text="hi", allow_plaintext=True))
    assert res.error is None, res.error
    assert "plaintext DM endpoint" in res.extracted_content
    assert "no registered X Chat keys" in res.extracted_content


@pytest.mark.asyncio
async def test_twitter_dm_cold_open_400_goes_plaintext(monkeypatch):
    """2026-10-04: X Chat 400'd every cold first contact; the plaintext endpoint
    still delivers them (the Sep 2026 cold opens all went out there)."""
    from polyrob_x.twitter_tool import TwitterDMAction
    t = _tool(monkeypatch)
    t.chat_client.send_message = AsyncMock(side_effect=XChatAPIError(
        "X Chat API 400: One or more parameters to your request was invalid.", 400))
    t.dm_client.create_direct_message.return_value = MagicMock(data={})
    res = await t.twitter_dm(TwitterDMAction(recipient="123456", text="hi", allow_plaintext=True))
    assert res.error is None, res.error
    assert "plaintext DM endpoint" in res.extracted_content
    assert t.dm_client.create_direct_message.call_args.kwargs["participant_id"] == "123456"


@pytest.mark.asyncio
async def test_twitter_dm_chat_error_never_silently_goes_plaintext(monkeypatch):
    from polyrob_x.twitter_tool import TwitterDMAction
    t = _tool(monkeypatch)
    t.chat_client.send_message = AsyncMock(side_effect=XChatAPIError("X Chat API 500: boom", 500))
    res = await t.twitter_dm(TwitterDMAction(recipient="123456", text="hi"))
    assert res.error and "500" in res.error
    t.dm_client.create_direct_message.assert_not_called()
    t.client.create_direct_message.assert_not_called()


@pytest.mark.asyncio
async def test_encrypted_send_never_downgrades_by_default(monkeypatch):
    from polyrob_x.twitter_tool import TwitterDMAction
    t = _tool(monkeypatch)
    t.chat_client.send_message = AsyncMock(side_effect=XChatAPIError("refused", 400))
    result = await t.twitter_dm(TwitterDMAction(recipient="123456", text="private"))
    assert result.error and "allow_plaintext=true" in result.error
    t.dm_client.create_direct_message.assert_not_called()
    t.client.create_direct_message.assert_not_called()
