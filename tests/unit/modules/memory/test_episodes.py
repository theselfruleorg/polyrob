import time
import pytest
from core.sqlite_util import wal_connect
from modules.memory.sqlite_memory_provider import SqliteMemoryProvider
from modules.memory.provider import EpisodeRecord


@pytest.fixture
def provider(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_REQUIRE_USER_ID", "true")
    return SqliteMemoryProvider(str(tmp_path / "memory.db"))


def _rec(**kw):
    base = dict(ts=int(time.time()), user_id="u1", session_id="s1", kind="goal",
                task="do a thing", outcome="done", summary="did the thing",
                artifacts=[{"path": "out.md"}], spend_usd=0.11, steps=4)
    base.update(kw)
    return EpisodeRecord(**base)


@pytest.mark.asyncio
async def test_record_then_recall_roundtrip(provider):
    now = int(time.time())
    await provider.record_episode(_rec(session_id="a", ts=now - 30, task="A"),
                                  session_id="a", user_id="u1")
    await provider.record_episode(_rec(session_id="b", ts=now - 10, task="B"),
                                  session_id="b", user_id="u1")
    out = await provider.recall_episodes(user_id="u1", limit=10)
    assert [e.task for e in out] == ["B", "A"]          # newest-first
    assert out[0].spend_usd == 0.11 and out[0].kind == "goal"
    assert out[0].artifacts == [{"path": "out.md"}]     # JSON round-trips


@pytest.mark.asyncio
async def test_recall_since_ts_window(provider):
    now = int(time.time())
    for sid, dt in [("old", 10 * 3600), ("mid", 5 * 3600), ("new", 1 * 3600)]:
        await provider.record_episode(_rec(session_id=sid, ts=now - dt, task=sid),
                                      session_id=sid, user_id="u1")
    out = await provider.recall_episodes(user_id="u1", since_ts=now - 8 * 3600, limit=10)
    assert {e.task for e in out} == {"mid", "new"}      # "old" (10h) excluded


@pytest.mark.asyncio
async def test_recall_kind_filter(provider):
    now = int(time.time())
    await provider.record_episode(_rec(session_id="g", kind="goal"), session_id="g", user_id="u1")
    await provider.record_episode(_rec(session_id="c", kind="cron"), session_id="c", user_id="u1")
    out = await provider.recall_episodes(user_id="u1", kind="goal", limit=10)
    assert [e.kind for e in out] == ["goal"]


@pytest.mark.asyncio
async def test_upsert_idempotent_on_session_id(provider):
    await provider.record_episode(_rec(session_id="s", outcome="partial", spend_usd=0.05, steps=2),
                                  session_id="s", user_id="u1")
    await provider.record_episode(_rec(session_id="s", outcome="done", spend_usd=0.20, steps=9),
                                  session_id="s", user_id="u1")
    out = await provider.recall_episodes(user_id="u1", limit=10)
    assert len(out) == 1                                 # one row, not two
    assert out[0].outcome == "done" and out[0].spend_usd == 0.20 and out[0].steps == 9


@pytest.mark.asyncio
async def test_tenant_scope(provider):
    await provider.record_episode(_rec(session_id="a", user_id="u1"), session_id="a", user_id="u1")
    await provider.record_episode(_rec(session_id="b", user_id="u2"), session_id="b", user_id="u2")
    assert len(await provider.recall_episodes(user_id="u1", limit=10)) == 1
    assert len(await provider.recall_episodes(user_id="u2", limit=10)) == 1


@pytest.mark.asyncio
async def test_anon_refused_when_require_user_id(provider):
    await provider.record_episode(_rec(session_id="x", user_id=""), session_id="x", user_id="")
    assert await provider.recall_episodes(user_id="", limit=10) == []


@pytest.mark.asyncio
async def test_thread_key_filter(provider):
    now = int(time.time())
    await provider.record_episode(_rec(session_id="c1", kind="chat", thread_key="tg:42"),
                                  session_id="c1", user_id="u1")
    await provider.record_episode(_rec(session_id="c2", kind="chat", thread_key="tg:99"),
                                  session_id="c2", user_id="u1")
    out = await provider.recall_episodes(user_id="u1", thread_key="tg:42", limit=5)
    assert [e.session_id for e in out] == ["c1"]


@pytest.mark.asyncio
async def test_memories_store_unchanged(provider):
    # episodic writes must not touch the relevance store
    await provider.record_episode(_rec(session_id="s"), session_id="s", user_id="u1")
    assert await provider.search("thing", user_id="u1") == ""   # memories still empty


@pytest.mark.asyncio
async def test_null_provider_noop(tmp_path):
    from modules.memory.provider import NullMemoryProvider
    p = NullMemoryProvider()
    await p.record_episode(_rec(), session_id="s", user_id="u1")   # no crash
    assert await p.recall_episodes(user_id="u1") == []


@pytest.mark.asyncio
async def test_same_session_id_different_user_isolated(provider):
    # Two different tenants sharing a caller-supplied session_id string must NOT
    # merge into one row (review fix 1: composite (user_id, session_id) key).
    await provider.record_episode(_rec(session_id="shared", user_id="u1", task="A"),
                                  session_id="shared", user_id="u1")
    await provider.record_episode(_rec(session_id="shared", user_id="u2", task="B"),
                                  session_id="shared", user_id="u2")
    out_u1 = await provider.recall_episodes(user_id="u1", limit=10)
    out_u2 = await provider.recall_episodes(user_id="u2", limit=10)
    assert len(out_u1) == 1 and out_u1[0].task == "A"
    assert len(out_u2) == 1 and out_u2[0].task == "B"


@pytest.mark.asyncio
async def test_migrates_stale_single_column_index(tmp_path, monkeypatch):
    # Simulate a DB that already ran the pre-fix schema: episodes table +
    # a single-column UNIQUE index named idx_episodes_session ON (session_id).
    # CREATE ... IF NOT EXISTS matches by NAME only, so if _init_schema still
    # (re)used that same name for the composite index, the stale single-column
    # definition would survive and ON CONFLICT(user_id, session_id) would have
    # no matching unique constraint -> silent-dark writes (review round 2 bug).
    monkeypatch.setenv("MEMORY_REQUIRE_USER_ID", "true")
    db_path = str(tmp_path / "memory.db")
    conn = wal_connect(db_path)
    conn.execute(
        "CREATE TABLE episodes ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "ts INTEGER NOT NULL, started_ts INTEGER, "
        "user_id TEXT NOT NULL, session_id TEXT NOT NULL, thread_key TEXT, "
        "kind TEXT NOT NULL, task TEXT, outcome TEXT, summary TEXT, "
        "artifacts TEXT NOT NULL DEFAULT '[]', spend_usd REAL NOT NULL DEFAULT 0, "
        "steps INTEGER NOT NULL DEFAULT 0, goal_id TEXT, "
        "surfaced INTEGER NOT NULL DEFAULT 0, meta TEXT NOT NULL DEFAULT '{}', "
        "created_at INTEGER NOT NULL)"
    )
    conn.execute(
        "CREATE UNIQUE INDEX idx_episodes_session ON episodes(session_id)"
    )
    conn.commit()
    conn.close()

    provider = SqliteMemoryProvider(db_path)  # _init_schema runs the migration

    check_conn = wal_connect(db_path)
    try:
        rows = check_conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='index' AND tbl_name='episodes'"
        ).fetchall()
    finally:
        check_conn.close()
    names = {r["name"] for r in rows}
    assert "idx_episodes_tenant_session" in names
    # No surviving UNIQUE index on session_id alone.
    for r in rows:
        if r["name"] == "idx_episodes_session":
            assert False, "stale single-column index must be dropped"
        if r["sql"] and "session_id)" in r["sql"].replace(" ", "") and "user_id" not in r["sql"]:
            assert False, f"unexpected single-column index survived: {r['name']}"

    await provider.record_episode(
        _rec(session_id="shared", user_id="u1", task="A"),
        session_id="shared", user_id="u1",
    )
    await provider.record_episode(
        _rec(session_id="shared", user_id="u2", task="B"),
        session_id="shared", user_id="u2",
    )
    out_u1 = await provider.recall_episodes(user_id="u1", limit=10)
    out_u2 = await provider.recall_episodes(user_id="u2", limit=10)
    assert len(out_u1) == 1 and out_u1[0].task == "A"
    assert len(out_u2) == 1 and out_u2[0].task == "B"


@pytest.mark.asyncio
async def test_large_artifacts_stored_as_valid_json(provider):
    # An artifacts list that serializes past the 8000-char cap must degrade
    # gracefully (drop trailing entries, re-serialize) rather than being
    # character-sliced into invalid JSON that silently parses back to [].
    big_artifacts = [{"path": "f" * 200} for _ in range(80)]
    await provider.record_episode(_rec(session_id="big", artifacts=big_artifacts),
                                  session_id="big", user_id="u1")
    out = await provider.recall_episodes(user_id="u1", limit=10)
    assert len(out) == 1
    assert isinstance(out[0].artifacts, list)
    assert len(out[0].artifacts) > 0
