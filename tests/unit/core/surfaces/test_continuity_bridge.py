"""Task 6 — idle-reset continuity bridge (`core/surfaces/continuity.py`).

`build_bridge_message` is a pure consumer of the episodic store: it recalls the most
recent `kind="chat"` episode for a `thread_key` that carries a non-empty `summary` and
wraps it as a `SESSION_BRIDGE` control message. The summary itself is written at
session cleanup (Task 6 Part A, `agents/task/session/cleanup.py`), NOT here — see
`core/surfaces/continuity.py`'s module docstring for why the dispatcher-reset-boundary
write from the original brief was dropped (upsert-clobber risk on `record_episode`).
"""
import pytest

from modules.memory.sqlite_memory_provider import SqliteMemoryProvider
import modules.memory.registry as reg
from modules.memory.episodic import finalize_episode
from core.surfaces.continuity import build_bridge_message


@pytest.fixture
def provider(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_REQUIRE_USER_ID", "true")
    monkeypatch.setenv("EPISODIC_MEMORY_ENABLED", "true")
    monkeypatch.setenv("CONTINUITY_BRIDGE_ENABLED", "true")
    p = SqliteMemoryProvider(str(tmp_path / "memory.db"))
    reg.reset_memory_registry()
    reg.set_external_memory_provider(p)
    yield p
    reg.reset_memory_registry()


@pytest.mark.asyncio
async def test_bridge_built_from_prior_chat_episode_with_summary(provider):
    await finalize_episode(session_id="old-1", user_id="u1", kind="chat",
                           thread_key="tg:42", outcome="done",
                           summary="We drafted the launch tweet.")

    msg = await build_bridge_message(user_id="u1", thread_key="tg:42")

    assert msg is not None
    assert "launch tweet" in msg.content
    from modules.llm.messages import MessageOrigin
    assert getattr(msg, "origin", None) == MessageOrigin.SESSION_BRIDGE


@pytest.mark.asyncio
async def test_bridge_none_when_no_prior_episode(provider):
    msg = await build_bridge_message(user_id="u1", thread_key="tg:99")
    assert msg is None


@pytest.mark.asyncio
async def test_bridge_none_when_only_empty_summaries(provider):
    await finalize_episode(session_id="old-2", user_id="u1", kind="chat",
                           thread_key="tg:7", outcome="done", summary=None)
    await finalize_episode(session_id="old-3", user_id="u1", kind="chat",
                           thread_key="tg:7", outcome="done", summary="   ")

    msg = await build_bridge_message(user_id="u1", thread_key="tg:7")

    assert msg is None


@pytest.mark.asyncio
async def test_bridge_off_returns_none(provider, monkeypatch):
    await finalize_episode(session_id="old-4", user_id="u1", kind="chat",
                           thread_key="tg:8", outcome="done", summary="x")
    monkeypatch.setenv("CONTINUITY_BRIDGE_ENABLED", "false")

    msg = await build_bridge_message(user_id="u1", thread_key="tg:8")

    assert msg is None


@pytest.mark.asyncio
async def test_bridge_none_when_no_thread_key(provider):
    msg = await build_bridge_message(user_id="u1", thread_key=None)
    assert msg is None
