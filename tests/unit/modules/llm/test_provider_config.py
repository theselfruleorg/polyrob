"""Tests for PROVIDER_CONFIG — single source of truth for provider→client mapping.

Covers:
1. Every provider in PROVIDER_CONFIG maps to the expected client class (by name).
2. Unknown provider → create_chat_model raises ValueError (no silent fallback).
3. deepseek IS in PROVIDER_CONFIG and has fallback_eligible=False.
4. deepseek is NOT in FALLBACK_HIERARCHY (intentional disablement preserved).
5. GUARD: every entry in FALLBACK_HIERARCHY maps to a provider that is
   fallback_eligible=True in PROVIDER_CONFIG.
6. GUARD: every fallback_eligible=True provider appears in FALLBACK_HIERARCHY.
"""

import pytest
from unittest.mock import MagicMock, patch

from modules.llm.model_registry import PROVIDER_CONFIG


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fallback_hierarchy_providers():
    """Return the provider strings from LLMManager.FALLBACK_HIERARCHY.

    We derive the provider string from the client_name key (e.g.
    'anthropic_client' → 'anthropic') so the test stays in sync with the
    hierarchy automatically — only the set of entries matters here.
    """
    # Import here to avoid top-level circular import in the test module.
    from modules.llm.llm_manager import LLMManager
    from core.config import BotConfig
    # Build a minimal manager without initializing (we only need the constant).
    config_mock = MagicMock(spec=BotConfig)
    config_mock.get_llm_config.return_value = {}
    manager = LLMManager.__new__(LLMManager)
    # Copy the class-level default list by pulling from a temporary instance.
    LLMManager.__init__(manager, name="test", config=config_mock)
    return {
        client_name.replace("_client", "")
        for client_name, _ in manager.FALLBACK_HIERARCHY
    }


# ---------------------------------------------------------------------------
# 1. Provider → client_class_name correctness
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("provider,expected_class_name", [
    ("openai",     "OpenAIClient"),
    ("anthropic",  "AnthropicClient"),
    ("deepseek",   "DeepSeekClient"),
    ("gemini",     "GeminiClient"),
    ("openrouter", "OpenRouterClient"),
    ("nvidia",     "NvidiaClient"),
])
def test_provider_config_client_class_name(provider, expected_class_name):
    """PROVIDER_CONFIG[provider].client_class_name matches expected class name."""
    assert provider in PROVIDER_CONFIG, f"Provider '{provider}' missing from PROVIDER_CONFIG"
    entry = PROVIDER_CONFIG[provider]
    assert entry.client_class_name == expected_class_name, (
        f"PROVIDER_CONFIG['{provider}'].client_class_name = {entry.client_class_name!r}, "
        f"expected {expected_class_name!r}"
    )


def test_provider_config_all_six_providers_present():
    """PROVIDER_CONFIG covers exactly the six known providers."""
    expected = {"openai", "anthropic", "deepseek", "gemini", "openrouter", "nvidia"}
    actual = set(PROVIDER_CONFIG.keys())
    assert actual == expected, f"Provider set mismatch: got {actual}, expected {expected}"


# ---------------------------------------------------------------------------
# 2. Unknown provider → ValueError (no silent fallback)
# ---------------------------------------------------------------------------

def test_create_chat_model_unknown_provider_raises():
    """create_chat_model raises ValueError for an unknown provider string."""
    from modules.llm.llm_factory import create_chat_model

    dummy_client = MagicMock()
    with pytest.raises(ValueError, match="Unsupported LLM provider"):
        create_chat_model(
            provider="banana",
            model="some-model",
            temperature=0.7,
            llm_client=dummy_client,
        )


def test_create_chat_model_empty_provider_raises():
    """create_chat_model raises ValueError for an empty provider string."""
    from modules.llm.llm_factory import create_chat_model

    dummy_client = MagicMock()
    with pytest.raises(ValueError, match="Unsupported LLM provider"):
        create_chat_model(
            provider="",
            model="gpt-5",
            temperature=0.7,
            llm_client=dummy_client,
        )


# ---------------------------------------------------------------------------
# 3. deepseek IS in PROVIDER_CONFIG and has fallback_eligible=False
# ---------------------------------------------------------------------------

def test_deepseek_in_provider_config():
    """deepseek must appear in PROVIDER_CONFIG (still constructable explicitly)."""
    assert "deepseek" in PROVIDER_CONFIG, "deepseek missing from PROVIDER_CONFIG"


def test_deepseek_fallback_eligible_false():
    """deepseek.fallback_eligible must be False — direct client has broken tool calling."""
    entry = PROVIDER_CONFIG["deepseek"]
    assert entry.fallback_eligible is False, (
        "deepseek.fallback_eligible is True — this is a regression. The deepseek direct "
        "client is INTENTIONALLY excluded from the fallback hierarchy because its tool "
        "calling is broken. Use OpenRouter's DeepSeek endpoint instead."
    )


# ---------------------------------------------------------------------------
# 4. deepseek is NOT in FALLBACK_HIERARCHY
# ---------------------------------------------------------------------------

def test_deepseek_not_in_fallback_hierarchy():
    """deepseek must NOT appear in LLMManager.FALLBACK_HIERARCHY."""
    fallback_providers = _fallback_hierarchy_providers()
    assert "deepseek" not in fallback_providers, (
        "deepseek appeared in FALLBACK_HIERARCHY — this reverses the intentional "
        "disablement documented in the hierarchy comment ('tool calling broken')."
    )


# ---------------------------------------------------------------------------
# 5. GUARD: every FALLBACK_HIERARCHY entry is fallback_eligible=True
# ---------------------------------------------------------------------------

def test_fallback_hierarchy_providers_are_all_eligible():
    """Every provider in FALLBACK_HIERARCHY must have fallback_eligible=True in PROVIDER_CONFIG."""
    fallback_providers = _fallback_hierarchy_providers()
    for provider in fallback_providers:
        assert provider in PROVIDER_CONFIG, (
            f"Provider '{provider}' in FALLBACK_HIERARCHY is not in PROVIDER_CONFIG"
        )
        entry = PROVIDER_CONFIG[provider]
        assert entry.fallback_eligible is True, (
            f"Provider '{provider}' is in FALLBACK_HIERARCHY but has "
            f"fallback_eligible=False in PROVIDER_CONFIG — inconsistency detected."
        )


# ---------------------------------------------------------------------------
# 6. GUARD: every fallback_eligible=True provider is in FALLBACK_HIERARCHY
# ---------------------------------------------------------------------------

def test_all_eligible_providers_in_fallback_hierarchy():
    """Every provider with fallback_eligible=True must appear in FALLBACK_HIERARCHY."""
    fallback_providers = _fallback_hierarchy_providers()
    for provider, entry in PROVIDER_CONFIG.items():
        if entry.fallback_eligible:
            assert provider in fallback_providers, (
                f"Provider '{provider}' has fallback_eligible=True in PROVIDER_CONFIG "
                f"but is missing from FALLBACK_HIERARCHY — add it or set fallback_eligible=False."
            )
