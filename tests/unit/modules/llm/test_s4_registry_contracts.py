"""S4 (2026-08-29) contracts for the LLM registry layer.

1. The ``LLM_PROVIDER_REGISTRY`` kill-switch is gone: the env var is inert and
   the seams derive from ``get_specs()`` (subscription rows present) — there is
   no second legacy literal table to fall back to.
2. ``TokenCounter.estimate_cost`` forwards BOTH cache token classes (B11): a
   cache-WRITE surcharge is never dropped on the telemetry estimate.
3. ``ProviderSpec.cache_strategy`` is the ONE per-provider caching table (B17).
"""
import pytest


@pytest.fixture
def clean_registry(monkeypatch):
    monkeypatch.setenv("LLM_CUSTOM_PROVIDERS", "")
    from modules.llm import provider_spec
    provider_spec.reset_provider_registry_cache()
    yield
    provider_spec.reset_provider_registry_cache()


def test_kill_switch_is_gone_and_inert(monkeypatch, clean_registry):
    from modules.llm import provider_spec
    assert not hasattr(provider_spec, "provider_registry_enabled")
    monkeypatch.setenv("LLM_PROVIDER_REGISTRY", "false")
    provider_spec.reset_provider_registry_cache()
    names = [s.name for s in provider_spec.get_specs()]
    assert names[:6] == ["openrouter", "anthropic", "openai", "gemini", "nvidia", "deepseek"]
    assert "zai-coding" in names  # a 024 T0 row — only the registry path knows it


def test_seams_derive_from_the_registry(clean_registry):
    from modules.llm.model_registry import PROVIDER_CONFIG
    from modules.llm.profiles import PROFILES
    from api.openai_compat.model_map import _known_providers, _prefix_to_provider
    from cli.config_store import _key_to_provider
    assert "zai-coding" in PROVIDER_CONFIG and "zai-coding" in PROFILES
    assert "openrouter" in _known_providers()
    assert ("kimi", "nvidia") in _prefix_to_provider()
    assert ("OPENROUTER_API_KEY", "openrouter") in _key_to_provider()
    assert all(p != "deepseek" for _, p in _key_to_provider())  # never auto-resolves


def test_estimate_cost_forwards_cache_write_tokens():
    from modules.llm.model_registry import calculate_cost
    from modules.llm.token_counter import TokenCounter, TokenUsage
    usage = TokenUsage(prompt_tokens=1000, completion_tokens=10, total_tokens=1010,
                       cached_tokens=100, cache_creation_tokens=400)
    est = TokenCounter().estimate_cost(usage, "claude-sonnet-4-5")
    assert est == pytest.approx(calculate_cost("claude-sonnet-4-5", 1000, 10, 100, 400))
    assert est > calculate_cost("claude-sonnet-4-5", 1000, 10, 100, 0)  # the 1.25x write surcharge


def test_cache_strategy_is_a_spec_field(clean_registry):
    from modules.llm import cache_hints
    from modules.llm.provider_spec import get_spec
    for name, expected in (("anthropic", "in_client"), ("openai", "in_client"),
                           ("gemini", "explicit"), ("nvidia", "automatic"),
                           ("deepseek", "automatic")):
        assert get_spec(name).cache_strategy == expected
        assert cache_hints.provider_cache_strategy(name) == expected
    assert get_spec("openrouter").cache_strategy is None  # model-dependent
    assert cache_hints.provider_cache_strategy("openrouter", "anthropic/claude-3.5") == "breakpoints"
