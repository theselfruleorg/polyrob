r"""
Model Registry for LLM Service - UPDATED DECEMBER 2025

This module centralizes all model configurations for currently available models.
Single source of truth for model information based on official docs.

Sources:
- OpenAI: https://platform.openai.com/docs/models
- Anthropic: https://docs.anthropic.com/en/docs/about-claude/models
- Google: https://ai.google.dev/gemini-api/docs/models
- DeepSeek: https://api-docs.deepseek.com/

Layout since S4 (2026-08-29):
  * ``model_types``   — ``ModelProvider``, ``ModelPricing``, ``ModelCapabilities``,
                        ``ModelConfig``, cache multipliers, display-name helper
  * ``model_catalog`` — the built-in ``ModelConfig`` rows (data only)
  * ``model_pricing`` — ``calculate_cost``
  * this module       — ``ModelRegistry`` + lookup helpers + ``PROVIDER_CONFIG``, and
                        re-exports of every public name so existing importers keep working.
"""
from typing import Dict, Optional, List
import logging
import re  # noqa: F401  (namespace parity with the pre-split module)

from modules.llm.model_types import (  # noqa: F401  (re-exported public surface)
    CACHE_READ_PRICE_MULTIPLIER,
    CACHE_WRITE_PRICE_MULTIPLIER,
    CANONICAL_PROVIDER_NAMES,
    ModelCapabilities,
    ModelConfig,
    ModelPricing,
    ModelProvider,
    PROVIDER_CANONICAL_NAMES,
    STREAMING_PROVIDER_NAMES,
    _DEFAULT_CACHE_READ_MULTIPLIER,
    _DISPLAY_NAME_ACRONYMS,
    _derive_display_name,
    canonical_provider_name,
)
from modules.llm.model_pricing import calculate_cost  # noqa: F401  (re-exported)

logger = logging.getLogger(__name__)


class ModelRegistry:
    """Central registry for all model configurations"""

    def __init__(self):
        self._models: Dict[str, ModelConfig] = {}
        self._aliases: Dict[str, str] = {}  # alias -> canonical name
        self._initialize_models()

    def _initialize_models(self):
        """Register the built-in catalog (``modules/llm/model_catalog.py``)."""
        from modules.llm.model_catalog import register_builtin_models
        register_builtin_models(self._register_model)

    def _register_model(self, config: ModelConfig):
        """Register a model and its aliases"""
        # Register main name
        self._models[config.name] = config

        # Register aliases
        for alias in config.aliases:
            self._aliases[alias] = config.name

        logger.debug(f"Registered model: {config.name} with {len(config.aliases)} aliases")

    def get_model(self, name: str) -> Optional[ModelConfig]:
        """Get model configuration by name or alias with fallback support"""
        # Check if it's an alias
        canonical_name = self._aliases.get(name, name)

        # Get model
        model = self._models.get(canonical_name)

        if not model:
            logger.warning(f"Model '{name}' not found in registry, attempting fallback")

            # Implement fallback logic based on model patterns
            model_lower = name.lower()

            # OpenAI models: default to 5.1, fallback chain: 5.1 -> 4.1 -> 4o
            if any(x in model_lower for x in ['gpt', 'o1', 'o3', 'o4', 'openai']):
                # Try 5.1 first (latest default)
                model = self._models.get('gpt-5.1')
                if model:
                    logger.info(f"Fallback: '{name}' -> 'gpt-5.1' (default)")
                else:
                    # Fallback to 4.1
                    model = self._models.get('gpt-4.1')
                    if model:
                        logger.info(f"Fallback: '{name}' -> 'gpt-4.1' (secondary)")
                    else:
                        # Fallback to 4o
                        model = self._models.get('gpt-4o')
                        if model:
                            logger.info(f"Fallback: '{name}' -> 'gpt-4o' (tertiary)")
            # Claude variants fallback to claude-sonnet-4-5
            elif 'claude' in model_lower:
                model = self._models.get('claude-sonnet-4-5')
                if model:
                    logger.info(f"Fallback: '{name}' -> 'claude-sonnet-4-5'")
            # Gemini variants fallback chain: 2.5-flash (stable) -> 3-pro-preview -> 2.5-pro
            elif 'gemini' in model_lower:
                # Check if user wants a specific series
                if '3' in model_lower or 'pro' in model_lower:
                    # Try gemini-3-pro-preview first for pro/3 requests
                    model = self._models.get('gemini-3-pro-preview')
                    if model:
                        logger.info(f"Fallback: '{name}' -> 'gemini-3-pro-preview'")
                if not model and ('flash' in model_lower or '2.5' in model_lower or '2.0' in model_lower):
                    # Try 2.5-flash for flash/2.x requests
                    model = self._models.get('gemini-2.5-flash')
                    if model:
                        logger.info(f"Fallback: '{name}' -> 'gemini-2.5-flash'")
                if not model:
                    # Final gemini fallback: 2.5-flash (most stable, best price-performance)
                    model = self._models.get('gemini-2.5-flash')
                if model:
                        logger.info(f"Fallback: '{name}' -> 'gemini-2.5-flash' (default)")
            # DeepSeek variants - try OpenRouter version first (better tool calling)
            # then fall back to direct DeepSeek client
            elif 'deepseek' in model_lower:
                # Check if user explicitly wants OpenRouter version
                if 'openrouter' in model_lower or 'or-' in model_lower:
                    model = self._models.get('deepseek/deepseek-chat')
                    if model:
                        logger.info(f"Fallback: '{name}' -> 'deepseek/deepseek-chat' (OpenRouter)")
                else:
                    # Try OpenRouter DeepSeek first (better tool support)
                    model = self._models.get('deepseek/deepseek-chat')
                    if model:
                        logger.info(f"Fallback: '{name}' -> 'deepseek/deepseek-chat' (OpenRouter preferred)")
                    else:
                        # Fall back to direct DeepSeek client
                        model = self._models.get('deepseek-chat')
                        if model:
                            logger.info(f"Fallback: '{name}' -> 'deepseek-chat' (direct)")
            # GLM / Z.AI variants fallback to z-ai/glm-5.2 (must precede the
            # final gpt fallback so an unknown glm id keeps GLM's metadata).
            elif 'glm' in model_lower or 'z-ai' in model_lower or 'zhipu' in model_lower:
                model = self._models.get('z-ai/glm-5.2')
                if model:
                    logger.info(f"Fallback: '{name}' -> 'z-ai/glm-5.2'")
            # OpenRouter/Grok variants fallback to x-ai/grok-4.5 (newest live flagship;
            # grok-4.1-fast is 404'd by OpenRouter so it must not be the fallback target).
            elif 'grok' in model_lower:
                model = self._models.get('x-ai/grok-4.5')
                if model:
                    logger.info(f"Fallback: '{name}' -> 'x-ai/grok-4.5'")
            # Kimi variants fallback to moonshotai/kimi-k2-0905
            elif 'kimi' in model_lower:
                model = self._models.get('moonshotai/kimi-k2-0905')
                if model:
                    logger.info(f"Fallback: '{name}' -> 'moonshotai/kimi-k2-0905'")
            # Qwen variants fallback to qwen/qwen3-235b-a22b
            elif 'qwen' in model_lower:
                model = self._models.get('qwen/qwen3-235b-a22b')
                if model:
                    logger.info(f"Fallback: '{name}' -> 'qwen/qwen3-235b-a22b'")

            # Final fallback: gpt-5.1 (default) -> gpt-4.1 -> gpt-4o
            if not model:
                model = self._models.get('gpt-5.1')
                if model:
                    logger.info(f"Final fallback: '{name}' -> 'gpt-5.1' (default)")
                else:
                    model = self._models.get('gpt-4.1')
                    if model:
                        logger.info(f"Final fallback: '{name}' -> 'gpt-4.1' (secondary)")
                    else:
                        model = self._models.get('gpt-4o')
                        if model:
                            logger.info(f"Final fallback: '{name}' -> 'gpt-4o' (last resort)")

        return model

    def list_models(self, provider: Optional[ModelProvider] = None,
                   include_deprecated: bool = False) -> List[ModelConfig]:
        """List all registered models, optionally filtered by provider"""
        models = list(self._models.values())

        if provider:
            models = [m for m in models if m.provider == provider]

        if not include_deprecated:
            models = [m for m in models if not m.deprecated]

        return models

    def get_model_names(self, provider: Optional[ModelProvider] = None,
                       include_deprecated: bool = False) -> List[str]:
        """Get list of model names"""
        models = self.list_models(provider, include_deprecated)
        return [m.name for m in models]


# Singleton instance
_registry = None

def get_registry() -> ModelRegistry:
    """Get the singleton model registry"""
    global _registry
    if _registry is None:
        _registry = ModelRegistry()
    return _registry


# OpenAI temperature/param handling — SSOT for the "which models reject temperature"
# decision (H1). Prefix-based so new o-series/gpt-5 variants are covered automatically;
# the old inline substring lists (['o1','o1-mini','o3-mini','o1-preview']) missed the
# registered o3 and o4-mini models -> temperature=0.0 was sent and OpenAI 400'd them.
_OPENAI_REASONING_PREFIXES = ("o1", "o3", "o4")


def openai_reasoning_model(model: str) -> bool:
    """True for OpenAI o-series reasoning models (o1/o3/o4 families). These reject
    ``temperature``, ``parallel_tool_calls`` and ``max_tokens``."""
    if not model:
        return False
    m = model.lower().rsplit("/", 1)[-1]
    return any(m.startswith(p) for p in _OPENAI_REASONING_PREFIXES)


def openai_omits_temperature(model: str) -> bool:
    """True if the OpenAI model rejects a custom ``temperature`` and it must be omitted:
    the o-series reasoning models plus the gpt-5 family (default-temperature only)."""
    if not model:
        return False
    m = model.lower().rsplit("/", 1)[-1]
    return openai_reasoning_model(model) or m.startswith("gpt-5")


def get_model_config(model_name: str) -> Optional[ModelConfig]:
    """Convenience function to get model config"""
    return get_registry().get_model(model_name)


def thinking_config_enabled() -> bool:
    """UP-07 gate for SENDING per-model thinking params (Anthropic thinking block,
    DeepSeek max_cot_tokens-from-registry, OpenAI reasoning_effort).

    Default **OFF** so the hot path is byte-identical today — enabling extended thinking
    is a real behavior change (Anthropic forces temperature=1 + streaming, adds reasoning
    cost). The budgets live in the registry as the single source of truth regardless;
    this flag controls whether they're applied. Enable with THINKING_CONFIG_ENABLED in
    {1, true, yes, on}.
    """
    from core.env import bool_env as _bool_env
    return _bool_env("THINKING_CONFIG_ENABLED", False)


def get_thinking_config(model_name: str) -> Dict[str, object]:
    """Per-model reasoning config (UP-07). Empty dict => no thinking params (provider/SDK
    default, current behavior). Returns {"budget_tokens": int} and/or
    {"reasoning_effort": str} for a thinking-capable model that has them set.
    """
    config = get_model_config(model_name)
    if not config or not config.capabilities.supports_thinking:
        return {}
    out: Dict[str, object] = {}
    budget = config.capabilities.thinking_budget_tokens
    effort = config.capabilities.reasoning_effort
    if budget:
        out["budget_tokens"] = budget
    if effort:
        out["reasoning_effort"] = effort
    return out


def register_custom_model(config: ModelConfig):
    """Register a custom model configuration

    Args:
        config: ModelConfig object with model details
    """
    registry = get_registry()
    registry._register_model(config)
    logger.info(f"Registered custom model: {config.name}")


def get_all_models(provider: Optional[ModelProvider] = None,
                   include_deprecated: bool = False) -> List[ModelConfig]:
    """Get all registered models

    Args:
        provider: Optional filter by provider
        include_deprecated: Include deprecated models

    Returns:
        List of ModelConfig objects
    """
    registry = get_registry()
    return registry.list_models(provider, include_deprecated)


def get_model_config_exact(model_name: str):
    """Model config by exact name or declared alias — NO family fallback.

    ``get_model_config`` falls back by family and ultimately to a default, which
    is right for capability questions (context window, max_tokens: a sane
    default beats nothing) and WRONG for billing: it silently answers "what does
    MiniMax-M2.7 cost" with GPT-5.1's price. Billing uses this instead.
    """
    registry = get_registry()
    canonical = registry._aliases.get(model_name, model_name)
    return registry._models.get(canonical)


# ---------------------------------------------------------------------------
# PROVIDER_CONFIG — single source of truth for provider→client mapping
# ---------------------------------------------------------------------------
# Maps the canonical provider string (same strings used throughout the agent
# stack) to a lightweight record describing which client class to instantiate
# and whether the provider participates in the automatic fallback hierarchy.
#
# Rules:
#   fallback_eligible=True  → provider is in LLMManager.FALLBACK_HIERARCHY
#   fallback_eligible=False → provider is NOT in the fallback hierarchy;
#                             can still be constructed explicitly (e.g. deepseek
#                             is excluded because its direct client has broken
#                             tool calling — use OpenRouter's DeepSeek instead)
#
# Client classes are imported lazily inside the record factory to avoid
# circular imports at module load time.
# ---------------------------------------------------------------------------

from dataclasses import dataclass as _dataclass


@_dataclass(frozen=True)
class _ProviderEntry:
    """Metadata for one LLM provider."""
    # String used throughout the agent stack — matches PROVIDER_CANONICAL_NAMES
    provider: str
    # Uninstantiated client class (lazy import avoids circular deps at load)
    client_class_name: str   # e.g. "AnthropicClient" — for documentation
    # Whether this provider participates in LLMManager.FALLBACK_HIERARCHY.
    # NOTE: deepseek=False is INTENTIONAL (direct client has broken tool calling).
    fallback_eligible: bool


def _build_provider_config() -> "Dict[str, _ProviderEntry]":
    """Build PROVIDER_CONFIG dict.

    Since proposal 024 (L0) the entries are DERIVED from the ProviderSpec
    registry (built-ins + user-declared providers.yaml rows; a user row's
    client class is the generic client for its declared transport). A registry
    fault falls back to ``BUILTIN_SPECS`` — the registry's own data — never to a
    second hand-maintained table (the legacy literal went with the
    ``LLM_PROVIDER_REGISTRY`` kill-switch, 2026-08-29).
    """
    from modules.llm.provider_spec import BUILTIN_SPECS, generic_client_class_name, get_specs
    try:
        specs = get_specs()
    except Exception:
        specs = BUILTIN_SPECS  # provider lookup must never crash on a bad providers.yaml
    out: "Dict[str, _ProviderEntry]" = {}
    for s in specs:
        cls = s.client_class_name or generic_client_class_name(s.transport)
        if cls is None:
            continue  # transport with no generic client yet (RESPONSES)
        out[s.name] = _ProviderEntry(s.name, cls, fallback_eligible=s.fallback_eligible)
    return out


class _LazyProviderConfig:
    """Proxy that builds PROVIDER_CONFIG on first access to avoid circular imports."""

    def __init__(self) -> None:
        self._config: "Optional[Dict[str, _ProviderEntry]]" = None

    def _ensure(self) -> "Dict[str, _ProviderEntry]":
        if self._config is None:
            self._config = _build_provider_config()
        return self._config

    # dict-like interface
    def __getitem__(self, key: str) -> _ProviderEntry:
        return self._ensure()[key]

    def __contains__(self, key: object) -> bool:
        return key in self._ensure()

    def keys(self):
        return self._ensure().keys()

    def values(self):
        return self._ensure().values()

    def items(self):
        return self._ensure().items()

    def get(self, key: str, default=None):
        return self._ensure().get(key, default)


#: Single source of truth for provider → client-class + fallback eligibility.
#: Use PROVIDER_CONFIG[provider_str] throughout the LLM subsystem instead of
#: maintaining per-file client maps.
PROVIDER_CONFIG = _LazyProviderConfig()