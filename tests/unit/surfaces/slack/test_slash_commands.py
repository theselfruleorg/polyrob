"""F-01 (113 W7): owner verbs reach the agent on Slack through the app's command.

Slack's own client takes every line that starts with ``/`` as a Slack command,
so ``/status`` typed to the bot never arrives as a message. The app registers
one command; Socket Mode delivers it as a ``slash_commands`` envelope whose
payload has the HTTP slash-command fields. The consumer acks it and the
harness routes ``/<app> status`` as the line ``/status``.
"""
import asyncio
import json

from surfaces.slack.socket_mode import (SLASH_ENVELOPE, SlackSocketModeClient,
                                        parse_slash_command, slash_line)
from tests.unit.surfaces.slack.test_slack import _FakeWS


def _body(**over):
    body = {"token": "t", "team_id": "T1", "channel_id": "D1",
            "channel_name": "directmessage", "user_id": "U1", "user_name": "someone",
            "command": "/agent", "text": "status", "api_app_id": "A1",
            "response_url": "https://hooks.slack.com/commands/x",
            "trigger_id": "13345224609.738474920.8088930838d88f008e0"}
    body.update(over)
    return body


def test_slash_line_maps_the_text_to_the_verb():
    assert slash_line("status") == "/status"
    assert slash_line("  wallet balance ") == "/wallet balance"
    assert slash_line("/status") == "/status"
    assert slash_line("") == "/help"


def test_dm_command_is_a_dm_line():
    inbound = parse_slash_command(_body())
    assert inbound.text == "/status"
    src = inbound.identity.source
    assert (src.surface_id, src.chat_id, src.chat_type) == ("slack", "D1", "dm")
    assert inbound.identity.raw_user_id == "U1"
    assert inbound.identity.user_id == "u_slack_U1"
    assert inbound.idempotency_key.startswith("slash:")
    assert inbound.mentions_bot is True


def test_channel_command_is_a_room_line_that_names_the_bot():
    inbound = parse_slash_command(_body(channel_id="C9", channel_name="general"))
    assert inbound.identity.source.chat_type == "group"
    assert inbound.mentions_bot is True


def test_owner_alias_applies(monkeypatch):
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "u_owner_fixture")
    monkeypatch.setenv("OWNER_SLACK_ID", "U1")
    assert parse_slash_command(_body()).identity.user_id == "u_owner_fixture"


def test_incomplete_payload_is_ignored():
    assert parse_slash_command(_body(user_id="")) is None
    assert parse_slash_command(_body(channel_id="")) is None
    assert parse_slash_command(_body(command="")) is None
    assert parse_slash_command("not a dict") is None


def test_consume_acks_and_dispatches_the_command():
    async def scenario():
        got = []

        async def handler(event):
            got.append(event)
        frame = json.dumps({"type": SLASH_ENVELOPE, "envelope_id": "env-s",
                            "accepts_response_payload": True, "payload": _body()})
        ws = _FakeWS([frame])
        client = SlackSocketModeClient(lambda: None)
        await client._consume(ws, handler)
        for t in list(client._tasks):
            await t
        assert ws.sent == [{"envelope_id": "env-s"}]
        assert got == [{"type": SLASH_ENVELOPE, "payload": _body()}]

    asyncio.run(scenario())


def test_harness_routes_the_command_line(monkeypatch):
    import surfaces._shared as shared
    from surfaces.slack.harness import SlackHarness
    routed, sent = [], []

    async def fake_route_and_act(container, agent, inbound, deliver, **kw):
        routed.append(inbound.text)
        await deliver("ok")
    monkeypatch.setattr(shared, "route_and_act", fake_route_and_act)

    class _Client:
        async def send_message(self, channel, text, thread_ts=None):
            sent.append((channel, text, thread_ts))

    class _Dedup:
        def __init__(self):
            self.keys = set()

        def seen(self, key):
            if key in self.keys:
                return True
            self.keys.add(key)
            return False
    h = SlackHarness(None, None, _Client(), socket=None, dedup=_Dedup())
    event = {"type": SLASH_ENVELOPE, "payload": _body()}
    asyncio.run(h.handle_event(event))
    asyncio.run(h.handle_event(event))          # a redelivery is dropped
    assert routed == ["/status"]
    assert sent == [("D1", "ok", None)]
