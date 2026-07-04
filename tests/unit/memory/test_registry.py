"""P7 — process-global memory registry + agent-facing helpers."""
import pytest

from modules.memory.registry import (
    get_memory_registry, set_external_memory_provider, reset_memory_registry,
    memory_prefetch, memory_sync_turn,
)
from modules.memory.provider import MemoryProvider, NullMemoryProvider


class _Ext(MemoryProvider):
    def __init__(self):
        self.synced = []

    @property
    def name(self):
        return "ext"

    async def prefetch(self, query, *, session_id, user_id=None):
        return f"ctx[{query}]"

    async def sync_turn(self, user_content, assistant_content, *, session_id, user_id=None):
        self.synced.append((user_content, assistant_content, session_id, user_id))


@pytest.fixture(autouse=True)
def _clean():
    reset_memory_registry()
    yield
    reset_memory_registry()


def test_default_active_is_null():
    assert isinstance(get_memory_registry().active(), NullMemoryProvider)


@pytest.mark.asyncio
async def test_prefetch_and_sync_are_noops_by_default():
    assert await memory_prefetch("q", session_id="s") == ""
    await memory_sync_turn("u", "a", session_id="s")  # no-op, must not raise


@pytest.mark.asyncio
async def test_external_provider_is_routed_through():
    ext = _Ext()
    set_external_memory_provider(ext)
    assert await memory_prefetch("hello", session_id="s1", user_id="u1") == "ctx[hello]"
    await memory_sync_turn("u", "a", session_id="s1", user_id="u1")
    assert ext.synced == [("u", "a", "s1", "u1")]


@pytest.mark.asyncio
async def test_prefetch_fails_open_on_provider_error():
    class _Bad(_Ext):
        async def prefetch(self, query, *, session_id, user_id=None):
            raise RuntimeError("backend down")

    set_external_memory_provider(_Bad())
    assert await memory_prefetch("q", session_id="s") == ""  # swallowed
