"""UP-09 Step 9.3 — session_search multi-shape + provider-gating.

- Registered ONLY when an external memory provider is active (byte-identical default).
- Routes query/limit/sort to memory_search (which hits provider.search).
- Tenant scoping / empty-user_id refusal is the PROVIDER's job (UP-03 _anon_blocked) —
  the controller must NOT re-read MEMORY_REQUIRE_USER_ID.
"""
import logging

import agents.task.agent.service  # noqa: F401 — avoid import cycle
import pytest

from modules.memory.provider import MemoryProvider, NullMemoryProvider
from modules.memory import registry as mem_registry
from tools.controller.registry.service import Registry
from tools.controller.service import Controller


class _StubExternalProvider(MemoryProvider):
    is_external = True
    name = "stub-external"

    def __init__(self):
        self.calls = []

    async def initialize(self):  # pragma: no cover - abstract satisfier
        pass

    async def prefetch(self, query, *, session_id, user_id=None):
        return f"prefetch:{query}"

    async def search(self, query, *, user_id=None, session_id=None, limit=5, sort=None):
        self.calls.append({"query": query, "user_id": user_id, "limit": limit, "sort": sort})
        return f"hit:{query}:{limit}:{sort}" if query else f"browse:{limit}"

    async def sync_turn(self, user_content, assistant_content, *, session_id, user_id=None):
        pass


def _bare_controller():
    c = object.__new__(Controller)
    c.logger = logging.getLogger("session-search-test")
    c.registry = Registry()
    c.user_id = "tenant-A"
    c.session_id = "sess-1"
    return c


@pytest.fixture(autouse=True)
def _clean_registry():
    mem_registry.reset_memory_registry()
    yield
    mem_registry.reset_memory_registry()


def test_not_registered_with_null_provider():
    mem_registry.get_memory_registry()  # default Null
    c = _bare_controller()
    c._register_session_search_action()
    assert "session_search" not in c.registry.registry.actions


def test_registered_with_external_provider():
    mem_registry.set_external_memory_provider(_StubExternalProvider())
    c = _bare_controller()
    c._register_session_search_action()
    assert "session_search" in c.registry.registry.actions


@pytest.mark.asyncio
async def test_discover_routes_limit_sort():
    prov = _StubExternalProvider()
    mem_registry.set_external_memory_provider(prov)
    c = _bare_controller()
    c._register_session_search_action()
    action = c.registry.registry.actions["session_search"]
    params = action.param_model(query="widget", limit=3, sort="newest")
    res = await action.function(params, execution_context=None)
    assert prov.calls == [{"query": "widget", "user_id": "tenant-A", "limit": 3, "sort": "newest"}]
    assert "hit:widget:3:newest" in res.extracted_content


@pytest.mark.asyncio
async def test_browse_empty_query():
    prov = _StubExternalProvider()
    mem_registry.set_external_memory_provider(prov)
    c = _bare_controller()
    c._register_session_search_action()
    action = c.registry.registry.actions["session_search"]
    params = action.param_model()  # query defaults to ""
    res = await action.function(params, execution_context=None)
    assert prov.calls[0]["query"] == ""
    assert "most-recent sessions" in res.extracted_content
