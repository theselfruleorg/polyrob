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


# --------------------------------------------------------------------------------- #
# T2.6 — before_id pagination + id/hint line, at the action layer
# --------------------------------------------------------------------------------- #

def test_before_id_field_validation():
    """before_id is Optional[int], ge=1 — rejects 0/negative, None and >=1 are fine."""
    prov = _StubExternalProvider()
    mem_registry.set_external_memory_provider(prov)
    c = _bare_controller()
    c._register_session_search_action()
    action = c.registry.registry.actions["session_search"]
    action.param_model(query="x")  # before_id omitted -> default None, fine
    action.param_model(query="x", before_id=1)  # fine
    action.param_model(query="x", before_id=42)  # fine
    with pytest.raises(Exception):
        action.param_model(query="x", before_id=0)
    with pytest.raises(Exception):
        action.param_model(query="x", before_id=-5)


class _T26StubProvider(MemoryProvider):
    """A T2.6-aware provider double: `search` accepts before_id/with_ids and embeds
    `(id N)` tags exactly like the real SqliteMemoryProvider format, so the ACTION's
    hint-line construction (regex over the returned text) can be exercised without
    a real sqlite backend."""
    is_external = True
    name = "t26-stub"

    def __init__(self, rows):
        self.rows = rows  # list of (rowid, content), newest (largest id) first
        self.calls = []

    async def initialize(self):  # pragma: no cover
        pass

    async def prefetch(self, query, *, session_id, user_id=None):
        return ""

    async def search(self, query, *, user_id=None, session_id=None, limit=5,
                     sort=None, before_id=None, with_ids=False):
        self.calls.append({"before_id": before_id, "with_ids": with_ids, "limit": limit})
        rows = [r for r in self.rows if before_id is None or r[0] < before_id]
        rows = rows[:limit]
        lines = [f"- {content}" + (f" (id {rowid})" if with_ids else "")
                 for rowid, content in rows]
        return "\n".join(lines)

    async def sync_turn(self, user_content, assistant_content, *, session_id, user_id=None):
        pass


@pytest.mark.asyncio
async def test_hint_line_appended_when_full_page_newest_sort():
    """sort="newest" is the one lossless forward-pagination mode — the concrete
    before_id=<smallest id> hint is only advertised there (T2.6 review fix)."""
    prov = _T26StubProvider([(5, "e"), (4, "d"), (3, "c"), (2, "b"), (1, "a")])
    mem_registry.set_external_memory_provider(prov)
    c = _bare_controller()
    c._register_session_search_action()
    action = c.registry.registry.actions["session_search"]
    params = action.param_model(query="x", limit=2, sort="newest")
    res = await action.function(params, execution_context=None)
    assert "(id 5)" in res.extracted_content and "(id 4)" in res.extracted_content
    assert "More available: pass before_id=4 to page further." in res.extracted_content


@pytest.mark.asyncio
async def test_rank_mode_full_page_gets_refine_nudge_not_before_id_hint():
    """Review-fix proof case: for the default rank sort, before_id narrows MATCH
    candidates BEFORE ranking — a rank-ordered page1 like ids [2, 4] followed by
    before_id=2 can permanently strand an id (e.g. 3 or 5) that ranked between
    them. So rank mode (sort=None) must NEVER show the concrete before_id hint,
    even on a full page — only an honest "refine or switch to newest" nudge."""
    prov = _T26StubProvider([(5, "e"), (4, "d"), (3, "c"), (2, "b"), (1, "a")])
    mem_registry.set_external_memory_provider(prov)
    c = _bare_controller()
    c._register_session_search_action()
    action = c.registry.registry.actions["session_search"]
    params = action.param_model(query="x", limit=2)  # sort defaults to None (rank)
    res = await action.function(params, execution_context=None)
    assert "before_id=" not in res.extracted_content
    assert "Refine the query or use sort='newest' with before_id to page chronologically." \
        in res.extracted_content


@pytest.mark.asyncio
async def test_oldest_mode_full_page_gets_no_hint_at_all():
    prov = _T26StubProvider([(5, "e"), (4, "d"), (3, "c"), (2, "b"), (1, "a")])
    mem_registry.set_external_memory_provider(prov)
    c = _bare_controller()
    c._register_session_search_action()
    action = c.registry.registry.actions["session_search"]
    params = action.param_model(query="x", limit=2, sort="oldest")
    res = await action.function(params, execution_context=None)
    assert "before_id=" not in res.extracted_content
    assert "Refine the query" not in res.extracted_content
    assert "More available" not in res.extracted_content


@pytest.mark.asyncio
async def test_no_hint_line_when_partial_page():
    prov = _T26StubProvider([(2, "b"), (1, "a")])
    mem_registry.set_external_memory_provider(prov)
    c = _bare_controller()
    c._register_session_search_action()
    action = c.registry.registry.actions["session_search"]
    params = action.param_model(query="x", limit=5, sort="newest")  # only 2 rows exist
    res = await action.function(params, execution_context=None)
    assert "More available" not in res.extracted_content
    assert "Refine the query" not in res.extracted_content


@pytest.mark.asyncio
async def test_before_id_passed_through_to_provider():
    prov = _T26StubProvider([(5, "e"), (4, "d"), (3, "c"), (2, "b"), (1, "a")])
    mem_registry.set_external_memory_provider(prov)
    c = _bare_controller()
    c._register_session_search_action()
    action = c.registry.registry.actions["session_search"]
    params = action.param_model(query="x", limit=2, before_id=4)
    res = await action.function(params, execution_context=None)
    assert prov.calls[-1]["before_id"] == 4
    assert prov.calls[-1]["with_ids"] is True
    # page filtered strictly below 4: only ids 3, 2 in range, limit=2 -> both returned
    assert "(id 3)" in res.extracted_content and "(id 2)" in res.extracted_content
    assert "(id 4)" not in res.extracted_content and "(id 5)" not in res.extracted_content
