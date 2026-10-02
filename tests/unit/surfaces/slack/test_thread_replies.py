"""OS7 — a Slack reply to a message inside a thread goes into that thread
(``thread_ts``), on the harness deliver path and on the surface send."""
import asyncio

import surfaces._shared as shared
from core.surfaces.envelopes import (Identity, InboundMessage, OutboundMessage,
                                     SessionSource)
from core.surfaces.session_chat_registry import build_session_key
from surfaces.slack.harness import SlackHarness
from surfaces.slack.surface import SlackSurface


class _Client:
    def __init__(self):
        self.sent = []

    async def send_message(self, channel, text, thread_ts=None):
        self.sent.append((channel, text, thread_ts))
        return {"ts": "1.1"}


def _inbound(thread):
    src = SessionSource(surface_id="slack", chat_id="C1", chat_type="group",
                        thread_id=thread)
    return InboundMessage(text="hi", identity=Identity(user_id="u", source=src,
                                                       raw_user_id="U1"))


def test_harness_reply_goes_into_the_thread(monkeypatch):
    async def fake_route_and_act(container, agent, inbound, deliver, **kw):
        await deliver("answer")

    monkeypatch.setattr(shared, "route_and_act", fake_route_and_act)
    client = _Client()
    h = SlackHarness(None, None, client, socket=None, dedup=None)
    asyncio.run(h._route(_inbound("1700.42")))
    asyncio.run(h._route(_inbound(None)))
    assert client.sent == [("C1", "answer", "1700.42"), ("C1", "answer", None)]


def test_surface_send_threads_when_the_key_carries_a_thread():
    client = _Client()
    surface = SlackSurface(client)
    key = build_session_key(SessionSource(surface_id="slack", chat_id="C1",
                                          chat_type="group", thread_id="1700.42"))
    res = asyncio.run(surface.send(OutboundMessage(session_key=key, text="hello")))
    assert res.success
    assert client.sent == [("C1", "hello", "1700.42")]
