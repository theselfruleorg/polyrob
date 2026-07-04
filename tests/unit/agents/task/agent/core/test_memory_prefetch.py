"""B-T2 — memory prefetch into the step path.

`build_prefetch_message` routes a query through the active memory provider and wraps
any recalled text as a MEMORY-origin control message for injection. With the default
Null provider it returns None (prod unchanged); with an external provider it returns
an enveloped HumanMessage. Fail-open: a provider error yields None, never raises.
"""
import pytest

from modules.memory.provider import NullMemoryProvider
from modules.memory.registry import (
    reset_memory_registry,
    set_external_memory_provider,
)
from modules.llm.messages import MessageOrigin
from agents.task.agent.core.memory_prefetch import build_prefetch_message


class _FakeExternalProvider(NullMemoryProvider):
    def __init__(self, recalled):
        self._recalled = recalled

    @property
    def is_external(self) -> bool:
        return True

    async def prefetch(self, query, *, session_id, user_id=None):
        return self._recalled


class _BoomProvider(NullMemoryProvider):
    @property
    def is_external(self) -> bool:
        return True

    async def prefetch(self, query, *, session_id, user_id=None):
        raise RuntimeError("backend down")


@pytest.fixture(autouse=True)
def _clean_registry():
    reset_memory_registry()
    yield
    reset_memory_registry()


@pytest.mark.asyncio
async def test_null_provider_returns_none():
    # Default registry (Null) -> no injection.
    assert await build_prefetch_message("what did we do?", session_id="s1") is None


@pytest.mark.asyncio
async def test_external_recall_returns_memory_message():
    set_external_memory_provider(_FakeExternalProvider("User prefers dark mode."))
    msg = await build_prefetch_message("preferences?", session_id="s1")
    assert msg is not None
    assert getattr(msg, "origin", None) == MessageOrigin.RECALL
    assert "User prefers dark mode." in msg.content


@pytest.mark.asyncio
async def test_empty_recall_returns_none():
    set_external_memory_provider(_FakeExternalProvider("   "))
    assert await build_prefetch_message("preferences?", session_id="s1") is None


@pytest.mark.asyncio
async def test_provider_error_is_fail_open():
    set_external_memory_provider(_BoomProvider())
    # memory_prefetch swallows the error -> "" -> None, no exception.
    assert await build_prefetch_message("preferences?", session_id="s1") is None


@pytest.mark.asyncio
async def test_blank_query_returns_none():
    set_external_memory_provider(_FakeExternalProvider("something"))
    assert await build_prefetch_message("", session_id="s1") is None
