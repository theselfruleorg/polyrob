from core.surfaces.envelopes import (
    MessageKind, SessionSource, Identity, InboundMessage,
    OutboundMessage, SurfaceCapabilities, SendResult,
)


def test_message_kind_is_str_enum():
    assert MessageKind.AGENT_TEXT == "agent_text"
    assert MessageKind.ASK.value == "ask"


def test_session_source_defaults():
    s = SessionSource(surface_id="telegram", chat_id="123")
    assert s.chat_type == "dm"
    assert s.thread_id is None


def test_inbound_message_carries_identity_and_idempotency():
    ident = Identity(user_id="u_abc", source=SessionSource("telegram", "123"))
    msg = InboundMessage(text="hi", identity=ident, idempotency_key="update_42")
    assert msg.identity.user_id == "u_abc"
    assert msg.idempotency_key == "update_42"
    assert msg.internal is False
    assert msg.media == []


def test_outbound_partial_defaults_false():
    o = OutboundMessage(session_key="k", text="chunk")
    assert o.partial is False
    assert o.kind == MessageKind.AGENT_TEXT


def test_capabilities_defaults_conservative():
    cap = SurfaceCapabilities()
    assert cap.supports_streaming is False
    assert cap.supports_interactive_ask is False
    assert cap.max_message_bytes == 4096


def test_send_result():
    r = SendResult(success=True, surface_message_id="m1")
    assert r.success and r.surface_message_id == "m1" and r.error is None
