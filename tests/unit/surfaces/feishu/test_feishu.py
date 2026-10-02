"""064 W1 order 0001 — the Feishu / Lark text surface, offline.

Every inbound below is a RECORDED ``im.message.receive_v1`` event shape (schema
2.0, as the long connection hands it over); nothing opens a socket, calls the
platform or needs the lark-oapi SDK.
"""
import asyncio
import json
import os
import types

import pytest

from core.surfaces.access import AccessTier, resolve_access_tier
from core.surfaces.envelopes import OutboundMessage
from surfaces.feishu.events import parse_event

BOT = "ou_bot0000000000000000000000000000"
OWNER = "ou_owner000000000000000000000000"
STRANGER = "ou_strngr00000000000000000000000"


def _event(*, text="hello", chat_type="p2p", sender=OWNER, sender_type="user",
           mentions=None, message_type="text", message_id="om_1", chat_id="oc_dm1",
           content=None):
    return {
        "schema": "2.0",
        "header": {"event_id": "ev_1", "event_type": "im.message.receive_v1",
                   "create_time": "1727000000000", "app_id": "cli_test",
                   "tenant_key": "tk"},
        "event": {
            "sender": {"sender_id": {"open_id": sender, "union_id": "on_x",
                                     "user_id": "u1"},
                       "sender_type": sender_type, "tenant_key": "tk"},
            "message": {
                "message_id": message_id, "chat_id": chat_id, "chat_type": chat_type,
                "message_type": message_type, "create_time": "1727000000000",
                "content": content if content is not None else json.dumps({"text": text}),
                "mentions": mentions or [],
            },
        },
    }


def _mention(key, open_id, name):
    return {"key": key, "id": {"open_id": open_id, "union_id": "on_m", "user_id": "m"},
            "name": name, "tenant_key": "tk"}


# --- parse -------------------------------------------------------------------

def test_dm_parses_as_dm_with_the_sender_open_id():
    inbound = parse_event(_event(), BOT)
    assert inbound.text == "hello"
    src = inbound.identity.source
    assert (src.surface_id, src.chat_type, src.chat_id) == ("feishu", "dm", "oc_dm1")
    assert inbound.identity.raw_user_id == OWNER
    assert inbound.identity.user_id == f"u_feishu_{OWNER}"
    assert inbound.idempotency_key == "om_1"


def test_group_message_without_the_bot_mention_is_dropped():
    ev = _event(chat_type="group", chat_id="oc_grp", text="@_user_1 hi",
                mentions=[_mention("@_user_1", "ou_someone_else", "Ann")])
    assert parse_event(ev, BOT) is None
    assert parse_event(_event(chat_type="group", chat_id="oc_grp"), BOT) is None


def test_group_message_that_mentions_the_bot_routes_with_the_key_stripped():
    ev = _event(chat_type="group", chat_id="oc_grp", text="@_user_1 status @_user_2",
                mentions=[_mention("@_user_1", BOT, "Rob"),
                          _mention("@_user_2", "ou_ann", "Ann")])
    inbound = parse_event(ev, BOT)
    assert inbound is not None
    assert inbound.identity.source.chat_type == "group"
    assert inbound.mentions_bot is True
    assert inbound.text == "status @Ann"


def test_a_group_mention_is_refused_when_the_bot_id_is_unknown():
    """Fail closed: with no bot open_id there is no proof the bot was addressed."""
    ev = _event(chat_type="group", text="@_user_1 hi",
                mentions=[_mention("@_user_1", BOT, "Rob")])
    assert parse_event(ev, "") is None


def test_bots_non_text_and_malformed_events_are_ignored():
    assert parse_event(_event(sender_type="app"), BOT) is None
    assert parse_event(_event(sender=BOT), BOT) is None
    # an image is media since order 0004; a sticker is still not read
    assert parse_event(_event(message_type="sticker", content='{"file_key":"k"}'), BOT) is None
    assert parse_event(_event(content="not json"), BOT) is None
    assert parse_event(_event(text="   "), BOT) is None
    assert parse_event({"event": "junk"}, BOT) is None
    ev = _event()
    ev["header"]["event_type"] = "im.chat.member.bot.added_v1"
    assert parse_event(ev, BOT) is None


def test_the_user_directory_supplies_the_stable_id():
    class _Dir:
        def resolve_internal(self, raw, surface):
            assert surface == "feishu"
            return f"u_hash_{raw[-4:]}"
    inbound = parse_event(_event(), BOT, user_directory=_Dir())
    assert inbound.identity.user_id == f"u_hash_{OWNER[-4:]}"


# --- tier: a DM routes as the paired owner / the correspondent / nobody ---------

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
    inbound = parse_event(_event(), BOT, user_directory=directory)
    store = PairingStore(str(tmp_path / "pairing.db"))
    assert store.approve(store.request(inbound.identity.user_id)) == inbound.identity.user_id
    env = {"POLYROB_OWNER_USER_ID": "u_owner"}
    assert resolve_access_tier(_Container(str(tmp_path)), inbound.identity,
                               env=env) == AccessTier.OWNER


def test_a_dm_from_a_bound_correspondent_routes_as_correspondent(tmp_path):
    from core.surfaces.correspondents import CorrespondentRegistry
    reg = CorrespondentRegistry(str(tmp_path / "corr.db"))
    reg.seed(surface="feishu", address=STRANGER, session_id="s1", user_id="u_owner",
             thread_id=None, provenance="owner", require_approval=False)
    inbound = parse_event(_event(sender=STRANGER), BOT)
    env = {"POLYROB_OWNER_USER_ID": "u_owner"}
    assert resolve_access_tier(_Container(str(tmp_path), reg), inbound.identity,
                               env=env) == AccessTier.CORRESPONDENT


def test_an_unknown_dm_sender_is_denied(tmp_path):
    inbound = parse_event(_event(sender=STRANGER), BOT)
    assert resolve_access_tier(_Container(str(tmp_path)), inbound.identity,
                               env={"POLYROB_OWNER_USER_ID": "u_owner"}) == AccessTier.DENIED


def test_feishu_is_never_an_owner_alias_surface():
    """alias_owner stays False: the owner is recognised through a pairing row."""
    from core.instance import owner_surface_alias
    assert owner_surface_alias(OWNER, "feishu",
                               env={"OWNER_FEISHU_ID": OWNER,
                                    "POLYROB_OWNER_USER_ID": "rob"}) is None


# --- the REST client: chunking, receive_id_type, token refresh -----------------

class _FakeHTTP:
    """Records every call; answers from a script keyed on the path."""

    def __init__(self, script=None):
        self.calls = []
        self.script = script or {}

    async def __call__(self, method, path, *, json=None, headers=None, params=None):
        self.calls.append((method, path, json, dict(headers or {}), dict(params or {})))
        answers = self.script.get(path)
        if isinstance(answers, list):
            return answers.pop(0) if len(answers) > 1 else answers[0]
        if answers is not None:
            return answers
        if path.endswith("tenant_access_token/internal"):
            return {"code": 0, "tenant_access_token": f"t-{len(self.calls)}", "expire": 7200}
        return {"code": 0, "data": {"message_id": f"om_{len(self.calls)}"}}


def _client(http):
    from surfaces.feishu.client import FeishuClient
    c = FeishuClient("cli_test", "secret-value", domain="feishu")
    c._request = http
    return c


def test_a_long_reply_is_chunked_at_the_platform_limit():
    from core.surfaces import catalog
    limit = catalog.get("feishu").max_message_chars
    http = _FakeHTTP()
    asyncio.run(_client(http).send_message("oc_dm1", "y" * (2 * limit + 5)))
    sends = [c for c in http.calls if c[1].endswith("/im/v1/messages")]
    assert len(sends) == 3
    texts = [json.loads(c[2]["content"])["text"] for c in sends]
    assert all(len(t) <= limit for t in texts) and "".join(texts) == "y" * (2 * limit + 5)
    assert all(c[2]["msg_type"] == "text" and c[4] == {"receive_id_type": "chat_id"}
               for c in sends)


@pytest.mark.parametrize("target,kind", [("oc_1", "chat_id"), ("ou_1", "open_id"),
                                         ("on_1", "union_id"), ("a@b.cn", "email"),
                                         ("e33ggbyz", "user_id")])
def test_the_receive_id_type_follows_the_address(target, kind):
    from surfaces.feishu.client import receive_id_type
    assert receive_id_type(target) == kind


def test_the_tenant_token_is_cached_and_sent_as_bearer():
    http = _FakeHTTP()
    c = _client(http)
    asyncio.run(c.send_message("oc_1", "a"))
    asyncio.run(c.send_message("oc_1", "b"))
    token_calls = [x for x in http.calls if x[1].endswith("tenant_access_token/internal")]
    assert len(token_calls) == 1
    assert token_calls[0][2] == {"app_id": "cli_test", "app_secret": "secret-value"}
    sends = [x for x in http.calls if x[1].endswith("/im/v1/messages")]
    assert all(x[3]["Authorization"] == "Bearer t-1" for x in sends)


def test_an_invalid_token_answer_refreshes_once_and_retries():
    http = _FakeHTTP({"/open-apis/im/v1/messages": [
        {"code": 99991663, "msg": "Invalid access token"},
        {"code": 0, "data": {"message_id": "om_ok"}}]})
    c = _client(http)
    res = asyncio.run(c.send_message("oc_1", "hi"))
    assert res["message_id"] == "om_ok"
    token_calls = [x for x in http.calls if x[1].endswith("tenant_access_token/internal")]
    assert len(token_calls) == 2


def test_a_platform_error_raises_without_the_secret():
    http = _FakeHTTP({"/open-apis/im/v1/messages": {"code": 230002, "msg": "bot not in chat"}})
    with pytest.raises(RuntimeError) as ei:
        asyncio.run(_client(http).send_message("oc_1", "hi"))
    assert "230002" in str(ei.value) and "secret-value" not in str(ei.value)


def test_lark_is_the_default_host_and_feishu_cn_is_config():
    from surfaces.feishu.client import FeishuClient, api_base
    assert api_base("lark") == api_base("") == api_base(None) == "https://open.larksuite.com"
    assert api_base("feishu") == "https://open.feishu.cn"
    assert FeishuClient("a", "b").base == "https://open.larksuite.com"
    assert FeishuClient("a", "b", domain="FEISHU").base == "https://open.feishu.cn"


# --- surface -------------------------------------------------------------------

class _RecClient:
    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    async def send_message(self, target, text):
        if self.fail:
            raise RuntimeError("boom")
        self.sent.append((target, text))
        return {"message_id": "om_9"}


def test_surface_sends_to_the_chat_in_the_session_key():
    from surfaces.feishu.surface import FeishuSurface
    client = _RecClient()
    res = asyncio.run(FeishuSurface(client).send(OutboundMessage(
        session_key="agent:main:feishu:dm:oc_dm1:u_x", text="hi")))
    assert res.success and client.sent == [("oc_dm1", "hi")]
    assert res.surface_message_id == "om_9"


def test_surface_send_is_fail_open():
    from surfaces.feishu.surface import FeishuSurface
    res = asyncio.run(FeishuSurface(_RecClient(fail=True)).send(OutboundMessage(
        session_key="agent:main:feishu:dm:oc_dm1:u_x", text="hi")))
    assert res.success is False


# --- harness: dedup + deliver back --------------------------------------------

@pytest.mark.asyncio
async def test_harness_routes_once_and_delivers_to_the_chat(tmp_path, monkeypatch):
    from surfaces.feishu.harness import build_feishu_harness

    class _C:
        def __init__(self):
            self._svc = {}
            self.config = types.SimpleNamespace(data_dir=str(tmp_path))

        def get_service(self, name):
            return self._svc.get(name)

        def register_service(self, name, svc):
            self._svc[name] = svc

    container = _C()
    h = build_feishu_harness(container, task_agent=None, app_id="cli_test",
                             app_secret="s", data_dir=str(tmp_path))
    h.bot_open_id = BOT
    sent = []

    async def fake_send(target, text):
        sent.append((target, text))
        return {"message_id": "om_x"}

    monkeypatch.setattr(h._client, "send_message", fake_send)

    async def fake_act(task_agent, result, deliver=None, **kw):
        return "ack!"

    monkeypatch.setattr("surfaces.telegram.harness.act_on_inbound", fake_act)
    await h.handle_event(_event())
    assert sent == [("oc_dm1", "ack!")]
    await h.handle_event(_event())          # the same message_id: a redelivery
    assert len(sent) == 1
    assert container.get_service("feishu_sink") is not None
    assert os.path.exists(tmp_path / "feishu_dedup.db")


# --- launch + probe -------------------------------------------------------------

def _ctx(warns):
    from surfaces._launch import LaunchContext
    return LaunchContext(container=None, task_agent=None, data_dir="/tmp", port=0,
                         warn=warns.append, note=lambda m: None)


def test_launch_skips_without_credentials(monkeypatch):
    from surfaces.feishu.launch import launch
    monkeypatch.delenv("FEISHU_APP_ID", raising=False)
    monkeypatch.delenv("FEISHU_APP_SECRET", raising=False)
    warns = []
    assert asyncio.run(launch(_ctx(warns))) is None
    assert "FEISHU_APP_ID" in warns[0] and "FEISHU_APP_SECRET" in warns[0]


def test_launch_names_the_extra_when_the_sdk_is_missing(monkeypatch):
    from surfaces.feishu import launch as mod
    monkeypatch.setenv("FEISHU_APP_ID", "cli_test")
    monkeypatch.setenv("FEISHU_APP_SECRET", "s")
    monkeypatch.setattr(mod, "extra_available", lambda extra: False)
    warns = []
    assert asyncio.run(mod.launch(_ctx(warns))) is None
    assert "polyrob[feishu]" in warns[0]


def _probe_with(monkeypatch, answers):
    from surfaces.feishu import probe as mod
    calls = []

    async def fake_http_json(method, url, *, headers=None, json=None):
        calls.append((method, url, headers, json))
        return answers.pop(0)

    monkeypatch.setattr(mod, "http_json", fake_http_json)
    return mod, calls


def test_probe_proves_the_app_with_the_tenant_token_read(monkeypatch):
    mod, calls = _probe_with(monkeypatch, [
        (200, {"code": 0, "tenant_access_token": "t-abc", "expire": 7200}, ""),
        (200, {"code": 0, "bot": {"app_name": "Rob", "open_id": BOT}}, "")])
    res = asyncio.run(mod.probe({"FEISHU_APP_ID": "cli_test", "FEISHU_APP_SECRET": "s3cr3t"}))
    assert res.state == "ok" and "Rob" in res.detail
    # Lark (international) is the default host; feishu.cn is config.
    assert calls[0][1] == "https://open.larksuite.com/open-apis/auth/v3/tenant_access_token/internal"
    assert calls[1][2]["Authorization"] == "Bearer t-abc"
    assert all(c[0] in ("POST", "GET") for c in calls)
    assert "s3cr3t" not in res.render() and "t-abc" not in res.render()


def test_probe_reports_a_refused_secret_as_failed(monkeypatch):
    mod, _ = _probe_with(monkeypatch, [(200, {"code": 10014, "msg": "app secret invalid"}, "")])
    res = asyncio.run(mod.probe({"FEISHU_APP_ID": "cli_test", "FEISHU_APP_SECRET": "bad"}))
    assert res.state == "failed" and "10014" in res.detail


def test_probe_is_unavailable_on_a_missing_credential_or_network_fault(monkeypatch):
    mod, _ = _probe_with(monkeypatch, [(None, None, "ClientConnectorError")])
    assert asyncio.run(mod.probe({})).state == "unavailable"
    res = asyncio.run(mod.probe({"FEISHU_APP_ID": "a", "FEISHU_APP_SECRET": "b"}))
    assert res.state == "unavailable" and "ClientConnectorError" in res.detail


def test_probe_uses_the_feishu_cn_host_when_configured(monkeypatch):
    mod, calls = _probe_with(monkeypatch, [(200, {"code": 1, "msg": "x"}, "")])
    asyncio.run(mod.probe({"FEISHU_APP_ID": "a", "FEISHU_APP_SECRET": "b",
                           "FEISHU_DOMAIN": "feishu"}))
    assert calls[0][1].startswith("https://open.feishu.cn/")


# --- the long connection hands events to the main loop without waiting --------

def test_the_ws_event_payload_is_the_plain_dict_parse_reads():
    from surfaces.feishu.ws import event_payload
    header = types.SimpleNamespace(event_id="ev_9", event_type="im.message.receive_v1")
    data = types.SimpleNamespace(header=header, event={"message": {"message_id": "om_9"}})
    payload = event_payload(data)
    assert payload == {"header": {"event_id": "ev_9", "event_type": "im.message.receive_v1"},
                       "event": {"message": {"message_id": "om_9"}}}


def _fake_lark(monkeypatch, *, fail=None):
    """A stand-in for lark-oapi with the two traits the bridge must survive: a
    MODULE-LEVEL loop the client drives, and a ``start()`` that blocks forever
    (connect, then reconnect on a failure, then an endless select)."""
    import sys

    ws_client = types.ModuleType("lark_oapi.ws.client")
    ws_client.loop = None
    seen = {}

    async def _forever():
        while True:
            await asyncio.sleep(3600)

    class Client:
        def __init__(self, app_id, app_secret, *, event_handler, domain, log_level):
            seen.update(app_id=app_id, domain=domain)
            self._handler = event_handler

        def start(self):
            loop = ws_client.loop
            seen["thread_loop"] = loop
            if fail is not None:
                raise fail
            header = types.SimpleNamespace(event_id="ev_1", event_type="im.message.receive_v1")
            self._handler(types.SimpleNamespace(header=header, event=_event()["event"]))
            try:
                loop.run_until_complete(_forever())      # the SDK's "connect"
            except RuntimeError:
                pass                                     # … its reconnect branch swallows it
            loop.run_until_complete(_forever())          # … and selects forever

    class _Builder:
        def register_p2_customized_event(self, event_type, fn):
            seen.setdefault("event_type", event_type)
            seen.setdefault("event_types", []).append(event_type)
            self._fn = fn
            return self

        def build(self):
            return self._fn

    lark = types.ModuleType("lark_oapi")
    lark.EventDispatcherHandler = types.SimpleNamespace(builder=lambda *a: _Builder())
    lark.LogLevel = types.SimpleNamespace(WARNING=30)
    lark.ws = types.ModuleType("lark_oapi.ws")
    lark.ws.Client, lark.ws.client = Client, ws_client
    monkeypatch.setitem(sys.modules, "lark_oapi", lark)
    monkeypatch.setitem(sys.modules, "lark_oapi.ws", lark.ws)
    monkeypatch.setitem(sys.modules, "lark_oapi.ws.client", ws_client)
    return seen


@pytest.mark.asyncio
async def test_the_ws_bridge_hands_events_to_the_main_loop_and_stops(monkeypatch):
    from surfaces.feishu.ws import FeishuLongConnection
    seen = _fake_lark(monkeypatch)
    main_loop = asyncio.get_running_loop()
    got = asyncio.Event()
    handled = []

    async def handler(payload):
        handled.append((payload, asyncio.get_running_loop()))
        got.set()

    conn = FeishuLongConnection("cli_test", "s", domain="https://open.feishu.cn")
    runner = asyncio.create_task(conn.run(handler))
    await asyncio.wait_for(got.wait(), 10)
    payload, loop = handled[0]
    assert loop is main_loop and seen["thread_loop"] is not main_loop
    assert parse_event(payload, BOT).text == "hello"
    assert seen["event_type"] == "im.message.receive_v1"
    assert seen["event_types"] == ["im.message.receive_v1", "card.action.trigger"]
    await conn.stop()
    await asyncio.wait_for(runner, 10)          # the swallowed first stop is not the last
    assert runner.exception() is None


@pytest.mark.asyncio
async def test_a_refused_long_connection_names_the_platform_code(monkeypatch):
    from surfaces.feishu.ws import FeishuLongConnection

    class ClientException(Exception):
        def __init__(self, code, msg):
            super().__init__(msg)
            self.code = code

    _fake_lark(monkeypatch, fail=ClientException(1000040350, "exceed conn limit"))

    async def handler(payload):
        pass

    with pytest.raises(RuntimeError) as ei:
        await asyncio.wait_for(FeishuLongConnection("a", "s3cr3t", domain="d").run(handler), 10)
    assert "1000040350" in str(ei.value) and "s3cr3t" not in str(ei.value)
