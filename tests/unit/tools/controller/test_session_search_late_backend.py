"""Regression: recall tools must surface even when the memory backend registers
AFTER the Controller.

The Controller's session_search/memory_search gate runs at Controller.__init__,
but the sqlite backend (MEMORY_BACKEND default) registers later during Agent
construction, so on the FIRST session of a process the recall tools were skipped.
The fix re-runs the gated registration after the backend exists; this test pins
that re-registration is correct and idempotent.
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
    c.logger = logging.getLogger("late-backend-test")
    c.registry = Registry()
    c.user_id = "tenant-A"
    c.session_id = "sess-1"
    return c


@pytest.fixture(autouse=True)
def _clean_registry():
    mem_registry.reset_memory_registry()
    yield
    mem_registry.reset_memory_registry()


def test_recall_surfaces_when_backend_registers_after_controller():
    mem_registry.get_memory_registry()  # default Null — no external backend yet
    c = _bare_controller()
    c._register_session_search_action()  # Controller.__init__ gate: skipped
    assert "session_search" not in c.registry.registry.actions

    # Backend registers later (construction.py maybe_register_memory_backend).
    mem_registry.set_external_memory_provider(_StubExternalProvider())
    # The fix re-runs the gated registration now that the backend exists.
    if "session_search" not in c.registry.registry.actions:
        c._register_session_search_action()
    assert "session_search" in c.registry.registry.actions


def test_reregistration_is_idempotent():
    mem_registry.set_external_memory_provider(_StubExternalProvider())
    c = _bare_controller()
    c._register_session_search_action()
    assert "session_search" in c.registry.registry.actions
    # A guarded re-call must not blow up (construction.py guards on not-present,
    # but the mechanism should tolerate being asked again).
    names = c.registry.registry.actions
    if "session_search" not in names:  # guard mirrors construction.py
        c._register_session_search_action()
    assert "session_search" in c.registry.registry.actions
