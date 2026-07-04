"""P3 (Task 3.2): surface_ask_capability — the wait_for_response deadlock guard.

send_message with wait_for_response=True pauses the session for a reply. On a surface
that cannot collect a reply (supports_interactive_ask=False) that pause never resolves.
surface_ask_capability lets the action degrade to a non-blocking notify in that case,
while staying byte-identical (return None -> legacy pause) when nothing is bound.
"""
import pytest

from core.surfaces.binding import surface_ask_capability
from core.surfaces.session_chat_registry import SessionChatRegistry
from core.surfaces.registry import SurfaceRegistry
from core.surfaces.surface import Surface
from core.surfaces.envelopes import SendResult, SurfaceCapabilities


class _Surface(Surface):
    def __init__(self, ask: bool):
        super().__init__()
        self._ask = ask
    @property
    def surface_id(self): return "telegram"
    @property
    def capabilities(self): return SurfaceCapabilities(supports_interactive_ask=self._ask)
    async def send(self, msg): return SendResult(success=True)
    async def start(self, c): pass
    async def stop(self): pass


class _Container:
    def __init__(self): self._svc = {}
    def get_service(self, n): return self._svc.get(n)
    def register_service(self, n, i, **k): self._svc[n] = i


class _Orch:
    def __init__(self, container=None, key=None):
        self.container = container
        if key is not None:
            self._chat_session_key = key


def _bound(tmp_path, ask: bool):
    c = _Container()
    reg = SessionChatRegistry(str(tmp_path / "chat.db"))
    reg.bind("k1", "sess_1", "u1", "telegram", "555")
    c.register_service("session_chat_registry", reg)
    sr = SurfaceRegistry(); sr.add(_Surface(ask))
    c.register_service("surface_registry", sr)
    return c


def test_unbound_orchestrator_returns_none():
    assert surface_ask_capability(_Orch(container=None, key=None)) is None


def test_no_key_returns_none(tmp_path):
    assert surface_ask_capability(_Orch(container=_bound(tmp_path, True))) is None


def test_ask_capable_surface_true(tmp_path):
    c = _bound(tmp_path, ask=True)
    assert surface_ask_capability(_Orch(container=c, key="k1")) is True


def test_non_ask_surface_false(tmp_path):
    c = _bound(tmp_path, ask=False)
    assert surface_ask_capability(_Orch(container=c, key="k1")) is False


def test_missing_surface_returns_none(tmp_path):
    c = _Container()
    reg = SessionChatRegistry(str(tmp_path / "chat.db"))
    reg.bind("k1", "sess_1", "u1", "telegram", "555")
    c.register_service("session_chat_registry", reg)
    c.register_service("surface_registry", SurfaceRegistry())  # empty
    assert surface_ask_capability(_Orch(container=c, key="k1")) is None
