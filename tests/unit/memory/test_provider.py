"""P7 — MemoryProvider ABC, NullMemoryProvider, one-provider registry."""
import pytest

from modules.memory.provider import (
    MemoryProvider, NullMemoryProvider, MemoryProviderRegistry, MemoryProviderError,
)


# --- ABC contract ------------------------------------------------------------

def test_cannot_instantiate_abstract():
    with pytest.raises(TypeError):
        MemoryProvider()  # abstract methods unimplemented


class _Stub(MemoryProvider):
    @property
    def name(self):
        return "stub"

    async def prefetch(self, query, *, session_id):
        return f"ctx:{query}"

    async def sync_turn(self, user_content, assistant_content, *, session_id):
        self.synced = (user_content, assistant_content)


@pytest.mark.asyncio
async def test_optional_hooks_default_to_noops():
    p = _Stub()
    assert p.get_tool_schemas() == []
    assert await p.is_available() is True
    # optional lifecycle hooks exist and are no-ops
    await p.initialize(session_id="s")
    await p.on_session_end(session_id="s")
    await p.shutdown()
    assert p.is_external is True


@pytest.mark.asyncio
async def test_stub_stateful_ops():
    p = _Stub()
    assert await p.prefetch("q", session_id="s") == "ctx:q"
    await p.sync_turn("u", "a", session_id="s")
    assert p.synced == ("u", "a")


# --- NullMemoryProvider ------------------------------------------------------

@pytest.mark.asyncio
async def test_null_provider_is_inert_and_not_external():
    n = NullMemoryProvider()
    assert n.is_external is False
    assert n.get_tool_schemas() == []
    assert await n.prefetch("q", session_id="s") == ""
    await n.sync_turn("u", "a", session_id="s")  # no-op, must not raise


# --- one-provider registry ---------------------------------------------------

def test_registry_allows_one_external_plus_null():
    reg = MemoryProviderRegistry()
    reg.register(NullMemoryProvider())
    reg.register(_Stub())  # first external ok
    assert reg.active().name == "stub"  # external wins over null


def test_registry_rejects_second_external():
    reg = MemoryProviderRegistry()
    reg.register(_Stub())
    with pytest.raises(MemoryProviderError):
        reg.register(_Stub())


def test_registry_active_falls_back_to_null():
    reg = MemoryProviderRegistry()
    n = NullMemoryProvider()
    reg.register(n)
    assert reg.active() is n


def test_registry_empty_active_is_none():
    assert MemoryProviderRegistry().active() is None
