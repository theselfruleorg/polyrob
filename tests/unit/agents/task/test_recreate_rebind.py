"""#0 mute-on-resume fix: a recreated orchestrator must be RE-bound to the outbound
chat surface (_message_router + _chat_session_key), else the resumed chat's replies are
silently dropped (the streaming + send_message seams read those attrs by getattr→None)."""
from types import SimpleNamespace

import pytest


class _Reg:
    def __init__(self, row):
        self._row = row
        self.bound = []
    def resolve_by_session_id(self, sid):
        return self._row if (self._row and self._row["session_id"] == sid) else None
    def bind(self, *a, **k):
        self.bound.append((a, k))


def _agent(row, *, router=object()):
    from agents.task_agent_lite import TaskAgent
    a = object.__new__(TaskAgent)
    reg = _Reg(row)
    svc = {"message_router": router, "session_chat_registry": reg}
    a.container = SimpleNamespace(get_service=lambda n: svc.get(n))
    return a, reg, router


def test_rebind_reattaches_router_and_key(monkeypatch):
    monkeypatch.setenv("SINGULAR_CHAT_ENABLED", "true")
    row = {"session_id": "s1", "session_key": "agent:main:telegram:dm:555:u1",
           "surface_id": "telegram", "chat_id": "555", "user_id": "u1"}
    a, reg, router = _agent(row)
    orch = SimpleNamespace()
    a._rebind_recreated_chat(orch, "s1", "u1")
    assert getattr(orch, "_message_router", None) is router
    assert getattr(orch, "_chat_session_key", None) == "agent:main:telegram:dm:555:u1"


def test_rebind_noop_when_no_binding(monkeypatch):
    monkeypatch.setenv("SINGULAR_CHAT_ENABLED", "true")
    a, reg, router = _agent(None)  # no chat row for this session
    orch = SimpleNamespace()
    a._rebind_recreated_chat(orch, "s1", "u1")
    assert getattr(orch, "_message_router", None) is None  # not a chat session -> untouched


def test_rebind_fail_open_no_container(monkeypatch):
    from agents.task_agent_lite import TaskAgent
    a = object.__new__(TaskAgent)
    a.container = None
    orch = SimpleNamespace()
    a._rebind_recreated_chat(orch, "s1", "u1")  # must not raise
    assert getattr(orch, "_message_router", None) is None
