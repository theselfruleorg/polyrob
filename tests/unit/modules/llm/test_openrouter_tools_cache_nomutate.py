"""L2: apply_openrouter_tools_cache_control stamped cache_control onto tools[-1] IN PLACE.
The tools list is the memoized Registry.get_all_actions_for_provider("openrouter") object,
keyed by provider (NOT model) — so once any breakpoint model (claude/gemini via OpenRouter)
ran with OPENROUTER_PROMPT_CACHE on, the stray cache_control polluted the shared schema for
every other OpenRouter model in the process. Must copy before stamping.
"""
import copy

from modules.llm.cache_hints import apply_openrouter_tools_cache_control


def test_does_not_mutate_the_shared_tools_list(monkeypatch):
    monkeypatch.setenv("OPENROUTER_PROMPT_CACHE", "true")
    monkeypatch.delenv("LLM_PROMPT_CACHE", raising=False)
    monkeypatch.delenv("ANTHROPIC_PROMPT_CACHE", raising=False)

    tools = [
        {"type": "function", "function": {"name": "a"}},
        {"type": "function", "function": {"name": "b"}},
    ]
    snapshot = copy.deepcopy(tools)

    out = apply_openrouter_tools_cache_control(tools, "anthropic/claude-3.5-sonnet")

    # The caller's (cached) list and its dicts are untouched.
    assert tools == snapshot
    assert "cache_control" not in tools[-1]
    # The marker is applied on a fresh copy.
    assert out[-1].get("cache_control") == {"type": "ephemeral"}
    assert out[-1] is not tools[-1]
