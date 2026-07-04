"""Task 7 acceptance test: the episodic activity ledger defeats the "nothing ran"
incident end-to-end.

Before the episodic feature, an owner asking "what did you run in the last 8
hours?" in a fresh chat session got no evidence that 3 real goals had actually
completed — H-MEM findings-driven memory only captures a run if it happened to
produce a "finding", so routine goal completions were invisible. This test
reproduces the shape of that incident (finalize a few goal runs, then recall
from a fresh vantage point) and asserts the recall answers it correctly.
"""
import time

import pytest

import modules.memory.registry as reg
from modules.memory.episodic import finalize_episode
from modules.memory.sqlite_memory_provider import SqliteMemoryProvider


@pytest.fixture
def wired(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_REQUIRE_USER_ID", "true")
    monkeypatch.setenv("EPISODIC_MEMORY_ENABLED", "true")
    p = SqliteMemoryProvider(str(tmp_path / "memory.db"))
    reg.reset_memory_registry()
    reg.set_external_memory_provider(p)
    yield p
    reg.reset_memory_registry()


@pytest.mark.asyncio
async def test_incident_reproduced_then_defeated(wired):
    # 1) Run a few goals (findings-independent episodic writes).
    goals = [("draft launch tweet", 0.11), ("reconcile P&L", 0.06), ("3h digest", 0.04)]
    for i, (task, spend) in enumerate(goals):
        await finalize_episode(session_id=f"goal-{i}", user_id="u1", kind="goal",
                               task=task, outcome="done", spend_usd=spend)

    # 2) Fresh chat session asks "what did you do recently?" -> recall answers it.
    from modules.memory.registry import memory_recall_episodes
    rows = await memory_recall_episodes(user_id="u1", since_ts=int(time.time()) - 8 * 3600,
                                        limit=20)
    tasks = {r.task for r in rows}
    assert {"draft launch tweet", "reconcile P&L", "3h digest"} <= tasks   # NOT "nothing ran"
    assert abs(sum(r.spend_usd for r in rows) - 0.21) < 1e-6


@pytest.mark.asyncio
async def test_prune_removes_old_keeps_recent(wired):
    """Curator-cadence retention: old episodes age out, recent ones survive, and
    the recall path used by the incident test above is unaffected by pruning
    fresh data."""
    now = int(time.time())
    await finalize_episode(session_id="ancient", user_id="u1", kind="goal",
                           task="long-forgotten task", outcome="done")
    # finalize_episode always stamps ts=now via time.time(); backdate directly
    # through the provider to simulate an old row (mirrors the unit-level test).
    from modules.memory.provider import EpisodeRecord
    await wired.record_episode(
        EpisodeRecord(ts=now - 100 * 86400, user_id="u1", session_id="really-old",
                      kind="goal", task="really old task"),
        session_id="really-old", user_id="u1")

    removed = wired.prune_episodes(older_than_ts=now - 90 * 86400)
    assert removed == 1

    from modules.memory.registry import memory_recall_episodes
    rows = await memory_recall_episodes(user_id="u1", limit=20)
    tasks = {r.task for r in rows}
    assert "really old task" not in tasks
    assert "long-forgotten task" in tasks


@pytest.mark.asyncio
async def test_digest_excludes_surfaced(wired):
    """A surfaced episode (delivered out-of-band) is dropped from the digest's
    exclude_surfaced=True recall, but still visible to an explicit query."""
    await finalize_episode(session_id="surfaced-goal", user_id="u1", kind="goal",
                           task="already told the owner", outcome="done")
    wired.mark_episode_surfaced(session_id="surfaced-goal")

    from modules.memory.registry import memory_recall_episodes
    digest_rows = await memory_recall_episodes(user_id="u1", exclude_surfaced=True, limit=20)
    assert "already told the owner" not in {r.task for r in digest_rows}

    explicit_rows = await memory_recall_episodes(user_id="u1", limit=20)
    assert "already told the owner" in {r.task for r in explicit_rows}
