import time
import pytest
from modules.memory.sqlite_memory_provider import SqliteMemoryProvider
import modules.memory.registry as reg
from modules.memory.episodic import finalize_episode


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
async def test_goal_completion_writes_one_episode_without_findings(provider):
    # Simulate a goal run that promoted ZERO H-MEM findings.
    await finalize_episode(session_id="goal-sess-1", user_id="u1", kind="goal",
                           task="reconcile P&L", outcome="done", goal_id="g-1",
                           spend_usd=0.06, steps=3)
    out = await reg.memory_recall_episodes(user_id="u1", kind="goal", limit=5)
    assert len(out) == 1 and out[0].goal_id == "g-1" and out[0].kind == "goal"


@pytest.mark.asyncio
async def test_cron_completion_writes_one_episode(provider):
    await finalize_episode(session_id="cron-sess-1", user_id="u1", kind="cron",
                           task="3h digest", outcome="done", spend_usd=0.04)
    out = await reg.memory_recall_episodes(user_id="u1", kind="cron", limit=5)
    assert len(out) == 1


@pytest.mark.asyncio
async def test_reset_and_cleanup_collapse_to_one_row(provider):
    # Same session finalized twice (reset writes a stub, cleanup enriches it) -> ONE row.
    await finalize_episode(session_id="chat-sess-1", user_id="u1", kind="chat",
                           thread_key="tg:42", outcome="partial", summary="mid")
    await finalize_episode(session_id="chat-sess-1", user_id="u1", kind="chat",
                           thread_key="tg:42", outcome="done", summary="final", spend_usd=0.2)
    out = await reg.memory_recall_episodes(user_id="u1", kind="chat", limit=5)
    assert len(out) == 1 and out[0].outcome == "done" and out[0].summary == "final"


def test_no_live_skip_memory_reference():
    import subprocess
    # No production reference to the dead flag remains (docs/tests excluded).
    hits = subprocess.run(
        ["grep", "-rn", "skip_memory",
         "agents/task/goals/dispatcher.py", "cron/runner.py", "cron/scheduler.py"],
        capture_output=True, text=True).stdout
    assert hits.strip() == "", f"dead skip_memory still referenced:\n{hits}"


def test_p2_5_chat_episode_wires_task_and_provenance():
    """P2-5: the chat-episode write in cleanup passes task + collect_provenance
    (spend/steps) — previously every chat row was `- chat:done $0.00 ""`."""
    import inspect
    from agents.task.session import cleanup
    src = inspect.getsource(cleanup)
    # locate the chat finalize_episode call region
    assert 'kind="chat"' in src
    # the chat write must now thread task + provenance
    assert "collect_provenance(self)" in src
    assert "task=_task" in src
    assert "spend_usd=_prov" in src
