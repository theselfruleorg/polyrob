"""Memory tenant guard must block the non-empty anonymous sentinels (findings F1).

Before the fix, `_anon_blocked` only treated the empty string as anonymous, so the
non-empty sentinels callers actually pass ("_anonymous_", "system", synthetic server
placeholders) sailed through and shared one mutually-readable bucket — silently
bypassing MEMORY_REQUIRE_USER_ID. `"local"` (the real CLI tenant) must stay allowed.
"""
import pytest

from modules.memory.sqlite_memory_provider import SqliteMemoryProvider
from modules.memory.local_vector_memory_provider import LocalVectorMemoryProvider


def _provider(tmp_path):
    return SqliteMemoryProvider(str(tmp_path / "memory.db"))


def test_anonymous_sentinels_blocked_under_require(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_REQUIRE_USER_ID", "true")
    p = _provider(tmp_path)
    for sentinel in ("_anonymous_", "system", "x402_user", "authenticated_api_user"):
        assert p._anon_blocked(sentinel) is True, sentinel


def test_real_tenants_not_blocked_under_require(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_REQUIRE_USER_ID", "true")
    p = _provider(tmp_path)
    assert p._anon_blocked("local") is False
    assert p._anon_blocked("alice") is False


def test_opt_out_allows_anonymous(tmp_path, monkeypatch):
    """Single-user/local escape hatch is unchanged: =false restores the shared bucket."""
    monkeypatch.setenv("MEMORY_REQUIRE_USER_ID", "false")
    p = _provider(tmp_path)
    assert p._anon_blocked("_anonymous_") is False
    assert p._anon_blocked("") is False


@pytest.mark.asyncio
async def test_anonymous_recall_isolated_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_REQUIRE_USER_ID", "true")
    p = _provider(tmp_path)
    # write under the anon sentinel is skipped...
    await p.sync_turn("secret plan", "anon result", session_id="s1", user_id="_anonymous_")
    # ...so an anon read finds nothing (nothing was stored).
    assert await p.prefetch("secret", session_id="s2", user_id="_anonymous_") == ""


def test_local_vector_inherits_the_guard(tmp_path, monkeypatch):
    """The vector subclass must not silently override the guard (regression lock)."""
    assert LocalVectorMemoryProvider._anon_blocked is SqliteMemoryProvider._anon_blocked
    monkeypatch.setenv("MEMORY_REQUIRE_USER_ID", "true")
    p = LocalVectorMemoryProvider(str(tmp_path / "memory.db"))
    assert p._anon_blocked("_anonymous_") is True
    assert p._anon_blocked("local") is False
