"""Regression: recent_activity + curated memory tool must surface even when the
memory backend registers AFTER the Controller (first session of a process).

ME-D1: the Controller's provider-gated action registration (session_search,
recent_activity, memory) runs at Controller.__init__, but the sqlite/local_vector
backend registers later during Agent construction. The existing repair block
(construction.py ~652-659) only re-ran session_search; recent_activity and the
curated memory tool were left stranded on the FIRST session of a process. This
test pins the extended repair (re-registers all three, guarded + idempotent).

SK-F5: the `recent_activity` steering sentence in the memory system prompt must
only be advertised when the action is actually registerable — i.e. the episodic
flag is on AND an external memory provider is active. With MEMORY_BACKEND=none
(no external provider), the sentence must not appear even if the flag is on.
"""
import logging

import agents.task.agent.service  # noqa: F401 — avoid import cycle
import pytest

from modules.memory.provider import MemoryProvider
from modules.memory import registry as mem_registry
from tools.controller.registry.service import Registry
from tools.controller.service import Controller


class _StubExternalProvider(MemoryProvider):
    is_external = True
    name = "stub-external"

    async def initialize(self):
        pass

    async def prefetch(self, query, *, session_id, user_id=None):
        return ""

    async def search(self, query, *, user_id=None, session_id=None, limit=5, sort=None):
        return ""

    async def sync_turn(self, user_content, assistant_content, *, session_id, user_id=None):
        pass


def _bare_controller():
    c = object.__new__(Controller)
    c.logger = logging.getLogger("first-session-registration-test")
    c.registry = Registry()
    c.user_id = "tenant-A"
    c.session_id = "sess-1"
    return c


@pytest.fixture(autouse=True)
def _clean_registry():
    mem_registry.reset_memory_registry()
    yield
    mem_registry.reset_memory_registry()


def _run_repair_block(controller):
    """Mirrors the extended repair block in construction.py ME-D1 fix.

    B5 / KEEP-IN-SYNC: this is a hand-copy of the real repair block in
    ``AgentConstructionMixin`` (construction.py ~lines 675-686 —
    ``is_external`` gate → ``_register_session_search_action`` /
    ``_register_recent_activity_action`` / ``_register_memory_tool_action``).
    It does NOT drive the real construction path, so if you change that block
    (add/rename a registration, change the gate) update this helper too or the
    coverage here silently drifts from what construction.py actually does.
    """
    _provider = mem_registry.get_memory_registry().active()
    if _provider is not None and getattr(_provider, "is_external", False) and controller is not None:
        _names = controller.registry.list_action_names()
        if "session_search" not in _names:
            controller._register_session_search_action()
        if "recent_activity" not in _names:
            controller._register_recent_activity_action()
        if "memory" not in _names:
            controller._register_memory_tool_action()


def test_recent_activity_registers_when_backend_arrives_after_controller(monkeypatch):
    monkeypatch.setenv("EPISODIC_MEMORY_ENABLED", "true")
    mem_registry.get_memory_registry()  # default Null — no external backend yet
    c = _bare_controller()
    c._register_recent_activity_action()  # Controller.__init__ gate: skipped (no provider yet)
    assert "recent_activity" not in c.registry.list_action_names()

    # Backend registers later (construction.py maybe_register_memory_backend).
    mem_registry.set_external_memory_provider(_StubExternalProvider())
    _run_repair_block(c)
    assert "recent_activity" in c.registry.list_action_names()


def test_memory_tool_registers_when_backend_arrives_after_controller(monkeypatch):
    monkeypatch.setenv("MEMORY_TOOL_ENABLED", "true")
    mem_registry.get_memory_registry()  # default Null — no external backend yet
    c = _bare_controller()
    c._register_memory_tool_action()  # Controller.__init__ gate: skipped
    assert "memory" not in c.registry.list_action_names()

    class _CuratedProvider(_StubExternalProvider):
        async def curated_add(self, user_id, content):
            return True

        async def curated_read(self, user_id):
            return ""

        async def curated_remove(self, user_id, content):
            return 0

    mem_registry.set_external_memory_provider(_CuratedProvider())
    _run_repair_block(c)
    assert "memory" in c.registry.list_action_names()


def test_repair_block_is_idempotent():
    mem_registry.set_external_memory_provider(_StubExternalProvider())
    c = _bare_controller()
    _run_repair_block(c)
    names_after_first = set(c.registry.list_action_names())
    # A second, guarded call must not blow up or double-register.
    _run_repair_block(c)
    assert set(c.registry.list_action_names()) == names_after_first


def test_prompt_omits_recent_activity_when_no_external_provider(monkeypatch):
    """SK-F5: flag on, but MEMORY_BACKEND=none => no external provider => no mention."""
    monkeypatch.setenv("EPISODIC_MEMORY_ENABLED", "true")
    mem_registry.get_memory_registry()  # default Null provider, not external

    from agents.task.agent.prompts import SystemPrompt

    builder = object.__new__(SystemPrompt)
    content = builder._get_memory_system_content()
    assert "recent_activity" not in content


def test_prompt_mentions_recent_activity_when_external_provider_active(monkeypatch):
    """SK-F5: flag on AND external provider active => sentence appears."""
    monkeypatch.setenv("EPISODIC_MEMORY_ENABLED", "true")
    mem_registry.set_external_memory_provider(_StubExternalProvider())

    from agents.task.agent.prompts import SystemPrompt

    builder = object.__new__(SystemPrompt)
    content = builder._get_memory_system_content()
    assert "recent_activity" in content
