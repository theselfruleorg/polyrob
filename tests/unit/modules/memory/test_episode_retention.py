"""Task 7: retention prune + surfaced dedup (RED-first).

Covers ``SqliteMemoryProvider.prune_episodes`` (global retention sweep, all
tenants) and the ``surfaced`` dedup flag consumed by the digest builder via
``exclude_surfaced``.
"""
import time

import pytest

from modules.memory.sqlite_memory_provider import SqliteMemoryProvider


@pytest.fixture
def provider(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_REQUIRE_USER_ID", "true")
    return SqliteMemoryProvider(str(tmp_path / "memory.db"))


@pytest.mark.asyncio
async def test_prune_deletes_old_keeps_recent(provider):
    from modules.memory.provider import EpisodeRecord

    now = int(time.time())
    for sid, dt in [("old", 100 * 86400), ("new", 1 * 86400)]:
        await provider.record_episode(
            EpisodeRecord(ts=now - dt, user_id="u1", session_id=sid, kind="goal"),
            session_id=sid, user_id="u1")
    removed = provider.prune_episodes(older_than_ts=now - 90 * 86400)
    assert removed == 1
    out = await provider.recall_episodes(user_id="u1", limit=10)
    assert [e.session_id for e in out] == ["new"]


@pytest.mark.asyncio
async def test_prune_across_all_tenants(provider):
    """prune_episodes is a GLOBAL retention sweep — not scoped to a single tenant."""
    from modules.memory.provider import EpisodeRecord

    now = int(time.time())
    await provider.record_episode(
        EpisodeRecord(ts=now - 200 * 86400, user_id="u1", session_id="old-u1", kind="goal"),
        session_id="old-u1", user_id="u1")
    await provider.record_episode(
        EpisodeRecord(ts=now - 200 * 86400, user_id="u2", session_id="old-u2", kind="goal"),
        session_id="old-u2", user_id="u2")
    removed = provider.prune_episodes(older_than_ts=now - 90 * 86400)
    assert removed == 2
    assert await provider.recall_episodes(user_id="u1", limit=10) == []
    assert await provider.recall_episodes(user_id="u2", limit=10) == []


def test_prune_fails_open_on_error(provider, monkeypatch):
    """A DB error must degrade to 0, never raise (curator tick calls this fire-and-forget)."""
    def boom(*a, **k):
        raise RuntimeError("disk full")
    monkeypatch.setattr(
        "modules.memory.sqlite_memory_provider.execute_retry", boom)
    assert provider.prune_episodes(older_than_ts=0) == 0


@pytest.mark.asyncio
async def test_mark_episode_surfaced_sets_flag(provider):
    from modules.memory.episodic import finalize_episode
    import modules.memory.registry as reg

    reg.reset_memory_registry()
    reg.set_external_memory_provider(provider)
    try:
        import os
        os_env = os.environ
        os_env["EPISODIC_MEMORY_ENABLED"] = "true"
        await finalize_episode(session_id="s1", user_id="u1", kind="goal", outcome="done")
        provider.mark_episode_surfaced(session_id="s1")
        # exclude_surfaced=True must now omit this row
        out = await provider.recall_episodes(user_id="u1", exclude_surfaced=True, limit=5)
        assert out == []
        # exclude_surfaced=False (default) must still include it
        out_all = await provider.recall_episodes(user_id="u1", limit=5)
        assert len(out_all) == 1
    finally:
        os_env.pop("EPISODIC_MEMORY_ENABLED", None)
        reg.reset_memory_registry()


@pytest.mark.asyncio
async def test_mark_episode_surfaced_is_tenant_scoped(provider):
    """FIX2: episodes are keyed on the composite (user_id, session_id) -- two
    tenants can legitimately share the same session_id string. Marking one
    tenant's episode surfaced must NOT flip the other tenant's row sharing
    that session_id."""
    from modules.memory.provider import EpisodeRecord

    now = int(time.time())
    await provider.record_episode(
        EpisodeRecord(ts=now, user_id="u1", session_id="shared", kind="goal"),
        session_id="shared", user_id="u1")
    await provider.record_episode(
        EpisodeRecord(ts=now, user_id="u2", session_id="shared", kind="goal"),
        session_id="shared", user_id="u2")

    provider.mark_episode_surfaced(session_id="shared", user_id="u1")

    out_u1 = await provider.recall_episodes(user_id="u1", exclude_surfaced=True, limit=5)
    out_u2 = await provider.recall_episodes(user_id="u2", exclude_surfaced=True, limit=5)
    assert out_u1 == []       # u1's row IS surfaced -> excluded
    assert len(out_u2) == 1   # u2's row must remain un-surfaced


def test_mark_episode_surfaced_fails_open(provider, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("disk full")
    monkeypatch.setattr(
        "modules.memory.sqlite_memory_provider.execute_retry", boom)
    # must not raise
    provider.mark_episode_surfaced(session_id="nope")


@pytest.mark.asyncio
async def test_digest_excludes_surfaced(provider):
    from modules.memory.episodic import finalize_episode
    import modules.memory.registry as reg
    import os

    reg.reset_memory_registry()
    reg.set_external_memory_provider(provider)
    os.environ["EPISODIC_MEMORY_ENABLED"] = "true"
    try:
        await finalize_episode(session_id="s", user_id="u1", kind="goal", outcome="done")
        provider.mark_episode_surfaced(session_id="s")
        out = await provider.recall_episodes(user_id="u1", exclude_surfaced=True, limit=5)
        assert out == []
    finally:
        os.environ.pop("EPISODIC_MEMORY_ENABLED", None)
        reg.reset_memory_registry()
