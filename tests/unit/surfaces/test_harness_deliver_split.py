"""OS1 — a reply longer than the platform's per-message limit is split on the
harness deliver path (catalog ``max_message_chars``), never sent whole."""
import asyncio

import surfaces._shared as shared
from surfaces._shared import BaseHarness, TextSink, split_for_surface


class _Harness(BaseHarness):
    surface_id = "discord"

    def __init__(self):
        super().__init__(None, None, None)
        self.sent = []

    async def _deliver_to(self, target, text):
        if len(text) > 2000:
            raise RuntimeError("discord 400: content too long")
        self.sent.append(text)


class _Src:
    chat_id = "c1"


class _Ident:
    source = _Src()


class _Inbound:
    identity = _Ident()
    media = []


def _long(n):
    return "\n\n".join(f"paragraph {i} " + "x" * 300 for i in range(n))


def test_base_harness_splits_long_reply(monkeypatch):
    text = _long(25)                          # ~7,800 chars
    assert len(text) > 6000

    async def fake_route_and_act(container, agent, inbound, deliver, **kw):
        await deliver(text)

    monkeypatch.setattr(shared, "route_and_act", fake_route_and_act)
    h = _Harness()
    asyncio.run(h._route(_Inbound()))
    assert len(h.sent) >= 4
    assert all(len(c) <= 2000 for c in h.sent)
    assert "paragraph 24" in h.sent[-1]


def test_text_sink_splits_for_its_surface():
    sent = []

    async def send(chat_id, text):
        assert len(text) <= 2000
        sent.append(text)

    sink = TextSink(send, label="DiscordSink", surface_id="discord")
    assert asyncio.run(sink.send_message("c1", _long(25))) is True
    assert len(sent) >= 4


def test_split_for_unknown_surface_is_one_chunk():
    assert split_for_surface("nope", "a" * 9000) == ["a" * 9000]


def test_whatsapp_immediate_reply_is_split():
    from core.surfaces.envelopes import Identity, InboundMessage, SessionSource
    from surfaces.whatsapp.inbound import WhatsAppInbound

    sent = []

    async def responder(to, text, reply_to=None):
        assert len(text) <= 4096
        sent.append(text)

    class _Store:
        def seen(self, k):
            return False

    inbound = WhatsAppInbound(_Store(), user_directory=None, responder=responder)
    msg = InboundMessage(text="hi", identity=Identity(
        user_id="u", raw_user_id="15550001",
        source=SessionSource(surface_id="whatsapp", chat_id="15550001", chat_type="dm")))
    asyncio.run(inbound._send_immediate(msg, _long(25)))
    assert len(sent) >= 2
