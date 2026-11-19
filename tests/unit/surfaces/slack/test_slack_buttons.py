"""Order 0017 — Slack native buttons, offline.

Out: explicit OutboundMessage.actions render as Block Kit buttons (never derived
from text). In: a block_actions press is the authenticated PRESSER typing the
command; a payload that is not a command is dropped.
"""
import asyncio
import json

from core.surfaces.envelopes import Action, OutboundMessage
from surfaces.slack.blocks import parse_block_actions, render_blocks
from surfaces.slack.socket_mode import INTERACTIVE_ENVELOPE, SlackSocketModeClient
from surfaces.slack.surface import SlackSurface
from tests.unit.surfaces.slack.test_slack import _FakeWS

TOKEN = "/approve_p_a1b2c3"


def _press(value=TOKEN, *, channel="C1", user="U7", trigger="trig1", atype="button"):
    return {"type": "block_actions", "trigger_id": trigger,
            "user": {"id": user, "username": "pat"},
            "channel": {"id": channel}, "response_url": "https://hooks.slack.com/x",
            "message": {"ts": "1.2"},
            "actions": [{"type": atype, "action_id": "act_0", "value": value}]}


def test_supports_actions():
    assert SlackSurface(object()).capabilities.supports_actions is True


def test_render_blocks_shape_and_limits():
    acts = [Action("Approve", TOKEN, "primary"), Action("Reject", "/reject_p_a1b2c3", "danger"),
            Action("Read", "/status")]
    blocks = render_blocks("hello", acts)
    assert blocks[0]["type"] == "section" and blocks[0]["text"]["text"] == "hello"
    els = blocks[1]["elements"]
    assert [e.get("style") for e in els] == ["primary", "danger", None]
    assert els[0]["value"] == TOKEN and els[0]["text"]["text"] == "Approve"
    many = [Action(f"a{i}", f"/approve_p_{i:06x}") for i in range(40)]
    blocks = render_blocks("x", many)
    rows = blocks[1:]
    assert all(len(r["elements"]) <= 5 for r in rows)
    assert 1 <= sum(len(r["elements"]) for r in rows) <= 25   # actions_for caps the list first
    ids = [e["action_id"] for r in rows for e in r["elements"]]
    assert len(set(ids)) == len(ids)
    assert render_blocks("x", []) == [] and render_blocks("x", None) == []


def test_press_is_the_presser_typing_the_command():
    m = parse_block_actions(_press())
    assert m.text == TOKEN
    assert m.identity.raw_user_id == "U7"
    assert m.identity.source.surface_id == "slack"
    assert m.identity.source.chat_id == "C1"
    assert m.identity.source.chat_type == "group"
    assert m.mentions_bot is True
    assert m.idempotency_key == "card:trig1"
    assert "response_url" not in json.dumps(m.raw)


def test_dm_press_is_dm_tier():
    assert parse_block_actions(_press(channel="D1")).identity.source.chat_type == "dm"


def test_non_command_and_non_button_dropped():
    assert parse_block_actions(_press("rm -rf /")) is None
    assert parse_block_actions(_press("hello there")) is None
    assert parse_block_actions(_press(atype="static_select")) is None
    assert parse_block_actions({**_press(), "type": "view_submission"}) is None
    assert parse_block_actions({}) is None
    assert parse_block_actions(_press(user="")) is None
    assert parse_block_actions(_press(channel="")) is None


class _Client:
    def __init__(self):
        self.sent = []

    async def send_message(self, channel, text, thread_ts=None, blocks=None):
        self.sent.append((channel, text, blocks))
        return {"ts": f"{len(self.sent)}.0"}


def test_send_puts_blocks_on_last_chunk_only():
    c = _Client()
    res = asyncio.run(SlackSurface(c).send(OutboundMessage(
        session_key="agent:main:slack:dm:D1:u_x", text="x" * 7000,
        actions=[Action("Approve", TOKEN, "primary")])))
    assert res.success
    assert [x[2] is not None for x in c.sent] == [False, False, True]
    assert all(len(x[1]) <= 3000 for x in c.sent)           # a section holds 3000
    assert c.sent[-1][2][1]["elements"][0]["value"] == TOKEN
    assert c.sent[-1][2][0]["text"]["text"] == c.sent[-1][1]


def test_send_without_actions_passes_no_blocks_and_keeps_4000_split():
    c = _Client()
    asyncio.run(SlackSurface(c).send(OutboundMessage(
        session_key="agent:main:slack:dm:D1:u_x", text="hi")))
    assert c.sent == [("D1", "hi", None)]
    c2 = _Client()
    asyncio.run(SlackSurface(c2).send(OutboundMessage(
        session_key="agent:main:slack:dm:D1:u_x", text="y" * 3900)))
    assert len(c2.sent) == 1


def test_consume_acks_and_dispatches_the_press():
    async def scenario():
        got = []

        async def handler(event):
            got.append(event)
        frame = json.dumps({"type": INTERACTIVE_ENVELOPE, "envelope_id": "env-i",
                            "payload": _press()})
        ws = _FakeWS([frame])
        client = SlackSocketModeClient(lambda: None)
        await client._consume(ws, handler)
        for t in list(client._tasks):
            await t
        assert ws.sent == [{"envelope_id": "env-i"}]
        assert got == [{"type": INTERACTIVE_ENVELOPE, "payload": _press()}]

    asyncio.run(scenario())


def test_harness_press_goes_through_route_once():
    from surfaces.slack.harness import SlackHarness
    routed = []

    class _Dedup:
        def __init__(self):
            self.keys = set()

        def seen(self, key):
            if key in self.keys:
                return True
            self.keys.add(key)
            return False
    h = SlackHarness(None, None, _Client(), socket=None, dedup=_Dedup())

    async def _route(inbound):
        routed.append(inbound.text)
    h._route = _route
    ev = {"type": INTERACTIVE_ENVELOPE, "payload": _press()}
    asyncio.run(h.handle_event(ev))
    asyncio.run(h.handle_event(ev))                       # a redelivery is dropped
    asyncio.run(h.handle_event({"type": INTERACTIVE_ENVELOPE,
                                "payload": _press("nope", trigger="t2")}))
    assert routed == [TOKEN]
