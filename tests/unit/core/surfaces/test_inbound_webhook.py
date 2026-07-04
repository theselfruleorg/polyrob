import os, pytest
from core.surfaces.inbound_webhook import WebhookSurface
from core.surfaces.idempotency import IdempotencyStore
from core.surfaces.envelopes import InboundMessage, Identity, SessionSource
from core.surfaces.media import Media


class _WA(WebhookSurface):
    def __init__(self, store): super().__init__(store); self.parsed = 0
    @property
    def surface_id(self): return "wa"
    def verify_signature(self, headers, body): return headers.get("x-sig") == "ok"
    def parse(self, payload):
        self.parsed += 1
        ident = Identity(user_id="u1", source=SessionSource(surface_id="wa", chat_id="123"),
                         raw_user_id="123")
        return [InboundMessage(text=payload.get("text", ""), identity=ident,
                               idempotency_key=payload.get("id"))]
    def idempotency_key(self, inbound): return inbound.idempotency_key


class _VoiceWA(WebhookSurface):
    """Surface that emits a voice-only message (no text)."""
    def __init__(self, store):
        super().__init__(store)
        self.immediate_calls = []
    @property
    def surface_id(self): return "wa_voice"
    def verify_signature(self, headers, body): return True
    def parse(self, payload):
        ident = Identity(user_id="u2", source=SessionSource(surface_id="wa_voice", chat_id="456"),
                         raw_user_id="456")
        return [InboundMessage(text="", identity=ident, idempotency_key="vm1",
                               media=[Media(kind="voice", mime="audio/ogg", data=b"\xff\xfb")])]
    def idempotency_key(self, inbound): return inbound.idempotency_key
    async def _send_immediate(self, inbound, text): self.immediate_calls.append(text)


@pytest.mark.asyncio
async def test_bad_signature_is_rejected(tmp_path):
    wa = _WA(IdempotencyStore(os.path.join(tmp_path, "i.db")))
    out = await wa.handle_post(None, {"x-sig": "no"}, b"{}", task_agent=object())
    assert out["ok"] is False and wa.parsed == 0


@pytest.mark.asyncio
async def test_duplicate_message_processed_once(tmp_path, monkeypatch):
    calls = []
    async def fake_route(container, inbound, **k):
        from core.surfaces.dispatcher import RouteDecision, RouteKind
        return RouteDecision(RouteKind.TASK_AGENT, "sk")
    async def fake_act(task_agent, result, **k): calls.append(result.inbound.text); return None
    monkeypatch.setattr("core.surfaces.inbound_webhook.route_inbound", fake_route)
    monkeypatch.setattr("core.surfaces.inbound_webhook.act_on_inbound", fake_act)

    wa = _WA(IdempotencyStore(os.path.join(tmp_path, "i.db")))
    body = b'{"id": "m1", "text": "hi"}'
    await wa.handle_post(None, {"x-sig": "ok"}, body, task_agent=object())
    await wa.handle_post(None, {"x-sig": "ok"}, body, task_agent=object())
    assert calls == ["hi"]          # second is a dedup no-op


@pytest.mark.asyncio
async def test_voice_guard_fires_route_not_called(tmp_path, monkeypatch):
    """A voice-only message with no text must NOT reach route_inbound; _send_immediate fires."""
    route_calls = []
    async def fake_route(container, inbound, **k):
        route_calls.append(inbound)
        from core.surfaces.dispatcher import RouteDecision, RouteKind
        return RouteDecision(RouteKind.TASK_AGENT, "sk")
    monkeypatch.setattr("core.surfaces.inbound_webhook.route_inbound", fake_route)
    # Transcription returns None (not available / disabled) — guard must still fire fail-open.
    async def _null_transcribe(container, media):
        return None
    monkeypatch.setattr("core.surfaces.inbound_webhook.transcribe_inbound_media", _null_transcribe)

    wa = _VoiceWA(IdempotencyStore(os.path.join(tmp_path, "v.db")))
    result = await wa.handle_post(None, {}, b"{}", task_agent=object())
    assert result["ok"] is True
    assert len(route_calls) == 0                 # never dispatched
    assert len(wa.immediate_calls) == 1          # guard notice sent
    assert "voice" in wa.immediate_calls[0].lower() or "transcri" in wa.immediate_calls[0].lower()


@pytest.mark.asyncio
async def test_voice_transcribed_text_reaches_route(tmp_path, monkeypatch):
    """When transcription succeeds, the resulting text is set and the message is routed."""
    route_calls = []
    async def fake_route(container, inbound, **k):
        route_calls.append(inbound.text)
        from core.surfaces.dispatcher import RouteDecision, RouteKind
        return RouteDecision(RouteKind.TASK_AGENT, "sk")
    async def fake_act(task_agent, result, **k): return None
    monkeypatch.setattr("core.surfaces.inbound_webhook.route_inbound", fake_route)
    monkeypatch.setattr("core.surfaces.inbound_webhook.act_on_inbound", fake_act)
    async def _transcribe(container, media):
        return "hello world"
    monkeypatch.setattr("core.surfaces.inbound_webhook.transcribe_inbound_media", _transcribe)

    wa = _VoiceWA(IdempotencyStore(os.path.join(tmp_path, "v2.db")))
    result = await wa.handle_post(None, {}, b"{}", task_agent=object())
    assert result["ok"] is True
    assert route_calls == ["hello world"]        # transcribed text forwarded
    assert len(wa.immediate_calls) == 0          # no guard notice
