"""2026-08-17: the fallback exclusion list must not carry duplicates.

`list(llm_providers_failed) + [current_provider]` doubled the current provider
whenever it had already failed — prod logs read
"Attempting LLM fallback (excluding: ['openrouter', 'openrouter'])".
"""
import asyncio
import logging
from types import SimpleNamespace

from agents.task.agent.service import Agent


def _make_agent(failed, current):
    a = object.__new__(Agent)
    a.logger = logging.getLogger("test-fallback-exclusion-dedup")
    a.state = SimpleNamespace(llm_providers_failed=set(failed))
    # Agent.model_name is a property backed by MessageManager (the SSOT);
    # give the double a minimal stand-in before assigning through the setter.
    a.message_manager = SimpleNamespace(model_name="glm-5")
    a.model_name = "glm-5"
    a._get_provider_from_model = lambda m: current
    return a


def _capture_fallback(agent):
    captured = {}

    async def _fake(exclude_providers, original_model):
        captured["exclude"] = exclude_providers
        return None

    agent._get_fallback_llm = _fake
    return captured


def test_current_provider_already_failed_not_doubled():
    agent = _make_agent(failed={"openrouter"}, current="openrouter")
    captured = _capture_fallback(agent)
    ok = asyncio.run(agent._attempt_llm_fallback_in_handler("LLMRateLimitError"))
    assert ok is False
    assert captured["exclude"] == ["openrouter"]


def test_distinct_providers_both_kept():
    agent = _make_agent(failed={"openrouter"}, current="zai-coding")
    captured = _capture_fallback(agent)
    asyncio.run(agent._attempt_llm_fallback_in_handler("LLMRateLimitError"))
    assert sorted(captured["exclude"]) == ["openrouter", "zai-coding"]
    assert len(captured["exclude"]) == 2
