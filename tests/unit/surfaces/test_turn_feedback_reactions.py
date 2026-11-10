"""F-08 (113 W7): turn feedback outside Telegram.

Slack and Discord put 👀 on the message while the turn runs and take it off
after; a turn that raised gets ❌. Discord also renews its typing state while
the turn runs. A room line that does not name the bot gets no reaction, and a
failing reaction never costs the turn.
"""
import asyncio

import pytest

import surfaces._shared as shared
from core.surfaces.envelopes import Identity, InboundMessage, SessionSource
from surfaces._shared import wants_feedback


def _inbound(surface, chat_type="dm", *, raw=None, mentions=None, **kw):
    src = SessionSource(surface_id=surface, chat_id="C1", chat_type=chat_type)
    return InboundMessage(text="hi", identity=Identity(user_id="u", source=src,
                                                       raw_user_id="U1"),
                          raw=raw, mentions_bot=mentions, **kw)


class _SlackClient:
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    async def add_reaction(self, channel, ts, name):
        self.calls.append(("add", channel, ts, name))
        if self.fail:
            raise RuntimeError("missing_scope")

    async def remove_reaction(self, channel, ts, name):
        self.calls.append(("remove", channel, ts, name))

    async def send_message(self, channel, text, thread_ts=None):
        return {"ts": "9.9"}


class _DiscordClient:
    def __init__(self):
        self.calls = []

    async def add_reaction(self, channel, mid, emoji):
        self.calls.append(("add", channel, mid, emoji))

    async def remove_reaction(self, channel, mid, emoji):
        self.calls.append(("remove", channel, mid, emoji))

    async def trigger_typing(self, channel):
        self.calls.append(("typing", channel))

    async def send_message(self, channel, text, reply_to=None):
        return {"id": "m2"}


def _ok_route(monkeypatch, *, raises=False):
    async def fake(container, agent, inbound, deliver, **kw):
        if raises:
            raise RuntimeError("turn failed")
        await deliver("answer")
    monkeypatch.setattr(shared, "route_and_act", fake)


def test_gate_dm_yes_room_only_when_named():
    assert wants_feedback(_inbound("slack"))
    assert not wants_feedback(_inbound("slack", "group"))
    assert not wants_feedback(_inbound("slack", "group", mentions=False))
    assert wants_feedback(_inbound("slack", "group", mentions=True))
    assert not wants_feedback(_inbound("slack", internal=True))
    assert not wants_feedback(_inbound("slack", sender_is_bot=True))


def test_slack_seen_then_removed(monkeypatch):
    from surfaces.slack.harness import SlackHarness
    _ok_route(monkeypatch)
    client = _SlackClient()
    h = SlackHarness(None, None, client, socket=None, dedup=None)
    asyncio.run(h._route(_inbound("slack", raw={"ts": "1.5"})))
    assert client.calls == [("add", "C1", "1.5", "eyes"), ("remove", "C1", "1.5", "eyes")]


def test_slack_failed_turn_gets_x(monkeypatch):
    from surfaces.slack.harness import SlackHarness
    _ok_route(monkeypatch, raises=True)
    client = _SlackClient()
    h = SlackHarness(None, None, client, socket=None, dedup=None)
    with pytest.raises(RuntimeError):
        asyncio.run(h._route(_inbound("slack", raw={"ts": "1.5"})))
    assert client.calls[-1] == ("add", "C1", "1.5", "x")


def test_slack_room_chatter_and_slash_command_get_no_reaction(monkeypatch):
    from surfaces.slack.harness import SlackHarness
    _ok_route(monkeypatch)
    client = _SlackClient()
    h = SlackHarness(None, None, client, socket=None, dedup=None)
    asyncio.run(h._route(_inbound("slack", "group", raw={"ts": "1.5"}, mentions=False)))
    asyncio.run(h._route(_inbound("slack", raw={"command": "/agent"})))   # no ts
    assert client.calls == []


def test_slack_reaction_failure_never_costs_the_turn(monkeypatch):
    from surfaces.slack.harness import SlackHarness
    delivered = []

    async def fake(container, agent, inbound, deliver, **kw):
        delivered.append(True)
    monkeypatch.setattr(shared, "route_and_act", fake)
    h = SlackHarness(None, None, _SlackClient(fail=True), socket=None, dedup=None)
    asyncio.run(h._route(_inbound("slack", raw={"ts": "1.5"})))
    assert delivered == [True]


def test_discord_seen_typing_pulse_then_removed(monkeypatch):
    import surfaces.discord.harness as dh
    monkeypatch.setattr(dh, "TYPING_PULSE_S", 0.01)

    async def slow(container, agent, inbound, deliver, **kw):
        await asyncio.sleep(0.05)
    monkeypatch.setattr(shared, "route_and_act", slow)
    client = _DiscordClient()
    h = dh.DiscordHarness(None, None, client, gateway=None, dedup=None)
    asyncio.run(h._route(_inbound("discord", raw={"id": "m1"})))
    kinds = [c[0] for c in client.calls]
    assert client.calls[1] == ("add", "C1", "m1", "👀")
    assert kinds.count("typing") >= 2          # the first one plus the pulse
    assert client.calls[-1] == ("remove", "C1", "m1", "👀")
    assert h._pulses == {}


def test_discord_failed_turn_gets_x_and_stops_the_pulse(monkeypatch):
    import surfaces.discord.harness as dh
    _ok_route(monkeypatch, raises=True)
    client = _DiscordClient()
    h = dh.DiscordHarness(None, None, client, gateway=None, dedup=None)
    with pytest.raises(RuntimeError):
        asyncio.run(h._route(_inbound("discord", raw={"id": "m1"})))
    assert client.calls[-1] == ("add", "C1", "m1", "❌")
    assert h._pulses == {}


def test_client_calls_have_the_platform_shape():
    from surfaces.discord.client import DiscordClient
    from surfaces.slack.client import SlackClient
    seen = []

    async def fake_request(method, path, json=None):
        seen.append((method, path))

    async def fake_call(method, *, token, json=None):
        seen.append((method, json))
        return {"ok": True}
    d = DiscordClient("t")
    d._request = fake_request
    asyncio.run(d.add_reaction("C1", "m1", "👀"))
    asyncio.run(d.remove_reaction("C1", "m1", "👀"))
    s = SlackClient("b", "a")
    s._call = fake_call
    asyncio.run(s.add_reaction("C1", "1.5", "eyes"))
    assert seen[0] == ("PUT", "/channels/C1/messages/m1/reactions/%F0%9F%91%80/@me")
    assert seen[1][0] == "DELETE"
    assert seen[2] == ("reactions.add", {"channel": "C1", "timestamp": "1.5", "name": "eyes"})
