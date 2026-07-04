"""Tests for T13 — opt-in KB auto-prefetch in build_prefetch_message.

Coverage:
  - KB OFF (flag False): kb_search is NEVER called; return is byte-identical to memory-only path.
  - KB ON + both memory and KB return content: message body contains BOTH sections; KB part
    is wrapped in <untrusted_tool_result source="knowledge_base">.
  - KB ON + kb_search raises: fail-open — memory-only result is still returned, no exception.
  - KB ON + memory empty, KB has content: KB section alone is returned.
  - KB ON + both empty: returns None.
"""
from __future__ import annotations

import os
import pytest
from unittest.mock import AsyncMock, patch, MagicMock


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

QUERY = "what is the capital of France"
SESSION_ID = "test-session-123"
USER_ID = "user_abc"

MEMORY_CONTENT = "Previously recalled: Paris is the capital."
KB_CONTENT = "From knowledge base: France capital is Paris, population ~2M."


async def _call(query=QUERY, *, session_id=SESSION_ID, user_id=USER_ID, **env):
    """Import-fresh call to build_prefetch_message with env overrides."""
    import importlib
    import agents.task.agent.core.memory_prefetch as mod
    importlib.reload(mod)
    return await mod.build_prefetch_message(query, session_id=session_id, user_id=user_id)


# ---------------------------------------------------------------------------
# Test: KB OFF — byte-identical to pre-T13 (no kb_search call)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_kb_off_no_kb_call():
    """When KB_AUTO_PREFETCH is False, kb_search must NEVER be called."""
    kb_sentinel = AsyncMock(side_effect=AssertionError("kb_search must not be called when KB is OFF"))

    with (
        patch("agents.task.constants.AutonomyConfig.kb_auto_prefetch", return_value=False),
        patch("modules.memory.registry.memory_prefetch", new=AsyncMock(return_value=MEMORY_CONTENT)),
        patch("modules.memory.registry.kb_search", new=kb_sentinel),
        patch("agents.task.constants.UNTRUSTED_TOOL_RESULT_WRAP", True),
    ):
        from agents.task.agent.core.memory_prefetch import build_prefetch_message
        result = await build_prefetch_message(QUERY, session_id=SESSION_ID, user_id=USER_ID)

    # Must succeed and contain memory content
    assert result is not None
    text = result.content if hasattr(result, "content") else str(result)
    assert MEMORY_CONTENT in text

    # kb_search must not have been called
    kb_sentinel.assert_not_called()


@pytest.mark.asyncio
async def test_kb_off_returns_none_when_memory_empty():
    """When KB OFF and memory is empty, returns None (unchanged)."""
    with (
        patch("agents.task.constants.AutonomyConfig.kb_auto_prefetch", return_value=False),
        patch("modules.memory.registry.memory_prefetch", new=AsyncMock(return_value="")),
    ):
        from agents.task.agent.core.memory_prefetch import build_prefetch_message
        result = await build_prefetch_message(QUERY, session_id=SESSION_ID, user_id=USER_ID)

    assert result is None


# ---------------------------------------------------------------------------
# Test: KB ON + both memory and KB have content
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_kb_on_both_sections_present():
    """KB ON + both recall sources populated → one message with two labelled sections."""
    with (
        patch("agents.task.constants.AutonomyConfig.kb_auto_prefetch", return_value=True),
        patch("modules.memory.registry.memory_prefetch", new=AsyncMock(return_value=MEMORY_CONTENT)),
        patch("modules.memory.registry.kb_search", new=AsyncMock(return_value=KB_CONTENT)),
        patch("agents.task.constants.UNTRUSTED_TOOL_RESULT_WRAP", True),
    ):
        from agents.task.agent.core.memory_prefetch import build_prefetch_message
        result = await build_prefetch_message(QUERY, session_id=SESSION_ID, user_id=USER_ID)

    assert result is not None
    text = result.content if hasattr(result, "content") else str(result)

    # Both section headers present
    assert "## Recalled from memory" in text
    assert "## Recalled from knowledge base" in text

    # Memory content present (wrapped as cross_session_memory)
    assert MEMORY_CONTENT in text

    # KB content present and wrapped as knowledge_base
    assert KB_CONTENT in text
    assert 'source="knowledge_base"' in text


@pytest.mark.asyncio
async def test_kb_on_memory_wrapped_cross_session():
    """With UNTRUSTED_TOOL_RESULT_WRAP ON, memory is wrapped with cross_session_memory source."""
    with (
        patch("agents.task.constants.AutonomyConfig.kb_auto_prefetch", return_value=True),
        patch("modules.memory.registry.memory_prefetch", new=AsyncMock(return_value=MEMORY_CONTENT)),
        patch("modules.memory.registry.kb_search", new=AsyncMock(return_value=KB_CONTENT)),
        patch("agents.task.constants.UNTRUSTED_TOOL_RESULT_WRAP", True),
    ):
        from agents.task.agent.core.memory_prefetch import build_prefetch_message
        result = await build_prefetch_message(QUERY, session_id=SESSION_ID, user_id=USER_ID)

    text = result.content if hasattr(result, "content") else str(result)
    assert 'source="cross_session_memory"' in text


# ---------------------------------------------------------------------------
# Test: KB ON + kb_search raises → fail-open, memory-only returned
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_kb_on_kb_raises_fail_open():
    """kb_search raising must not break the memory branch — memory-only result returned."""
    with (
        patch("agents.task.constants.AutonomyConfig.kb_auto_prefetch", return_value=True),
        patch("modules.memory.registry.memory_prefetch", new=AsyncMock(return_value=MEMORY_CONTENT)),
        patch("modules.memory.registry.kb_search", new=AsyncMock(side_effect=RuntimeError("KB exploded"))),
        patch("agents.task.constants.UNTRUSTED_TOOL_RESULT_WRAP", True),
    ):
        from agents.task.agent.core.memory_prefetch import build_prefetch_message
        result = await build_prefetch_message(QUERY, session_id=SESSION_ID, user_id=USER_ID)

    # Must not raise, must return memory content
    assert result is not None
    text = result.content if hasattr(result, "content") else str(result)
    assert MEMORY_CONTENT in text

    # KB section header must NOT be present
    assert "## Recalled from knowledge base" not in text


# ---------------------------------------------------------------------------
# Test: KB ON + only KB has content (memory empty)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_kb_on_only_kb_content():
    """KB ON + memory empty + KB populated → KB section returned (no memory section header)."""
    with (
        patch("agents.task.constants.AutonomyConfig.kb_auto_prefetch", return_value=True),
        patch("modules.memory.registry.memory_prefetch", new=AsyncMock(return_value="")),
        patch("modules.memory.registry.kb_search", new=AsyncMock(return_value=KB_CONTENT)),
        patch("agents.task.constants.UNTRUSTED_TOOL_RESULT_WRAP", True),
    ):
        from agents.task.agent.core.memory_prefetch import build_prefetch_message
        result = await build_prefetch_message(QUERY, session_id=SESSION_ID, user_id=USER_ID)

    assert result is not None
    text = result.content if hasattr(result, "content") else str(result)

    assert KB_CONTENT in text
    assert 'source="knowledge_base"' in text

    # No memory section header (only KB)
    assert "## Recalled from memory" not in text


# ---------------------------------------------------------------------------
# Test: KB ON + both empty → None
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_kb_on_both_empty_returns_none():
    """KB ON + both memory and KB empty → returns None."""
    with (
        patch("agents.task.constants.AutonomyConfig.kb_auto_prefetch", return_value=True),
        patch("modules.memory.registry.memory_prefetch", new=AsyncMock(return_value="")),
        patch("modules.memory.registry.kb_search", new=AsyncMock(return_value="")),
        patch("agents.task.constants.UNTRUSTED_TOOL_RESULT_WRAP", True),
    ):
        from agents.task.agent.core.memory_prefetch import build_prefetch_message
        result = await build_prefetch_message(QUERY, session_id=SESSION_ID, user_id=USER_ID)

    assert result is None


# ---------------------------------------------------------------------------
# Test: AutonomyConfig.kb_auto_prefetch() reads env correctly
# ---------------------------------------------------------------------------

def test_kb_auto_prefetch_default_off_without_local():
    """Without POLYROB_LOCAL, KB_AUTO_PREFETCH defaults False."""
    env = {k: v for k, v in os.environ.items() if k not in ("POLYROB_LOCAL", "KB_AUTO_PREFETCH")}
    env.pop("POLYROB_LOCAL", None)
    env.pop("KB_AUTO_PREFETCH", None)
    with patch.dict(os.environ, env, clear=True):
        from agents.task.constants import AutonomyConfig
        assert AutonomyConfig.kb_auto_prefetch() is False


def test_kb_auto_prefetch_on_with_local():
    """With POLYROB_LOCAL=true, KB_AUTO_PREFETCH defaults True."""
    env = {k: v for k, v in os.environ.items() if k not in ("POLYROB_LOCAL", "KB_AUTO_PREFETCH")}
    env["POLYROB_LOCAL"] = "true"
    env.pop("KB_AUTO_PREFETCH", None)
    with patch.dict(os.environ, env, clear=True):
        from agents.task.constants import AutonomyConfig
        assert AutonomyConfig.kb_auto_prefetch() is True


def test_kb_auto_prefetch_explicit_env_wins():
    """An explicit KB_AUTO_PREFETCH=true wins even without POLYROB_LOCAL."""
    env = {k: v for k, v in os.environ.items() if k not in ("POLYROB_LOCAL", "KB_AUTO_PREFETCH")}
    env.pop("POLYROB_LOCAL", None)
    env["KB_AUTO_PREFETCH"] = "true"
    with patch.dict(os.environ, env, clear=True):
        from agents.task.constants import AutonomyConfig
        assert AutonomyConfig.kb_auto_prefetch() is True
