"""SQLite FTS memory provider: sync_turn persists; prefetch recalls across sessions."""
import pytest
from modules.memory.sqlite_memory_provider import SqliteMemoryProvider


@pytest.mark.asyncio
async def test_sync_then_prefetch_same_session(tmp_path, monkeypatch):
    # Exercises the anon/shared-"" bucket path: allow empty-user_id recall.
    monkeypatch.setenv("MEMORY_REQUIRE_USER_ID", "false")
    p = SqliteMemoryProvider(str(tmp_path / "mem.db"))
    await p.sync_turn("what is the api base url?", "the base url is https://api.acme.test", session_id="s1")
    out = await p.prefetch("api base url", session_id="s1")
    assert "api.acme.test" in out


@pytest.mark.asyncio
async def test_prefetch_recalls_across_sessions(tmp_path):
    p = SqliteMemoryProvider(str(tmp_path / "mem.db"))
    await p.sync_turn("remember the deploy host", "deploy host is 192.0.2.1",
                      session_id="old", user_id="u1")
    out = await p.prefetch("deploy host", session_id="new", user_id="u1")  # different session, same user
    assert "192.0.2.1" in out


@pytest.mark.asyncio
async def test_prefetch_is_user_scoped_no_cross_tenant_leak(tmp_path):
    # P0-0 correctness: one user's memory must NEVER appear in another user's recall.
    p = SqliteMemoryProvider(str(tmp_path / "mem.db"))
    await p.sync_turn("my secret api key", "secret is sk-USERA-PRIVATE",
                      session_id="sa", user_id="userA")
    leaked = await p.prefetch("secret api key", session_id="sb", user_id="userB")
    assert "sk-USERA-PRIVATE" not in leaked
    assert leaked == ""
    # userA still recalls their own across their sessions
    own = await p.prefetch("secret api key", session_id="sa2", user_id="userA")
    assert "sk-USERA-PRIVATE" in own


@pytest.mark.asyncio
async def test_prefetch_none_user_isolated_from_named_user(tmp_path, monkeypatch):
    # This test asserts None-user "recalls None" — only meaningful when the
    # shared-"" bucket is allowed (MEMORY_REQUIRE_USER_ID defaults true otherwise).
    monkeypatch.setenv("MEMORY_REQUIRE_USER_ID", "false")
    p = SqliteMemoryProvider(str(tmp_path / "mem.db"))
    await p.sync_turn("anon note", "anon-only-value", session_id="s1")  # user_id defaults None
    assert "anon-only-value" not in await p.prefetch("anon note", session_id="s2", user_id="userX")
    assert "anon-only-value" in await p.prefetch("anon note", session_id="s2")  # None recalls None


@pytest.mark.asyncio
async def test_prefetch_empty_when_no_match(tmp_path):
    p = SqliteMemoryProvider(str(tmp_path / "mem.db"))
    assert await p.prefetch("nothing stored yet", session_id="s1") == ""


def test_is_external_true(tmp_path):
    assert SqliteMemoryProvider(str(tmp_path / "m.db")).is_external is True


def test_factory_registers_when_env_set(monkeypatch, tmp_path):
    from modules.memory.registry import get_memory_registry
    from modules.memory.backend_factory import maybe_register_memory_backend
    get_memory_registry().clear()
    monkeypatch.setenv("MEMORY_BACKEND", "sqlite")
    p = maybe_register_memory_backend(data_dir=str(tmp_path))
    assert p is not None
    assert get_memory_registry().active() is p
    get_memory_registry().clear()


def test_factory_on_by_default_when_unset(monkeypatch, tmp_path):
    # P0-1: cross-session memory is default-on (unset MEMORY_BACKEND => sqlite).
    from modules.memory.registry import get_memory_registry
    from modules.memory.backend_factory import maybe_register_memory_backend
    get_memory_registry().clear()
    monkeypatch.delenv("MEMORY_BACKEND", raising=False)
    p = maybe_register_memory_backend(data_dir=str(tmp_path))
    assert p is not None
    assert get_memory_registry().active() is p
    get_memory_registry().clear()


def test_factory_disabled_when_explicitly_off(monkeypatch, tmp_path):
    from modules.memory.registry import get_memory_registry
    from modules.memory.backend_factory import maybe_register_memory_backend
    get_memory_registry().clear()
    monkeypatch.setenv("MEMORY_BACKEND", "none")
    assert maybe_register_memory_backend(data_dir=str(tmp_path)) is None
    get_memory_registry().clear()


def test_factory_idempotent_second_call(monkeypatch, tmp_path):
    # Called once per agent construction; a second call (sibling agent, same process)
    # must reuse the active external provider, not raise the one-external-provider error.
    from modules.memory.registry import get_memory_registry
    from modules.memory.backend_factory import maybe_register_memory_backend
    get_memory_registry().clear()
    monkeypatch.setenv("MEMORY_BACKEND", "sqlite")
    first = maybe_register_memory_backend(data_dir=str(tmp_path))
    second = maybe_register_memory_backend(data_dir=str(tmp_path))
    assert first is not None
    assert second is first                       # same provider reused, no error
    assert get_memory_registry().active() is first
    get_memory_registry().clear()
