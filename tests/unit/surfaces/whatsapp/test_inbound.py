import hashlib, hmac, os
from core.surfaces.idempotency import IdempotencyStore
from surfaces.whatsapp.inbound import WhatsAppInbound


def _sig(secret, body):
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _wa(tmp_path):
    return WhatsAppInbound(IdempotencyStore(os.path.join(tmp_path, "i.db")),
                           user_directory=_UD())


class _UD:
    def resolve_internal(self, raw, surface): return "u_" + raw
    def get_or_create_by_external_id(self, *a, **k): return "u_x"


def test_signature_verify(monkeypatch, tmp_path):
    monkeypatch.setenv("WHATSAPP_WEBHOOK_SECRET", "s3cret")
    wa = _wa(tmp_path)
    body = b'{"x":1}'
    assert wa.verify_signature({"x-hub-signature-256": _sig("s3cret", body)}, body) is True
    assert wa.verify_signature({"x-hub-signature-256": "sha256=deadbeef"}, body) is False


def test_parse_text_message(tmp_path):
    wa = _wa(tmp_path)
    payload = {"entry": [{"changes": [{"value": {"messages": [
        {"id": "wamid.1", "from": "15550001111", "type": "text",
         "text": {"body": "hello"}}]}}]}]}
    msgs = wa.parse(payload)
    assert len(msgs) == 1
    assert msgs[0].text == "hello"
    assert msgs[0].idempotency_key == "wamid.1"
    assert msgs[0].identity.source.surface_id == "whatsapp"
    assert msgs[0].identity.raw_user_id == "15550001111"


def test_signature_no_secret(monkeypatch, tmp_path):
    """verify_signature returns False (fail-closed) when secret is unset."""
    monkeypatch.delenv("WHATSAPP_WEBHOOK_SECRET", raising=False)
    wa = _wa(tmp_path)
    body = b'{"x":1}'
    assert wa.verify_signature({"x-hub-signature-256": _sig("whatever", body)}, body) is False


def test_signature_no_prefix(monkeypatch, tmp_path):
    """verify_signature returns False when header lacks the sha256= prefix."""
    monkeypatch.setenv("WHATSAPP_WEBHOOK_SECRET", "s3cret")
    wa = _wa(tmp_path)
    body = b'{"x":1}'
    # Compute the raw hex (no prefix) — even a matching digest must be rejected
    bare_hex = hmac.new("s3cret".encode(), body, hashlib.sha256).hexdigest()
    assert wa.verify_signature({"x-hub-signature-256": bare_hex}, body) is False


def test_verify_challenge_match(monkeypatch, tmp_path):
    """verify_challenge echoes hub.challenge on token match, None otherwise."""
    monkeypatch.setenv("WHATSAPP_VERIFY_TOKEN", "mytoken")
    wa = _wa(tmp_path)
    assert wa.verify_challenge({"hub.verify_token": "mytoken", "hub.challenge": "abc123"}) == "abc123"
    assert wa.verify_challenge({"hub.verify_token": "wrong", "hub.challenge": "abc123"}) is None
    assert wa.verify_challenge({"hub.verify_token": "", "hub.challenge": "abc123"}) is None


def test_parse_empty_payloads(tmp_path):
    """parse({}) and parse({"entry": []}) return [] without raising."""
    wa = _wa(tmp_path)
    assert wa.parse({}) == []
    assert wa.parse({"entry": []}) == []


def test_parse_skips_missing_from(tmp_path):
    """A message dict without a 'from' key is skipped."""
    wa = _wa(tmp_path)
    payload = {"entry": [{"changes": [{"value": {"messages": [
        {"id": "wamid.2", "type": "text", "text": {"body": "hi"}}]}}]}]}
    assert wa.parse(payload) == []


def test_parse_text_identity_fields(tmp_path):
    """Parsed text message has correct chat_type, chat_id, and raw_user_id."""
    wa = _wa(tmp_path)
    payload = {"entry": [{"changes": [{"value": {"messages": [
        {"id": "wamid.3", "from": "15559990000", "type": "text",
         "text": {"body": "world"}}]}}]}]}
    msgs = wa.parse(payload)
    assert len(msgs) == 1
    assert msgs[0].identity.source.chat_type == "dm"
    assert msgs[0].identity.source.chat_id == "15559990000"
    assert msgs[0].identity.raw_user_id == "15559990000"


def test_send_immediate_delivers_via_responder(tmp_path):
    """A voice-guard / DENIED notice must actually be SENT to the sender (not just logged) —
    the responder is invoked with the wa phone + text (24h window is open: it's a reply)."""
    import asyncio
    from types import SimpleNamespace
    sent = []
    async def _responder(to, text, reply_to=None):
        sent.append((to, text))
    wa = WhatsAppInbound(IdempotencyStore(os.path.join(tmp_path, "i.db")),
                         user_directory=_UD(), responder=_responder)
    inbound = SimpleNamespace(identity=SimpleNamespace(raw_user_id="15550001111", user_id="u_x"))
    asyncio.get_event_loop().run_until_complete(wa._send_immediate(inbound, "voice unavailable"))
    assert sent == [("15550001111", "voice unavailable")]


def test_send_immediate_fail_open_without_responder(tmp_path):
    """No responder wired -> logs + returns, never raises into handle_post."""
    import asyncio
    from types import SimpleNamespace
    wa = WhatsAppInbound(IdempotencyStore(os.path.join(tmp_path, "i.db")), user_directory=_UD())
    inbound = SimpleNamespace(identity=SimpleNamespace(raw_user_id="x", user_id="u"))
    asyncio.get_event_loop().run_until_complete(wa._send_immediate(inbound, "hi"))  # no raise
