"""UP-08 Step 8.1 — the per-provider cache-strategy seam."""
import pytest

from modules.llm import cache_hints


def test_provider_cache_strategy_table():
    assert cache_hints.provider_cache_strategy("anthropic") == "in_client"
    assert cache_hints.provider_cache_strategy("openai") == "in_client"
    assert cache_hints.provider_cache_strategy("deepseek") == "automatic"
    assert cache_hints.provider_cache_strategy("nvidia") == "automatic"
    assert cache_hints.provider_cache_strategy("gemini") == "automatic"
    assert cache_hints.provider_cache_strategy("unknown-provider") == "none"


def test_provider_cache_strategy_openrouter_delegates(monkeypatch):
    # claude via OpenRouter => breakpoints
    assert cache_hints.provider_cache_strategy("openrouter", "anthropic/claude-3.5") == "breakpoints"
    # gpt via OpenRouter => automatic
    assert cache_hints.provider_cache_strategy("openrouter", "openai/gpt-4o") == "automatic"


def test_tools_breakpoint_marks_last_when_on(monkeypatch):
    monkeypatch.delenv("LLM_PROMPT_CACHE", raising=False)
    tools = [{"type": "function", "function": {"name": "a"}},
             {"type": "function", "function": {"name": "b"}}]
    out = cache_hints.apply_openrouter_tools_cache_control(tools, "anthropic/claude-3.5")
    assert out[-1]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in out[0]


def test_tools_breakpoint_noop_for_automatic_model(monkeypatch):
    tools = [{"type": "function", "function": {"name": "a"}}]
    out = cache_hints.apply_openrouter_tools_cache_control(tools, "openai/gpt-4o")
    assert "cache_control" not in out[-1]


def test_anthropic_transport_spec_rows_report_in_client():
    """P8b (context-usage audit 2026-08-15): a spec-served provider on the
    ANTHROPIC_MESSAGES transport inherits AnthropicClient's in-client
    cache_control breakpoints — the strategy table must say so instead of
    stamping a false "none"."""
    from modules.llm import cache_hints
    assert cache_hints.provider_cache_strategy("zai-coding") == "in_client"
    # Unknown providers (no spec row) still report none.
    assert cache_hints.provider_cache_strategy("unknown-provider") == "none"


def test_the_gemini_explicit_cache_flags_are_retired():
    from core.config_policy.retired_flags import RETIRED_FLAGS
    from core.flags_catalog import CATALOG
    names = {row[0] for row in CATALOG}
    for flag in ("GEMINI_PROMPT_CACHE", "GEMINI_CACHE_TTL_MIN"):
        assert flag in RETIRED_FLAGS and flag not in names
