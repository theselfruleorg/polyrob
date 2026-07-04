import time
import pytest
from modules.memory.sqlite_memory_provider import SqliteMemoryProvider
import modules.memory.registry as reg
from modules.memory.episodic import finalize_episode, parse_since


@pytest.fixture
def provider(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_REQUIRE_USER_ID", "true")
    monkeypatch.setenv("EPISODIC_MEMORY_ENABLED", "true")
    p = SqliteMemoryProvider(str(tmp_path / "memory.db"))
    reg.reset_memory_registry()
    reg.set_external_memory_provider(p)
    yield p
    reg.reset_memory_registry()


@pytest.mark.asyncio
async def test_finalize_writes_row(provider):
    await finalize_episode(session_id="g1", user_id="u1", kind="goal",
                           task="draft tweet", outcome="done", spend_usd=0.42)
    out = await reg.memory_recall_episodes(user_id="u1", limit=5)
    assert len(out) == 1 and out[0].outcome == "done" and out[0].spend_usd == 0.42


@pytest.mark.asyncio
async def test_finalize_noop_when_flag_off(provider, monkeypatch):
    monkeypatch.setenv("EPISODIC_MEMORY_ENABLED", "false")
    await finalize_episode(session_id="g2", user_id="u1", kind="goal", outcome="done")
    assert await reg.memory_recall_episodes(user_id="u1", limit=5) == []


@pytest.mark.asyncio
async def test_finalize_noop_on_anon(provider):
    await finalize_episode(session_id="g3", user_id="", kind="goal", outcome="done")
    assert await reg.memory_recall_episodes(user_id="", limit=5) == []


@pytest.mark.asyncio
async def test_finalize_failopen(provider, monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(provider, "record_episode", boom)
    # must NOT raise
    await finalize_episode(session_id="g4", user_id="u1", kind="goal", outcome="done")


@pytest.mark.asyncio
async def test_router_noop_without_external_provider(monkeypatch):
    reg.reset_memory_registry()                     # default Null
    from modules.memory.provider import EpisodeRecord
    await reg.memory_record_episode(
        EpisodeRecord(ts=int(time.time()), user_id="u1", session_id="s", kind="goal"),
        session_id="s", user_id="u1")               # no crash
    assert await reg.memory_recall_episodes(user_id="u1") == []


def test_parse_since():
    now = int(time.time())
    assert abs(parse_since("8h") - (now - 8 * 3600)) <= 2
    assert abs(parse_since("2d") - (now - 2 * 86400)) <= 2
    assert abs(parse_since("30m") - (now - 30 * 60)) <= 2
    assert parse_since(None) is None
    assert parse_since("garbage") is None
    assert parse_since("2026-07-03T00:00:00Z") is not None
