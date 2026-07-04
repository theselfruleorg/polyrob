"""Task 4 — `recent_activity` agent action: recall own runs (chat/goal/cron),
tenant-scoped, newest-first, with a mandatory aggregate footer (count + spend).
Gated on EPISODIC_MEMORY_ENABLED + an active external memory provider so the
default (NullMemoryProvider) config is byte-identical (mirrors session_search).

Review-round-1 fix wave:
- FIX 1: the `recent_activity` steering sentence in
  `agents/task/agent/prompts.py::_get_memory_system_content()` is now gated on
  the SAME flag (`AutonomyConfig.episodic_memory_enabled()`) that gates the
  action's own registration, so the prompt never advertises a tool that isn't
  in the schema on the server default (flag off).
- FIX 2: a malformed (non-empty, unparseable) `since` no longer silently drops
  the time filter and recalls ALL history — it degrades to the same 24h
  default used when `since` is omitted.
"""
import time

import pytest

from modules.memory.sqlite_memory_provider import SqliteMemoryProvider
import modules.memory.registry as reg
# reuse the repo's controller test harness helper
from tests.unit.tools.test_memory_tool_action import _controller  # existing helper


@pytest.fixture
def provider(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_REQUIRE_USER_ID", "true")
    monkeypatch.setenv("EPISODIC_MEMORY_ENABLED", "true")
    p = SqliteMemoryProvider(str(tmp_path / "memory.db"))
    reg.reset_memory_registry()
    reg.set_external_memory_provider(p)
    yield p
    reg.reset_memory_registry()


def test_absent_when_flag_off(provider, monkeypatch):
    monkeypatch.setenv("EPISODIC_MEMORY_ENABLED", "false")
    c = _controller()
    c._register_recent_activity_action()
    assert "recent_activity" not in c.registry.registry.actions


def test_absent_without_external_provider(monkeypatch):
    monkeypatch.setenv("EPISODIC_MEMORY_ENABLED", "true")
    reg.reset_memory_registry()
    c = _controller()
    c._register_recent_activity_action()
    assert "recent_activity" not in c.registry.registry.actions
    reg.reset_memory_registry()


def test_registered_when_enabled(provider):
    c = _controller()
    c._register_recent_activity_action()
    assert "recent_activity" in c.registry.registry.actions


@pytest.mark.asyncio
async def test_recent_activity_since_and_footer(provider):
    from modules.memory.episodic import finalize_episode
    await finalize_episode(session_id="s1", user_id="u1", kind="goal",
                           task="draft tweet", outcome="done", spend_usd=0.11)
    await finalize_episode(session_id="s2", user_id="u1", kind="cron",
                           task="digest", outcome="done", spend_usd=0.04)

    c = _controller(user_id="u1")
    c._register_recent_activity_action()
    action = c.registry.registry.actions["recent_activity"]

    class Ctx: user_id = "u1"; session_id = "cur"
    result = await action.function(action.param_model(since="8h"), execution_context=Ctx())
    text = result.extracted_content
    assert "draft tweet" in text and "digest" in text
    assert "2 runs" in text and "0.15" in text          # aggregate footer
    assert "<untrusted_tool_result" in text             # DATA-framed


def test_prompt_steering_gated(provider, monkeypatch):
    """FIX 1 (+ ME-D1/SK-F5): the recent_activity steering sentence in the
    memory-system prompt section is only present when EPISODIC_MEMORY_ENABLED
    is true AND an external memory provider is active — the same combined
    gate the action itself uses (`_register_recent_activity_action`).
    Prevents the prompt from advertising a tool that isn't in the schema
    on the (default-off / no-external-provider) server config."""
    from agents.task.agent.prompts import SystemPrompt

    # `provider` fixture sets EPISODIC_MEMORY_ENABLED=true + registers an
    # external SqliteMemoryProvider — the action is actually registerable.
    builder_on = SystemPrompt(action_description="noop")
    content_on = builder_on._get_memory_system_content()
    assert "recent_activity" in content_on

    monkeypatch.setenv("EPISODIC_MEMORY_ENABLED", "false")
    builder_off = SystemPrompt(action_description="noop")
    content_off = builder_off._get_memory_system_content()
    assert "recent_activity" not in content_off

    # Flag on, but no external provider active => still not advertised.
    monkeypatch.setenv("EPISODIC_MEMORY_ENABLED", "true")
    reg.reset_memory_registry()
    builder_no_provider = SystemPrompt(action_description="noop")
    content_no_provider = builder_no_provider._get_memory_system_content()
    assert "recent_activity" not in content_no_provider


@pytest.mark.asyncio
async def test_malformed_since_defaults_to_24h(provider):
    """FIX 2: an unparseable `since` string must degrade to the 24h default,
    NOT silently drop the filter and recall the entire history."""
    from modules.memory.provider import EpisodeRecord

    now = int(time.time())
    recent = EpisodeRecord(
        ts=now - 2 * 3600, user_id="u1", session_id="recent-session",
        kind="goal", task="recent task", outcome="done", spend_usd=0.01)
    old = EpisodeRecord(
        ts=now - 30 * 3600, user_id="u1", session_id="old-session",
        kind="goal", task="old task", outcome="done", spend_usd=0.02)
    await provider.record_episode(recent, session_id="recent-session", user_id="u1")
    await provider.record_episode(old, session_id="old-session", user_id="u1")

    c = _controller(user_id="u1")
    c._register_recent_activity_action()
    action = c.registry.registry.actions["recent_activity"]

    class Ctx: user_id = "u1"; session_id = "cur"
    result = await action.function(action.param_model(since="garbage"), execution_context=Ctx())
    text = result.extracted_content
    assert "recent task" in text
    assert "old task" not in text
