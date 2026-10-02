"""064 W1 order 0007 — the DingTalk text surface, offline.

Every inbound below is a RECORDED Stream Mode frame / bot-message callback
shape; nothing opens a socket or calls the platform.
"""
import asyncio
import json
import os
import time
import types

import pytest

from core.surfaces.access import AccessTier, resolve_access_tier
from core.surfaces.envelopes import OutboundMessage
from surfaces.dingtalk.events import TOPIC, parse_message

BOT = "$:LWCP_v1:$botbotbotbotbot"
OWNER = "0123456789"            # a staff id
STRANGER = "9876543210"
HOOK = "https://oapi.dingtalk.com/robot/sendBySession?session=abc"


def _data(*, text="hello", conv_type="1", staff=OWNER, sender_id="$:LWCP_v1:$user",
          msgtype="text", msg_id="msg_1", conv="cid_dm_1", at=None,
          hook=HOOK, expires_ms=None):
    d = {
        "conversationId": conv, "conversationType": conv_type,
        "senderStaffId": staff, "senderId": sender_id, "senderNick": "Owner",
        "chatbotUserId": BOT, "msgId": msg_id, "msgtype": msgtype,
        "text": {"content": text}, "sessionWebhook": hook,
        "sessionWebhookExpiredTime": expires_ms if expires_ms is not None
        else int((time.time() + 3600) * 1000),
        "createAt": 1727000000000, "robotCode": "ding_app",
    }
    if at is not None:
        d["isInAtList"] = at
    return d


# --- parse -------------------------------------------------------------------

def test_dm_parses_as_dm_with_the_staff_id():
    inbound = parse_message(_data())
    assert inbound.text == "hello"
    src = inbound.identity.source
    assert (src.surface_id, src.chat_type, src.chat_id) == ("dingtalk", "dm", "cid_dm_1")
    assert inbound.identity.raw_user_id == OWNER
    assert inbound.identity.user_id == f"u_dingtalk_{OWNER}"
    assert inbound.idempotency_key == "msg_1"


def test_sender_id_falls_back_when_there_is_no_staff_id():
    inbound = parse_message(_data(staff=None, sender_id="$:LWCP_v1:$ext"))
    assert inbound.identity.raw_user_id == "$:LWCP_v1:$ext"


def test_group_message_without_the_at_flag_is_dropped():
    assert parse_message(_data(conv_type="2", conv="cid_grp")) is None
    assert parse_message(_data(conv_type="2", conv="cid_grp", at=False)) is None


def test_group_message_that_ats_the_bot_routes_as_group():
    inbound = parse_message(_data(conv_type="2", conv="cid_grp", at=True, text=" status "))
    assert inbound is not None
    assert inbound.identity.source.chat_type == "group"
    assert inbound.text == "status"


def test_bot_self_non_text_and_malformed_are_ignored():
    assert parse_message(_data(staff=BOT)) is None
    assert parse_message(_data(staff=None, sender_id=BOT)) is None
    assert parse_message(_data(msgtype="picture")) is None
    assert parse_message(_data(text="   ")) is None
    assert parse_message("junk") is None
    d = _data()
    d["text"] = "not a dict"
    assert parse_message(d) is None


def test_the_user_directory_supplies_the_stable_id():
    class _Dir:
        def resolve_internal(self, raw, surface):
            assert surface == "dingtalk"
            return f"u_hash_{raw[-4:]}"
    assert parse_message(_data(), user_directory=_Dir()).identity.user_id == "u_hash_6789"


# --- tier ----------------------------------------------------------------------

class _Container:
    def __init__(self, data_dir, registry=None):
        self.config = types.SimpleNamespace(data_dir=data_dir)
        self._svc = {"correspondent_registry": registry} if registry else {}

    def get_service(self, name):
        return self._svc.get(name)


def test_a_dm_from_the_paired_owner_routes_as_owner(tmp_path):
    from core.pairing import PairingStore
    from tools.user_directory import UserDirectory
    directory = UserDirectory(str(tmp_path / "users.db"))
    inbound = parse_message(_data(), user_directory=directory)
    store = PairingStore(str(tmp_path / "pairing.db"))
    assert store.approve(store.request(inbound.identity.user_id)) == inbound.identity.user_id
    env = {"POLYROB_OWNER_USER_ID": "u_owner"}
    assert resolve_access_tier(_Container(str(tmp_path)), inbound.identity,
                               env=env) == AccessTier.OWNER


def test_a_dm_from_a_bound_correspondent_routes_as_correspondent(tmp_path):
    from core.surfaces.correspondents import CorrespondentRegistry
    reg = CorrespondentRegistry(str(tmp_path / "corr.db"))
    reg.seed(surface="dingtalk", address=STRANGER, session_id="s1", user_id="u_owner",
             thread_id=None, provenance="owner", require_approval=False)
    inbound = parse_message(_data(staff=STRANGER))
    env = {"POLYROB_OWNER_USER_ID": "u_owner"}
    assert resolve_access_tier(_Container(str(tmp_path), reg), inbound.identity,
                               env=env) == AccessTier.CORRESPONDENT


def test_an_unknown_dm_sender_is_denied(tmp_path):
    inbound = parse_message(_data(staff=STRANGER))
    assert resolve_access_tier(_Container(str(tmp_path)), inbound.identity,
                               env={"POLYROB_OWNER_USER_ID": "u_owner"}) == AccessTier.DENIED


def test_dingtalk_is_never_an_owner_alias_surface():
    from core.instance import owner_surface_alias
    assert owner_surface_alias(OWNER, "dingtalk",
                               env={"OWNER_DINGTALK_ID": OWNER,
                                    "POLYROB_OWNER_USER_ID": "rob"}) is None


# --- the client: token, session webhook, proactive, chunking -------------------

class _FakeHTTP:
    """Records every call; answers from a script keyed on the URL."""

    def __init__(self, script=None):
        self.calls = []
        self.script = script or {}

    async def __call__(self, method, url, *, json=None, headers=None):
        self.calls.append((method, url, json, dict(headers or {})))
        answers = self.script.get(url)
        if isinstance(answers, list):
            return answers.pop(0) if len(answers) > 1 else answers[0]
        if answers is not None:
            return answers
        if url.endswith("/v1.0/oauth2/accessToken"):
            return 200, {"accessToken": f"t-{len(self.calls)}", "expireIn": 7200}
        if url.startswith("https://oapi.dingtalk.com/robot/sendBySession"):
            return 200, {"errcode": 0, "errmsg": "ok"}
        return 200, {"processQueryKey": f"pq_{len(self.calls)}"}


def _client(http):
    from surfaces.dingtalk.client import DingTalkClient
    c = DingTalkClient("ding_app", "secret-value")
    c._request = http
    return c


def _remember(c, chat, *, is_group=False, expires_at=None, user=OWNER, hook=HOOK):
    from surfaces.dingtalk.client import Conversation
    c.conversations.remember(chat, Conversation(
        webhook=hook, expires_at=expires_at if expires_at is not None else time.time() + 3600,
        is_group=is_group, user_id=user))


def test_a_reply_uses_the_live_session_webhook_and_no_token():
    http = _FakeHTTP()
    c = _client(http)
    _remember(c, "cid_dm_1")
    res = asyncio.run(c.send_text("cid_dm_1", "hi"))
    assert res == {"via": "session_webhook"}
    assert len(http.calls) == 1
    method, url, body, headers = http.calls[0]
    assert url == HOOK and body == {"msgtype": "text", "text": {"content": "hi"}}
    assert "x-acs-dingtalk-access-token" not in headers


def test_an_expired_webhook_falls_back_to_the_proactive_dm_api():
    http = _FakeHTTP()
    c = _client(http)
    _remember(c, "cid_dm_1", expires_at=time.time() - 1)
    asyncio.run(c.send_text("cid_dm_1", "hi"))
    urls = [x[1] for x in http.calls]
    assert urls[0].endswith("/v1.0/oauth2/accessToken")
    send = http.calls[1]
    assert send[1] == "https://api.dingtalk.com/v1.0/robot/oToMessages/batchSend"
    assert send[2]["userIds"] == [OWNER] and send[2]["robotCode"] == "ding_app"
    assert json.loads(send[2]["msgParam"]) == {"content": "hi"}
    assert send[3]["x-acs-dingtalk-access-token"] == "t-1"


def test_an_expired_group_webhook_uses_the_group_api():
    http = _FakeHTTP()
    c = _client(http)
    _remember(c, "cid_grp", is_group=True, expires_at=time.time() - 1)
    asyncio.run(c.send_text("cid_grp", "hi"))
    send = http.calls[-1]
    assert send[1] == "https://api.dingtalk.com/v1.0/robot/groupMessages/send"
    assert send[2]["openConversationId"] == "cid_grp"


def test_an_unknown_staff_id_target_is_a_proactive_dm():
    http = _FakeHTTP()
    asyncio.run(_client(http).send_text(OWNER, "owner notice"))
    assert http.calls[-1][1].endswith("/oToMessages/batchSend")
    assert http.calls[-1][2]["userIds"] == [OWNER]


def test_a_session_webhook_off_the_dingtalk_host_is_never_used():
    http = _FakeHTTP()
    c = _client(http)
    _remember(c, "cid_dm_1", hook="https://evil.example/steal")
    asyncio.run(c.send_text("cid_dm_1", "hi"))
    assert all("evil.example" not in x[1] for x in http.calls)
    assert http.calls[-1][1].endswith("/oToMessages/batchSend")
    from surfaces.dingtalk.client import webhook_host_allowed
    assert not webhook_host_allowed("http://oapi.dingtalk.com/x")
    assert not webhook_host_allowed("https://dingtalk.com.evil.io/x")
    assert webhook_host_allowed(HOOK)


def test_a_long_reply_is_chunked_at_the_platform_limit():
    from core.surfaces import catalog
    limit = catalog.get("dingtalk").max_message_chars
    http = _FakeHTTP()
    c = _client(http)
    _remember(c, "cid_dm_1")
    asyncio.run(c.send_text("cid_dm_1", "y" * (2 * limit + 5)))
    texts = [x[2]["text"]["content"] for x in http.calls]
    assert len(texts) == 3 and all(len(t) <= limit for t in texts)
    assert "".join(texts) == "y" * (2 * limit + 5)


def test_the_token_is_cached():
    http = _FakeHTTP()
    c = _client(http)
    asyncio.run(c.send_text(OWNER, "a"))
    asyncio.run(c.send_text(OWNER, "b"))
    token_calls = [x for x in http.calls if x[1].endswith("/oauth2/accessToken")]
    assert len(token_calls) == 1
    assert token_calls[0][2] == {"appKey": "ding_app", "appSecret": "secret-value"}


def test_an_invalid_token_answer_refreshes_once_and_retries():
    url = "https://api.dingtalk.com/v1.0/robot/oToMessages/batchSend"
    http = _FakeHTTP({url: [
        (401, {"code": "InvalidAuthentication", "message": "token invalid"}),
        (200, {"processQueryKey": "pq_ok"})]})
    res = asyncio.run(_client(http).send_text(OWNER, "hi"))
    assert res["processQueryKey"] == "pq_ok"
    assert len([x for x in http.calls if x[1].endswith("/oauth2/accessToken")]) == 2


def test_a_platform_error_raises_without_the_secret_or_token():
    url = "https://api.dingtalk.com/v1.0/robot/oToMessages/batchSend"
    http = _FakeHTTP({url: (400, {"code": "Robot.NotFound", "message": "robot missing"})})
    with pytest.raises(RuntimeError) as ei:
        asyncio.run(_client(http).send_text(OWNER, "hi"))
    assert "Robot.NotFound" in str(ei.value)
    assert "secret-value" not in str(ei.value) and "t-1" not in str(ei.value)


def test_a_refused_webhook_send_raises():
    http = _FakeHTTP({HOOK: (200, {"errcode": 300001, "errmsg": "session expired"})})
    c = _client(http)
    _remember(c, "cid_dm_1")
    with pytest.raises(RuntimeError, match="300001"):
        asyncio.run(c.send_text("cid_dm_1", "hi"))


# --- stream frames ----------------------------------------------------------------

def _frame(ftype, topic, data="{}", mid="m-1"):
    return {"specVersion": "1.0", "type": ftype,
            "headers": {"topic": topic, "messageId": mid, "contentType": "application/json"},
            "data": data}


def test_a_callback_is_acked_with_its_message_id_and_routed():
    from surfaces.dingtalk.stream import handle_frame
    reply, data, disconnect = handle_frame(_frame("CALLBACK", TOPIC, json.dumps(_data())))
    assert reply == {"code": 200, "headers": {"contentType": "application/json",
                                              "messageId": "m-1"},
                     "message": "OK", "data": json.dumps({"response": None})}
    assert data["msgId"] == "msg_1" and disconnect is False


def test_ping_is_echoed_and_disconnect_reconnects():
    from surfaces.dingtalk.stream import handle_frame
    reply, data, disconnect = handle_frame(_frame("SYSTEM", "ping", '{"opaque":"x"}', "p-1"))
    assert reply["data"] == '{"opaque":"x"}' and reply["headers"]["messageId"] == "p-1"
    assert data is None and disconnect is False
    reply, data, disconnect = handle_frame(_frame("SYSTEM", "disconnect"))
    assert reply is None and disconnect is True


def test_other_frames_are_acked_but_never_routed():
    from surfaces.dingtalk.stream import handle_frame
    reply, data, _ = handle_frame(_frame("CALLBACK", "/v1.0/card/instances/callback"))
    assert reply["code"] == 200 and data is None
    reply, data, _ = handle_frame(_frame("CALLBACK", TOPIC, "not json"))
    assert reply["code"] == 200 and data is None
    reply, data, _ = handle_frame(_frame("EVENT", "chat_update"))
    assert json.loads(reply["data"])["status"] == "SUCCESS" and data is None


def test_open_request_and_connect_url():
    from surfaces.dingtalk.stream import connect_url, open_request
    body = open_request("ding_app", "s")
    assert body["subscriptions"] == [{"type": "CALLBACK", "topic": TOPIC}]
    assert connect_url("wss://x/connect", "a b+c") == "wss://x/connect?ticket=a%20b%2Bc"


@pytest.mark.asyncio
async def test_dispatch_acks_first_and_hands_the_callback_to_a_task():
    from surfaces.dingtalk.stream import DingTalkStream
    stream = DingTalkStream("ding_app", "s")
    order, got = [], asyncio.Event()

    async def send(raw):
        order.append(("ack", json.loads(raw)["headers"]["messageId"]))

    async def handler(data):
        order.append(("route", data["msgId"]))
        got.set()

    disconnect = await stream._dispatch(
        json.dumps(_frame("CALLBACK", TOPIC, json.dumps(_data()))), send, handler)
    await asyncio.wait_for(got.wait(), 5)
    assert disconnect is False and order == [("ack", "m-1"), ("route", "msg_1")]


@pytest.mark.asyncio
async def test_a_refused_stream_open_fails_loudly_without_the_secret():
    from surfaces.dingtalk.stream import DingTalkStream

    async def http(url, body):
        return 400, {"code": "InvalidClient", "message": "bad secret"}

    stream = DingTalkStream("ding_app", "s3cr3t", http=http)

    async def handler(data):
        pass

    with pytest.raises(RuntimeError) as ei:
        await asyncio.wait_for(stream.run(handler), 5)
    assert "InvalidClient" in str(ei.value) and "s3cr3t" not in str(ei.value)


# --- surface ---------------------------------------------------------------------

class _RecClient:
    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    async def send_text(self, target, text):
        if self.fail:
            raise RuntimeError("boom")
        self.sent.append((target, text))
        return {"processQueryKey": "pq_9"}


def test_surface_sends_to_the_conversation_in_the_session_key():
    from surfaces.dingtalk.surface import DingTalkSurface
    client = _RecClient()
    res = asyncio.run(DingTalkSurface(client).send(OutboundMessage(
        session_key="agent:main:dingtalk:dm:cid_dm_1:u_x", text="hi")))
    assert res.success and client.sent == [("cid_dm_1", "hi")]
    assert res.surface_message_id == "pq_9"


def test_surface_send_is_fail_open():
    from surfaces.dingtalk.surface import DingTalkSurface
    res = asyncio.run(DingTalkSurface(_RecClient(fail=True)).send(OutboundMessage(
        session_key="agent:main:dingtalk:dm:cid_dm_1:u_x", text="hi")))
    assert res.success is False


def test_the_reply_window_is_a_session_webhook_that_never_blocks_a_send():
    from core.surfaces.send_policy import SendDecision
    from surfaces.dingtalk.surface import DingTalkSurface
    s = DingTalkSurface(_RecClient())
    win = s.capabilities.effective_reply_window()
    assert win.kind == "session_webhook" and win.ttl_s > 0
    assert SendDecision(win.outside) is SendDecision.ALLOW
    assert s.can_send_now("agent:main:dingtalk:dm:cid_dm_1:u_x") is SendDecision.ALLOW


# --- harness: dedup + deliver back through the session webhook ------------------

@pytest.mark.asyncio
async def test_harness_routes_once_and_replies_via_the_session_webhook(tmp_path, monkeypatch):
    from surfaces.dingtalk.harness import build_dingtalk_harness

    class _C:
        def __init__(self):
            self._svc = {}
            self.config = types.SimpleNamespace(data_dir=str(tmp_path))

        def get_service(self, name):
            return self._svc.get(name)

        def register_service(self, name, svc):
            self._svc[name] = svc

    container = _C()
    h = build_dingtalk_harness(container, task_agent=None, client_id="ding_app",
                               client_secret="s", data_dir=str(tmp_path))
    http = _FakeHTTP()
    h._client._request = http

    async def fake_act(task_agent, result, deliver=None, **kw):
        return "ack!"

    monkeypatch.setattr("surfaces.telegram.harness.act_on_inbound", fake_act)
    await h.handle_callback(_data())
    assert [(x[1], x[2]) for x in http.calls] == [
        (HOOK, {"msgtype": "text", "text": {"content": "ack!"}})]
    await h.handle_callback(_data())          # the same msgId: a redelivery
    assert len(http.calls) == 1
    assert container.get_service("dingtalk_sink") is not None
    assert os.path.exists(tmp_path / "dingtalk_dedup.db")


# --- launch + probe ---------------------------------------------------------------

def _ctx(warns):
    from surfaces._launch import LaunchContext
    return LaunchContext(container=None, task_agent=None, data_dir="/tmp", port=0,
                         warn=warns.append, note=lambda m: None)


def test_launch_skips_without_credentials(monkeypatch):
    from surfaces.dingtalk.launch import launch
    monkeypatch.delenv("DINGTALK_CLIENT_ID", raising=False)
    monkeypatch.delenv("DINGTALK_CLIENT_SECRET", raising=False)
    warns = []
    assert asyncio.run(launch(_ctx(warns))) is None
    assert "DINGTALK_CLIENT_ID" in warns[0] and "DINGTALK_CLIENT_SECRET" in warns[0]


def _probe_with(monkeypatch, answers):
    from surfaces.dingtalk import probe as mod
    calls = []

    async def fake_http_json(method, url, *, headers=None, json=None):
        calls.append((method, url, headers, json))
        return answers.pop(0)

    monkeypatch.setattr(mod, "http_json", fake_http_json)
    return mod, calls


def test_probe_proves_the_app_with_the_access_token_read(monkeypatch):
    mod, calls = _probe_with(monkeypatch, [
        (200, {"accessToken": "t-abc", "expireIn": 7200}, "")])
    res = asyncio.run(mod.probe({"DINGTALK_CLIENT_ID": "ding_app",
                                 "DINGTALK_CLIENT_SECRET": "s3cr3t"}))
    assert res.state == "ok"
    assert calls == [("POST", "https://api.dingtalk.com/v1.0/oauth2/accessToken", None,
                      {"appKey": "ding_app", "appSecret": "s3cr3t"})]
    assert "s3cr3t" not in res.render() and "t-abc" not in res.render()


def test_probe_reports_a_refused_secret_as_failed(monkeypatch):
    mod, _ = _probe_with(monkeypatch, [
        (400, {"code": "invalidClientSecret", "message": "bad"}, "")])
    res = asyncio.run(mod.probe({"DINGTALK_CLIENT_ID": "a", "DINGTALK_CLIENT_SECRET": "b"}))
    assert res.state == "failed" and "invalidClientSecret" in res.detail


def test_probe_is_unavailable_on_a_missing_credential_or_network_fault(monkeypatch):
    mod, _ = _probe_with(monkeypatch, [(None, None, "ClientConnectorError")])
    assert asyncio.run(mod.probe({})).state == "unavailable"
    res = asyncio.run(mod.probe({"DINGTALK_CLIENT_ID": "a", "DINGTALK_CLIENT_SECRET": "b"}))
    assert res.state == "unavailable" and "ClientConnectorError" in res.detail
