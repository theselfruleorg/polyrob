import pytest

import core.surfaces.inbound_webhook as wh
from core.surfaces.media import Media
from core.surfaces.envelopes import InboundMessage, Identity, SessionSource
from core.surfaces.idempotency import IdempotencyStore
from core.surfaces.dispatcher import RouteDecision, RouteKind
from surfaces.whatsapp.inbound import WhatsAppInbound


class _UD:
    def resolve_internal(self, raw, surface):
        return "u_" + raw


def _voice_inbound():
    return InboundMessage(
        text="",
        identity=Identity(user_id="u1",
                          source=SessionSource("whatsapp", "555", "dm"), raw_user_id="555"),
        idempotency_key="wamid.1", media=[Media(kind="voice", mime="audio/ogg")])


class _Echoable(wh.WebhookSurface):
    def __init__(self, idem, inbound):
        super().__init__(idem)
        self._inbound = inbound
        self.sent = []
        self.read = []

    @property
    def surface_id(self):
        return "test"

    def verify_signature(self, headers, body):
        return True

    def parse(self, payload):
        return [self._inbound]

    def idempotency_key(self, inbound):
        return inbound.idempotency_key or ""

    async def _send_immediate(self, inbound, text, reply_to=None):
        self.sent.append((text, reply_to))

    async def mark_read_inbound(self, inbound):
        self.read.append(inbound.idempotency_key)


def _patch_pipeline(monkeypatch, *, transcript="hello from voice"):
    async def fake_transcribe(container, media):
        return transcript
    monkeypatch.setattr(wh, "transcribe_inbound_media", fake_transcribe)
    monkeypatch.setattr(wh, "voice_needs_guard", lambda m, t: False)

    async def fake_act(task_agent, result):
        return None
    monkeypatch.setattr(wh, "act_on_inbound", fake_act)


@pytest.mark.asyncio
async def test_handle_post_echoes_transcript_before_route(monkeypatch, tmp_path):
    monkeypatch.setenv("VOICE_TRANSCRIPT_ECHO", "true")
    monkeypatch.setenv("VOICE_TRANSCRIPTION_ENABLED", "true")
    surf = _Echoable(IdempotencyStore(str(tmp_path / "i.db")), _voice_inbound())
    _patch_pipeline(monkeypatch)

    async def fake_route(container, inbound, **k):
        # echo must already be sent by the time we route
        assert ('🎙️ Transcript: "hello from voice"', "wamid.1") in surf.sent
        return RouteDecision(RouteKind.DENIED, "k")
    monkeypatch.setattr(wh, "route_inbound", fake_route)

    await surf.handle_post(object(), {}, b'{"x":1}', task_agent=object())
    assert ('🎙️ Transcript: "hello from voice"', "wamid.1") in surf.sent
    assert surf.read == ["wamid.1"]


@pytest.mark.asyncio
async def test_handle_post_flag_off_no_echo(monkeypatch, tmp_path):
    monkeypatch.setenv("VOICE_TRANSCRIPT_ECHO", "false")
    monkeypatch.setenv("VOICE_TRANSCRIPTION_ENABLED", "true")
    surf = _Echoable(IdempotencyStore(str(tmp_path / "i.db")), _voice_inbound())
    _patch_pipeline(monkeypatch)

    async def fake_route(container, inbound, **k):
        return RouteDecision(RouteKind.DENIED, "k")
    monkeypatch.setattr(wh, "route_inbound", fake_route)

    await surf.handle_post(object(), {}, b'{"x":1}', task_agent=object())
    assert not any(t.startswith("🎙️ Transcript") for (t, _) in surf.sent)


@pytest.mark.asyncio
async def test_wa_send_immediate_threads_reply_to(tmp_path):
    calls = []

    async def responder(to, text, reply_to=None):
        calls.append((to, text, reply_to))

    wa = WhatsAppInbound(IdempotencyStore(str(tmp_path / "i.db")),
                         user_directory=_UD(), responder=responder)
    await wa._send_immediate(_voice_inbound(), "hi", reply_to="wamid.9")
    assert calls == [("555", "hi", "wamid.9")]


@pytest.mark.asyncio
async def test_wa_mark_read_inbound(tmp_path):
    reads = []

    async def mr(mid):
        reads.append(mid)

    wa = WhatsAppInbound(IdempotencyStore(str(tmp_path / "i.db")),
                         user_directory=_UD(), mark_read=mr)
    await wa.mark_read_inbound(_voice_inbound())
    assert reads == ["wamid.1"]
