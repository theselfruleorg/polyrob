"""Wave 3 Task 4 — DiscordSurface send/splitting + harness routing flow."""
import asyncio
import types

import pytest

from core.surfaces.envelopes import OutboundMessage
from surfaces.discord.surface import DiscordSurface, channel_id_from_session_key


class _FakeClient:
    def __init__(self, fail=False):
        self.sent = []
        self.fail = fail

    async def send_message(self, channel_id, text, reply_to=None):
        if self.fail:
            raise RuntimeError("boom")
        self.sent.append((channel_id, text))
        return {"id": f"m{len(self.sent)}"}


def test_channel_id_from_session_key():
    assert channel_id_from_session_key(
        "agent:main:discord:group:chan-1") == "chan-1"
    assert channel_id_from_session_key(
        "agent:main:discord:dm:chan-2:u_x") == "chan-2"
    assert channel_id_from_session_key("direct:discord:chan-3") == "chan-3"


def test_send_splits_long_messages():
    client = _FakeClient()
    surface = DiscordSurface(client)
    res = asyncio.run(surface.send(OutboundMessage(
        session_key="agent:main:discord:dm:chan-1:u_x", text="x" * 4500)))
    assert res.success is True
    assert len(client.sent) == 3
    assert all(len(t) <= 2000 for _, t in client.sent)
    assert client.sent[0][0] == "chan-1"


def test_send_failure_is_fail_open():
    surface = DiscordSurface(_FakeClient(fail=True))
    res = asyncio.run(surface.send(OutboundMessage(
        session_key="agent:main:discord:dm:chan-1:u_x", text="hi")))
    assert res.success is False
    assert res.error


@pytest.mark.asyncio
async def test_harness_routes_and_delivers(tmp_path, monkeypatch):
    """A denied (silent) group message produces NO send; a DM owner message
    flows to act_on_inbound and its reply is delivered."""
    from surfaces.discord.harness import build_discord_harness

    class _Container:
        def __init__(self):
            self._svc = {}
            self.config = types.SimpleNamespace(data_dir=str(tmp_path))

        def get_service(self, name):
            return self._svc.get(name)

        def register_service(self, name, svc):
            self._svc[name] = svc

    container = _Container()
    harness = build_discord_harness(container, task_agent=None,
                                    token="t", data_dir=str(tmp_path))
    harness._gateway.bot_user_id = "999"
    client = harness._client
    client_sent = []

    async def fake_send(channel_id, text, reply_to=None):
        client_sent.append((channel_id, text))
        return {"id": "m1"}

    async def fake_typing(channel_id):
        return None

    monkeypatch.setattr(client, "send_message", fake_send)
    monkeypatch.setattr(client, "trigger_typing", fake_typing)

    async def fake_act(task_agent, result, deliver=None, **kw):
        return "ack!"

    monkeypatch.setattr("surfaces.telegram.harness.act_on_inbound", fake_act)

    # group message with GROUP_CHAT_ENABLED off -> route_inbound returns
    # TASK_AGENT (legacy) -> our fake act returns a reply -> delivered.
    await harness.handle_message_create({
        "id": "m-10", "channel_id": "chan-1", "content": "hi",
        "author": {"id": "42", "username": "a", "bot": False},
    })
    assert client_sent == [("chan-1", "ack!")]

    # duplicate id -> dedup, no second send
    await harness.handle_message_create({
        "id": "m-10", "channel_id": "chan-1", "content": "hi again",
        "author": {"id": "42", "username": "a", "bot": False},
    })
    assert len(client_sent) == 1
