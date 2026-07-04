"""P1b-2: bind_chat_surface wires a session's orchestrator to the outbound bus.

This is what makes the P1a mirrors fire: it sets orchestrator._message_router +
._chat_session_key (read BY VALUE inside _register_stream_callback, so it MUST run
before orchestrator.initialize()) and writes the SessionChatRegistry row so
message_router.publish can resolve session_key -> surface. Flag-gated default-OFF:
when OFF, or no router/key, it touches nothing -> legacy path byte-identical.
"""
import pytest

from core.surfaces.binding import bind_chat_surface
from core.surfaces.envelopes import SessionSource
from core.surfaces.message_router import MessageRouter
from core.surfaces.session_chat_registry import SessionChatRegistry, build_session_key


class _FakeOrchestrator:
    pass  # bare; bind_chat_surface sets attrs on it


class _FakeContainer:
    def __init__(self):
        self._svc = {}

    def get_service(self, name):
        return self._svc.get(name)

    def register_service(self, name, instance, **kwargs):
        self._svc[name] = instance


def _container_with_bus(tmp_path):
    c = _FakeContainer()
    reg = SessionChatRegistry(str(tmp_path / "chat.db"))
    c.register_service("session_chat_registry", reg)
    c.register_service("message_router", MessageRouter(reg))
    return c, reg


def test_flag_off_touches_nothing(tmp_path, monkeypatch):
    monkeypatch.delenv("SINGULAR_CHAT_ENABLED", raising=False)
    c, reg = _container_with_bus(tmp_path)
    orch = _FakeOrchestrator()
    src = SessionSource("cli", "local", "dm")
    key = build_session_key(src, "u1")
    assert bind_chat_surface(orch, c, session_source=src, chat_session_key=key,
                             session_id="s1", user_id="u1") is False
    assert not hasattr(orch, "_message_router")
    assert not hasattr(orch, "_chat_session_key")
    assert reg.resolve(key) is None


def test_no_container_returns_false(tmp_path, monkeypatch):
    monkeypatch.setenv("SINGULAR_CHAT_ENABLED", "true")
    orch = _FakeOrchestrator()
    assert bind_chat_surface(orch, None, session_source=None, chat_session_key="k",
                             session_id="s1", user_id="u1") is False


def test_no_router_returns_false(tmp_path, monkeypatch):
    monkeypatch.setenv("SINGULAR_CHAT_ENABLED", "true")
    c = _FakeContainer()  # bus NOT installed
    orch = _FakeOrchestrator()
    assert bind_chat_surface(orch, c, session_source=None, chat_session_key="k",
                             session_id="s1", user_id="u1") is False
    assert not hasattr(orch, "_message_router")


def test_no_key_returns_false(tmp_path, monkeypatch):
    monkeypatch.setenv("SINGULAR_CHAT_ENABLED", "true")
    c, reg = _container_with_bus(tmp_path)
    orch = _FakeOrchestrator()
    src = SessionSource("cli", "local", "dm")
    assert bind_chat_surface(orch, c, session_source=src, chat_session_key=None,
                             session_id="s1", user_id="u1") is False
    assert not hasattr(orch, "_message_router")


def test_full_bind_sets_attrs_and_writes_row(tmp_path, monkeypatch):
    monkeypatch.setenv("SINGULAR_CHAT_ENABLED", "true")
    c, reg = _container_with_bus(tmp_path)
    orch = _FakeOrchestrator()
    src = SessionSource("telegram", "555", "dm")
    key = build_session_key(src, "u_abc")
    assert bind_chat_surface(orch, c, session_source=src, chat_session_key=key,
                             session_id="sess_1", user_id="u_abc") is True
    assert orch._message_router is c.get_service("message_router")
    assert orch._chat_session_key == key
    row = reg.resolve(key)
    assert row["session_id"] == "sess_1"
    assert row["surface_id"] == "telegram"
    assert row["chat_id"] == "555"


@pytest.mark.asyncio
async def test_bound_orchestrator_publishes_resolvably(tmp_path, monkeypatch):
    """End-to-end: after bind, a publish on the bound key resolves to the surface."""
    monkeypatch.setenv("SINGULAR_CHAT_ENABLED", "true")
    from core.surfaces.surface import Surface
    from core.surfaces.envelopes import OutboundMessage, SendResult, SurfaceCapabilities

    class _S(Surface):
        def __init__(self): super().__init__(); self.sent = []
        @property
        def surface_id(self): return "telegram"
        @property
        def capabilities(self): return SurfaceCapabilities()
        async def send(self, msg): self.sent.append(msg); return SendResult(success=True)
        async def start(self, c): pass
        async def stop(self): pass

    c, reg = _container_with_bus(tmp_path)
    surf = _S()
    c.get_service("message_router").subscribe("telegram", surf)
    orch = _FakeOrchestrator()
    src = SessionSource("telegram", "555", "dm")
    key = build_session_key(src, "u_abc")
    bind_chat_surface(orch, c, session_source=src, chat_session_key=key,
                      session_id="sess_1", user_id="u_abc")
    await orch._message_router.publish(OutboundMessage(session_key=orch._chat_session_key, text="hi"))
    assert len(surf.sent) == 1 and surf.sent[0].text == "hi"
