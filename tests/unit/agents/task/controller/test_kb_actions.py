"""Task 7 — KB recall via `collection` param on session_search (TDD).

- With AutonomyConfig.kb_enabled() True and kb_search mocked, invoking
  session_search(collection="contracts") calls kb_search (NOT memory_search)
  with that collection, and result is wrapped <untrusted_tool_result source="knowledge_base">.
- collection=None => routes to memory_search (existing behavior, unchanged).
- KB disabled (kb_enabled() False) + collection set => falls back to memory_search.
"""
import logging
from unittest.mock import AsyncMock, patch

import agents.task.agent.service  # noqa: F401 — avoid import cycle
import pytest

from modules.memory.provider import MemoryProvider
from modules.memory import registry as mem_registry
from tools.controller.registry.service import Registry
from tools.controller.service import Controller


class _StubExternalProvider(MemoryProvider):
    is_external = True
    name = "stub-external"

    def __init__(self):
        self.search_calls = []
        self.kb_search_calls = []

    async def initialize(self):  # pragma: no cover
        pass

    async def prefetch(self, query, *, session_id, user_id=None):
        return ""

    async def search(self, query, *, user_id=None, session_id=None, limit=5, sort=None):
        self.search_calls.append({"query": query, "limit": limit, "sort": sort})
        return f"session_result:{query}"

    async def sync_turn(self, user_content, assistant_content, *, session_id, user_id=None):
        pass

    async def kb_search(self, query, *, user_id=None, collection="default", limit=8):
        self.kb_search_calls.append({"query": query, "collection": collection, "limit": limit})
        return f"kb_result:{collection}:{query}"


def _bare_controller():
    c = object.__new__(Controller)
    c.logger = logging.getLogger("kb-actions-test")
    c.registry = Registry()
    c.user_id = "tenant-A"
    c.session_id = "sess-1"
    return c


@pytest.fixture(autouse=True)
def _clean_registry():
    mem_registry.reset_memory_registry()
    yield
    mem_registry.reset_memory_registry()


@pytest.mark.asyncio
async def test_collection_routes_to_kb_search_when_kb_enabled(monkeypatch):
    """collection="contracts" + kb_enabled=True => kb_search called, not memory_search."""
    monkeypatch.setenv("KB_ENABLED", "true")
    prov = _StubExternalProvider()
    mem_registry.set_external_memory_provider(prov)

    kb_mock = AsyncMock(return_value="contract documents found")
    with patch("modules.memory.registry.kb_search", kb_mock):
        c = _bare_controller()
        c._register_session_search_action()
        action = c.registry.registry.actions["session_search"]
        params = action.param_model(query="indemnity clause", collection="contracts")
        res = await action.function(params, execution_context=None)

    # kb_search was called exactly once and FORWARDED the collection (+ query/limit)
    kb_mock.assert_awaited_once()
    call = kb_mock.call_args
    assert call.kwargs["collection"] == "contracts", "collection must be forwarded to kb_search"
    assert call.args[0] == "indemnity clause", "query must be forwarded (positional)"
    assert call.kwargs["limit"] == 5, "limit must be forwarded to kb_search"

    # memory_search was NOT called (provider.search untouched)
    assert prov.search_calls == [], "memory_search must not be called when KB route taken"

    # result is wrapped with source="knowledge_base"
    assert "<untrusted_tool_result" in res.extracted_content
    assert 'source="knowledge_base"' in res.extracted_content
    # header present
    assert "knowledge base" in res.extracted_content.lower()


@pytest.mark.asyncio
async def test_collection_none_routes_to_memory_search(monkeypatch):
    """collection=None => existing memory_search path, byte-identical."""
    monkeypatch.setenv("KB_ENABLED", "true")
    prov = _StubExternalProvider()
    mem_registry.set_external_memory_provider(prov)

    kb_mock = AsyncMock(return_value="should not be called")
    with patch("modules.memory.registry.kb_search", kb_mock):
        c = _bare_controller()
        c._register_session_search_action()
        action = c.registry.registry.actions["session_search"]
        params = action.param_model(query="widget", limit=3)  # no collection
        res = await action.function(params, execution_context=None)

    # kb_search NOT called
    kb_mock.assert_not_awaited()
    # memory_search was called (provider.search had a call)
    assert len(prov.search_calls) == 1
    assert prov.search_calls[0]["query"] == "widget"

    # normal session_search wrap
    assert 'source="session_search"' in res.extracted_content


@pytest.mark.asyncio
async def test_kb_disabled_with_collection_falls_back_to_memory_search(monkeypatch):
    """KB disabled => falls back to memory_search even if collection is set."""
    monkeypatch.setenv("KB_ENABLED", "false")
    prov = _StubExternalProvider()
    mem_registry.set_external_memory_provider(prov)

    kb_mock = AsyncMock(return_value="should not be called")
    with patch("modules.memory.registry.kb_search", kb_mock):
        c = _bare_controller()
        c._register_session_search_action()
        action = c.registry.registry.actions["session_search"]
        params = action.param_model(query="contract", collection="contracts")
        res = await action.function(params, execution_context=None)

    # kb_search NOT called when disabled
    kb_mock.assert_not_awaited()
    # memory_search called as fallback
    assert len(prov.search_calls) == 1
    # no crash
    assert res.extracted_content  # non-empty


@pytest.mark.asyncio
async def test_kb_exception_falls_through_to_memory_search(monkeypatch):
    """KB branch raises => fail-open: memory_search still runs."""
    monkeypatch.setenv("KB_ENABLED", "true")
    prov = _StubExternalProvider()
    mem_registry.set_external_memory_provider(prov)

    kb_mock = AsyncMock(side_effect=RuntimeError("kb_search blown up"))
    with patch("modules.memory.registry.kb_search", kb_mock):
        c = _bare_controller()
        c._register_session_search_action()
        action = c.registry.registry.actions["session_search"]
        params = action.param_model(query="docs", collection="manuals")
        res = await action.function(params, execution_context=None)

    # Despite KB error, memory_search fallback ran — no crash
    assert len(prov.search_calls) == 1
    assert res.extracted_content  # non-empty, not an exception
