"""UP-03 — SQLite cross-session memory provider: tenant isolation + anonymous-bucket
safety (MEMORY_REQUIRE_USER_ID).

Default-ON backend, so these guard the multi-tenant baseline directly.
"""
import pytest

from modules.memory.sqlite_memory_provider import SqliteMemoryProvider


def _provider(tmp_path):
    return SqliteMemoryProvider(str(tmp_path / "memory.db"))


@pytest.mark.asyncio
async def test_recall_is_tenant_scoped(tmp_path):
    """Two distinct users never see each other's findings (regression for the FTS filter)."""
    p = _provider(tmp_path)
    await p.sync_turn("deploy plan", "ship to hetzner alpha", session_id="s1", user_id="alice")
    await p.sync_turn("deploy plan", "ship to render beta", session_id="s2", user_id="bob")

    a = await p.prefetch("deploy", session_id="sX", user_id="alice")
    b = await p.prefetch("deploy", session_id="sX", user_id="bob")
    assert "hetzner" in a and "render" not in a
    assert "render" in b and "hetzner" not in b


@pytest.mark.asyncio
async def test_empty_user_blocked_by_default(tmp_path, monkeypatch):
    """MEMORY_REQUIRE_USER_ID defaults true: empty user_id does no read/write."""
    monkeypatch.delenv("MEMORY_REQUIRE_USER_ID", raising=False)
    p = _provider(tmp_path)
    # write attempt with no user_id is skipped...
    await p.sync_turn("secret task", "anon result", session_id="s1", user_id="")
    await p.sync_turn("secret task", "anon result", session_id="s1", user_id=None)
    # ...so even an anon read finds nothing (nothing was stored in the "" bucket).
    assert await p.prefetch("secret", session_id="s1", user_id="") == ""
    # And a named user obviously sees nothing either.
    assert await p.prefetch("secret", session_id="s1", user_id="alice") == ""


@pytest.mark.asyncio
async def test_empty_user_allowed_when_flag_off(tmp_path, monkeypatch):
    """Single-user/local opt-out: MEMORY_REQUIRE_USER_ID=false restores the shared bucket."""
    monkeypatch.setenv("MEMORY_REQUIRE_USER_ID", "false")
    p = _provider(tmp_path)
    await p.sync_turn("local note", "remember this", session_id="s1", user_id="")
    recall = await p.prefetch("local", session_id="s2", user_id=None)
    assert "remember this" in recall


@pytest.mark.asyncio
async def test_empty_user_warns_once(tmp_path, monkeypatch, caplog):
    monkeypatch.delenv("MEMORY_REQUIRE_USER_ID", raising=False)
    p = _provider(tmp_path)
    import logging
    with caplog.at_level(logging.WARNING):
        await p.prefetch("x term", session_id="s1", user_id="")
        await p.prefetch("y term", session_id="s1", user_id="")
        await p.sync_turn("a", "b", session_id="s1", user_id="")
    warnings = [r for r in caplog.records if "anonymous/default user_id" in r.getMessage()]
    assert len(warnings) == 1  # one-time only
