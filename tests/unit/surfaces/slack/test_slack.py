"""Wave 4 Task 1 — Slack parse / surface / harness (mocked transports)."""
import asyncio
import types

import pytest

from core.surfaces.envelopes import OutboundMessage
from surfaces.slack.socket_mode import parse_event
from surfaces.slack.surface import SlackSurface, channel_id_from_session_key

BOT_ID = "UBOT"


def _event(**kw):
    e = {"type": "message", "user": "U42", "text": "hello",
         "channel": "C7", "channel_type": "im", "ts": "111.222",
         "client_msg_id": "cm-1"}
    e.update(kw)
    return e


def test_parse_dm():
    inbound = parse_event(_event(), BOT_ID)
    assert inbound.text == "hello"
    assert inbound.identity.source.chat_type == "dm"
    assert inbound.identity.source.chat_id == "C7"
    assert inbound.identity.user_id == "u_slack_U42"
    assert inbound.idempotency_key == "cm-1"
    assert inbound.mentions_bot is False


def test_parse_channel_is_group_and_thread():
    inbound = parse_event(_event(channel_type="channel",
                                 thread_ts="99.1"), BOT_ID)
    assert inbound.identity.source.chat_type == "group"
    assert inbound.identity.source.thread_id == "99.1"


def test_parse_skips_bot_own_subtype_and_nonmessage():
    assert parse_event(_event(bot_id="B1"), BOT_ID) is None
    assert parse_event(_event(user=BOT_ID), BOT_ID) is None
    assert parse_event(_event(subtype="message_changed"), BOT_ID) is None
    assert parse_event({"type": "reaction_added"}, BOT_ID) is None


def test_parse_mention():
    inbound = parse_event(_event(text=f"<@{BOT_ID}> help"), BOT_ID)
    assert inbound.mentions_bot is True


class _FakeClient:
    def __init__(self, fail=False):
        self.sent = []
        self.fail = fail

    async def send_message(self, channel, text, thread_ts=None):
        if self.fail:
            raise RuntimeError("boom")
        self.sent.append((channel, text))
        return {"ts": f"{len(self.sent)}.0"}


def test_surface_send_splits():
    client = _FakeClient()
    surface = SlackSurface(client)
    res = asyncio.run(surface.send(OutboundMessage(
        session_key="agent:main:slack:dm:C7:u_x", text="y" * 9000)))
    assert res.success and len(client.sent) == 3
    assert channel_id_from_session_key("agent:main:slack:dm:C7:u_x") == "C7"


def test_surface_send_fail_open():
    res = asyncio.run(SlackSurface(_FakeClient(fail=True)).send(
        OutboundMessage(session_key="agent:main:slack:dm:C7:u_x", text="hi")))
    assert res.success is False


@pytest.mark.asyncio
async def test_harness_dedup_and_delivery(tmp_path, monkeypatch):
    from surfaces.slack.harness import build_slack_harness

    class _Container:
        def __init__(self):
            self._svc = {}
            self.config = types.SimpleNamespace(data_dir=str(tmp_path))

        def get_service(self, name):
            return self._svc.get(name)

        def register_service(self, name, svc):
            self._svc[name] = svc

    harness = build_slack_harness(_Container(), task_agent=None,
                                  bot_token="xoxb-t", app_token="xapp-t",
                                  data_dir=str(tmp_path))
    harness.bot_user_id = BOT_ID
    sent = []

    async def fake_send(channel, text, thread_ts=None):
        sent.append((channel, text))
        return {"ts": "1.0"}

    monkeypatch.setattr(harness._client, "send_message", fake_send)

    async def fake_act(task_agent, result, deliver=None, **kw):
        return "ack!"

    monkeypatch.setattr("surfaces.telegram.harness.act_on_inbound", fake_act)

    await harness.handle_event(_event())
    assert sent == [("C7", "ack!")]
    await harness.handle_event(_event())  # same client_msg_id -> dedup
    assert len(sent) == 1


# --- finalization hardening: user-target DMs, error hints, socket loop -------


def _client_with_fake_call(recorder):
    from surfaces.slack.client import SlackClient
    client = SlackClient(bot_token="xoxb-t", app_token="xapp-t")

    async def fake_call(method, *, token, json=None):
        recorder.append((method, dict(json or {})))
        if method == "conversations.open":
            return {"ok": True, "channel": {"id": "D999"}}
        return {"ok": True, "ts": "1.0"}

    client._call = fake_call
    return client


def test_send_to_user_id_opens_dm_and_caches():
    """REGRESSION: chat.postMessage accepts conversation ids (C/G/D), NOT a
    bare user id — a message() tool/sink target like U… landed in Slackbot
    (or channel_not_found). The client must conversations.open first."""
    calls = []
    client = _client_with_fake_call(calls)
    asyncio.run(client.send_message("U012AB3CD", "hi"))
    asyncio.run(client.send_message("U012AB3CD", "again"))
    methods = [m for m, _ in calls]
    assert methods == ["conversations.open", "chat.postMessage",
                       "chat.postMessage"]  # cache: opened once
    assert calls[0][1] == {"users": "U012AB3CD"}
    assert calls[1][1]["channel"] == "D999"
    assert calls[2][1]["channel"] == "D999"


def test_send_to_conversation_id_posts_directly():
    calls = []
    client = _client_with_fake_call(calls)
    asyncio.run(client.send_message("C7", "hi"))
    asyncio.run(client.send_message("D42", "hi"))
    assert [m for m, _ in calls] == ["chat.postMessage", "chat.postMessage"]


def test_not_in_channel_error_carries_actionable_hint():
    from surfaces.slack.client import SlackClient
    with pytest.raises(RuntimeError) as ei:
        SlackClient._check_ok("chat.postMessage",
                              {"ok": False, "error": "not_in_channel"})
    assert "not_in_channel" in str(ei.value)
    assert "/invite" in str(ei.value)


class _FakeWS:
    """Async-iterable WS stub: yields scripted TEXT frames, records ACKs."""

    def __init__(self, frames):
        import aiohttp
        self._frames = [types.SimpleNamespace(type=aiohttp.WSMsgType.TEXT,
                                              data=f) for f in frames]
        self.sent = []
        self.ack_event = asyncio.Event()

    def __aiter__(self):
        self._it = iter(self._frames)
        return self

    async def __anext__(self):
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration

    async def send_json(self, payload):
        self.sent.append(payload)
        self.ack_event.set()


def _envelope(n):
    import json as _json
    return _json.dumps({"type": "events_api", "envelope_id": f"env-{n}",
                        "payload": {"event": {"n": n}}})


def test_consume_acks_next_envelope_while_turn_runs():
    """REGRESSION: the agent turn was awaited INLINE in the read loop, so a
    slow turn blocked the next envelope's ACK past Slack's redelivery timer.
    With task dispatch, both ACKs must land while turn 1 is still blocked."""
    from surfaces.slack.socket_mode import SlackSocketModeClient

    async def scenario():
        gate = asyncio.Event()
        handled = []

        async def handler(event):
            handled.append(event["n"])
            if event["n"] == 1:
                await gate.wait()

        ws = _FakeWS([_envelope(1), _envelope(2)])
        client = SlackSocketModeClient(lambda: None)
        consume = asyncio.create_task(client._consume(ws, handler))

        async def both_acked():
            while len(ws.sent) < 2:
                await asyncio.sleep(0.01)

        await asyncio.wait_for(both_acked(), timeout=2)  # turn 1 still blocked
        gate.set()
        await asyncio.wait_for(consume, timeout=2)
        await asyncio.wait_for(asyncio.gather(*client._tasks), timeout=2)
        assert sorted(handled) == [1, 2]

    asyncio.run(scenario())


def test_consume_skips_malformed_frame():
    from surfaces.slack.socket_mode import SlackSocketModeClient

    async def scenario():
        handled = []

        async def handler(event):
            handled.append(event["n"])

        ws = _FakeWS(["{not json", _envelope(7)])
        client = SlackSocketModeClient(lambda: None)
        await client._consume(ws, handler)
        for t in list(client._tasks):
            await t
        assert handled == [7]
        assert [p["envelope_id"] for p in ws.sent] == ["env-7"]

    asyncio.run(scenario())
