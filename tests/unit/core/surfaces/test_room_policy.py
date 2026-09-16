import types
from core.surfaces.binding import bind_chat_surface
from core.surfaces.envelopes import SessionSource
from core.surfaces.room_policy import is_public_session


class _C:
    def __init__(self):
        self._svc = {"message_router": object()}
        self.config = types.SimpleNamespace(data_dir=None)
    def get_service(self, n):
        return self._svc.get(n)


def test_bind_marks_group_session_public(monkeypatch):
    monkeypatch.setenv("SINGULAR_CHAT_ENABLED", "true")
    orch = types.SimpleNamespace()
    src = SessionSource(surface_id="telegram", chat_id="-100", chat_type="group")
    assert bind_chat_surface(orch, _C(), session_source=src,
                             chat_session_key="agent:main:telegram:group:-100",
                             session_id="s", user_id="u") is True
    assert is_public_session(orch) is True


def test_bind_marks_dm_session_private(monkeypatch):
    monkeypatch.setenv("SINGULAR_CHAT_ENABLED", "true")
    orch = types.SimpleNamespace()
    src = SessionSource(surface_id="telegram", chat_id="5", chat_type="dm")
    bind_chat_surface(orch, _C(), session_source=src,
                      chat_session_key="agent:main:telegram:dm:5:u",
                      session_id="s", user_id="u")
    assert is_public_session(orch) is False


def test_unbound_is_private():
    assert is_public_session(types.SimpleNamespace()) is False
    assert is_public_session(None) is False
